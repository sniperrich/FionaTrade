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

## 说明

- 默认仅 paper，不接真实券商 API。
- 默认 SQLite，本地文件 `fionatrade.db`。
- 1 分钟轮询由应用内调度器执行，可在配置中关闭。
- 日志默认写入 `logs/`：
  - `app.log`：控制台同源应用日志
  - `writeout.log`：结构化流水日志（pipeline/health）
  - `health.log`：自动健康巡检日志

## 中文提示

- 默认已预置 `LLM_BASE_URL=https://api.duojie.games`、`LLM_MODEL=claude-sonnet-4-6`；如网关要求鉴权请补 `LLM_API_KEY`。
- 未配置 `LLM_BASE_URL` 或 `LLM_MODEL` 时，系统自动使用规则回退分析（不会中断主链路）。
- 已写入新闻/消息接入：`SEC`、`RSS`，以及可选 `Finnhub`（需 `FINNHUB_API_KEY`）。
- SEC 抓取已增加重试和 404 原子订阅回退（ATOM feed）；`SEC_USER_AGENT` 请填写真实邮箱。
- 新闻页提供来源在线状态（ONLINE/OFFLINE）与离线报错明细。
- 新闻页支持“加载更早历史”、来源过滤、关键词过滤（基于本地已采集数据）。
- 回测前可先调用 `POST /api/market/backfill` 回填历史 1m 行情。
- 回测新增硬风控：分钟级止损/止盈（`hard_stops`）、动态风险仓位（`risk_sizing` + `risk_per_trade_pct`）、日内熔断（`daily_circuit_breaker`）。
- 回测支持 `slippage_bps` 参数，可先用 `0` 做无摩擦诊断；默认滑点已调为 `4 bps`。
- `MIN_TRADE_CONFIDENCE` 默认调整为 `70`（避免实时链路在 `75` 下几乎全部被过滤）。
- 已写入短中长线管理（`SHORT/MID/LONG` 周期桶），默认开启：`ENABLE_TERM_MANAGEMENT=true`。
- 回测默认不启用周期桶：`BACKTEST_ENABLE_TERM_HORIZON=false`（需要时可在回测参数 `enable_term_horizon=true` 打开）。
- 支持一周网格回测（不跑月度）+ 最小交易数过滤，命令：`python scripts/run_weekly_backtest_grid.py --min-trades 5`。
- 支持 1m 行情覆盖审计：`python scripts/audit_bar_coverage.py --start-date 2026-01-02 --end-date 2026-01-10`。
- 支持 LLM 一致性测试（同窗重复 3 次）：`python scripts/run_llm_consistency_check.py --runs 3`。
