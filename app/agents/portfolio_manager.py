from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import AgentSignal, BaseAgent
from app.core.logging import get_app_logger

logger = get_app_logger()

_SYSTEM_PROMPT = """\
Task: Portfolio management — final trade decision.
Make the FINAL trade decision for a specific ticker, synthesizing analysis
from multiple specialist agents: macro, news sentiment, fundamentals, technicals, and risk.
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

Based on all inputs above, make the final decision.

Return a JSON object with these exact fields:
{{
  "action": "<BUY|SHORT|HOLD>",
  "position_pct": <float 0.0-0.20, percentage of portfolio to allocate>,
  "conviction": "<HIGH|MEDIUM|LOW>",
  "supporting_agents": ["<agent1>", "<agent2>"],
  "dissenting_agents": ["<agent1>"],
  "entry_rationale": "<2-3 sentences explaining why to enter or stay out>",
  "exit_criteria": "<1-2 sentences on when to exit this position>",
  "reasoning": "<comprehensive 3-4 sentence summary>"
}}

IMPORTANT RULES:
1. If risk_manager approved=false, you MUST set action=HOLD and position_pct=0.0
2. position_pct must not exceed max_position_pct from risk manager
3. Weight technicals highest for timing; fundamentals/macro for direction; news for catalyst
4. conviction=HIGH requires at least 3 agents aligned; MEDIUM requires 2; LOW for 1 or mixed
"""


class PortfolioManagerAgent(BaseAgent):
    """Makes the final trade decision by synthesizing all agent signals."""

    name = "portfolio_manager"

    def analyze(self, session: Session, ticker: str, context: dict | None = None) -> AgentSignal:
        context = context or {}
        agent_signals: dict[str, dict] = context.get("agent_signals", {})

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
            if action not in ("BUY", "SHORT", "HOLD"):
                action = "HOLD"
            if not risk_approved:
                action = "HOLD"

            position_pct = min(float(parsed.get("position_pct", 0.0)), max_pct)
            if action == "HOLD":
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

    @staticmethod
    def _compute_weighted_confidence(agent_signals: dict[str, dict]) -> float:
        """Weighted average confidence: technicals=30%, news=25%, fundamentals=25%, macro=20%."""
        weights = {
            "technicals": 0.30,
            "news_sentiment": 0.25,
            "fundamentals": 0.25,
            "macro_analyst": 0.20,
        }
        total, weight_sum = 0.0, 0.0
        for agent, weight in weights.items():
            sig = agent_signals.get(agent)
            if sig and sig.get("signal") != "NO_SIGNAL":
                total += float(sig.get("confidence", 50)) * weight
                weight_sum += weight
        return (total / weight_sum) if weight_sum > 0 else 50.0
