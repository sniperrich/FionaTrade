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
_MIN_CONSENSUS_COUNT = 2       # at least 2 agents must agree on direction
_MAX_SAME_DIRECTION = 3        # max tickers in same direction (long or short)
_TICKER_MAX_CONSECUTIVE_LOSSES = 2  # block ticker after N consecutive losses

_SYSTEM_PROMPT = """\
Task: Risk management assessment for equity trading.
You are a risk manager who sizes positions appropriately. When rule-based checks pass
and multiple agents agree on a direction, you APPROVE with proper sizing. You only
REJECT when there are concrete portfolio-level dangers (excessive concentration,
large drawdown, or very conflicting signals).
Respond ONLY with valid JSON, no markdown fences, in the exact format specified below.
"""

_USER_PROMPT_TEMPLATE = """\
Review the following agent signals for {ticker} and assess execution risk.

AGENT SIGNALS:
{signals_summary}

PORTFOLIO CONTEXT:
{portfolio_context}

RULE-BASED PRE-CHECKS (already passed):
{rule_checks}

The rule-based system has already validated consensus, concentration, and loss limits.
Your job is to determine appropriate POSITION SIZING, not whether to trade.

Return a JSON object with these exact fields:
{{
  "risk_level": "<LOW|MEDIUM|HIGH|EXTREME>",
  "approved": <true|false>,
  "max_position_pct": <float 0.0-0.20>,
  "stop_loss_pct": <float, e.g. 0.05 for 5%>,
  "concerns": ["<concern1>", "<concern2>"],
  "reasoning": "<2-3 sentence summary>"
}}

IMPORTANT:
- Default to approved=true since rule checks already passed
- Set position size: 5-8% for 2 agents agreeing, 8-12% for 3+ agents agreeing
- Only set approved=false if you see a SPECIFIC danger not caught by rule checks
- If approved=false, set max_position_pct to 0
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
            block_reasons: list[str] = []
            capital = self.settings.initial_nav

            # Check portfolio state — prefer backtest-injected state over live DB
            bt_portfolio = context.get("portfolio_state")
            if bt_portfolio:
                position_pct = bt_portfolio.get("position_pct", 0.0)
                daily_pnl = bt_portfolio.get("daily_pnl", 0.0)
                current_equity = bt_portfolio.get("equity", capital)
                current_side = bt_portfolio.get("current_side")
                capital = current_equity
            else:
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

            # ── Check 1: Max position size ──
            if position_pct >= _MAX_POSITION_PCT:
                block_reasons.append(f"Max position reached ({position_pct:.1%})")
                hard_block = True
            else:
                rule_checks.append(f"Position size: {position_pct:.1%} (max {_MAX_POSITION_PCT:.0%})")

            # ── Check 2: Daily loss limit ──
            if daily_pnl < -(_MAX_DAILY_LOSS_PCT * capital):
                block_reasons.append(f"Daily loss limit hit (${daily_pnl:,.0f})")
                hard_block = True
            else:
                rule_checks.append(f"Daily P&L: ${daily_pnl:+,.0f}")

            # ── Check 3: Agent consensus (require 2+ directional agreement) ──
            actionable_signals = [
                v for v in agent_signals.values()
                if isinstance(v, dict) and v.get("signal") in ("BUY", "SHORT")
            ]
            buy_count = sum(1 for v in actionable_signals if v.get("signal") == "BUY")
            short_count = sum(1 for v in actionable_signals if v.get("signal") == "SHORT")
            dominant_direction = "BUY" if buy_count >= short_count else "SHORT"
            dominant_count = max(buy_count, short_count)

            if dominant_count < _MIN_CONSENSUS_COUNT:
                block_reasons.append(
                    f"Weak consensus: {buy_count} BUY, {short_count} SHORT (need {_MIN_CONSENSUS_COUNT}+ aligned)"
                )
                hard_block = True
            else:
                rule_checks.append(f"Consensus: {buy_count} BUY, {short_count} SHORT")

            # ── Check 4: Concentration limit (max N tickers same direction) ──
            portfolio_positions = context.get("portfolio_positions", {})
            if portfolio_positions:
                long_count = sum(1 for p in portfolio_positions.values() if p.get("side") == "LONG")
                short_count_port = sum(1 for p in portfolio_positions.values() if p.get("side") == "SHORT")

                proposed_direction = dominant_direction
                if proposed_direction == "BUY" and long_count >= _MAX_SAME_DIRECTION:
                    block_reasons.append(
                        f"Concentration limit: already {long_count} LONG positions (max {_MAX_SAME_DIRECTION})"
                    )
                    hard_block = True
                elif proposed_direction == "SHORT" and short_count_port >= _MAX_SAME_DIRECTION:
                    block_reasons.append(
                        f"Concentration limit: already {short_count_port} SHORT positions (max {_MAX_SAME_DIRECTION})"
                    )
                    hard_block = True
                else:
                    rule_checks.append(f"Portfolio: {long_count}L/{short_count_port}S positions")

            # ── Check 5: Per-ticker consecutive loss tracking ──
            ticker_losses = context.get("ticker_loss_streak", {})
            loss_streak = ticker_losses.get(ticker, 0)
            if loss_streak >= _TICKER_MAX_CONSECUTIVE_LOSSES:
                block_reasons.append(
                    f"{ticker} on {loss_streak}-trade losing streak (max {_TICKER_MAX_CONSECUTIVE_LOSSES})"
                )
                hard_block = True
            elif loss_streak > 0:
                rule_checks.append(f"{ticker} loss streak: {loss_streak}")

            # ── Check 6: Total portfolio drawdown ──
            total_dd = context.get("portfolio_state", {}).get("drawdown_pct", 0.0)
            if total_dd > 0.05:
                block_reasons.append(f"Portfolio drawdown {total_dd:.1%} exceeds 5% limit")
                hard_block = True
            elif total_dd > 0.03:
                rule_checks.append(f"⚠️ Elevated drawdown: {total_dd:.1%}")

            if hard_block:
                return AgentSignal(
                    agent_name=self.name,
                    signal="HOLD",
                    confidence=95,
                    reasoning=f"BLOCKED: {'; '.join(block_reasons)}",
                    metadata={
                        "approved": False, "max_position_pct": 0.0,
                        "hard_block": True, "block_reasons": block_reasons,
                    },
                )

            # ── Tier 2: Borderline (exactly 2 aligned) → LLM review ──
            # ── Tier 3: Strong consensus (3+ aligned) → auto-approve ──
            if dominant_count >= 3:
                auto_pct = 0.10
                return AgentSignal(
                    agent_name=self.name,
                    signal=dominant_direction,
                    confidence=85,
                    reasoning=f"Auto-approved: {dominant_count} agents agree on {dominant_direction}. "
                              f"Checks: {'; '.join(rule_checks)}",
                    metadata={
                        "approved": True,
                        "risk_level": "LOW",
                        "max_position_pct": auto_pct,
                        "stop_loss_pct": 0.07,
                        "concerns": [],
                        "hard_block": False,
                    },
                )

            # ── LLM risk review (for borderline 2-agent consensus) ──
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
                f"Daily P&L: ${daily_pnl:+,.0f}\n"
                f"Drawdown: {total_dd:.1%}"
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
                approved = dominant_count >= _MIN_CONSENSUS_COUNT
                return AgentSignal(
                    agent_name=self.name,
                    signal="HOLD" if not approved else dominant_direction,
                    confidence=40,
                    reasoning="LLM unavailable; rule-based fallback applied",
                    metadata={
                        "approved": approved,
                        "max_position_pct": 0.07 if approved else 0.0,
                        "hard_block": False,
                    },
                )

            approved = bool(parsed.get("approved", False))
            max_pct = min(float(parsed.get("max_position_pct", 0.05)), _MAX_POSITION_PCT)

            return AgentSignal(
                agent_name=self.name,
                signal="HOLD" if not approved else dominant_direction,
                confidence=70,
                reasoning=parsed.get("reasoning", ""),
                metadata={
                    "approved": approved,
                    "risk_level": parsed.get("risk_level", "MEDIUM"),
                    "max_position_pct": max_pct,
                    "stop_loss_pct": float(parsed.get("stop_loss_pct", 0.05)),
                    "concerns": parsed.get("concerns", []),
                    "hard_block": False,
                },
            )

        except Exception as exc:
            logger.exception("[risk_manager] Unexpected error for %s: %s", ticker, exc)
            return AgentSignal.error_signal(self.name, str(exc))
