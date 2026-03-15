from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import AgentSignal, BaseAgent
from app.core.logging import get_app_logger
from app.db.models import Position, PaperFill

logger = get_app_logger()

# Risk rule thresholds
_MAX_POSITION_PCT = 0.20       # max 20% of portfolio in one ticker
_MAX_DAILY_LOSS_PCT = 0.03     # halt trading if intraday loss > 3%
_MIN_CONSENSUS_COUNT = 2       # at least 2 agents must agree for high-risk action
_SCORE_SCALE = 100             # full signal scale

_SYSTEM_PROMPT = """\
You are a risk manager for a US equities trading fund.
Your job is to assess the risk of executing a proposed trade given the current portfolio
and market conditions. You review signals from other analysis agents.
Respond ONLY with valid JSON, no markdown fences, in the exact format specified.
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
1. Whether the trade should proceed
2. The maximum recommended position size as a % of portfolio (0-20%)
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

            # Check current position size
            position = session.execute(
                select(Position).where(Position.ticker == ticker)
            ).scalar_one_or_none()
            position_pct = 0.0
            if position:
                position_pct = abs(float(position.qty) * float(position.avg_price or 0)) / max(
                    self.settings.paper_initial_capital, 1
                )
                if position_pct >= _MAX_POSITION_PCT:
                    rule_checks.append(
                        f"Already at max position ({position_pct:.1%}); no additional size permitted"
                    )
                    hard_block = True
                else:
                    rule_checks.append(f"Current position size: {position_pct:.1%}")

            # Check intraday P&L via today's fills
            today_fills = session.execute(
                select(PaperFill).where(
                    PaperFill.ticker == ticker,
                    PaperFill.filled_at >= date.today(),
                )
            ).scalars().all()
            # Approximate P&L from fill notional (sells reduce, buys increase cost basis)
            intraday_pnl = sum(
                float(f.notional) * (-1 if f.side in ("BUY", "COVER") else 1)
                for f in today_fills
            )
            capital = self.settings.paper_initial_capital
            if intraday_pnl < -(_MAX_DAILY_LOSS_PCT * capital):
                rule_checks.append(
                    f"Daily loss limit reached for {ticker} (P&L: ${intraday_pnl:,.0f})"
                )
                hard_block = True
            else:
                rule_checks.append(f"Intraday P&L for {ticker}: ${intraday_pnl:+,.0f}")

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

            portfolio_context = (
                f"Current {ticker} position: {position_pct:.1%} of portfolio\n"
                f"Available capital: ${capital:,.0f}\n"
                f"Initial capital: ${self.settings.paper_initial_capital:,.0f}"
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
                # LLM unavailable — apply conservative rule: approve with reduced size
                approved = len(actionable_signals) >= _MIN_CONSENSUS_COUNT
                return AgentSignal(
                    agent_name=self.name,
                    signal="HOLD" if not approved else "BUY",
                    confidence=40,
                    reasoning="LLM unavailable; rule-based fallback applied",
                    metadata={
                        "approved": approved,
                        "max_position_pct": 0.05 if approved else 0.0,
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
