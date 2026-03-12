# FionaTrade V1

事件驱动研究与纸面交易系统（采集 -> 事件分析 -> 信号 -> paper execution -> 回测 -> WebUI）。

## 快速启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp .env.example .env
uvicorn app.main:app --reload
```

WebUI: <http://127.0.0.1:8000>
实时新闻页: <http://127.0.0.1:8000/news>

## API

- `POST /api/ingest/run`
- `POST /api/market/backfill`
- `GET /api/events`
- `POST /api/signals/run`
- `GET /api/signals`
- `POST /api/paper/execute`
- `GET /api/paper/portfolio`
- `POST /api/backtests/run`
- `GET /api/backtests/{run_id}`
- `GET /api/health`

## 数据源

| 来源 | 内容 | 订阅要求 |
|------|------|----------|
| SEC EDGAR | 8-K/6-K 原文 | 免费 |
| RSS (Bloomberg/CNBC/MarketWatch) | 实时新闻 | 免费 |
| Finnhub `/company-news` | 每 ticker 公司新闻（1年历史，tier-1）| Basic+ |
| Finnhub `/stock/candle` resolution=1 | 1 分钟 K 线（10年历史）| Basic+ |
| Finnhub `/stock/earnings` | EPS 实际/预期/surprise | Basic+ |
| Finnhub `/scan/technical-indicator` | 聚合技术信号（buy/neutral/sell + ADX）| Basic+ |
| Finnhub `/scan/support-resistance` | 支撑/阻力位 | Basic+ |
| yfinance hourly | 1小时 K 线（fallback） | 免费 |
| stooq daily | 日线（last resort fallback） | 免费 |

## LLM 架构

- **分类器**：`gemini-3-flash`（仅对 RSS/SEC 非 ticker-tagged 条目做事件类型分类，Finnhub company-news 跳过）
- **主分析**：`claude-sonnet-4-5`（读全文 + EPS context + 技术信号 + 支撑阻力 + 分析师共识，输出 UP/DOWN/NEUTRAL + 持仓周期 + 仓位建议）
- **信号验证**：`SignalValidator`（纯规则，无 LLM 调用）— 主分析完成后对信号做质量审查
- **事件质量筛选**：可选 `gemini-3-flash` 质量门控（HIGH/MEDIUM/LOW + 0-100 分），用于过滤低质量事件
- **回测并发**：`ThreadPoolExecutor(workers=8)` 并发预取所有 LLM 信号，再串行执行交易逻辑

## Signal Validation Layer（信号验证层）

**位置**：主分析（LLM）完成后、回测/实盘执行前。

**作用**：这不是第二个方向预测器，而是一个执行质量审查门控（reviewer / risk gate），用来过滤执行质量差的信号。

**Pipeline 位置**：
```
Event → AnalysisService.event_to_signal() → SignalValidator.validate() → execute / skip
```

**输出维度**（`SignalValidationResult`）：
| 字段 | 含义 |
|------|------|
| `novelty` | NEW / PARTIALLY_KNOWN / STALE / DUPLICATE |
| `event_strength` | STRONG / MODERATE / WEAK / NOISE |
| `priced_in_risk` | LOW / MEDIUM / HIGH |
| `tradeability` | GOOD / MARGINAL / POOR |
| `consistency` | STRONG / MIXED / WEAK |
| `review_score` | 0–100（执行质量，独立于主分析 confidence） |
| `execution_recommendation` | APPROVE / DOWNWEIGHT / REJECT / NO_TRADE |
| `issue_tags` | 机器可读问题列表 |
| `rationale` | 人类可读推理列表 |

**验证规则（deterministic，无 LLM 调用）**：
- 过期新闻检测（`stale_news`）
- 重复事件检测（`duplicate_event`）
- 噪音标题过滤（`noise_headline`）
- 已定价判断（`large_move_before_entry`，价格已移动超过阈值）
- 阻力位过近（`near_resistance` / `near_support`）
- 持仓周期不合理（`horizon_too_short`）
- Ticker 匹配错误（`ticker_mismatch`）
- 方向与分类学冲突（`direction_vs_taxonomy_conflict`）

**配置项**（`.env`）：
```
VALIDATION_ENABLED=true
VALIDATION_MIN_REVIEW_SCORE=40
VALIDATION_REJECT_ON_STALE=true
VALIDATION_REJECT_ON_DUPLICATE=true
VALIDATION_PRICE_MOVE_THRESHOLD_PCT=3.0
VALIDATION_ALLOW_DOWNWEIGHT_EXECUTION=true
VALIDATION_STALE_MINUTES=120
```

**回测关闭验证**（用于单元测试或诊断）：在回测参数中加 `"use_signal_validation": false`。

**日志**：每个信号验证结果以结构化方式输出到 `app.log`，字段包括 `ticker`, `event_type`, `review_score`, `recommendation`, `tags`。

## 历史数据回填脚本

```bash
# 拉 Finnhub 公司新闻（47 个 SP100 ticker，~11000 条/2个月）
python scripts/backfill_finnhub_news.py --from 2026-01-06 --to 2026-03-08

