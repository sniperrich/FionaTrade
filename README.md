# FionaTrade v0.2.0

> 自主多 Agent 交易系统 + 量化研究平台  
> Python 3.11 · FastAPI · SQLAlchemy · APScheduler · LangGraph · Alpaca

---

## 快速启动

```bash
# 安装
pip install -e .[dev]

# 配置
cp .env.example .env
nano .env   # 填写 FINNHUB_API_KEY / LLM_API_KEY / ALPACA_API_KEY

# 一键本地启动（自动同时拉起 Web + Worker Supervisor）
./run_local.sh

# Web（只负责 API + UI）
uvicorn app.main:app --host 0.0.0.0 --port 6888 --reload

# Worker Supervisor（负责自动拉起 worker / 崩溃重启 / 失败恢复）
python -m app.worker.supervisor

# 访问 WebUI
open http://localhost:6888
```

说明：
- `run_local.sh` 会优先使用当前已激活的 conda 环境
- 如果当前没激活 conda 环境，它会自动尝试 `CONDA_ENV_NAME`，默认值是 `FionaTrade`
- `run_local.sh` 现在会自动探测常见 Miniconda/Anaconda 安装路径，并把输出写到 `logs/web.local.log` / `logs/supervisor.local.log`
- 若 supervisor 在启动后几秒内退出，脚本会直接报错，不再出现“只有 web 起了、worker 没起来”的假成功
- 已修复 Bash 变量展开坑：中文标点紧邻 `$WEB_PID` 这类变量时会被误判成更长变量名，当前脚本已统一改成 `${VAR}` 写法
- 需要改端口时可这样运行：`PORT=6999 ./run_local.sh`
- 停止时直接 `Ctrl+C`

### SQLite -> Postgres 迁移

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --sqlite-url sqlite:///./fionatrade.db \
  --postgres-url postgresql+psycopg://USER:PASS@HOST:5432/fionatrade \
  --truncate-target
```

说明：
- 脚本按表分批迁移（主键升序 chunk）
- 默认会阻止“迁移期间仍有 live/backtest 正在写库”的情况
- 如需强制跳过该保护，可加 `--allow-active-writes`（不推荐）
- 迁移后会自动输出逐表 `source/target` 行数校验与抽样一致性结果

---

## 架构

### 三层拆分（当前默认）

```text
web    -> FastAPI + Jinja UI + command/control API
worker -> APScheduler + ingestion + live cycle + backfill + command pump
db     -> SQLite/Postgres，统一保存状态、结果、行情缓存、运行态
```

关键点：
- `app.main` 已经变成纯 Web 入口，不再持有 scheduler
- `app.worker.supervisor` 负责自动拉起和重启 `app.worker.main`
- `app.worker.main` 只负责后台任务，不负责自我守护
- `Enable Live` 写入 `runtime_controls`，不再只改当前进程内存
- worker 每 5 秒写一次 heartbeat 到 `runtime_controls`，WebUI 可判断 `ONLINE / STALE / OFFLINE`
- supervisor 也会写 heartbeat，WebUI/API 能区分 “worker 挂了” 和 “根本没人守护”
- live runtime 读取 `worker_runs / worker_run_events`，不再依赖进程内 runtime store
- `MarketDataService` 统一 chart/cache/freshness/fallback/backfill
- `live_cycle` 现在带进程内互斥：scheduled / manual / queued 三条路径不会再在同一 worker 内重叠执行
- worker command pump 已拆成高优先级（live / bar refresh / ingestion / earnings）和低优先级（backtest）两条 lane，长 backtest 不再把 live 命令整段饿死
- live 下单前会强制检查本地 `Bar1m` 新鲜度；超过 `LIVE_DATA_MAX_AGE_MINUTES` 只分析不下单
- live entry planning 已接入：`HOLD` 可附带 `WAIT_*` 计划（pullback / breakout / until_open），worker 在后续 cycle 自动触发
- 同一 ticker 只保留一个 active entry plan（Replace Old），新计划会替换旧计划
- 浏览器只是控制面板：关闭 UI 不会停止自动交易；真正执行取决于 worker 是否存活
- SQLite 现在默认启用 `WAL + busy_timeout`，降低 worker/supervisor/backtest 并发写锁冲突
- 已支持 PostgreSQL 单库运行（推荐 live/backtest 并发场景使用 Postgres）
- worker 启动时会自动清算遗留的 `RUNNING` 命令/运行/回测，避免重启后旧任务永久显示运行中
- `CNBC / Yahoo` 现在统一视为 **secondary confirmation sources**：可做 corroboration，但不会再作为单独 primary trigger 使用
- 回测默认已打开 **同 ticker + 同有效事件类型 + 同日去重**（`backtest_dedup_same_day_event=true`）

### Agent 模式（默认，`AGENT_MODE_ENABLED=true`）

```
IngestionService（新闻 + FRED + 基本面，每 60s 轮询）
        ↓
