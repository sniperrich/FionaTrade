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
- **主分析**：`claude-sonnet-4-5`（读全文 + EPS context + 技术信号 + 支撑阻力 + 分析师共识，输出 UP/DOWN/NEUTRAL + 持仓周期）
- **信号验证**：`SignalValidator`（纯规则，无 LLM 调用）— 主分析完成后对信号做质量审查
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

- 默认已预置 `LLM_BASE_URL=https://api.duojie.games`、`LLM_MODEL=claude-sonnet-4-6`；如网关要求鉴权请补 `LLM_API_KEY`。
- 未配置 `LLM_BASE_URL` 或 `LLM_MODEL` 时，系统自动使用规则回退分析（不会中断主链路）。
- Finnhub Basic 订阅需设置 `FINNHUB_API_KEY`，开启 `MARKET_BACKFILL_ALLOW_STOOQ_FALLBACK=false`。
- SEC 抓取已增加重试和 404 原子订阅回退（ATOM feed）；`SEC_USER_AGENT` 请填写真实邮箱。
- 新闻页提供来源在线状态（ONLINE/OFFLINE）与离线报错明细。
- 回测前可先调用 `POST /api/market/backfill` 回填历史 1m 行情。
- 回测新增硬风控：分钟级止损/止盈（`hard_stops`）、动态风险仓位（`risk_sizing` + `risk_per_trade_pct`）、日内熔断（`daily_circuit_breaker`）。
- 回测支持 `slippage_bps` 参数，可先用 `0` 做无摩擦诊断；默认滑点已调为 `4 bps`。
- `MIN_TRADE_CONFIDENCE` 默认调整为 `70`（避免实时链路在 `75` 下几乎全部被过滤）。
- 已写入短中长线管理（`SHORT/MID/LONG` 周期桶），默认开启：`ENABLE_TERM_MANAGEMENT=true`。
- 回测默认不启用周期桶：`BACKTEST_ENABLE_TERM_HORIZON=false`（需要时可在回测参数 `enable_term_horizon=true` 打开）。
- 支持一周网格回测（不跑月度）+ 最小交易数过滤，命令：`python scripts/run_weekly_backtest_grid.py --min-trades 5`。
- 支持 1m 行情覆盖审计：`python scripts/audit_bar_coverage.py --start-date 2026-01-02 --end-date 2026-01-10`。
- 支持 LLM 一致性测试（同窗重复 3 次）：`python scripts/run_llm_consistency_check.py --runs 3`。
- 回测并发 LLM workers 可通过参数 `llm_workers`（默认 8）调整。
- 回测支持 `min_severity` 参数（默认 0 不过滤；70 = 只交易强信号事件 regulatory/accident/supply_chain/litigation 类）。

## 近期变更

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