# 回填 Finnhub 1分钟 K 线
POST /api/market/backfill  或  python -c “from app.market.backfill import ...”

# 对现有 SEC raw_items 补全原文 body
python scripts/backfill_sec_bodies.py --limit 500

# 跑归一化 + 验证（处理 unprocessed raw_items）
# 见 app/normalization/service.py build_clusters() + app/validation/service.py validate_and_store()
```

## 说明

- 默认仅 paper，不接真实券商 API。
- 默认 SQLite，本地文件 `fionatrade.db`。
- 1 分钟轮询由应用内调度器执行，可在配置中关闭。
- 日志默认写入 `logs/`：
  - `app.log`：控制台同源应用日志
  - `writeout.log`：结构化流水日志（pipeline/health）
  - `health.log`：自动健康巡检日志

## 配置说明

- 默认已预置 `LLM_BASE_URL=https://api.duojie.games`、`LLM_MODEL=claude-sonnet-4-5`；如网关要求鉴权请补 `LLM_API_KEY`。
- 网关调优参数（建议先保守）：`LLM_TIMEOUT_SECONDS=30`、`LLM_MAX_RETRIES=3`、`LLM_RETRY_BACKOFF_SECONDS=1.5`、`LLM_RETRY_BACKOFF_MULTIPLIER=1.8`、`LLM_RETRY_MAX_DELAY_SECONDS=12`。
- 新增规则 tradeability 过滤：`EVENT_TRADEABILITY_FILTER_ENABLED=true`、`EVENT_TRADEABILITY_MIN_SCORE=55`，会硬过滤观点文、估值文、技术分析、价格复盘、弱 `unknown` 噪音。
- 默认不再把同时间窗消息先合并成一个可交易事件：`NORMALIZATION_MERGE_WINDOW_MIN=0`。如手动开启聚合，`event_time` 会自动取该聚合里最后一条证据时间，避免前视。
- SQLite 锁等待可配置：`SQLITE_BUSY_TIMEOUT_SECONDS=30`（避免并发写入时立刻 `database is locked`）。
- 未配置 `LLM_BASE_URL` 或 `LLM_MODEL` 时，系统自动使用规则回退分析（不会中断主链路）。
- Finnhub Basic 订阅需设置 `FINNHUB_API_KEY`，开启 `MARKET_BACKFILL_ALLOW_STOOQ_FALLBACK=false`。
- SEC 抓取已增加重试和 404 原子订阅回退（ATOM feed）；`SEC_USER_AGENT` 请填写真实邮箱。
- 新闻页提供来源在线状态（ONLINE/OFFLINE）与离线报错明细。
- 回测前可先调用 `POST /api/market/backfill` 回填历史 1m 行情。
- 回测新增硬风控：分钟级止损/止盈（`hard_stops`）、动态风险仓位（`risk_sizing` + `risk_per_trade_pct`）、日内熔断（`daily_circuit_breaker`）。
- 回测进场窗口可配置：`entry_window_min`（默认 120 分钟，替代旧版硬编码 60 分钟）。
- 回测默认不做同日粗暴去重：`dedup_same_day_event=false`，按“消息到达即处理”回放；后续应由加仓/减仓逻辑替代简单去重。
- 回测支持宏观 regime 仓位调节：`regime_risk_adjust=true` 时，按 SPY 20交易日趋势对 `risk_per_trade_pct` 乘系数（BULL 1.2 / BEAR 0.8，均可参数覆盖）。
- 回测新增 routine filing 硬过滤：`<ticker> filed 8-K/10-Q/10-K...` 且无实质负面关键词时直接跳过，避免误分类触发交易。
- 回测出场锚点修复：`planned_exit` 按 `entry_ts + horizon` 计算，不再用 `event_ts + horizon`。
- 回测支持可选 Gemini 质量筛选：`use_event_quality_filter=true` + `event_quality_min_score=70`，过滤低质量事件。
- 回测默认启用规则 tradeability 过滤：`use_tradeability_filter=true` + `tradeability_min_score=55`，会在进入 LLM/执行前硬过滤弱事件与观点型内容。
- 回测支持 unknown 放行策略：`allow_unknown_with_llm=true` 时，`unknown` 在 LLM 模式可进入方向判断，不再被一刀切排除。
- 回测支持下一交易时段开盘进场：`allow_next_session_entry=true` 时，超出 `entry_window_min` 的事件可在“下一时段首根 bar”进场（适配盘前/盘后事件）。
- 回测新增时间轴约束：`regular_session_only=true` 时仅使用美股正式交易时段 bar；`max_next_session_delay_min=1080` 限制 next-session 进场不能拖过久（默认 18 小时，避免周末事件拖到下周还进场）。
- 回测支持 `slippage_bps` 参数，可先用 `0` 做无摩擦诊断；默认滑点已调为 `4 bps`。
- 回测默认仓位参数已上调：`MAX_POSITION_PCT=0.15`、`BACKTEST_RISK_PER_TRADE_PCT=0.002`、`BACKTEST_CONVICTION_POSITION_FLOOR=0.60`。
- `MIN_TRADE_CONFIDENCE` 默认调整为 `70`（避免实时链路在 `75` 下几乎全部被过滤）。
- 已写入短中长线管理（`SHORT/MID/LONG` 周期桶），默认开启：`ENABLE_TERM_MANAGEMENT=true`。
- 回测默认不启用周期桶：`BACKTEST_ENABLE_TERM_HORIZON=false`（需要时可在回测参数 `enable_term_horizon=true` 打开）。
- LLM 可输出仓位建议：`position_pct_suggestion`（0~1，表示“最大允许仓位”的比例），执行层始终受硬风控上限钳制。
- 回测新增 conviction position sizing：`conviction_position_sizing=true` 时，高 tradeability / 高 confidence / 高 severity 的 LLM 信号会自动抬高最小仓位建议与风险预算倍率，但仍受 `max_position_pct` 和 `risk_per_trade_pct` 上限约束。
- 支持一周网格回测（不跑月度）+ 最小交易数过滤，命令：`python scripts/run_weekly_backtest_grid.py --min-trades 5`。
- 支持 1m 行情覆盖审计：`python scripts/audit_bar_coverage.py --start-date 2026-01-02 --end-date 2026-01-10`。
- 支持历史 routine filing 事件重标注：`python scripts/relabel_routine_filings.py --start-date 2026-01-01 --end-date 2026-02-01`。
- 支持 SP100 财报日历自动回填：`python scripts/backfill_earnings_calendar.py --from 2025-10-01 --to 2025-12-31`。
- 支持按当前 normalization/validation 逻辑重建历史事件：`python scripts/rebuild_events_from_raw.py --start-date 2025-10-01 --end-date 2025-11-01`。
- 支持独立检查“财报数据是否拿到 + 当前是否可交易”：`python scripts/check_earnings_tradeability.py --ticker AAPL --event-time 2026-01-28T14:30:00+00:00 --event-type earnings_miss --event-summary "AAPL quarterly earnings beat estimates but stock falls on softer guidance"`。
- 支持 LLM 一致性测试（同窗重复 3 次）：`python scripts/run_llm_consistency_check.py --runs 3`。
- 回测并发 LLM workers 可通过参数 `llm_workers`（默认 8）调整。
- 回测支持 `min_severity` 参数（默认 0 不过滤；70 = 只交易强信号事件 regulatory/accident/supply_chain/litigation 类）。