AgentGraph（app/agent_graph/graph.py）
  ├─ [并行] MacroAnalystAgent     → FRED 指标 + 宏观新闻 → LLM
  ├─ [并行] NewsSentimentAgent    → RawItem/Event DB    → LLM（可请求全文）
  ├─ [并行] FundamentalsAgent     → FundamentalsSnapshot + AnalystRating → LLM
  ├─ [并行] TechnicalsAgent       → Bar1m → ta 库 → 纯规则（无 LLM）
  ├─ [串行] RiskManagerAgent      → 持仓状态 + Position → LLM
  └─ [串行] PortfolioManagerAgent → 汇总 → LLM → BUY/SHORT/HOLD
        ↓
AgentRun 写入 DB
        ↓
LiveTradingService → AlpacaBroker（bracket orders + ATR stops）
```

**Agent 权重（收缩版默认）：** 新闻 60% · 技术 20% · 宏观 10% · 基本面 10%

**Live 调度（收缩版默认）：**
- 事件驱动优先：有新增可交易事件才跑完整决策
- 无新增事件时仅 10 分钟兜底触发一次 fast-path
- fast-path 复用 Macro/Fund TTL 缓存（默认 60/120 分钟）
- live 执行前有置信度闸门：`LIVE_MIN_CONFIDENCE`（默认 50），低于阈值的 BUY/SHORT/SELL 会被降级为 HOLD（不下单）
- `Disable Live` 支持三种模式：`PAUSE_ONLY / CANCEL_ORDERS / FLATTEN_ALL`
- 新增独立 `overnight_risk_control`：默认收盘前 5 分钟检查一次总敞口，按 `REDUCE / FLATTEN / ALERT_ONLY` 执行；即使 `live=false` 也可继续保护已有仓位

**资金确认层（软门槛）：**
- 输出 `flow_score(0-100)` + `flow_bucket(HIGH/MEDIUM/LOW/WEAK)` + `position_multiplier`
- `>=70→1.10`, `55-69→0.80`, `40-54→0.60`, `<40→0.35`
- 最终仓位 = `portfolio_position_pct * position_multiplier`
- `flow_score<40` 时 BUY/SHORT 会降级为 `WAIT_BREAKOUT_CONFIRMATION`（软降级，不是一刀切禁入）
- execution 层会在加仓后再次做单票上限钳制，不会突破 `max_position_pct`

**Backtests 页面（新增）：**
- Run Detail 现在包含收益率曲线（Return Curve）与回撤曲线（Drawdown Curve）
- 指标卡补充 `Sharpe Ratio / Max Drawdown / Profit Factor / Win Rate`

**风控限制（硬编码）：** 最大仓位 20% · 日亏损上限 3% · 最少 2 个信号共识

## WebUI 页面

| 路径 | 功能 |
|------|------|
| `/` | 仪表盘：组合状态、系统状态、最新 Agent 决策、新闻、**portfolio curve + market snapshot + worker/queue 状态** |
| `/live` | 实盘：持仓、手动下单（market/limit/bracket）、Enable/Disable Live 停机模式、`Flatten All` 一键平仓、持仓风险摘要（gross/net exposure、position risk P&L、today total P&L、overnight guard）、runtime activity、worker heartbeat、command queue、worker history、local bar cache、ticker K-line、entry plan 面板、entry plan trigger log、成交历史翻页（策略参数改在 `/settings`） |
| `/agents` | AI Agent：LLM 状态、市场时钟、触发运行、推理展开、运行记录翻页、**Trigger Event 证据链可视化 + 关联 Live Trade 明细** |
| `/news` | 新闻流：全文展开、来源/ticker 过滤、**用途分层（RAW/Event/Agent/Live）**、按 published_at 排序 + 历史补录标识 + 30s 自动拉新 + 源状态/报错 + 历史翻页 |
| `/backtests` | Backtest 控制台：时间区间、LLM/rules、source filter、后台排队执行、结果列表与快速检查 |
| `/backtests/{run_id}` | Backtest 详情页：大图模式（收益率曲线 + 回撤曲线）+ Sharpe/回撤/PF + 全量交易明细 |
| `/attribution` | 模块归因面板：Agent 边际贡献、事件类型/source/source tier/flow bucket 分桶、过滤器价值排行、评分回填 |
| `/settings` | 统一配置中心：LLM 网关、采集周期、数据源开关 + Live 策略参数（ticker/source 白名单、节奏、flow gate、agent 权重、执行内核、收盘前是否平仓），并回显当前值（已选 ticker/source 等） |

---

## 目录结构

```
app/
  core/          配置（config.py）、日志、market_hours、SP100 ticker 集合
  db/            SQLAlchemy 模型（models.py）+ db_session() 上下文管理器
  worker/        supervisor.py + main.py（守护 / scheduler / command pump / live cycle / backfill）
  ingestion/     数据源：finnhub_client / rss_client / sec_client / fred_client / earnings_release_client
  normalization/ RawItem → Event（聚类 + ticker 提取 + taxonomy）
  validation/    事件去重 + 冲突检测
  tools/         Agent 专用 DB 只读工具（market_data / fundamentals / news / macro）
  agents/        6 个 Agent 类（继承 BaseAgent）
  agent_graph/   graph.py（AgentGraph）+ state.py（TypedDict 状态）
  broker/        alpaca.py（完整 Alpaca REST v2，777 行）+ paper.py
  services/      live_trading.py + overnight_risk.py + market_data.py + worker_runtime.py + runtime_control.py
  backtest_engine/ 离线研究/回测模块（现已通过 worker-backed `/backtests` 控制面暴露）
  market/        1m K 线回填（Finnhub → Alpaca → yfinance → stooq）
  monitoring/    HealthAuditService（数据源延迟 + 状态快照）
  api/routes.py  所有 REST API 端点
  webui/routes.py Jinja2 页面路由

