from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import AgentSignal, BaseAgent
from app.core.logging import get_app_logger

logger = get_app_logger()

_SYSTEM_PROMPT = """\
Task: Portfolio management — final trade decision.
You are an active portfolio manager who seeks alpha. Your job is to make decisive
trade calls, not to default to HOLD. Make the FINAL trade decision for a specific ticker,
synthesizing analysis from multiple specialist agents.
The decision must be actionable, justified, and risk-appropriate.
Respond ONLY with valid JSON, no markdown fences, in the exact format specified below.
"""

_USER_PROMPT_TEMPLATE = """\
Make the final trade decision for {ticker} based on the following agent analyses.

MACRO ANALYST:
signal={macro_signal}, confidence={macro_conf}
{macro_reasoning}

NEWS SENTIMENT:
signal={news_signal}, confidence={news_conf}
{news_reasoning}

FUNDAMENTALS:
signal={fund_signal}, confidence={fund_conf}
{fund_reasoning}

TECHNICALS:
signal={tech_signal}, confidence={tech_conf}
{tech_reasoning}

RISK MANAGER:
approved={risk_approved}, max_position_pct={max_pct:.1%}, risk_level={risk_level}
{risk_reasoning}

CURRENT POSITION:
{position_context}

Based on all inputs above, make the final decision.

Return a JSON object with these exact fields:
{{
  "action": "<BUY|SHORT|SELL|HOLD>",
  "position_pct": <float 0.0-0.20, percentage of portfolio to allocate>,
  "conviction": "<HIGH|MEDIUM|LOW>",
  "supporting_agents": ["<agent1>", "<agent2>"],
  "dissenting_agents": ["<agent1>"],
  "entry_rationale": "<2-3 sentences explaining why to enter, exit, or stay out>",
  "exit_criteria": "<1-2 sentences on when to exit this position>",
  "reasoning": "<comprehensive 3-4 sentence summary>"
}}

IMPORTANT RULES:
1. If risk_manager approved=false, you MUST set action=HOLD and position_pct=0.0
2. position_pct must not exceed max_position_pct from risk manager
3. If risk_manager approved=true, you MUST ACT (BUY or SHORT) — do NOT return HOLD when risk approved
4. Use SELL to close an existing long position when outlook has turned negative or neutral
5. Weight news and fundamentals highest for direction; technicals for timing; macro for context
6. conviction=HIGH requires at least 2 agents aligned; MEDIUM requires 1 strong signal; LOW for mixed
7. When 2+ agents say SHORT/SELL, you SHOULD short or sell — do not override with BUY
8. When agents disagree (e.g., fund=BUY, tech=SHORT, news=SHORT), side with the MAJORITY
9. Typical position_pct: 5-8% for MEDIUM conviction, 8-15% for HIGH conviction
10. If we already hold a profitable position and agents are mixed, prefer HOLD over reversal
"""