## 近期变更

### 2026-03-11
- 新增 `earnings_review` 系统：
  - 会结合 `earnings_calendar`、历史 earnings/guidance 事件时间戳和本地 `bars_1m`，计算过去数次财报后的 `2h` 反应、`beat_and_drop_rate`、`miss_and_pop_rate`、`high_bar_score`。
  - 目标是识别“beat 也跌”的高预期股票，而不是只看 headline 里的 `beat/miss`。
- `earnings_review` 已接入：
  - `AnalysisService._event_market_features()`：LLM prompt 可直接看到 `earnings_review`
  - `AnalysisService.assess_tradeability()`：财报类事件会把 `earnings_review` 纳入 tradeability 打分；`POOR` 会直接触发 `earnings_high_bar_risk`
- 新增脚本 `scripts/check_earnings_tradeability.py`
  - 会先刷新目标 ticker 的财报数据，再输出：
    - `earnings_context`
    - `earnings_review`
    - `tradeability`
    - `can_trade_now`
  - 当前 AAPL 冒烟结果：
    - `earnings_context` 可正常获取
    - `earnings_review` 为 `MARGINAL`
    - `can_trade_now=false`
  - 含义：现在系统已经能识别“财报数据拿到了，但 expectation gap 还没解清，不该直接交易”这种状态