templates/       10 个 HTML 模板（Dashboard / Live / Agents / News / Backtests / Backtest Detail / Attribution / Settings / Base 等）
static/ft.css    Claude 风格 CSS 设计系统
scripts/         独立工具脚本（回测、历史数据回填等）
tests/           pytest 测试集
```

---

## 关键 API 端点

| 方法 | 路径 | 返回格式 |
|------|------|---------|
| GET | `/api/health` | `{status, llm_configured, llm_model, sources_online, ...}` |
| GET | `/api/live/status` | **平铺字段**：`{enabled, market_tradeable, market_session(字符串), market_time, tickers, live_min_confidence, live_allowed_sources, live_disable_default_mode, live_overnight_* , overnight_state, worker, supervisor, command_queue, ...}` |
| GET | `/api/worker/status` | worker + supervisor heartbeat + command queue 快照 |
| GET | `/api/worker/history` | recent worker runs + commands + runtime events |
| GET | `/api/agent/runs` | **包装对象**：`{"runs": [...]}` — 每项用 `*_result` 字段名 |
| GET | `/api/news` | **分页对象**：`{"items": [...], "mode", "latest_id", ...}` |
| POST | `/api/agent/run` | 触发 Agent 图：`{"tickers": ["AAPL", "NVDA"]}` |
| POST | `/api/ingest/run` | 给 worker 排队一次 ingestion |
| GET | `/api/backtests/options` | 回测表单选项：sources / event profiles / default params |
| GET | `/api/backtests` | 最近回测 runs 列表 |
| GET | `/api/backtests/{run_id}` | 单个回测详情：params / metrics / equity_curve / trade_log |
| POST | `/api/backtests/run` | 给 worker 排队一条 backtest 任务 |
| GET | `/api/attribution/overview` | 模块归因总览（回测分桶 + Agent 贡献 + 过滤器价值） |
| GET | `/api/attribution/runs/{run_id}` | 单 run 归因详情（分桶 + filter hits） |
| POST | `/api/attribution/agent-scores/backfill` | 回填 AgentScore（历史评分批处理） |
| POST | `/api/live/set_enabled` | 写入共享 runtime control，并支持 `disable_mode=PAUSE_ONLY/CANCEL_ORDERS/FLATTEN_ALL` |
| POST | `/api/live/close_all_positions` | 取消全部挂单并一键平仓 |
| GET | `/api/settings/editable` | 返回可编辑的 `.env` 配置快照 |
| POST | `/api/settings/editable` | 保存配置到 `.env`（支持额外 `KEY=VALUE` 覆盖） |
| POST | `/api/live/cycle` | 给 worker 排队一次 live cycle |
| POST | `/api/live/order` | 手动下单 |
| GET | `/api/live/trades` | recent live trades |
| GET | `/api/live/plans` | delayed entry plans |
| GET | `/api/live/plans/events` | entry plan lifecycle/trigger events |
| POST | `/api/live/plans/{plan_id}/cancel` | cancel active entry plan |
| GET | `/api/live/positions` | Alpaca 当前持仓 + `gross_exposure / net_exposure / unrealized_pnl_total / open_orders_count / overnight_guard` |
| GET | `/api/live/open_orders` | Alpaca 挂单 |
| GET | `/api/live/portfolio_history` | Alpaca 权益曲线 |
| GET | `/api/live/bars` | K 线图接口（`source=auto/cache/broker`，默认 auto） |
| GET | `/api/live/runtime` | 当前 live runtime 状态：cycle stage / current ticker / current agent / recent events |
| GET | `/api/live/bar_cache` | 本地 `bars_1m` 缓存状态：最新时间 / stale 情况 / source 分布 |
| GET | `/api/ui/dashboard_snapshot` | Dashboard 聚合快照（health/live/positions/news/runs/chart） |
| GET | `/api/ui/live_snapshot` | Live Trading 聚合快照（market/positions/orders/trades/plans/plan_events/runtime/bar_cache/chart） |

> ⚠️ **JS 开发注意：**
> - `market_session` 是字符串，不是对象
> - `/api/agent/runs` 返回 `{runs:[...]}` 包装，需解包
> - AgentRun 字段名：`*_result`（不是 `*_output`）
> - Conviction：`r.portfolio_result.metadata.conviction`
> - Risk approved：`r.risk_result.metadata.approved`
> - 执行时间：`r.execution_time_ms`
> - `/api/news` 现在额外返回 `metadata` 和 `body_preview`
> - `/api/news` 默认排序改为 `published_at desc, id desc`（`since_id/before_id` 轮询兼容保留）
> - `/api/news` 现在额外返回 `historical_backfill` 和 `backfill_delay_min`，用于前端标记历史补录
> - `/api/news` 现支持 `purpose=all|raw|event|agent|live`，并返回 `purpose_layer / linked_event_ids / linked_agent_run_ids / linked_live_trade_ids / purpose_counts`
> - `/api/live/set_enabled` 同时接受 `POST` 和 `PUT`
> - Dashboard/Live 首屏改成 `snapshot + sessionStorage`，重新打开页面会先用上次结果秒开，再后台刷新
> - `/api/agent/runs` 现支持 `ticker/action/limit/offset`
> - `/api/agent/runs` 现返回 `trigger_event / trigger_event_evidence_count / live_trade_summary`；`/api/agent/runs/{id}` 返回 trigger event evidence 明细与 linked live trades
> - `/api/live/trades` 现支持 `ticker/limit/offset`
> - Live 页的 runtime 状态现在来自数据库 `worker_runs / worker_run_events`
> - `/api/live/status` 现额外返回 `worker` / `supervisor` / `command_queue`
> - `/api/worker/history` 用于 `/live` 的专门排障面板（command queue / worker history）
> - `/api/backtests/run` 会先创建 `QUEUED` 的 `BacktestRun`，再给 worker 排队命令；浏览器关闭后任务照样继续
> - `/api/backtests` / `/api/backtests/{run_id}` 现在额外返回 `phase / phase_label / phase_current / phase_total / phase_pct / phase_detail / last_progress_at`
> - `/api/attribution/overview` 支持 `start_date/end_date` 或 `lookback_days`、`mode`、`run_ids`、`tickers`、`min_sample`
> - `Module Attribution` 面板默认用 30 天窗口；`Backfill Agent Scores` 会触发 `/api/attribution/agent-scores/backfill`
> - `/api/attribution/overview` 与 `/api/attribution/runs/{run_id}` 现在都会返回 `source_buckets`（按具体消息源统计 trades/win_rate/pnl/drawdown）
> - `batch_score_runs` 已支持 `as_of` + ready cutoff，不再因参数不匹配导致 `agent_scores` 长期为 0
> - `batch_score_runs(limit=...)` 现在先在 SQL 层排除已评分 run，再应用 limit，避免“请求 10 条但实际处理更少”的偏差
> - Live 页 K 线图默认 `source=auto`：本地 `bars_1m` 足够新时优先显示 cache，否则回退 broker
> - 若当前是周末/美股闭市，live cycle 会显示 `analysis mode`，这是预期行为，不是失败
> - 若 `LIVE_TRADING_TICKERS` 与 `AGENT_TICKERS_OVERRIDE` 都为空，live cycle 会明确显示 `no live tickers configured`
> - 模板页面脚本必须放在 `base.html` 的 `{% block scripts %}` 中，不能直接内联在 `content` 里，否则会先于全局工具函数执行
> - `Enable Live` 现在是 DB 共享开关，worker 不运行时只会看到 queued command，不会真的执行
> - `Enable Live` 现在会先检查 worker + supervisor heartbeat；若后台不在线，会直接返回 `409`，阻止出现“按钮打开了但实际上没有执行进程”的假成功
> - `Disable Live` 现在会取消尚未执行的 live 相关命令，避免关闭后旧的 `refresh_bars / live_cycle / ingestion` 继续跑
> - `Disable Live` 现在还支持停机模式：只停新单 / 取消挂单 / 全部平仓
> - `/api/live/positions` 返回的 `risk_source_label=已有持仓浮盈亏`，用于前端明确区分“现有仓位风险”与“agent 偷跑”
> - worker 新增 `overnight_risk_control` 定时任务：默认在 `15:55-16:00 ET` 执行一次，使用 `LIVE_OVERNIGHT_*` 配置限制隔夜敞口；若 `LIVE_FLATTEN_BEFORE_CLOSE=true`，则收盘前直接全平
> - `/settings` 页面已支持写入 `.env`；保存后 web 端会热加载，worker/supervisor 需要重启才能完全应用后台参数变更
> - `/api/live/set_enabled` 现在会按当前数据库连接重试写入；若 SQLite 仍被长事务占住，会返回 `503 database is busy`，而不是直接 500
> - worker 遇到短时 SQLite 锁时会把受影响的 `RUNNING` command 重新排回 `PENDING`，避免残留假运行状态
> - 修复 `/live` 页面模板作用域问题：`scripts` block 不再引用未定义的 `led`，避免打开 Live 页面直接 `500 Internal Server Error`
> - worker runtime API 现在统一返回带 `+00:00` 的 UTC 时间；前端 “x ago” 不会再把 SQLite 的 naive UTC 误当成上海本地时间
> - `live_cycle` 与高频 scheduler 现在走 `fast ingestion`：跳过 SEC 重扫描和 SEC summary LLM，避免把 live command queue 长时间堵死
> - `Enable Live` 不再先排重型 ingestion；现在优先 `refresh_bars + live_cycle`
> - 若已有一个 `live_cycle` 正在运行，新触发的 cycle 会返回 `live_cycle_in_progress` 并跳过，避免重叠分析/重复下单
> - worker 现在优先消费 `run_live_cycle / refresh_bars / run_ingestion_validation / refresh_earnings_calendar`，backtest 放在低优先级 lane
> - live 下单前会检查本地 `Bar1m` 是否新鲜；若缓存缺失或过旧，会把该 ticker 记为 `stale_market_data` 并 suppress order
> - PortfolioManager 现在可输出 `execution_mode + entry_plan`；当 action=HOLD 且 mode=WAIT_* 时，live 会创建 entry plan 并在后续 cycle 触发执行
> - `/api/live/plans` 提供计划列表，`/api/live/plans/{plan_id}/cancel` 可手动取消 active 计划
> - `/api/live/plans/events` 提供 entry plan 事件流（created/evaluated/triggered/trigger_failed/expired/cancelled），`/live` 页有独立 Trigger Log 卡片用于排障
> - 已修复并行 agent 进度回调并发写库导致的 `Session is already flushing / prepared state`，live cycle 不会再因该错误把 AAPL/NVDA 直接打成 graph error
> - 一旦 live 已启用，只要 `python -m app.worker.supervisor` 还在运行，关闭浏览器不会停止 auto trading
> - 已修复 WebUI 模板渲染参数顺序（兼容 FastAPI/Starlette 新版 `TemplateResponse`），避免 `TypeError: unhashable type: 'dict'`

---

## 数据模型速查

### RawItem 实际列
```
id, source, source_tier, url, title, body, published_at,
ingested_at, item_hash, metadata_json, processed
```
全文在 `body`；Finnhub ticker 在 `metadata_json['ticker']`

### AgentRun 关键字段
```python
AgentRun.ticker           # str
AgentRun.final_action     # "BUY" / "SHORT" / "HOLD"
AgentRun.final_position_pct  # float 0~1
AgentRun.macro_output     # JSON → API 返回 macro_result
AgentRun.news_output      # JSON → news_result
AgentRun.fundamentals_output # JSON → fundamentals_result
AgentRun.technicals_output   # JSON → technicals_result
AgentRun.risk_output      # JSON → risk_result  (approved 在 metadata 里)
AgentRun.portfolio_output # JSON → portfolio_result (conviction 在 metadata 里)
AgentRun.execution_ms     # int → API 返回 execution_time_ms
```

---

## 实盘交易

### 启用方式
```bash
# 临时（重启后失效）
curl -X POST http://localhost:6888/api/live/set_enabled \
  -H 'Content-Type: application/json' -d '{"enabled": true}'

