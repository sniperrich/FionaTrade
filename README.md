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

# 启动（端口 6888）
uvicorn app.main:app --host 0.0.0.0 --port 6888 --reload

# 访问 WebUI
open http://localhost:6888
```

---

## 架构

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

**Agent 权重：** 技术 30% · 新闻 25% · 基本面 25% · 宏观 20%

**风控限制（硬编码）：** 最大仓位 20% · 日亏损上限 3% · 最少 2 个信号共识

### Legacy 模式（`AGENT_MODE_ENABLED=false`）

```
IngestionService → NormalizationService → ValidationService
    → SignalEngineService（AnalysisService + SignalValidator）→ PaperEngineService
```

---

## WebUI 页面

| 路径 | 功能 |
|------|------|
| `/` | 仪表盘：组合状态、系统状态、最新 Agent 决策、新闻 |
| `/live` | 实盘：持仓、手动下单（market/limit/bracket）、挂单管理、**Enable/Disable Live 按钮** |
| `/agents` | AI Agent：LLM 状态、市场时钟、触发运行、推理展开 |
| `/news` | 新闻流：全文展开、来源/ticker 过滤 |
| `/settings` | 配置信息 |
| `/signals` | 遗留信号流 |
| `/events` | 事件流 |
| `/paper` | 模拟盘组合 |
| `/backtests` | 回测历史 |

---

## 目录结构

```
app/
  core/          配置（config.py）、日志、market_hours、SP100 ticker 集合
  db/            SQLAlchemy 模型（models.py）+ db_session() 上下文管理器
  ingestion/     数据源：finnhub_client / rss_client / sec_client / fred_client / earnings_release_client
  normalization/ RawItem → Event（聚类 + ticker 提取 + taxonomy）
  validation/    事件去重 + 冲突检测
  tools/         Agent 专用 DB 只读工具（market_data / fundamentals / news / macro）
  agents/        6 个 Agent 类（继承 BaseAgent）
  agent_graph/   graph.py（AgentGraph）+ state.py（TypedDict 状态）
  broker/        alpaca.py（完整 Alpaca REST v2，777 行）+ paper.py
  services/      live_trading.py（实盘循环）+ orchestrator.py（模式切换）
  analysis/      [已弃用] legacy 分析服务，仅供回测兼容
  signal_engine/ 遗留信号引擎
  paper_engine/  模拟填单 + 持仓跟踪 + NAV
  backtest_engine/ 3 阶段回测：warmup → 并行 LLM → 串行执行
  market/        1m K 线回填（Finnhub → yfinance → stooq）
  monitoring/    HealthAuditService（数据源延迟 + 状态快照）
  api/routes.py  所有 REST API 端点
  webui/routes.py Jinja2 页面路由

templates/       9 个 HTML 模板（Claude 风格 sidebar 布局）
static/ft.css    Claude 风格 CSS 设计系统
scripts/         独立工具脚本（回测、历史数据回填等）
tests/           137 个 pytest 测试
```

---

## 关键 API 端点

| 方法 | 路径 | 返回格式 |
|------|------|---------|
| GET | `/api/health` | `{status, llm_configured, llm_model, sources_online, ...}` |
| GET | `/api/live/status` | **平铺字段**：`{enabled, market_tradeable, market_session(字符串), market_time, tickers, ...}` |
| GET | `/api/agent/runs` | **包装对象**：`{"runs": [...]}` — 每项用 `*_result` 字段名 |
| GET | `/api/news` | **分页对象**：`{"items": [...], "mode", "latest_id", ...}` |
| POST | `/api/agent/run` | 触发 Agent 图：`{"tickers": ["AAPL", "NVDA"]}` |
| POST | `/api/live/set_enabled` | 运行时启用/禁用交易：`{"enabled": true}` |
| POST | `/api/live/cycle` | 手动触发一次交易循环 |
| POST | `/api/live/order` | 手动下单 |
| GET | `/api/live/positions` | Alpaca 当前持仓 |
| GET | `/api/live/open_orders` | Alpaca 挂单 |
| GET | `/api/live/portfolio_history` | Alpaca 权益曲线 |

> ⚠️ **JS 开发注意：**
> - `market_session` 是字符串，不是对象
> - `/api/agent/runs` 返回 `{runs:[...]}` 包装，需解包
> - AgentRun 字段名：`*_result`（不是 `*_output`）
> - Conviction：`r.portfolio_result.metadata.conviction`
> - Risk approved：`r.risk_result.metadata.approved`
> - 执行时间：`r.execution_time_ms`

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

# 永久 —— 在 .env 设置：
LIVE_TRADING_ENABLED=true
LIVE_TRADING_TICKERS=AAPL,NVDA,MSFT,GOOGL,AMZN
```
或直接点击 `/live` 页面右上角的 **▶ Enable Live** 按钮。

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
# 本机推送代码
rsync -avz --exclude='.git' --exclude='*.db' --exclude='logs/' \
  /path/to/FionaTrade/ root@SERVER_IP:/opt/fionatrade/

# 服务器一键部署
cd /opt/fionatrade
bash deploy.sh
nano .env          # 填入 API keys
systemctl restart fionatrade

# 验证
curl http://localhost:6888/api/health
```

### 必填 API Keys

| 变量 | 说明 |
|------|------|
| `FINNHUB_API_KEY` | K 线 + 新闻（必须）|
| `LLM_API_KEY` + `LLM_BASE_URL` + `LLM_MODEL` | AI 接口（必须）|
| `ALPACA_API_KEY` + `ALPACA_API_SECRET` | 模拟/实盘（交易必须）|
| `FRED_API_KEY` | 宏观数据（可选，免费申请）|

### 运维命令
```bash
journalctl -u fionatrade -f        # 实时日志
systemctl restart fionatrade       # 重启
curl http://localhost:6888/api/health  # 健康检查
```

---

## 测试

```bash
pytest tests/                      # 137 个测试，~2.4s
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