- 默认交易语义切到“逐条消息事件化”：
  - `NormalizationService` 默认 `NORMALIZATION_MERGE_WINDOW_MIN=0`，每条 `RawItem` 单独生成事件，不再提前把多条消息揉成一个交易对象。
  - 若手动开启聚合窗口，`event_time` 会取聚合中最后一条证据时间，避免 merged event 用更早时间交易形成前视污染。
- `ValidationService` 改为“历史已见消息交叉验证”：
  - 单源消息不再因为 `tier=1` 就自动 VALID，严格遵守“单源永不下单”。
  - 新消息会与最近 `180` 分钟内已见的同 ticker / 同主题事件做 corroboration，第二独立来源到达后才升级为 VALID。
- 历史分析上下文修复前视：
  - `AnalysisService._evidence_rows()` 会过滤 `published_at > event_time` 的未来证据。
  - 历史事件默认不再读取“今天的” `tech_signal / analyst_consensus / support_resistance`。
- 新增“宏观叙事系统”：
  - `macro_market_context` 会拉取最近一个月的 `SPY/QQQ/IWM/TLT/XLK/XLF/XLE/XLV/XLI` 日线收益，输出 regime、breadth、leadership、laggard、narrative。
  - LLM prompt 现在可看到“上个月总体大盘状态”，用于模糊信号的方向倾斜。
- 新增 SP100 财报日历数据源：
  - 新表 `earnings_calendar`
  - 调度器会自动刷新 Finnhub earnings calendar
  - `earnings_context` 现在按 `event_time` 读取上一次/下一次财报，而不是直接拿“今天看到的最新财报数据”
- `major_litigation` 继续收紧：`favorable court ruling`、`settle AI lawsuits`、`positive outlook following ruling` 这类弱/偏正面诉讼文会降回 `unknown`。
- 新增脚本：
  - `scripts/backfill_earnings_calendar.py`
  - `scripts/rebuild_events_from_raw.py`
- 新增测试：
  - `tests/test_analysis_context.py` 覆盖财报上下文按事件时点读取、未来证据过滤
  - `tests/test_normalization_service.py` 覆盖默认不合并消息、可选 merge 时 `event_time` 取最后证据时间
  - `tests/test_validation_scoring.py` 覆盖“第二独立来源到达后升级 VALID”
- 注意：
  - `run_id=56` 仍是旧事件库上的结果。
  - 要验证这轮新逻辑，先执行 `python scripts/rebuild_events_from_raw.py --start-date 2025-10-01 --end-date 2025-11-01`，再跑回测。
- 事件类型解析新增文本纠偏：`resolve_event_type_for_text()` 会根据标题/摘要中的正负面措辞修正陈旧 taxonomy 标签，避免 `guides above estimates` 仍被当成 `earnings_miss`、`settles litigation` 仍被当成负面诉讼。
- `NormalizationService` 与 `AnalysisService` 统一改用“文本修正后的有效事件类型”：
  - LLM prompt 里同时提供 `original_event_type` 和 `effective_event_type`
  - 若存量标签与正文冲突，优先信正文
  - 规则 fallback 也基于修正后的事件类型出方向