# 当前默认 `.env`：
LIVE_TRADING_ENABLED=true
LIVE_TRADING_TICKERS=AAPL,NVDA,MSFT,AMZN,GOOGL,META,TSLA,JPM,XOM,UNH,JNJ,PG,HD,AVGO,BAC
```
或直接点击 `/live` 页面右上角的 **▶ Enable Live** 按钮。

`LIVE_TRADING_TICKERS` 与 `AGENT_TICKERS_OVERRIDE` 现在兼容 CSV 和 JSON 数组两种写法。

收缩升级新增配置（基础项在 `/settings`，实盘策略项在 `/live` 可直接改）：
- `LIVE_EVENT_DRIVEN_MODE=true`
- `LIVE_OPEN_CYCLE_SECONDS=900`（开盘 15 分钟）
- `LIVE_CLOSED_CYCLE_SECONDS=7200`（闭市 120 分钟）
- `LIVE_TICKER_COOLDOWN_MINUTES=60`（同票无新事件去抖）
- `LIVE_FALLBACK_CYCLE_SECONDS=600`（兼容旧版本保留）
- `LIVE_FAST_PATH_MACRO_TTL_MIN=60`
- `LIVE_FAST_PATH_FUND_TTL_MIN=120`
- `LIVE_PORTFOLIO_LLM_TIMEOUT_SECONDS=20`
- `LIVE_PORTFOLIO_LLM_MAX_RETRIES=2`
- `LIVE_ALLOWED_SOURCES=`（空=全部；可填 `finnhub,sec,cnbc,...`）
- `ENABLE_FINNHUB_COMPANY_NEWS_LIVE=true`
- `FINNHUB_COMPANY_NEWS_LIVE_LOOKBACK_DAYS=2`
- `FLOW_CONFIRMATION_ENABLED=true`
- `FLOW_CONFIRMATION_SOFT_GATE=true`
- `AGENT_WEIGHT_NEWS=0.60`
- `AGENT_WEIGHT_TECHNICALS=0.20`
- `AGENT_WEIGHT_MACRO=0.10`
- `AGENT_WEIGHT_FUNDAMENTALS=0.10`

归因与来源过滤修正：
- `Module Attribution` 的同 tier source 选择改为稳定顺序：`event_id -> source_tier -> captured_at -> id`，不再用字母序 tie-break。
- `Module Attribution` 页面已修正宽表溢出：卡片与表格在窄屏会自适应，长 bucket/source 文本会自动换行并保留 title 提示。
- source 归一化补齐 `Yahoo Finance` / `yahoo-finance`，并统一空格与连字符，保证白名单匹配一致。

> Enable Live 现在不会再让 Web 进程直接起后台线程。
> 它会写入共享 `runtime_controls`，再给 worker 排队 `refresh_bars + live_cycle`。
> worker/supervisor 心跳都写在 `runtime_controls`，可从 `/api/worker/status` 或 Live 页面直接确认后台是否在线。

---

## 回测控制面

- `/backtests` 现在是正式控制页面，不再要求手动跑脚本才能研究
- WebUI 负责：
  - 选择 `start_date / end_date`
  - 选择 `rules / llm`
  - 选择 `event_profile`
  - 选择 `source filter`
  - 选择 `flow confirmation` 与 `soft gate` 参数
  - 查看最近 runs、metrics、trade log、事件进度条
- Worker 负责：
  - 消费 `run_backtest` command
  - 创建/更新 `BacktestRun`
  - 在后台执行回测，不依赖浏览器存活
- Backtest 运行中会持续把 `progress_current / progress_total / progress_pct` 写回 `BacktestRun.metrics`
- 对于主循环前的耗时阶段，还会持续写入 `phase_*` 字段，因此 Finnhub 预热/LLM 预取期间 UI 不会一直卡在 `0/N`
- 回测已接入资金确认层：会记录 `flow_score / flow_bucket / flow_position_multiplier`，并在 soft gate 开启时按 multiplier 缩放仓位
- 当 `flow_score < 40` 且 soft gate 开启时，回测会模拟 `WAIT_BREAKOUT_CONFIRMATION`：在有效窗口内等待突破确认，未触发则跳过该笔
- Supervisor heartbeat 遇到短时 SQLite lock 会跳过本次写入并继续守护，不会再因为 heartbeat 写失败把 worker 一起带崩
- Worker 重启时会把上次异常中断留下的 `RUNNING` backtest / worker command / worker run 统一标记为 `FAILED`

当前支持的 source filter 语义：
- 若选择 `sources`，会按 **规范化后的 source 名称** 过滤（例如 `yahoo` 会归一为 `yahoo_finance`）
- 当旧事件缺少 `event_evidence` 时，analysis/backtest 会从同 ticker 的历史 `raw_items` 做 fallback 匹配，并补写 evidence lineage
- 若不选，默认使用全部 source

当前事件质量/来源规则补充：
- `CNBC / Yahoo / Yahoo Finance RSS` 会被降成二级确认源；`Reuters/Bloomberg/SEC/company` 这类仍可作为 primary evidence
- `trade tracker / what's going on with / why are ... trading / returns to haunt / preview / long-term potential / top movers / market chatter / recap` 这类 follow-up/commentary 标题会被降级或直接过滤
- 纯 secondary-only 的事件不会通过 validation 成为可交易 primary event，也会在 tradeability gate 被挡掉
- RSS/SEC/Finnhub source status 在写入 `source_status` 前会先按 `source_key` 聚合，避免同域多 feed 触发 SQLite `UNIQUE constraint failed: source_status.source_key`

