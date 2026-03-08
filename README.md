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
- **主分析**：`claude-sonnet-4-6`（读全文 + EPS context + 技术信号 + 支撑阻力，输出 UP/DOWN/NEUTRAL + 持仓周期）
- **回测并发**：`ThreadPoolExecutor(workers=8)` 并发预取所有 LLM 信号，再串行执行交易逻辑

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

## 近期变更

### 2026-03-08
- **Finnhub company-news 接入**：新增 `fetch_company_news()` 对 47 个 SP100 ticker 拉 1 年历史新闻；source_tier=1（等同于 Bloomberg/CNBC）
- **历史新闻回填脚本**：`scripts/backfill_finnhub_news.py`，支持 `--from/--to/--tickers/--dry-run`
- **1分钟 K 线 Finnhub 优先**：`app/market/backfill.py` 改为 `finnhub_1m` 作为首选，403 错误改为 per-ticker 粒度，不影响其他 ticker
- **gemini-3-flash 事件分类器**：对 RSS/SEC 条目的 unknown 事件用小模型分类；Finnhub ticker-tagged 条目跳过 LLM，直接用关键词匹配
- **公司名识别**：新增 `app/core/company_names.py`（~100 个 SP100 映射），正则 word-boundary 匹配，修复 GE 等缩写误识别
- **验证规则放宽**：单条 tier-1 来源（Bloomberg/CNBC/Finnhub company-news）即可通过 VALID
- **EPS/技术信号/支撑阻力注入 prompt**：`app/analysis/service.py` 新增 `_finnhub_earnings_context()`、`_finnhub_tech_signal()`、`_finnhub_support_resistance()`，有 1 小时 LRU 缓存，LLM rules 新增对应使用指导
- **回测并发 LLM**：`app/backtest_engine/service.py` 改为三阶段：①串行预热 Finnhub cache（避免并发 429），②`ThreadPoolExecutor` 并发预取所有 LLM 信号，③串行执行交易；支持 `llm_workers` 参数（默认 8）