- `SignalValidator` 不再对所有 `unknown` 一刀切拒绝：
  - 带硬催化剂措辞的 `unknown`（如产品召回、监管调查、CEO 变动、并购确认等）可进入 `WEAK/MODERATE` 审核流
  - 仍会对纯观点、估值、价格复盘型 `unknown` 继续拒绝
- 回测新增“财报窗口跨日去重”：
  - 同一 `ticker` 在 36 小时财报窗口内的 follow-up / price recap / after-earnings commentary 只保留首个 anchor 事件
  - metrics 新增 `earnings_window_dedup_dropped`
- 新增测试：
  - `tests/test_normalization_service.py` 覆盖正面财报/正面诉讼标题不再误落入负面事件类型
  - `tests/test_signal_validator.py` 覆盖 hard-catalyst `unknown` 不再被自动拒绝
  - `tests/test_backtest_handoff_followups.py` 覆盖财报窗口跨日去重
- 2025-10 整月参考结果（run_id=56，LLM + 重新跑 tradeability/source 筛选 + earnings-window dedup）：
  - `raw_items=3380`，`events=2931`，`valid_events=2924`
  - 预过滤后：`same_day_dedup_dropped=1981`，`earnings_window_dedup_dropped=14`，`events_considered=929`
  - 进入 LLM：`llm_signals=408`，`tradeability_filtered=368`，`validation_blocked=100`
  - 成交：`trades=10`，`win_rate=50.00%`，`total_return=+0.2114%`，`profit_factor=3.63`
  - 来源归因：`yahoo=+$204.01`，`dowjones=+$7.66`，`seekingalpha=-$0.26`
- 回测默认仓位参数上调：
  - `MAX_POSITION_PCT: 10% -> 15%`
  - `BACKTEST_RISK_PER_TRADE_PCT: 0.1% -> 0.2%`
  - `BACKTEST_CONVICTION_POSITION_FLOOR: 0.45 -> 0.60`
- 修复回测 `annualized_return` 计算：改为按实际时间跨度年化，并对极端短样本做对数/上限保护，避免高收益短样本直接 `OverflowError`。
- 新增 SQLite busy timeout 配置：`SQLITE_BUSY_TIMEOUT_SECONDS`，`app/db/database.py` 对 SQLite 引擎启用 `timeout + check_same_thread=False`。
- 新增规则 tradeability 过滤：`AnalysisService.assess_tradeability()` 会硬过滤观点/估值/技术分析/价格复盘类内容；`event_to_signal()` 与回测预取阶段都会拦截。
- 新增 conviction position sizing：高质量 LLM 信号会自动抬高 `effective_position_pct_suggestion` 和 `effective_risk_per_trade_pct`，避免强信号被明显低配。
- 修复 backtest timeline：
  - 默认只允许正式交易时段（RTH）bar 参与进出场与止盈止损判断。
  - `next_session_entry` 改为只接受正式开盘 bar，不再把盘前首根 bar 误当成“下一时段开盘”。
  - 新增 `max_next_session_delay_min=1080`，阻止周末/隔太久事件延迟进场。
- 新增回测开关：
  - `allow_unknown_with_llm=true`：LLM 回测允许 `unknown` 事件进入方向判断。
  - `allow_next_session_entry=true`：盘前/盘后事件超窗口时可在下一时段首根 bar 进场。
  - `use_tradeability_filter=true`：进入 LLM 前就过滤弱事件/观点型内容。
  - `regular_session_only=true`：只用正式交易时段 bar。
  - `max_next_session_delay_min=1080`：限制 next-session 最长延迟。
  - `conviction_position_sizing=true`：仅在 LLM 回测中启用的仓位放大器。