当前实现仍然是事件回测，不是 AgentGraph 全链回测。

### 下单逻辑
- Alpaca bracket 订单（止损 + 止盈原子提交）
- ATR 自适应止损：2.5× ATR，范围 [3%, 8%]；止盈 = 2× 止损距离
- 周末/盘后：`dry_run=True`，继续分析但不下单（记为 `status='analysis'`）

### Alpaca 配置
```
ALPACA_BASE_URL=https://paper-api.alpaca.markets  # 模拟盘
ALPACA_BASE_URL=https://api.alpaca.markets        # 实盘
```

---

## 部署（Debian 12）

```bash
# 服务器一键部署（deploy.sh 默认使用“脚本所在目录”作为 DEPLOY_DIR）
cd /你的项目目录/FionaTrade
bash deploy.sh
nano .env          # 填入 API keys
systemctl restart fionatrade
systemctl restart fionatrade-worker

# 验证
curl http://localhost:6888/api/health
```

注意：不要把本机的 `.venv/` 复制到服务器。若已复制，`deploy.sh` 现在会自动检测并重建虚拟环境。

可选：自定义部署目录/服务用户/端口

```bash
DEPLOY_DIR=/opt/fionatrade SERVICE_USER=fiona APP_PORT=6888 bash deploy.sh
```

