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
  "execution_mode": "<IMMEDIATE|WAIT_PULLBACK|WAIT_BREAKOUT_CONFIRMATION|WAIT_UNTIL_OPEN|NO_TRADE>",
  "planned_action": "<BUY|SHORT|SELL|HOLD>",
  "valid_for_minutes": <int 5-1440>,
  "entry_plan": {{
    "pullback_pct": <float optional>,
    "breakout_lookback_min": <int optional>,
    "notes": "<optional short note>"
  }},
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
3. HOLD is valid when: conviction=LOW, or fewer than 2 agents align on the same direction, or the signal is unclear/mixed
4. BUY or SHORT requires: conviction=HIGH or MEDIUM, AND at least 2 agents clearly aligned in the same direction
5. Use SELL to close an existing long position when outlook has turned negative or neutral
6. Weight news ({news_weight_pct}%) highest for direction; technicals ({tech_weight_pct}%) as timing confirmation; macro ({macro_weight_pct}%) and fundamentals ({fund_weight_pct}%) as low-weight filters
7. conviction=HIGH requires 3+ agents aligned; MEDIUM requires 2 agents aligned; LOW for 0-1 aligned or mixed signals
8. When 2+ agents say SHORT/SELL, you SHOULD short or sell — do not override with BUY
9. When agents disagree (e.g., fund=BUY, tech=SHORT, news=SHORT), side with the MAJORITY; if tie, return HOLD
10. Typical position_pct: 5-8% for MEDIUM conviction, 8-15% for HIGH conviction
11. If we already hold a profitable position and agents are mixed, prefer HOLD over reversal
12. It is FINE to return HOLD — do not force trades just because risk is approved
13. If direction is clear but timing is poor, use action=HOLD with execution_mode in WAIT_* and set planned_action accordingly
14. Use execution_mode=IMMEDIATE for direct entries, NO_TRADE when the setup should be ignored entirely
"""


class PortfolioManagerAgent(BaseAgent):
    """Makes the final trade decision by synthesizing all agent signals."""

    name = "portfolio_manager"

    def _base_weights(self) -> dict[str, float]:
        weights = {
            "news_sentiment": float(getattr(self.settings, "agent_weight_news", 0.60)),
            "technicals": float(getattr(self.settings, "agent_weight_technicals", 0.20)),
            "macro_analyst": float(getattr(self.settings, "agent_weight_macro", 0.10)),
            "fundamentals": float(getattr(self.settings, "agent_weight_fundamentals", 0.10)),
        }
        safe = {k: max(0.0, v) for k, v in weights.items()}
        total = sum(safe.values()) or 1.0
        return {k: (v / total) for k, v in safe.items()}

    def analyze(self, session: Session, ticker: str, context: dict | None = None) -> AgentSignal:
        context = context or {}
        agent_signals: dict[str, dict] = context.get("agent_signals", {})

        # Compute dynamic weights based on agent track records
        as_of = context.get("as_of")
        try:
            from app.agents.reward import compute_dynamic_weights
            dyn_weights = compute_dynamic_weights(session, as_of=as_of, base_weights=self._base_weights())
        except Exception:
            dyn_weights = None
        effective_weights = dyn_weights or self._base_weights()

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
                news_weight_pct=int(round(effective_weights.get("news_sentiment", 0.0) * 100)),
                tech_weight_pct=int(round(effective_weights.get("technicals", 0.0) * 100)),
                macro_weight_pct=int(round(effective_weights.get("macro_analyst", 0.0) * 100)),
                fund_weight_pct=int(round(effective_weights.get("fundamentals", 0.0) * 100)),
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

            llm_timeout: float | None = None
            llm_retries: int | None = None
            # Live cycles should fail fast here to avoid blocking the whole run.
            if not context.get("as_of"):
                llm_timeout = float(getattr(self.settings, "live_portfolio_llm_timeout_seconds", 20.0))
                llm_retries = int(getattr(self.settings, "live_portfolio_llm_max_retries", 2))

            raw = self._call_llm(
                self._get_market_time_context(context) + _SYSTEM_PROMPT,
                user_prompt,
                response_format="json",
                timeout_seconds=llm_timeout,
                max_retries=llm_retries,
            )
            parsed = self._parse_json_response(raw)

            if not parsed:
                return AgentSignal.no_signal(self.name, "LLM unavailable; cannot make final decision")

            # Safety checks: enforce risk manager constraints
            action = parsed.get("action", "HOLD").upper()
            if action not in ("BUY", "SHORT", "SELL", "HOLD"):
                action = "HOLD"
            if not risk_approved:
                action = "HOLD"

            raw_position_pct = min(float(parsed.get("position_pct", 0.0)), max_pct)
            position_pct = raw_position_pct
            if action in ("HOLD", "SELL"):
                position_pct = 0.0

            execution_mode = str(parsed.get("execution_mode", "") or "").upper().strip()
            if execution_mode not in (
                "IMMEDIATE",
                "WAIT_PULLBACK",
                "WAIT_BREAKOUT_CONFIRMATION",
                "WAIT_UNTIL_OPEN",
                "NO_TRADE",
            ):
                execution_mode = "IMMEDIATE" if action in ("BUY", "SHORT", "SELL") else "NO_TRADE"

            planned_action = str(parsed.get("planned_action", action) or action).upper().strip()
            if planned_action not in ("BUY", "SHORT", "SELL", "HOLD"):
                planned_action = action

            default_valid = max(5, int(getattr(self.settings, "live_entry_plan_default_valid_minutes", 180)))
            valid_for_minutes = int(parsed.get("valid_for_minutes", default_valid) or default_valid)
            valid_for_minutes = max(5, min(valid_for_minutes, 1440))

            raw_entry_plan = parsed.get("entry_plan")
            if not isinstance(raw_entry_plan, dict):
                raw_entry_plan = {}

            # Keep execution mode consistent with final decision semantics.
            if not risk_approved:
                execution_mode = "NO_TRADE"
                planned_action = "HOLD"
                raw_entry_plan = {}
                valid_for_minutes = default_valid
                planned_position_pct = 0.0
            elif action == "HOLD" and execution_mode == "IMMEDIATE":
                execution_mode = "NO_TRADE"
                planned_position_pct = 0.0
            elif action in ("BUY", "SHORT", "SELL") and execution_mode.startswith("WAIT_"):
                # Waiting mode must be expressed as HOLD + planned_action.
                planned_action = action
                action = "HOLD"
                position_pct = 0.0
                planned_position_pct = raw_position_pct
            else:
                planned_position_pct = raw_position_pct if execution_mode.startswith("WAIT_") else 0.0

            return AgentSignal(
                agent_name=self.name,
                signal=action,
                confidence=min(95, int(self._compute_weighted_confidence(agent_signals, effective_weights))),
                reasoning=parsed.get("reasoning", ""),
                metadata={
                    "action": action,
                    "position_pct": position_pct,
                    "conviction": parsed.get("conviction", "LOW"),
                    "supporting_agents": parsed.get("supporting_agents", []),
                    "dissenting_agents": parsed.get("dissenting_agents", []),
                    "entry_rationale": parsed.get("entry_rationale", ""),
                    "exit_criteria": parsed.get("exit_criteria", ""),
                    "execution_plan": {
                        "execution_mode": execution_mode,
                        "planned_action": planned_action,
                        "planned_position_pct": planned_position_pct,
                        "valid_for_minutes": valid_for_minutes,
                        "entry_plan": raw_entry_plan,
                    },
                },
            )

        except Exception as exc:
            logger.exception("[portfolio_manager] Unexpected error for %s: %s", ticker, exc)
            return AgentSignal.error_signal(self.name, str(exc))

    def _compute_weighted_confidence(self, agent_signals: dict[str, dict], weights: dict[str, float]) -> float:
        """Weighted average confidence using the weights resolved for this run."""
        total, weight_sum = 0.0, 0.0
        for agent, weight in weights.items():
            sig = agent_signals.get(agent)
            if sig and sig.get("signal") != "NO_SIGNAL":
                total += float(sig.get("confidence", 50)) * weight
                weight_sum += weight
        return (total / weight_sum) if weight_sum > 0 else 50.0