- 新增 `scripts/relabel_routine_filings.py`：可批量将历史 routine filing 重标注为 `sec_filing`。
- 新增测试：
  - `tests/test_backtest_handoff_followups.py` 覆盖 unknown 放行与下一时段进场。
  - `tests/test_backtest_handoff_followups.py` 覆盖观点文过滤与 conviction 仓位放大。
  - `tests/test_backtest_handoff_followups.py` 覆盖正式交易时段约束与周末延迟进场上限。
  - `tests/test_relabel_routine_filings.py` 覆盖 routine filing 识别。
- 参考结果（run_id=51，2026-01-21~2026-01-28，LLM + tradeability filter + conviction sizing）：
  - `events=88`，`tradeability_filtered=25`，`llm_signals=30`，`trades=10`
  - `win_rate=70.00%`，`total_return=+0.1680%`，`next_session_entry_used=7`
- timeline 修复后参考结果：
  - `run_id=53`（2026-01-12~2026-01-19）：`trades=2`，`win_rate=50.00%`，`total_return=+0.0125%`
  - `run_id=54`（2026-01-21~2026-01-28）：`trades=9`，`win_rate=66.67%`，`total_return=+0.1925%`，`entry_late_skipped=2`
- 整月参考结果（run_id=55，2026-01-01~2026-02-01，采用新的默认仓位参数）：
  - `events=308`，`tradeability_filtered=73`，`validation_blocked=25`，`llm_signals=91`，`trades=7`
  - `win_rate=42.86%`，`total_return=-0.1690%`，`next_session_entry_used=6`，`entry_late_skipped=2`
- 整月参考结果（run_id=56，2025-10-01~2025-11-01，重新跑消息源筛选/事件纠偏/财报窗口去重）：
  - `raw_items=3380`，`events=2931`，`valid_events=2924`
  - `same_day_dedup_dropped=1981`，`earnings_window_dedup_dropped=14`，`events_considered=929`
  - `tradeability_filtered=368`，`validation_blocked=100`，`llm_signals=408`，`trades=10`
  - `win_rate=50.00%`，`profit_factor=3.63`，`total_return=+0.2114%`
- 参考结果（run_id=50，2026-01-21~2026-01-28，LLM）：
  - `events=88`，`trades=18`，`win_rate=55.56%`，`total_return=+0.0934%`，`llm_fallback=1`，`next_session_entry_used=12`。

### 2026-03-10
- 回测引擎新增 `entry_window_min` 参数（默认取 `BACKTEST_ENTRY_WINDOW_MIN=120`），事件后 120 分钟内有 bar 才允许进场。
- 回测引擎新增 `regime_risk_adjust`：根据 SPY 近 20 交易日 regime（BULL/BEAR/NEUTRAL）动态调整每笔风险预算。
- 回测引擎新增 `dedup_same_day_event`：同 `ticker + event_type + day` 保留最高 `severity`，减少重复进场。
- 修复 `AnalysisService` 的 `macro_market_regime` 阈值单位（从 3.0 修正为 3%）。
- 一键回测脚本 `scripts/run_backtest.py` 新增顶部配置项：`ENTRY_WINDOW_MIN`、`REGIME_RISK_ADJUST`、`DEDUP_SAME_DAY_EVENT`。
- 新增测试 `tests/test_backtest_handoff_followups.py` 覆盖：进场窗口、同日去重、regime 风险倍率。
- 回测引擎新增 routine filing 硬过滤（`filed 8-K/10-Q/10-K...` 非实质事件直接跳过），并在 metrics 输出 `routine_filing_skipped`。
- 回测出场锚点修复：`planned_exit_bar` 由 `event_time + horizon` 改为 `entry_time + horizon`，避免错位出场。
- `SEC` ingestion 优先使用 `acceptanceDateTime` 写入 `published_at`，减少 `00:00:00` 假时间导致的 entry_late。
- `TradeSignal` 新增 `position_pct_suggestion`，LLM 可建议 0~1 仓位比例；执行层做硬上限钳制（不突破 max_position/risk cap）。
- 新增可选 Gemini 质量筛选（`use_event_quality_filter`），低于阈值事件直接过滤，支持 `event_quality_fail_open`。
- LLM 网关重试改为配置化参数（超时/重试/指数退避），`event_to_signal` 会按配置自动退避重试，降低 429/502 造成的全量 fallback。
- 回测新增 `allow_unknown_with_llm` 与 `allow_next_session_entry` 两个开关，解决 `unknown` 事件硬过滤和盘前/盘后 `entry_late` 导致的样本过少问题。