Python 版本要求：`>=3.11`。如服务器已有自定义解释器，可显式指定：

```bash
PYTHON_BIN=/usr/bin/python3.11 bash deploy.sh
```

若服务器 `apt-get update` 被错误第三方源卡住（例如 Debian 误加 Ubuntu deadsnakes PPA），可先跳过系统依赖步骤：

```bash
SKIP_APT=1 bash deploy.sh
```

Nginx 示例已预置：
- 域名：`trade.taylorswift.fit`
- 白名单 IP：`104.245.12.20`、`151.244.134.76`（另允许本机 `127.0.0.1` / `::1`）

如果服务器已有 OpenResty，请使用仓库内 `openresty.conf.example`（不要再启动 nginx 抢占 80 端口）：

```bash
cp /home/FionaTrade/openresty.conf.example /usr/local/openresty/nginx/conf/conf.d/fionatrade.conf
openresty -t
systemctl restart openresty
systemctl disable --now nginx || true
```

### 必填 API Keys

| 变量 | 说明 |
|------|------|
| `FINNHUB_API_KEY` | 新闻 + 一级 K 线源（推荐；若已配置 Alpaca，可作为 K 线主链路的第一优先级）|
| `LLM_API_KEY` + `LLM_BASE_URL` + `LLM_MODEL` | AI 接口（必须）|
| `ALPACA_API_KEY` + `ALPACA_API_SECRET` | 模拟/实盘（交易必须）|
| `FRED_API_KEY` | 宏观数据（可选，免费申请）|