class PortfolioManagerAgent(BaseAgent):
    """Makes the final trade decision by synthesizing all agent signals."""

    name = "portfolio_manager"

    def analyze(self, session: Session, ticker: str, context: dict | None = None) -> AgentSignal:
        context = context or {}
        agent_signals: dict[str, dict] = context.get("agent_signals", {})

        # Compute dynamic weights based on agent track records
        as_of = context.get("as_of")
        try:
            from app.agents.reward import compute_dynamic_weights
            dyn_weights = compute_dynamic_weights(session, as_of=as_of)
        except Exception:
            dyn_weights = None
        self._current_weights = dyn_weights  # store for confidence calc

        def _sig(agent: str) -> dict:
            return agent_signals.get(agent, {})

        try:
            macro = _sig("macro_analyst")
            news = _sig("news_sentiment")
            fund = _sig("fundamentals")
            tech = _sig("technicals")
            risk = _sig("risk_manager")

            risk_approved = risk.get("metadata", {}).get("approved", False) if risk else False
            max_pct = risk.get("metadata", {}).get("max_position_pct", 0.05) if risk else 0.05

            # Build position context for direction inertia
            current_pos = context.get("current_position", {})
            current_side = current_pos.get("side")
            if current_side:
                position_context = (
                    f"Currently holding {current_side} position "
                    f"({current_pos.get('shares', 0):.1f} shares @ ${current_pos.get('entry_price', 0):.2f}, "
                    f"opened {current_pos.get('entry_date', 'unknown')}). "
                    f"Reversing direction is COSTLY — only reverse if 3+ agents clearly support the opposite."
                )
            else:
                position_context = "No current position in this ticker."

            user_prompt = _USER_PROMPT_TEMPLATE.format(
                ticker=ticker,
                macro_signal=macro.get("signal", "N/A"),
                macro_conf=macro.get("confidence", 0),
                macro_reasoning=macro.get("reasoning", "No macro data")[:200],
                news_signal=news.get("signal", "N/A"),
                news_conf=news.get("confidence", 0),
                news_reasoning=news.get("reasoning", "No news data")[:200],
                fund_signal=fund.get("signal", "N/A"),
                fund_conf=fund.get("confidence", 0),
                fund_reasoning=fund.get("reasoning", "No fundamentals data")[:200],
                tech_signal=tech.get("signal", "N/A"),
                tech_conf=tech.get("confidence", 0),
                tech_reasoning=tech.get("reasoning", "No technicals data")[:200],
                risk_approved=risk_approved,
                max_pct=max_pct,
                risk_level=risk.get("metadata", {}).get("risk_level", "UNKNOWN") if risk else "UNKNOWN",
                risk_reasoning=risk.get("reasoning", "No risk assessment")[:200],
                position_context=position_context,
            )

            # If risk hard-blocked, skip LLM to save tokens
            if risk and risk.get("metadata", {}).get("hard_block"):
                return AgentSignal(
                    agent_name=self.name,
                    signal="HOLD",
                    confidence=95,
                    reasoning=f"Portfolio blocked by RiskManager: {risk.get('reasoning', '')}",
                    metadata={
                        "action": "HOLD",
                        "position_pct": 0.0,
                        "conviction": "LOW",
                        "supporting_agents": [],
                        "dissenting_agents": [],
                        "exit_criteria": "N/A — no position entered",
                    },
                )

            raw = self._call_llm(_SYSTEM_PROMPT, user_prompt, response_format="json")
            parsed = self._parse_json_response(raw)

            if not parsed:
                return AgentSignal.no_signal(self.name, "LLM unavailable; cannot make final decision")

            # Safety checks: enforce risk manager constraints
            action = parsed.get("action", "HOLD").upper()
            if action not in ("BUY", "SHORT", "SELL", "HOLD"):
                action = "HOLD"
            if not risk_approved:
                action = "HOLD"

            position_pct = min(float(parsed.get("position_pct", 0.0)), max_pct)
            if action in ("HOLD", "SELL"):
                position_pct = 0.0

            return AgentSignal(
                agent_name=self.name,
                signal=action,
                confidence=min(95, int(self._compute_weighted_confidence(agent_signals))),
                reasoning=parsed.get("reasoning", ""),
                metadata={
                    "action": action,
                    "position_pct": position_pct,
                    "conviction": parsed.get("conviction", "LOW"),
                    "supporting_agents": parsed.get("supporting_agents", []),
                    "dissenting_agents": parsed.get("dissenting_agents", []),
                    "entry_rationale": parsed.get("entry_rationale", ""),
                    "exit_criteria": parsed.get("exit_criteria", ""),
                },
            )

        except Exception as exc:
            logger.exception("[portfolio_manager] Unexpected error for %s: %s", ticker, exc)
            return AgentSignal.error_signal(self.name, str(exc))

    def _compute_weighted_confidence(self, agent_signals: dict[str, dict]) -> float:
        """Weighted average confidence — uses dynamic weights if available, else defaults."""
        weights = (
            getattr(self, "_current_weights", None)
            or {
                "technicals": 0.25,
                "news_sentiment": 0.30,
                "fundamentals": 0.30,
                "macro_analyst": 0.15,
            }
        )
        total, weight_sum = 0.0, 0.0
        for agent, weight in weights.items():
            sig = agent_signals.get(agent)
            if sig and sig.get("signal") != "NO_SIGNAL":
                total += float(sig.get("confidence", 50)) * weight
                weight_sum += weight
        return (total / weight_sum) if weight_sum > 0 else 50.0
