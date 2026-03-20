from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import AgentSignal, BaseAgent
from app.core.logging import get_app_logger
from app.db.models import Position, PaperFill

logger = get_app_logger()

# Risk rule thresholds
_MAX_POSITION_PCT = 0.20       # max 20% of portfolio in one ticker
_MAX_DAILY_LOSS_PCT = 0.03     # halt trading if intraday loss > 3%
_MIN_CONSENSUS_COUNT = 1       # at least 1 agent must give actionable signal
_SCORE_SCALE = 100             # full signal scale

_SYSTEM_PROMPT = """\
Task: Risk management assessment for equity trading.
You are a risk manager who enables trades when risk is acceptable, not one who blocks them.
Assess the risk of executing a proposed trade given the current portfolio and market conditions.
Your goal is to APPROVE trades with appropriate position sizing unless there is a clear, specific danger.
Respond ONLY with valid JSON, no markdown fences, in the exact format specified below.
"""

_USER_PROMPT_TEMPLATE = """\
Review the following agent signals for {ticker} and assess execution risk.

AGENT SIGNALS:
{signals_summary}

PORTFOLIO CONTEXT:
{portfolio_context}

RULE-BASED PRE-CHECKS:
{rule_checks}

Based on the signals and risk context, determine:
1. Whether the trade should proceed (default to APPROVE unless specific danger exists)
2. The recommended position size as a % of portfolio (5-15% for normal trades)
3. Any risk mitigations required

Return a JSON object with these exact fields:
{{
  "risk_level": "<LOW|MEDIUM|HIGH|EXTREME>",
  "approved": <true|false>,
  "max_position_pct": <float 0.0-0.20>,
  "stop_loss_pct": <float, e.g. 0.02 for 2%>,
  "concerns": ["<concern1>", "<concern2>"],
  "reasoning": "<2-3 sentence summary>"
}}

IMPORTANT: Approve trades when at least one agent provides a directional signal with >=40% confidence.
Only reject if there are concrete dangers like extreme daily loss, max position reached, or extreme VIX.
If approved=false, set max_position_pct to 0.
"""


class RiskManagerAgent(BaseAgent):
    """Evaluates portfolio risk and approves/rejects proposed trades."""

    name = "risk_manager"

    def analyze(self, session: Session, ticker: str, context: dict | None = None) -> AgentSignal:
        context = context or {}
        agent_signals: dict[str, dict] = context.get("agent_signals", {})

        try:
            # ── Rule-based pre-checks ──────────────────────────────────────
            rule_checks: list[str] = []
            hard_block = False
            capital = self.settings.initial_nav

            # Check portfolio state — prefer backtest-injected state over live DB
            bt_portfolio = context.get("portfolio_state")
            if bt_portfolio:
                # Backtest mode: use injected portfolio state
                position_pct = bt_portfolio.get("position_pct", 0.0)
                daily_pnl = bt_portfolio.get("daily_pnl", 0.0)
                current_equity = bt_portfolio.get("equity", capital)
                current_side = bt_portfolio.get("current_side")  # "LONG", "SHORT", or None
                capital = current_equity
            else:
                # Live mode: query Position table
                position = session.execute(
                    select(Position).where(Position.ticker == ticker)
                ).scalar_one_or_none()
                position_pct = 0.0
                current_side = None
                if position:
                    position_pct = abs(float(position.qty) * float(position.avg_price or 0)) / max(capital, 1)
                    current_side = "LONG" if float(position.qty) > 0 else "SHORT"

                today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                today_fills = session.execute(
                    select(PaperFill).where(
                        PaperFill.ticker == ticker,
                        PaperFill.filled_at >= today_start,
                    )
                ).scalars().all()
                daily_pnl = sum(
                    float(f.notional) * (-1 if f.side in ("BUY", "COVER") else 1)
                    for f in today_fills
                )

            if position_pct >= _MAX_POSITION_PCT:
                rule_checks.append(
                    f"Already at max position ({position_pct:.1%}); no additional size permitted"
                )
                hard_block = True
            else:
                rule_checks.append(f"Current position size: {position_pct:.1%}")

            if daily_pnl < -(_MAX_DAILY_LOSS_PCT * capital):
                rule_checks.append(
                    f"Daily loss limit reached for {ticker} (P&L: ${daily_pnl:,.0f})"
                )
                hard_block = True
            else:
                rule_checks.append(f"Intraday P&L for {ticker}: ${daily_pnl:+,.0f}")

            # Check agent consensus
            actionable_signals = [
                v for v in agent_signals.values()
                if isinstance(v, dict) and v.get("signal") in ("BUY", "SHORT")
            ]
            if len(actionable_signals) < _MIN_CONSENSUS_COUNT:
                rule_checks.append(
                    f"Low consensus: only {len(actionable_signals)} actionable signal(s)"
                )

            if hard_block:
                return AgentSignal(
                    agent_name=self.name,
                    signal="HOLD",
                    confidence=95,
                    reasoning=f"Hard risk block: {'; '.join(rule_checks)}",
                    metadata={"approved": False, "max_position_pct": 0.0, "hard_block": True},
                )

            # ── LLM risk review ────────────────────────────────────────────
            signals_summary = "\n".join(
                f"- {name}: signal={sig.get('signal', 'N/A')} "
                f"confidence={sig.get('confidence', 0)} "
                f"reason={sig.get('reasoning', '')[:100]}"
                for name, sig in agent_signals.items()
            ) or "No agent signals available"

            side_str = f" ({current_side})" if current_side else ""
            portfolio_context = (
                f"Current {ticker} position: {position_pct:.1%} of portfolio{side_str}\n"
                f"Available capital: ${capital:,.0f}\n"
                f"Initial capital: ${self.settings.initial_nav:,.0f}\n"
                f"Daily P&L: ${daily_pnl:+,.0f}"
            )

            user_prompt = _USER_PROMPT_TEMPLATE.format(
                ticker=ticker,
                signals_summary=signals_summary,
                portfolio_context=portfolio_context,
                rule_checks="\n".join(f"- {c}" for c in rule_checks),
            )

            raw = self._call_llm(_SYSTEM_PROMPT, user_prompt, response_format="json")
            parsed = self._parse_json_response(raw)

            if not parsed:
                # LLM unavailable — approve if any actionable signal exists
                approved = len(actionable_signals) >= _MIN_CONSENSUS_COUNT
                return AgentSignal(
                    agent_name=self.name,
                    signal="HOLD" if not approved else "BUY",
                    confidence=40,
                    reasoning="LLM unavailable; rule-based fallback applied",
                    metadata={
                        "approved": approved,
                        "max_position_pct": 0.10 if approved else 0.0,
                        "hard_block": False,
                    },
                )

            approved = bool(parsed.get("approved", False))
            max_pct = min(float(parsed.get("max_position_pct", 0.05)), _MAX_POSITION_PCT)

            return AgentSignal(
                agent_name=self.name,
                signal="HOLD" if not approved else "BUY",
                confidence=70,
                reasoning=parsed.get("reasoning", ""),
                metadata={
                    "approved": approved,
                    "risk_level": parsed.get("risk_level", "MEDIUM"),
                    "max_position_pct": max_pct,
                    "stop_loss_pct": float(parsed.get("stop_loss_pct", 0.02)),
                    "concerns": parsed.get("concerns", []),
                    "hard_block": False,
                },
            )

        except Exception as exc:
            logger.exception("[risk_manager] Unexpected error for %s: %s", ticker, exc)
            return AgentSignal.error_signal(self.name, str(exc))