### 运维命令
```bash
journalctl -u fionatrade -f        # Web 日志
journalctl -u fionatrade-worker -f # Worker/Supervisor 日志
systemctl restart fionatrade       # 重启
curl http://localhost:6888/api/health  # 健康检查
```

---

## 测试

```bash
pytest tests/                      # 运行测试集
pytest tests/ --cov=app            # 带覆盖率
pytest tests/test_agent_graph.py -v
```

---

## 关键约定

### 配置注入
所有服务通过构造函数接收 `settings`，通过方法参数接收 `session`。不直接 import 全局 settings。

### DB Session
```python
# 后台代码
from app.db.database import db_session
with db_session() as session:
    ...  # 成功自动 commit，异常自动 rollback

# FastAPI 路由
session: Session = Depends(get_db)
```

### SQLite 时区陷阱
SQLite 存储的 datetime 不带时区，`utc_now()` 返回 tz-aware，相减前需：
```python
from datetime import timezone
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
```

---

## 数据源

| 源 | Env Var | Tier | 说明 |
|----|---------|------|------|
| Finnhub | `FINNHUB_API_KEY` | 1 | 公司新闻 + 1m K 线 + 基本面 + 分析师评级 |
| FRED | `FRED_API_KEY` | 1 | CPI / GDP / UNRATE / FEDFUNDS / DGS10 / VIX |
| RSS | — | 1-2 | Reuters / AP / Axios / BBC / Al Jazeera / Bloomberg / FT |
| SEC EDGAR | `SEC_USER_AGENT` | 2 | 8-K / 10-Q / 10-K |
| EarningsRelease | — | 0 | 从 EarningsCalendar 结构化生成（最高优先级）|
| LLM | `LLM_BASE_URL/KEY/MODEL` | — | OpenAI-compatible，当前默认 `gemini-3-flash` |
| Alpaca | `ALPACA_API_KEY/SECRET` | — | 模拟/实盘交易 + 持仓 + 权益曲线 |

---

## 日志
- `logs/app.log` — 主日志（LLM 调用、Agent 运行、交易记录）
- `logs/health.log` — 健康巡检
- `logs/writeout.log` — pipeline tick 摘要（JSON）