### 2026-03-08 (第三批 — Signal Validation Layer)
- **新增 `app/analysis/signal_validator.py`**：Signal Validation Layer，纯规则、无 LLM、同步执行。输出 `SignalValidationResult`（8维评估 + `review_score` + `execution_recommendation`）。
- **集成到回测引擎**：`BacktestEngineService.run()` 新增 `use_signal_validation` 参数（默认跟随 `VALIDATION_ENABLED`），验证失败计入 `metrics["validation_blocked"]`
- **集成到信号引擎**：`SignalEngineService.run()` 新增验证门控，REJECT/NO_TRADE 的信号不写入 `signals` 表，`skipped_validation` 计数
- **新增配置项**：7个 `VALIDATION_*` 环境变量，默认值保持安全
- **新增 25 个测试**：`tests/test_signal_validator.py`，覆盖所有维度和边界条件（缺失 context 不崩溃）
- **已有测试**：`test_backtest_llm_mode.py` / `test_backtest_term_toggle.py` 加 `use_signal_validation=false`，保持原有行为
- **taxonomy 修复**：`supply_chain_disruption` 关键词中去掉了 "shutdown"（政府关门新闻）和 "disruption"（过宽泛），`merger_acquisition` 去掉 "deal"

### 2026-03-08 (第二批)
- **分析师评级共识注入 prompt**：`app/analysis/service.py` 新增 `_finnhub_analyst_consensus()` 调用 Finnhub `/stock/recommendation`，输出 `consensus_score`（-1~+1）和 `BULLISH/NEUTRAL/BEARISH`，1小时缓存，作为 LLM 副参考（不覆盖新闻信号）
- **事件强度过滤器**：回测支持 `min_severity` 参数（默认 0）；设为 70 只保留 regulatory_penalty/accident_disaster/financial_fraud/supply_chain_disruption/major_litigation 等强信号事件
- **高质量文章过滤**：`_build_evidence_payload()` 过滤 body<150 字符条目，按 source_tier 排序（tier-0/1 优先）
- **噪音标题过滤**：新增 `_NOISE_TITLE_PATTERNS` 正则过滤 "trending tickers/market movers/should you invest" 等无信号文章
- **LLM 亏损根因**：merger_acquisition 事件大量为低质量文章（"AES Stock Jumps"报道中顺带提到的其他 ticker），所有事件 severity 统一为55无区分度；解决方案：severity 过滤 + 证据质量过滤

### 2026-03-08 (第一批)
- **Finnhub company-news 接入**：新增 `fetch_company_news()` 对 47 个 SP100 ticker 拉 1 年历史新闻；source_tier=1（等同于 Bloomberg/CNBC）
- **历史新闻回填脚本**：`scripts/backfill_finnhub_news.py`，支持 `--from/--to/--tickers/--dry-run`
- **1分钟 K 线 Finnhub 优先**：`app/market/backfill.py` 改为 `finnhub_1m` 作为首选，403 错误改为 per-ticker 粒度，不影响其他 ticker
- **gemini-3-flash 事件分类器**：对 RSS/SEC 条目的 unknown 事件用小模型分类；Finnhub ticker-tagged 条目跳过 LLM，直接用关键词匹配
- **公司名识别**：新增 `app/core/company_names.py`（~100 个 SP100 映射），正则 word-boundary 匹配，修复 GE 等缩写误识别
- **验证规则放宽**：单条 tier-1 来源（Bloomberg/CNBC/Finnhub company-news）即可通过 VALID
- **EPS/技术信号/支撑阻力注入 prompt**：`app/analysis/service.py` 新增 `_finnhub_earnings_context()`、`_finnhub_tech_signal()`、`_finnhub_support_resistance()`，有 1 小时 LRU 缓存，LLM rules 新增对应使用指导
- **回测并发 LLM**：`app/backtest_engine/service.py` 改为三阶段：①串行预热 Finnhub cache（避免并发 429），②`ThreadPoolExecutor` 并发预取所有 LLM 信号，③串行执行交易；支持 `llm_workers` 参数（默认 8）
