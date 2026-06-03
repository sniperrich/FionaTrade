# Finnhub API 完整整理文档（中文）

- 来源：`https://finnhub.io/docs/api`
- 抽取日期：`2026-03-08`
- OpenAPI 版本：`2.0`
- 基础地址：`https://finnhub.io/api/v1`
- 接口总数：`110`（按 method+path 计）

## 1. 认证与通用规则

- 认证方式：Query 参数 `token=<YOUR_API_KEY>`。
- 文档中的绝大多数接口默认 `GET`，少数 AI/聊天接口使用 `POST`。
- 所有时间字段请优先按接口定义传 Unix 时间戳或 `YYYY-MM-DD`。
- Premium 相关文案以接口项内的 `premium` 字段为准。

## 2. 分类总览

- `Alternative Data`: 19 个接口
- `Bonds`: 4 个接口
- `Crypto`: 4 个接口
- `ETFs & Indices`: 7 个接口
- `Economic`: 4 个接口
- `Enterprise data`: 3 个接口
- `Forex`: 4 个接口
- `Global Filings Search`: 5 个接口
- `Mutual Funds`: 6 个接口
- `Stock Estimates`: 9 个接口
- `Stock Fundamentals`: 34 个接口
- `Stock Price`: 7 个接口
- `Technical Analysis`: 4 个接口

## 3. 全量接口清单（按分类）

### 3.1 Alternative Data

#### 1. `GET /airline/price-index`

- 标题：Airline Ticket Price Index API
- 摘要：Airline Price Index
- OperationId：`airline-price-index`
- 权限提示：Accessible with Fundamental data or All in One subscription.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `airline` | `query` | `string` | `true` | Filter data by airline. Accepted values: united , delta , american_airlines , southwest , southern_airways_express , ... |
| `from` | `query` | `string` | `true` | From date YYYY-MM-DD . |
| `to` | `query` | `string` | `true` | To date YYYY-MM-DD . |
- 示例请求：
  - `/airline/price-index?airline=united&from=2024-06-01&to=2024-07-15`
- 200 响应结构：`#/definitions/AirlinePriceIndexData`

#### 2. `GET /bank-branch`

- 标题：Bank Branch API
- 摘要：Bank Branch List
- OperationId：`bank-branch`
- 权限提示：Accessible with Fundamental or All in One subscription.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
- 示例请求：
  - `/bank-branch?symbol=JPM`
- 200 响应结构：`#/definitions/BankBranchRes`

#### 3. `GET /covid19/us`

- 标题：Real-time COVID-19 data API
- 摘要：COVID-19
- OperationId：`covid-19`
- High Usage：`High Usage`
- 参数：无（或仅 token）
- 示例请求：
  - `/covid19/us`
- 200 响应结构：`array<#/definitions/CovidInfo>`

#### 4. `GET /fda-advisory-committee-calendar`

- 标题：FDA Calendar | Finnhub
- 摘要：FDA Committee Meeting Calendar
- OperationId：`fda-committee-meeting-calendar`
- 参数：无（或仅 token）
- 示例请求：
  - `/fda-advisory-committee-calendar`
- 200 响应结构：`array<#/definitions/FDAComitteeMeeting>`

#### 5. `GET /stock/congressional-trading`

- 标题：Congressional Stock Trades API.
- 摘要：Congressional Trading
- OperationId：`congressional-trading`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
| `from` | `query` | `string` | `true` | From date YYYY-MM-DD . |
| `to` | `query` | `string` | `true` | To date YYYY-MM-DD . |
- 示例请求：
  - `/stock/congressional-trading?symbol=AAPL`
- 200 响应结构：`#/definitions/CongressionalTrading`

#### 6. `GET /stock/earnings-call-live`

- 标题：Earnings Call Live API | Stream Live Earnings Calls API
- 摘要：Earnings Call Audio Live
- OperationId：`earnings-call-live`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `from` | `query` | `string` | `false` | From date YYYY-MM-DD . |
| `to` | `query` | `string` | `false` | To date YYYY-MM-DD . |
| `symbol` | `query` | `string` | `false` | Filter by symbol: AAPL. |
- 示例请求：
  - `/stock/earnings-call-live?from=2024-11-01&to=2024-11-07`
- 200 响应结构：`#/definitions/EarningsCallLive`

#### 7. `GET /stock/earnings-quality-score`

- 标题：Company Earnings Quality Score API
- 摘要：Company Earnings Quality Score
- OperationId：`company-earnings-quality-score`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
| `freq` | `query` | `string` | `true` | Frequency. Currently support annual and quarterly |
- 示例请求：
  - `/stock/earnings-quality-score?symbol=AAPL&freq=quarterly`
  - `/stock/earnings-quality-score?symbol=WMT&freq=quarterly`
- 200 响应结构：`#/definitions/CompanyEarningsQualityScore`

#### 8. `GET /stock/esg`

- 标题：Company ESG Scores API
- 摘要：Company ESG Scores
- OperationId：`company-esg-score`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
- 示例请求：
  - `/stock/esg?symbol=AAPL`
  - `/stock/esg?symbol=WMT`
- 200 响应结构：`#/definitions/CompanyESG`

#### 9. `GET /stock/historical-esg`

- 标题：Company Historical ESG Scores API
- 摘要：Historical ESG Scores
- OperationId：`company-historical-esg-score`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
- 示例请求：
  - `/stock/historical-esg?symbol=AAPL`
  - `/stock/historical-esg?symbol=WMT`
- 200 响应结构：`#/definitions/HistoricalCompanyESG`

#### 10. `GET /stock/investment-theme`

- 标题：Investment Themes (Thematic Investing)
- 摘要：Investment Themes (Thematic Investing)
- OperationId：`investment-themes`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `theme` | `query` | `string` | `true` | Investment theme. A full list of themes supported can be found here . |
- 示例请求：
  - `/stock/investment-theme?theme=financialExchangesData`
  - `/stock/investment-theme?theme=futureFood`
- 200 响应结构：`#/definitions/InvestmentThemes`

#### 11. `GET /stock/lobbying`

- 标题：Senate and House lobbying data from public companies
- 摘要：Senate Lobbying
- OperationId：`stock-lobbying`
- 新接口标记：`New`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
| `from` | `query` | `string` | `true` | From date YYYY-MM-DD . |
| `to` | `query` | `string` | `true` | To date YYYY-MM-DD . |
- 示例请求：
  - `/stock/lobbying?symbol=AAPL&from=2021-01-01&to=2022-12-31`
- 200 响应结构：`#/definitions/LobbyingResult`

#### 12. `GET /stock/presentation`

- 标题：Company Presentation
- 摘要：Company Presentation
- OperationId：`stock-presentation`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Company symbol. |
- 示例请求：
  - `/stock/presentation?symbol=IBM`
- 200 响应结构：`#/definitions/StockPresentation`

#### 13. `GET /stock/social-sentiment`

- 标题：Stocks Social Media Sentiment
- 摘要：Social Sentiment
- OperationId：`social-sentiment`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Company symbol. |
| `from` | `query` | `string` | `false` | From date YYYY-MM-DD . |
| `to` | `query` | `string` | `false` | To date YYYY-MM-DD . |
- 示例请求：
  - `/stock/social-sentiment?symbol=GME`
- 200 响应结构：`#/definitions/SocialSentiment`

#### 14. `GET /stock/supply-chain`

- 标题：Supply Chain Relationships
- 摘要：Supply Chain Relationships
- OperationId：`supply-chain-relationships`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
- 示例请求：
  - `/stock/supply-chain?symbol=AAPL`
  - `/stock/supply-chain?symbol=WMT`
- 200 响应结构：`#/definitions/SupplyChainRelationships`

#### 15. `GET /stock/transcripts`

- 标题：Earnings Call Transcripts API | Finnhub Stock API
- 摘要：Earnings Call Transcripts
- OperationId：`transcripts`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `id` | `query` | `string` | `true` | Transcript's id obtained with Transcripts List endpoint . |
- 示例请求：
  - `/stock/transcripts?id=AAPL_162777`
- 200 响应结构：`#/definitions/EarningsCallTranscripts`

#### 16. `GET /stock/transcripts/list`

- 标题：List Earnings Call Transcripts
- 摘要：Earnings Call Transcripts List
- OperationId：`transcripts-list`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Company symbol: AAPL. Leave empty to list the latest transcripts |
- 示例请求：
  - `/stock/transcripts/list?symbol=AAPL`
  - `/stock/transcripts/list?symbol=AC.TO`
  - `/stock/transcripts/list?symbol=BARC.L`
- 200 响应结构：`#/definitions/EarningsCallTranscriptsList`

#### 17. `GET /stock/usa-spending`

- 标题：USA Spending | Government contracts API
- 摘要：USA Spending
- OperationId：`stock-usa-spending`
- 新接口标记：`New`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
| `from` | `query` | `string` | `true` | From date YYYY-MM-DD . Filter for actionDate |
| `to` | `query` | `string` | `true` | To date YYYY-MM-DD . Filter for actionDate |
- 示例请求：
  - `/stock/usa-spending?symbol=LMT&from=2021-01-01&to=2022-12-31`
  - `/stock/usa-spending?symbol=BA&from=2021-01-01&to=2022-12-31`
- 200 响应结构：`#/definitions/UsaSpendingResult`

#### 18. `GET /stock/uspto-patent`

- 标题：USPTO Patents
- 摘要：USPTO Patents
- OperationId：`stock-uspto-patent`
- 新接口标记：`New`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
| `from` | `query` | `string` | `true` | From date YYYY-MM-DD . |
| `to` | `query` | `string` | `true` | To date YYYY-MM-DD . |
- 示例请求：
  - `/stock/uspto-patent?symbol=NVDA&from=2021-01-01&to=2021-12-31`
- 200 响应结构：`#/definitions/UsptoPatentResult`

#### 19. `GET /stock/visa-application`

- 标题：H1-B Visa Application API for public companies
- 摘要：H1-B Visa Application
- OperationId：`stock-visa-application`
- 新接口标记：`New`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
| `from` | `query` | `string` | `true` | From date YYYY-MM-DD . Filter on the beginDate column. |
| `to` | `query` | `string` | `true` | To date YYYY-MM-DD . Filter on the beginDate column. |
- 示例请求：
  - `/stock/visa-application?symbol=AAPL&from=2021-01-01&to=2021-12-31`
- 200 响应结构：`#/definitions/VisaApplicationResult`

### 3.2 Bonds

#### 1. `GET /bond/price`

- 标题：Bond price API | Corporate bonds API
- 摘要：Bond price data
- OperationId：`bond-price`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `isin` | `query` | `string` | `true` | ISIN. |
| `from` | `query` | `integer` | `true` | UNIX timestamp. Interval initial value. |
| `to` | `query` | `integer` | `true` | UNIX timestamp. Interval end value. |
- 示例请求：
  - `/bond/price?isin=US912810TD00&from=1590988249&to=1649099548`
- 200 响应结构：`#/definitions/BondCandles`

#### 2. `GET /bond/profile`

- 标题：Bond Profile & Fundamental Data
- 摘要：Bond Profile
- OperationId：`bond-profile`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `isin` | `query` | `string` | `false` | ISIN |
| `cusip` | `query` | `string` | `false` | CUSIP |
| `figi` | `query` | `string` | `false` | FIGI |
- 示例请求：
  - `/bond/profile?figi=BBG0152KFHS6`
  - `/bond/profile?isin=US912810TD00`
  - `/bond/profile?cusip=912810TD0`
- 200 响应结构：`#/definitions/BondProfile`

#### 3. `GET /bond/tick`

- 标题：Bond Tick data | FINRA TRACE API | Corporate bonds API
- 摘要：Bond Tick Data
- OperationId：`bond-tick`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `isin` | `query` | `string` | `true` | ISIN. |
| `date` | `query` | `string` | `true` | Date: 2020-04-02. |
| `limit` | `query` | `integer` | `true` | Limit number of ticks returned. Maximum value: 25000 |
| `skip` | `query` | `integer` | `true` | Number of ticks to skip. Use this parameter to loop through the entire data. |
| `exchange` | `query` | `string` | `true` | Currently support the following values: trace . |
- 示例请求：
  - `/bond/tick?isin=US693475BF18&date=2022-08-19&limit=50&skip=6&format=json&exchange=trace`
- 200 响应结构：`#/definitions/BondTickData`

#### 4. `GET /bond/yield-curve`

- 标题：Treasury Bonds Yield Curve
- 摘要：Bond Yield Curve
- OperationId：`bond-yield-curve`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `code` | `query` | `string` | `true` | Bond's code. You can find the list of supported code here . |
- 示例请求：
  - `/bond/yield-curve?code=10y`
- 200 响应结构：`#/definitions/BondYieldCurve`

### 3.3 Crypto

#### 1. `GET /crypto/candle`

- 标题：Crypto OHLCV API
- 摘要：Crypto Candles
- OperationId：`crypto-candles`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Use symbol returned in /crypto/symbol endpoint for this field. |
| `resolution` | `query` | `string` | `true` | Supported resolution includes 1, 5, 15, 30, 60, D, W, M .Some timeframes might not be available depending on the exch... |
| `from` | `query` | `integer` | `true` | UNIX timestamp. Interval initial value. |
| `to` | `query` | `integer` | `true` | UNIX timestamp. Interval end value. |
- 示例请求：
  - `/crypto/candle?symbol=BINANCE:BTCUSDT&resolution=D&from=1572651390&to=1575243390`
- 200 响应结构：`#/definitions/CryptoCandles`

#### 2. `GET /crypto/exchange`

- 标题：Crypto Exchanges
- 摘要：Crypto Exchanges
- OperationId：`crypto-exchanges`
- 参数：无（或仅 token）
- 示例请求：
  - `/crypto/exchange`
- 200 响应结构：`array`

#### 3. `GET /crypto/profile`

- 标题：Crypto Profile API
- 摘要：Crypto Profile
- OperationId：`crypto-profile`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Crypto symbol such as BTC or ETH. |
- 示例请求：
  - `/crypto/profile?symbol=BTC`
  - `/crypto/profile?symbol=ETH`
- 200 响应结构：`#/definitions/CryptoProfile`

#### 4. `GET /crypto/symbol`

- 标题：Crypto Symbols By Exchange
- 摘要：Crypto Symbol
- OperationId：`crypto-symbols`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `exchange` | `query` | `string` | `true` | Exchange you want to get the list of symbols from. |
- 示例请求：
  - `/crypto/symbol?exchange=binance`
- 200 响应结构：`array<#/definitions/CryptoSymbol>`

### 3.4 ETFs & Indices

#### 1. `GET /etf/allocation`

- 标题：Global ETFs Allocation
- 摘要：ETFs Equity Allocation
- OperationId：`etfs-allocation`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | ETF symbol. |
| `isin` | `query` | `string` | `false` | ETF isin. |
- 示例请求：
  - `/etf/allocation?symbol=SPY`
  - `/etf/allocation?symbol=VOO`
- 200 响应结构：`#/definitions/ETFsAllocation`

#### 2. `GET /etf/country`

- 标题：Global ETFs Country Exposure Breakdown
- 摘要：ETFs Country Exposure
- OperationId：`etfs-country-exposure`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | ETF symbol. |
| `isin` | `query` | `string` | `false` | ETF isin. |
- 示例请求：
  - `/etf/country?symbol=SPY`
  - `/etf/country?symbol=VOO`
- 200 响应结构：`#/definitions/ETFsCountryExposure`

#### 3. `GET /etf/holdings`

- 标题：Global ETFs Holdings & Constituents API
- 摘要：ETFs Holdings
- OperationId：`etfs-holdings`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | ETF symbol. |
| `isin` | `query` | `string` | `false` | ETF isin. |
| `skip` | `query` | `integer` | `false` | Skip the first n results. You can use this parameter to query historical constituents data. The latest result is retu... |
| `date` | `query` | `string` | `false` | Query holdings by date. You can use either this param or skip param, not both. |
- 示例请求：
  - `/etf/holdings?symbol=SPY`
  - `/etf/holdings?symbol=AVUV&skip=1`
  - `/etf/holdings?symbol=IPO&date=2025-02-04`
- 200 响应结构：`#/definitions/ETFsHoldings`

#### 4. `GET /etf/profile`

- 标题：Global ETFs profile
- 摘要：ETFs Profile
- OperationId：`etfs-profile`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | ETF symbol. |
| `isin` | `query` | `string` | `false` | ETF isin. |
- 示例请求：
  - `/etf/profile?symbol=SPY`
  - `/etf/profile?isin=US78462F1030`
- 200 响应结构：`#/definitions/ETFsProfile`

#### 5. `GET /etf/sector`

- 标题：Global ETFs Sector Breakdown
- 摘要：ETFs Sector Exposure
- OperationId：`etfs-sector-exposure`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | ETF symbol. |
| `isin` | `query` | `string` | `false` | ETF isin. |
- 示例请求：
  - `/etf/sector?symbol=SPY`
  - `/etf/sector?symbol=VOO`
- 200 响应结构：`#/definitions/ETFsSectorExposure`

#### 6. `GET /index/constituents`

- 标题：Indices Constituents API
- 摘要：Indices Constituents
- OperationId：`indices-constituents`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | symbol |
- 示例请求：
  - `/index/constituents?symbol=^GSPC`
- 200 响应结构：`#/definitions/IndicesConstituents`

#### 7. `GET /index/historical-constituents`

- 标题：Indices Historical Constituents API | Join & Leave History
- 摘要：Indices Historical Constituents
- OperationId：`indices-historical-constituents`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | symbol |
- 示例请求：
  - `/index/historical-constituents?symbol=^GSPC`
- 200 响应结构：`#/definitions/IndicesHistoricalConstituents`

### 3.5 Economic

#### 1. `GET /calendar/economic`

- 标题：Economic Calendar API | Finnhub
- 摘要：Economic Calendar
- OperationId：`economic-calendar`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `from` | `query` | `string` | `false` | From date YYYY-MM-DD . |
| `to` | `query` | `string` | `false` | To date YYYY-MM-DD . |
- 示例请求：
  - `/calendar/economic`
- 200 响应结构：`#/definitions/EconomicCalendar`

#### 2. `GET /country`

- 标题：Country List
- 摘要：Country Metadata
- OperationId：`country`
- 参数：无（或仅 token）
- 示例请求：
  - `/country`
- 200 响应结构：`array<#/definitions/CountryMetadata>`

#### 3. `GET /economic`

- 标题：Global Economic Data API
- 摘要：Economic Data
- OperationId：`economic-data`
- 权限提示：Accessible with Fundamental data or All in One subscription.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `code` | `query` | `string` | `true` | Economic code. |
- 示例请求：
  - `/economic?code=MA-USA-656880`
- 200 响应结构：`#/definitions/EconomicData`

#### 4. `GET /economic/code`

- 标题：Economic Codes
- 摘要：Economic Code
- OperationId：`economic-code`
- 权限提示：Accessible with Fundamental data or All in One subscription.
- 参数：无（或仅 token）
- 示例请求：
  - `/economic/code`
- 200 响应结构：`array<#/definitions/EconomicCode>`

### 3.6 Enterprise data

#### 1. `POST /ai-chat`

- 标题：AI Copilot
- 摘要：AI Copilot
- OperationId：`ai-chat`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `search` | `body` | `` | `false` | Search body |
- 示例请求：
  - `/ai-chat`
- 200 响应结构：`#/definitions/AIChatResponse`

#### 2. `GET /stock/newsroom`

- 标题：Newsroom API
- 摘要：Newsroom
- OperationId：`newsroom`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Company symbol. |
| `from` | `query` | `string` | `false` | From time: 2025-01-01. |
| `to` | `query` | `string` | `false` | To time: 2026-01-05. |
- 示例请求：
  - `/stock/newsroom?symbol=AAPL`
  - `/stock/newsroom?symbol=NVDA&from=2025-01-01&to=2025-12-15`
- 200 响应结构：`#/definitions/Newsroom`

#### 3. `GET /stock/revenue-breakdown2`

- 标题：Revenue Breakdown | Revenue Segment & KPIs
- 摘要：Revenue Breakdown & KPI
- OperationId：`revenue-breakdown2`
- 权限提示：Premium
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
- 示例请求：
  - `/stock/revenue-breakdown2?symbol=AAPL`
- 200 响应结构：`#/definitions/RevenueBreakdown2`

### 3.7 Forex

#### 1. `GET /forex/candle`

- 标题：Forex OHLCV API
- 摘要：Forex Candles
- OperationId：`forex-candles`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Use symbol returned in /forex/symbol endpoint for this field. |
| `resolution` | `query` | `string` | `true` | Supported resolution includes 1, 5, 15, 30, 60, D, W, M .Some timeframes might not be available depending on the exch... |
| `from` | `query` | `integer` | `true` | UNIX timestamp. Interval initial value. |
| `to` | `query` | `integer` | `true` | UNIX timestamp. Interval end value. |
- 示例请求：
  - `/forex/candle?symbol=OANDA:EUR_USD&resolution=D&from=1572651390&to=1575243390`
- 200 响应结构：`#/definitions/ForexCandles`

#### 2. `GET /forex/exchange`

- 标题：List Forex Exchanges
- 摘要：Forex Exchanges
- OperationId：`forex-exchanges`
- 参数：无（或仅 token）
- 示例请求：
  - `/forex/exchange`
- 200 响应结构：`array`

#### 3. `GET /forex/rates`

- 标题：Forex All Rates
- 摘要：Forex rates
- OperationId：`forex-rates`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `base` | `query` | `string` | `false` | Base currency. Default to EUR. |
| `date` | `query` | `string` | `false` | Date. Leave blank to get the latest data. |
- 示例请求：
  - `/forex/rates?base=USD`
  - `/forex/rates?base=EUR&date=2022-02-10`
- 200 响应结构：`#/definitions/Forexrates`

#### 4. `GET /forex/symbol`

- 标题：Forex Symbols By Exchange
- 摘要：Forex Symbol
- OperationId：`forex-symbols`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `exchange` | `query` | `string` | `true` | Exchange you want to get the list of symbols from. |
- 示例请求：
  - `/forex/symbol?exchange=oanda`
- 200 响应结构：`array<#/definitions/ForexSymbol>`

### 3.8 Global Filings Search

#### 1. `GET /global-filings/download`

- 标题：Download Global Filings
- 摘要：Download Filings
- OperationId：`global-filings-download`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `documentId` | `query` | `string` | `true` | Document's id. Note that this is different from filingId as 1 filing can contain multiple documents. |
- 示例请求：
  - `/global-filings/download?documentId=76a87562f08df43a653d00bed73f467be4a6b045ffe72abd20ddc66f2c471f9c`
  - `/global-filings/download?documentId=3c020f32c165752dfcbc80bff8bb9aac6ac8bac31c400882bfee4b8ddc135165`

#### 2. `GET /global-filings/filter`

- 标题：Search Filter
- 摘要：Search Filter
- OperationId：`global-filings-search-filter`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `field` | `query` | `string` | `true` | Field to get available filters. Available filters are "countries", "exchanges", "exhibits", "forms", "gics", "naics",... |
| `source` | `query` | `string` | `false` | Get available forms for each source. |
- 示例请求：
  - `/global-filings/filter?field=forms&source=SEC`
- 200 响应结构：`#/definitions/SearchFilter`

#### 3. `POST /global-filings/search`

- 标题：Global Filings Search API | Earnings call transcripts
- 摘要：Global Filings Search
- OperationId：`global-filings-search`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `search` | `body` | `` | `false` | Search body |
- 示例请求：
  - `/global-filings/search`
- 200 响应结构：`#/definitions/SearchResponse`

#### 4. `POST /global-filings/search-in-filing`

- 标题：Search In Filing
- 摘要：Search In Filing
- OperationId：`search-in-filing`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `search` | `body` | `` | `false` | Search body |
- 示例请求：
  - `/global-filings/search-in-filing`
- 200 响应结构：`#/definitions/InFilingResponse`

#### 5. `GET /stock/international-filings`

- 标题：Global Company Filings
- 摘要：International Filings
- OperationId：`international-filings`
- 权限提示：Access approved on a case by case basis
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | Symbol. Leave empty to list latest filings. |
| `country` | `query` | `string` | `false` | Filter by country using country's 2-letter code. |
| `from` | `query` | `string` | `false` | From date: 2023-01-15. |
| `to` | `query` | `string` | `false` | To date: 2023-12-16. |
- 示例请求：
  - `/stock/international-filings?symbol=RY.TO`
  - `/stock/international-filings?country=CA`
- 200 响应结构：`array<#/definitions/InternationalFiling>`

### 3.9 Mutual Funds

#### 1. `GET /mutual-fund/country`

- 标题：Mutual Funds Country Exposure Breakdown
- 摘要：Mutual Funds Country Exposure
- OperationId：`mutual-fund-country-exposure`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | Symbol. |
| `isin` | `query` | `string` | `false` | Fund's isin. |
- 示例请求：
  - `/mutual-fund/country?symbol=FNILX`
  - `/mutual-fund/country?symbol=VFIAX`
- 200 响应结构：`#/definitions/MutualFundCountryExposure`

#### 2. `GET /mutual-fund/eet`

- 标题：Mutual Funds EET Data API
- 摘要：Mutual Funds EET
- OperationId：`mutual-fund-eet`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `isin` | `query` | `string` | `true` | ISIN. |
- 示例请求：
  - `/mutual-fund/eet?isin=LU2036931686`
- 200 响应结构：`#/definitions/MutualFundEet`

#### 3. `GET /mutual-fund/eet-pai`

- 标题：Mutual Funds EET PAI Data API
- 摘要：Mutual Funds EET PAI
- OperationId：`mutual-fund-eet-pai`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `isin` | `query` | `string` | `true` | ISIN. |
- 示例请求：
  - `/mutual-fund/eet-pai?isin=LU2036931686`
- 200 响应结构：`#/definitions/MutualFundEetPai`

#### 4. `GET /mutual-fund/holdings`

- 标题：Global Mutual Funds Holdings & Constituents API
- 摘要：Mutual Funds Holdings
- OperationId：`mutual-fund-holdings`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | Fund's symbol. |
| `isin` | `query` | `string` | `false` | Fund's isin. |
| `skip` | `query` | `integer` | `false` | Skip the first n results. You can use this parameter to query historical constituents data. The latest result is retu... |
- 示例请求：
  - `/mutual-fund/holdings?symbol=VTSAX`
  - `/mutual-fund/holdings?isin=US9229087286&skip=1`
  - `/mutual-fund/holdings?isin=LU0003562807`
- 200 响应结构：`#/definitions/MutualFundHoldings`

#### 5. `GET /mutual-fund/profile`

- 标题：Mutual Funds profile
- 摘要：Mutual Funds Profile
- OperationId：`mutual-fund-profile`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | Fund's symbol. |
| `isin` | `query` | `string` | `false` | Fund's isin. |
- 示例请求：
  - `/mutual-fund/profile?symbol=VTSAX`
  - `/mutual-fund/profile?isin=US9229087286`
  - `/mutual-fund/profile?isin=LU1748855837`
- 200 响应结构：`#/definitions/MutualFundProfile`

#### 6. `GET /mutual-fund/sector`

- 标题：Mutual Funds Sector Breakdown
- 摘要：Mutual Funds Sector Exposure
- OperationId：`mutual-fund-sector-exposure`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | Mutual Fund symbol. |
| `isin` | `query` | `string` | `false` | Fund's isin. |
- 示例请求：
  - `/mutual-fund/sector?symbol=VTSAX`
  - `/mutual-fund/sector?symbol=FNILX`
- 200 响应结构：`#/definitions/MutualFundSectorExposure`

### 3.10 Stock Estimates

#### 1. `GET /calendar/earnings`

- 标题：Earnings Calendar API | Finnhub Stock API
- 摘要：Earnings Calendar
- OperationId：`earnings-calendar`
- Free Tier：`1 month of historical earnings and new updates`
- 新接口标记：`New Endpoint`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `from` | `query` | `string` | `false` | From date: 2020-03-15. |
| `to` | `query` | `string` | `false` | To date: 2020-03-16. |
| `symbol` | `query` | `string` | `false` | Filter by symbol: AAPL. |
| `international` | `query` | `boolean` | `false` | Set to true to include international markets. Default value is false |
- 示例请求：
  - `/calendar/earnings?from=2025-08-01&to=2025-08-10`
  - `/calendar/earnings?from=2024-03-01&to=2025-08-09&symbol=AAPL`
- 200 响应结构：`#/definitions/EarningsCalendar`

#### 2. `GET /stock/earnings`

- 标题：Global Company EPS Surprises
- 摘要：Earnings Surprises
- OperationId：`company-earnings`
- Free Tier：`Last 4 quarters`
- High Usage：`High Usage`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
| `limit` | `query` | `integer` | `false` | Limit number of period returned. Leave blank to get the full history. |
- 示例请求：
  - `/stock/earnings?symbol=AAPL`
  - `/stock/earnings?symbol=TSLA`
- 200 响应结构：`array<#/definitions/EarningResult>`

#### 3. `GET /stock/ebit-estimate`

- 标题：Global Company Ebit Estimates
- 摘要：EBIT Estimates
- OperationId：`company-ebit-estimates`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
| `freq` | `query` | `string` | `false` | Can take 1 of the following values: annual, quarterly . Default to quarterly |
- 示例请求：
  - `/stock/ebit-estimate?symbol=AAPL`
  - `/stock/ebit-estimate?symbol=TSLA&freq=annual`
- 200 响应结构：`#/definitions/EbitEstimates`

#### 4. `GET /stock/ebitda-estimate`

- 标题：Global Company Ebitda Estimates
- 摘要：EBITDA Estimates
- OperationId：`company-ebitda-estimates`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
| `freq` | `query` | `string` | `false` | Can take 1 of the following values: annual, quarterly . Default to quarterly |
- 示例请求：
  - `/stock/ebitda-estimate?symbol=AAPL`
  - `/stock/ebitda-estimate?symbol=TSLA&freq=annual`
- 200 响应结构：`#/definitions/EbitdaEstimates`

#### 5. `GET /stock/eps-estimate`

- 标题：Global Company EPS Estimates
- 摘要：Earnings Estimates
- OperationId：`company-eps-estimates`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
| `freq` | `query` | `string` | `false` | Can take 1 of the following values: annual, quarterly . Default to quarterly |
- 示例请求：
  - `/stock/eps-estimate?symbol=AAPL`
  - `/stock/eps-estimate?symbol=AMZN&freq=annual`
- 200 响应结构：`#/definitions/EarningsEstimates`

#### 6. `GET /stock/price-target`

- 标题：Stocks Price Target
- 摘要：Price Target
- OperationId：`price-target`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
- 示例请求：
  - `/stock/price-target?symbol=NFLX`
  - `/stock/price-target?symbol=DIS`
- 200 响应结构：`#/definitions/PriceTarget`

#### 7. `GET /stock/recommendation`

- 标题：Analysts Recommendation Trends
- 摘要：Recommendation Trends
- OperationId：`recommendation-trends`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
- 示例请求：
  - `/stock/recommendation?symbol=AAPL`
  - `/stock/recommendation?symbol=TSLA`
- 200 响应结构：`array<#/definitions/RecommendationTrend>`

#### 8. `GET /stock/revenue-estimate`

- 标题：Global Company Revenue Estimates
- 摘要：Revenue Estimates
- OperationId：`company-revenue-estimates`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
| `freq` | `query` | `string` | `false` | Can take 1 of the following values: annual, quarterly . Default to quarterly |
- 示例请求：
  - `/stock/revenue-estimate?symbol=AAPL`
  - `/stock/revenue-estimate?symbol=TSLA&freq=annual`
- 200 响应结构：`#/definitions/RevenueEstimates`

#### 9. `GET /stock/upgrade-downgrade`

- 标题：Real-time Stocks Upgrade/Downgrade
- 摘要：Stock Upgrade/Downgrade
- OperationId：`upgrade-downgrade`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | Symbol of the company: AAPL. If left blank, the API will return latest stock upgrades/downgrades. |
| `from` | `query` | `string` | `false` | From date: 2000-03-15. |
| `to` | `query` | `string` | `false` | To date: 2020-03-16. |
- 示例请求：
  - `/stock/upgrade-downgrade?symbol=AAPL`
  - `/stock/upgrade-downgrade?symbol=BYND`
- 200 响应结构：`array<#/definitions/UpgradeDowngrade>`

### 3.11 Stock Fundamentals

#### 1. `GET /ca/isin-change`

- 标题：ISIN Change API.
- 摘要：ISIN Change
- OperationId：`isin-change`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `from` | `query` | `string` | `true` | From date YYYY-MM-DD . |
| `to` | `query` | `string` | `true` | To date YYYY-MM-DD . |
- 示例请求：
  - `/ca/isin-change?from=2022-09-01&to=2022-10-30`
- 200 响应结构：`#/definitions/IsinChange`

#### 2. `GET /ca/symbol-change`

- 标题：Symbol Change API | Ticker Change calendar.
- 摘要：Symbol Change
- OperationId：`symbol-change`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `from` | `query` | `string` | `true` | From date YYYY-MM-DD . |
| `to` | `query` | `string` | `true` | To date YYYY-MM-DD . |
- 示例请求：
  - `/ca/symbol-change?from=2022-09-01&to=2022-10-30`
- 200 响应结构：`#/definitions/SymbolChange`

#### 3. `GET /calendar/ipo`

- 标题：IPO Calendar API
- 摘要：IPO Calendar
- OperationId：`ipo-calendar`
- 新接口标记：`New Endpoint`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `from` | `query` | `string` | `true` | From date: 2020-03-15. |
| `to` | `query` | `string` | `true` | To date: 2020-03-16. |
- 示例请求：
  - `/calendar/ipo?from=2020-01-01&to=2020-04-30`
- 200 响应结构：`#/definitions/IPOCalendar`

#### 4. `GET /company-news`

- 标题：Real-time Global Company News API
- 摘要：Company News
- OperationId：`company-news`
- Free Tier：`1 year of historical news and new updates`
- High Usage：`High Usage`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Company symbol. |
| `from` | `query` | `string` | `true` | From date YYYY-MM-DD . |
| `to` | `query` | `string` | `true` | To date YYYY-MM-DD . |
- 示例请求：
  - `/company-news?symbol=AAPL&from=2025-05-15&to=2025-06-20`
- 200 响应结构：`array<#/definitions/CompanyNews>`

#### 5. `GET /institutional/ownership`

- 标题：Institutional Ownership.
- 摘要：Institutional Ownership
- OperationId：`institutional-ownership`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Filter by symbol. |
| `cusip` | `query` | `string` | `true` | Filter by CUSIP. |
| `from` | `query` | `string` | `true` | From date YYYY-MM-DD . |
| `to` | `query` | `string` | `true` | To date YYYY-MM-DD . |
- 示例请求：
  - `/institutional/ownership?symbol=TSLA&from=2022-09-01&to=2022-10-30`
- 200 响应结构：`#/definitions/InstitutionalOwnership`

#### 6. `GET /institutional/portfolio`

- 标题：Institutional Holdings 13-F API.
- 摘要：Institutional Portfolio
- OperationId：`institutional-portfolio`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `cik` | `query` | `string` | `true` | Fund's CIK. |
| `from` | `query` | `string` | `true` | From date YYYY-MM-DD . |
| `to` | `query` | `string` | `true` | To date YYYY-MM-DD . |
- 示例请求：
  - `/institutional/portfolio?cik=1000097&from=2022-05-01&to=2022-09-01`
- 200 响应结构：`#/definitions/InstitutionalPortfolio`

#### 7. `GET /institutional/profile`

- 标题：Institutional Profile API.
- 摘要：Institutional Profile
- OperationId：`institutional-profile`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `cik` | `query` | `string` | `false` | Filter by CIK. Leave blank to get the full list. |
- 示例请求：
  - `/institutional/profile`
- 200 响应结构：`#/definitions/InstitutionalProfile`

#### 8. `GET /news`

- 标题：Real-time Market News API
- 摘要：Market News
- OperationId：`market-news`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `category` | `query` | `string` | `true` | This parameter can be 1 of the following values general, forex, crypto, merger . |
| `minId` | `query` | `integer` | `false` | Use this field to get only news after this ID. Default to 0 |
- 示例请求：
  - `/news?category=general`
  - `/news?category=forex&minId=10`
- 200 响应结构：`array<#/definitions/MarketNews>`

#### 9. `GET /news-sentiment`

- 标题：News Sentiment
- 摘要：News Sentiment
- OperationId：`news-sentiment`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Company symbol. |
- 示例请求：
  - `/news-sentiment?symbol=V`
  - `/news-sentiment?symbol=AAPL`
- 200 响应结构：`#/definitions/NewsSentiment`

#### 10. `GET /press-releases`

- 标题：Real-time Press Releases API
- 摘要：Major Press Releases
- OperationId：`press-releases`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Company symbol. |
| `from` | `query` | `string` | `false` | From time: 2020-01-01. |
| `to` | `query` | `string` | `false` | To time: 2020-01-05. |
- 示例请求：
  - `/press-releases?symbol=AAPL`
  - `/press-releases?symbol=IBM&from=2019-11-01&to=2020-02-15`
- 200 响应结构：`#/definitions/PressRelease`

#### 11. `GET /search`

- 标题：Global Stocks Search
- 摘要：Symbol Lookup
- OperationId：`symbol-search`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `q` | `query` | `string` | `true` | Query text can be symbol, name, isin, or cusip. |
| `exchange` | `query` | `string` | `false` | Exchange limit. |
- 示例请求：
  - `/search?q=apple&exchange=US`
  - `/search?q=US5949181045`
- 200 响应结构：`#/definitions/SymbolLookup`

#### 12. `GET /sector/metrics`

- 标题：Get ratios for different sectors and regions/indices (S&P500, Nasdaq 100).
- 摘要：Sector Metrics
- OperationId：`sector-metric`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `region` | `query` | `string` | `true` | Region. A list of supported values for this field can be found here . |
- 示例请求：
  - `/sector/metrics?region=NA`
- 200 响应结构：`#/definitions/SectorMetric`

#### 13. `GET /stock/dividend`

- 标题：Global Stocks Dividends API
- 摘要：Dividends
- OperationId：`stock-dividends`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
| `from` | `query` | `string` | `true` | YYYY-MM-DD. |
| `to` | `query` | `string` | `true` | YYYY-MM-DD. |
- 示例请求：
  - `/stock/dividend?symbol=AAPL&from=2022-02-01&to=2023-02-01`
- 200 响应结构：`array<#/definitions/Dividends>`

#### 14. `GET /stock/executive`

- 标题：Global Company Executives & Compensation
- 摘要：Company Executive
- OperationId：`company-executive`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
- 示例请求：
  - `/stock/executive?symbol=AAPL`
  - `/stock/executive?symbol=AMZN`
- 200 响应结构：`#/definitions/CompanyExecutive`

#### 15. `GET /stock/filings`

- 标题：Real-time SEC Filings API
- 摘要：SEC Filings
- OperationId：`filings`
- 新接口标记：`New Endpoint`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | Symbol. Leave symbol , cik and accessNumber empty to list latest filings. |
| `cik` | `query` | `string` | `false` | CIK. |
| `accessNumber` | `query` | `string` | `false` | Access number of a specific report you want to retrieve data from. |
| `form` | `query` | `string` | `false` | Filter by form. You can use this value NT 10-K to find non-timely filings for a company. |
| `from` | `query` | `string` | `false` | From date: 2023-03-15. |
| `to` | `query` | `string` | `false` | To date: 2023-03-16. |
- 示例请求：
  - `/stock/filings?symbol=AAPL`
  - `/stock/filings?cik=320193`
  - `/stock/filings?accessNumber=0000320193-20-000052`
- 200 响应结构：`array<#/definitions/Filing>`

#### 16. `GET /stock/filings-sentiment`

- 标题：Sec Filings Sentiment Analysis
- 摘要：SEC Sentiment Analysis
- OperationId：`filings-sentiment`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `accessNumber` | `query` | `string` | `true` | Access number of a specific report you want to retrieve data from. |
- 示例请求：
  - `/stock/filings-sentiment?accessNumber=0000320193-20-000052`
- 200 响应结构：`#/definitions/SECSentimentAnalysis`

#### 17. `GET /stock/financials`

- 标题：Global Company Financial Statements
- 摘要：Financial Statements
- OperationId：`financials`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
| `statement` | `query` | `string` | `true` | Statement can take 1 of these values bs, ic, cf for Balance Sheet, Income Statement, Cash Flow respectively. |
| `freq` | `query` | `string` | `true` | Frequency can take 1 of these values annual, quarterly, ttm, ytd . TTM (Trailing Twelve Months) option is available f... |
| `preliminary` | `query` | `string` | `false` | If set to true , it will return Preliminary financial statements for the latest period which are usually available wi... |
- 示例请求：
  - `/stock/financials?symbol=AAPL&statement=bs&freq=annual`
  - `/stock/financials?symbol=AC.TO&statement=ic&freq=quarterly`
  - `/stock/financials?symbol=NVDA&statement=ic&freq=quarterly&preliminary=true`
- 200 响应结构：`#/definitions/FinancialStatements`

#### 18. `GET /stock/financials-reported`

- 标题：Financials As Reported | Stock API
- 摘要：Financials As Reported
- OperationId：`financials-reported`
- 新接口标记：`New Endpoint`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | Symbol. |
| `cik` | `query` | `string` | `false` | CIK. |
| `accessNumber` | `query` | `string` | `false` | Access number of a specific report you want to retrieve financials from. |
| `freq` | `query` | `string` | `false` | Frequency. Can be either annual or quarterly . Default to annual . |
| `from` | `query` | `string` | `false` | From date YYYY-MM-DD . Filter for endDate. |
| `to` | `query` | `string` | `false` | To date YYYY-MM-DD . Filter for endDate. |
- 示例请求：
  - `/stock/financials-reported?symbol=AAPL`
  - `/stock/financials-reported?cik=320193&freq=quarterly`
  - `/stock/financials-reported?accessNumber=0000320193-20-000052`
- 200 响应结构：`#/definitions/FinancialsAsReported`

#### 19. `GET /stock/fund-ownership`

- 标题：Fund Ownership
- 摘要：Fund Ownership
- OperationId：`fund-ownership`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
| `limit` | `query` | `integer` | `false` | Limit number of results. Leave empty to get the full list. |
- 示例请求：
  - `/stock/fund-ownership?symbol=TSLA&limit=20`
- 200 响应结构：`#/definitions/FundOwnership`

#### 20. `GET /stock/historical-employee-count`

- 标题：Global Historical Employee Count API
- 摘要：Historical Employee Count
- OperationId：`historical-employee-count`
- 权限提示：Accessible with Fundamental 2 or All in One subscription.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Company symbol. |
| `from` | `query` | `string` | `true` | From date YYYY-MM-DD . |
| `to` | `query` | `string` | `true` | To date YYYY-MM-DD . |
- 示例请求：
  - `/stock/historical-employee-count?symbol=AAPL&from=2022-01-01&to=2024-05-06`
- 200 响应结构：`#/definitions/HistoricalEmployeeCount`

#### 21. `GET /stock/historical-market-cap`

- 标题：Global Historical Market Cap API
- 摘要：Historical Market Cap
- OperationId：`historical-market-cap`
- 权限提示：Accessible with Fundamental 2 or All in One subscription.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Company symbol. |
| `from` | `query` | `string` | `true` | From date YYYY-MM-DD . |
| `to` | `query` | `string` | `true` | To date YYYY-MM-DD . |
- 示例请求：
  - `/stock/historical-market-cap?symbol=AAPL&from=2022-01-01&to=2024-05-06`
- 200 响应结构：`#/definitions/HistoricalMarketCapData`

#### 22. `GET /stock/insider-sentiment`

- 标题：Insider Sentiment API
- 摘要：Insider Sentiment
- OperationId：`insider-sentiment`
- 新接口标记：`New Endpoint`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
| `from` | `query` | `string` | `true` | From date: 2020-03-15. |
| `to` | `query` | `string` | `true` | To date: 2020-03-16. |
- 示例请求：
  - `/stock/insider-sentiment?symbol=TSLA&from=2015-01-01&to=2022-03-01`
- 200 响应结构：`#/definitions/InsiderSentiments`

#### 23. `GET /stock/insider-transactions`

- 标题：Insider Transactions
- 摘要：Insider Transactions
- OperationId：`insider-transactions`
- 新接口标记：`New Endpoint`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. Leave this param blank to get the latest transactions. |
| `from` | `query` | `string` | `false` | From date: 2020-03-15. |
| `to` | `query` | `string` | `false` | To date: 2020-03-16. |
- 示例请求：
  - `/stock/insider-transactions?symbol=TSLA&limit=20`
  - `/stock/insider-transactions?symbol=AC.TO`
- 200 响应结构：`#/definitions/InsiderTransactions`

#### 24. `GET /stock/market-holiday`

- 标题：Global Stock Market Holiday API
- 摘要：Market Holiday
- OperationId：`market-holiday`
- 新接口标记：`New Endpoint`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `exchange` | `query` | `string` | `true` | Exchange code. |
- 示例请求：
  - `/stock/market-holiday?exchange=US`
  - `/stock/market-holiday?exchange=L`
- 200 响应结构：`#/definitions/MarketHoliday`

#### 25. `GET /stock/market-status`

- 标题：Global Market Status API
- 摘要：Market Status
- OperationId：`market-status`
- 新接口标记：`New Endpoint`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `exchange` | `query` | `string` | `true` | Exchange code. |
- 示例请求：
  - `/stock/market-status?exchange=US`
  - `/stock/market-status?exchange=L`
- 200 响应结构：`#/definitions/MarketStatus`

#### 26. `GET /stock/metric`

- 标题：Global Company Basic Financials | P/E, EPS, Market cap, Shares Outstanding
- 摘要：Basic Financials
- OperationId：`company-basic-financials`
- High Usage：`High Usage`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
| `metric` | `query` | `string` | `true` | Metric type. Can be 1 of the following values all |
- 示例请求：
  - `/stock/metric?symbol=AAPL&metric=all`
- 200 响应结构：`#/definitions/BasicFinancials`

#### 27. `GET /stock/ownership`

- 标题：Company Ownership
- 摘要：Ownership
- OperationId：`ownership`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
| `limit` | `query` | `integer` | `false` | Limit number of results. Leave empty to get the full list. |
- 示例请求：
  - `/stock/ownership?symbol=AAPL&limit=20`
  - `/stock/ownership?symbol=IBM`
- 200 响应结构：`#/definitions/Ownership`

#### 28. `GET /stock/peers`

- 标题：Company Peers
- 摘要：Peers
- OperationId：`company-peers`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
| `grouping` | `query` | `string` | `false` | Specify the grouping criteria for choosing peers.Supporter values: sector , industry , subIndustry . Default to subIn... |
- 示例请求：
  - `/stock/peers?symbol=AAPL`
  - `/stock/peers?symbol=F&grouping=industry`
- 200 响应结构：`array`

#### 29. `GET /stock/price-metric`

- 标题：Price statistics API | 52-week high/low , YTD return, average volume.
- 摘要：Price Metrics
- OperationId：`price-metrics`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol of the company: AAPL. |
| `date` | `query` | `string` | `false` | Get data on a specific date in the past. The data is available weekly so your date will be automatically adjusted to ... |
- 示例请求：
  - `/stock/price-metric?symbol=AAPL`
- 200 响应结构：`#/definitions/PriceMetrics`

#### 30. `GET /stock/profile`

- 标题：Global Company Profile
- 摘要：Company Profile
- OperationId：`company-profile`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | Symbol of the company: AAPL e.g. |
| `isin` | `query` | `string` | `false` | ISIN |
| `cusip` | `query` | `string` | `false` | CUSIP |
- 示例请求：
  - `/stock/profile?symbol=AAPL`
  - `/stock/profile?symbol=IBM`
  - `/stock/profile?isin=US5949181045`
- 200 响应结构：`#/definitions/CompanyProfile`

#### 31. `GET /stock/profile2`

- 标题：Global Company Profile 2
- 摘要：Company Profile 2
- OperationId：`company-profile2`
- 新接口标记：`New Endpoint`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | Symbol of the company: AAPL e.g. |
| `isin` | `query` | `string` | `false` | ISIN |
| `cusip` | `query` | `string` | `false` | CUSIP |
- 示例请求：
  - `/stock/profile2?symbol=AAPL`
  - `/stock/profile2?isin=US5949181045`
  - `/stock/profile2?cusip=023135106`
- 200 响应结构：`#/definitions/CompanyProfile2`

#### 32. `GET /stock/revenue-breakdown`

- 标题：Revenue Breakdown | Revenue Segment
- 摘要：Revenue Breakdown
- OperationId：`revenue-breakdown`
- 权限提示：Premium
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | Symbol. |
| `cik` | `query` | `string` | `false` | CIK. |
- 示例请求：
  - `/stock/revenue-breakdown?symbol=AAPL`
  - `/stock/revenue-breakdown?cik=320193`
- 200 响应结构：`#/definitions/RevenueBreakdown`

#### 33. `GET /stock/similarity-index`

- 标题：SEC Filings - Similarity Index Analysis
- 摘要：Similarity Index
- OperationId：`similarity-index`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `false` | Symbol. Required if cik is empty |
| `cik` | `query` | `string` | `false` | CIK. Required if symbol is empty |
| `freq` | `query` | `string` | `false` | annual or quarterly . Default to annual |
- 示例请求：
  - `/stock/similarity-index?symbol=AAPL&freq=annual`
  - `/stock/similarity-index?cik=320193&freq=quarterly`
- 200 响应结构：`#/definitions/SimilarityIndex`

#### 34. `GET /stock/symbol`

- 标题：Stock Symbols By Exchange
- 摘要：Stock Symbol
- OperationId：`stock-symbols`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `exchange` | `query` | `string` | `true` | Exchange you want to get the list of symbols from. List of exchange codes can be found here . |
| `mic` | `query` | `string` | `false` | Filter by MIC code. |
| `securityType` | `query` | `string` | `false` | Filter by security type used by OpenFigi standard. |
| `currency` | `query` | `string` | `false` | Filter by currency. |
- 示例请求：
  - `/stock/symbol?exchange=US`
  - `/stock/symbol?exchange=US&mic=XNYS`
- 200 响应结构：`array<#/definitions/StockSymbol>`

### 3.12 Stock Price

#### 1. `GET /quote`

- 标题：Global Stocks, Forex, Crypto price
- 摘要：Quote
- OperationId：`quote`
- High Usage：`High Usage`
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol |
- 示例请求：
  - `/quote?symbol=AAPL`
  - `/quote?symbol=MSFT`
- 200 响应结构：`#/definitions/Quote`

#### 2. `GET /stock/bbo`

- 标题：Historical NBBO - Best bid/offer
- 摘要：Historical NBBO
- OperationId：`stock-nbbo`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
| `date` | `query` | `string` | `true` | Date: 2020-04-02. |
| `limit` | `query` | `integer` | `true` | Limit number of ticks returned. Maximum value: 25000 |
| `skip` | `query` | `integer` | `true` | Number of ticks to skip. Use this parameter to loop through the entire data. |
- 示例请求：
  - `/stock/bbo?symbol=AAPL&date=2025-06-25&limit=500&skip=0&format=json`
  - `/stock/bbo?symbol=AC.TO&date=2025-06-25&limit=500&skip=0&format=json`
  - `/stock/bbo?symbol=BARC.L&date=2025-06-25&limit=500&skip=0&format=json`
- 200 响应结构：`#/definitions/HistoricalNBBO`

#### 3. `GET /stock/bidask`

- 标题：Last Bid & Ask
- 摘要：Last Bid-Ask
- OperationId：`stock-bidask`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
- 示例请求：
  - `/stock/bidask?symbol=AAPL`
- 200 响应结构：`#/definitions/LastBid-Ask`

#### 4. `GET /stock/candle`

- 标题：Global Stocks OHLCV data | Real-time , Delayed, End-of-day
- 摘要：Stock Candles
- OperationId：`stock-candles`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
| `resolution` | `query` | `string` | `true` | Supported resolution includes 1, 5, 15, 30, 60, D, W, M .Some timeframes might not be available depending on the exch... |
| `from` | `query` | `integer` | `true` | UNIX timestamp. Interval initial value. |
| `to` | `query` | `integer` | `true` | UNIX timestamp. Interval end value. |
- 示例请求：
  - `/stock/candle?symbol=AAPL&resolution=1&from=1738655051&to=1738741451`
  - `/stock/candle?symbol=IBM&resolution=D&from=1735976651&to=1738741451`
- 200 响应结构：`#/definitions/StockCandles`

#### 5. `GET /stock/dividend2`

- 标题：Basic Dividends
- 摘要：Dividends 2 (Basic)
- OperationId：`stock-basic-dividends`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
- 示例请求：
  - `/stock/dividend2?symbol=AAPL`
- 200 响应结构：`#/definitions/Dividends2`

#### 6. `GET /stock/split`

- 标题：Global Stocks Splits API
- 摘要：Splits
- OperationId：`stock-splits`
- 权限提示：Premium required.
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
| `from` | `query` | `string` | `true` | YYYY-MM-DD. |
| `to` | `query` | `string` | `true` | YYYY-MM-DD. |
- 示例请求：
  - `/stock/split?symbol=AAPL&from=2015-02-01&to=2021-03-09`
- 200 响应结构：`array<#/definitions/Split>`

#### 7. `GET /stock/tick`

- 标题：Global Stocks Tick Data
- 摘要：Tick Data
- OperationId：`stock-tick`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol. |
| `date` | `query` | `string` | `true` | Date: 2020-04-02. |
| `limit` | `query` | `integer` | `true` | Limit number of ticks returned. Maximum value: 25000 |
| `skip` | `query` | `integer` | `true` | Number of ticks to skip. Use this parameter to loop through the entire data. |
- 示例请求：
  - `/stock/tick?symbol=AAPL&date=2021-03-09&limit=500&skip=0&format=json`
  - `/stock/tick?symbol=AC.TO&date=2021-03-09&limit=500&skip=0&format=json`
  - `/stock/tick?symbol=BARC.L&date=2021-03-09&limit=500&skip=0&format=json`
- 200 响应结构：`#/definitions/TickData`

### 3.13 Technical Analysis

#### 1. `GET /indicator`

- 标题：Technical Indicators API for Stocks, Forex, Crypto
- 摘要：Technical Indicators
- OperationId：`technical-indicator`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | symbol |
| `resolution` | `query` | `string` | `true` | Supported resolution includes 1, 5, 15, 30, 60, D, W, M .Some timeframes might not be available depending on the exch... |
| `from` | `query` | `integer` | `true` | UNIX timestamp. Interval initial value. |
| `to` | `query` | `integer` | `true` | UNIX timestamp. Interval end value. |
| `indicator` | `query` | `string` | `true` | Indicator name. Full list can be found here . |
| `indicator_fields` | `body` | `` | `false` | Check out this page to see which indicators and params are supported. |
- 示例请求：
  - `/indicator?symbol=symbol=AAPL&resolution=D&from=1583098857&to=1584308457&indicator=sma&timeperiod=3`
- 200 响应结构：`#/definitions/TechnicalIndicator`

#### 2. `GET /scan/pattern`

- 标题：Pattern Recognition API for Stocks, Forex, Crypto
- 摘要：Pattern Recognition
- OperationId：`pattern-recognition`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol |
| `resolution` | `query` | `string` | `true` | Supported resolution includes 1, 5, 15, 30, 60, D, W, M .Some timeframes might not be available depending on the exch... |
- 示例请求：
  - `/scan/pattern?symbol=AAPL&resolution=D`
- 200 响应结构：`#/definitions/PatternRecognition`

#### 3. `GET /scan/support-resistance`

- 标题：Support/Resistance Levels API for Stocks, Forex, Crypto
- 摘要：Support/Resistance
- OperationId：`support-resistance`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | Symbol |
| `resolution` | `query` | `string` | `true` | Supported resolution includes 1, 5, 15, 30, 60, D, W, M .Some timeframes might not be available depending on the exch... |
- 示例请求：
  - `/scan/support-resistance?symbol=IBM&resolution=D`
- 200 响应结构：`#/definitions/SupportResistance`

#### 4. `GET /scan/technical-indicator`

- 标题：Aggregate Indicators/Technical Signal API for Stocks, Forex, Crypto
- 摘要：Aggregate Indicators
- OperationId：`aggregate-indicator`
- 权限提示：Premium Access Required
- 参数：

| name | in | type | required | 说明 |
|---|---|---|---|---|
| `symbol` | `query` | `string` | `true` | symbol |
| `resolution` | `query` | `string` | `true` | Supported resolution includes 1, 5, 15, 30, 60, D, W, M .Some timeframes might not be available depending on the exch... |
- 示例请求：
  - `/scan/technical-indicator?symbol=AAPL&resolution=D`
- 200 响应结构：`#/definitions/AggregateIndicators`

## 4. Schema（定义对象）索引

- 定义对象总数：`191`
- 下方仅列出对象名，具体字段请结合上面的响应结构引用查看。

- `AIChatBody`
- `AIChatMessage`
- `AIChatResponse`
- `AggregateIndicators`
- `AirlinePriceIndex`
- `AirlinePriceIndexData`
- `BankBranchData`
- `BankBranchRes`
- `BasicFinancials`
- `BondCandles`
- `BondProfile`
- `BondTickData`
- `BondYieldCurve`
- `BondYieldCurveInfo`
- `BreakdownItem`
- `BreakdownItemMap`
- `Company`
- `CompanyESG`
- `CompanyESG2`
- `CompanyESGMap`
- `CompanyEarningsQualityScore`
- `CompanyEarningsQualityScoreData`
- `CompanyExecutive`
- `CompanyNews`
- `CompanyNewsStatistics`
- `CompanyProfile`
- `CompanyProfile2`
- `CongressionalTrading`
- `CongressionalTransaction`
- `CountryMetadata`
- `CovidInfo`
- `CryptoCandles`
- `CryptoProfile`
- `CryptoSymbol`
- `Development`
- `Dividends`
- `Dividends2`
- `Dividends2Info`
- `DocumentResponse`
- `ETFAllocationData`
- `ETFCountryExposureData`
- `ETFHoldingsData`
- `ETFProfileData`
- `ETFSectorExposureData`
- `ETFsAllocation`
- `ETFsCountryExposure`
- `ETFsHoldings`
- `ETFsProfile`
- `ETFsSectorExposure`
- `EarningRelease`
- `EarningResult`
- `EarningsCalendar`
- `EarningsCallLive`
- `EarningsCallLiveResult`
- `EarningsCallTranscripts`
- `EarningsCallTranscriptsList`
- `EarningsEstimates`
- `EarningsEstimatesInfo`
- `EbitEstimates`
- `EbitEstimatesInfo`
- `EbitdaEstimates`
- `EbitdaEstimatesInfo`
- `Economic event`
- `EconomicCalendar`
- `EconomicCode`
- `EconomicData`
- `EconomicDataInfo`
- `EmployeeCount`
- `ExcerptResponse`
- `FDAComitteeMeeting`
- `Filing`
- `FilingResponse`
- `FilingSentiment`
- `FinancialMap`
- `FinancialStatements`
- `FinancialsAsReported`
- `ForexCandles`
- `ForexRate`
- `ForexSymbol`
- `Forexrates`
- `FundOwnership`
- `FundOwnershipInfo`
- `HistoricalCompanyESG`
- `HistoricalEmployeeCount`
- `HistoricalMarketCapData`
- `HistoricalNBBO`
- `IPOCalendar`
- `IPOEvent`
- `InFilingResponse`
- `InFilingSearchBody`
- `IndexHistoricalConstituent`
- `Indicator`
- `IndicatorFields`
- `IndicesConstituents`
- `IndicesConstituentsBreakdown`
- `IndicesHistoricalConstituents`
- `InsiderSentiments`
- `InsiderSentimentsData`
- `InsiderTransactions`
- `InstitutionalOwnership`
- `InstitutionalOwnershipGroup`
- `InstitutionalOwnershipInfo`
- `InstitutionalPortfolio`
- `InstitutionalPortfolioGroup`
- `InstitutionalPortfolioInfo`
- `InstitutionalProfile`
- `InstitutionalProfileInfo`
- `InternationalFiling`
- `InvestmentThemePortfolio`
- `InvestmentThemes`
- `IsinChange`
- `IsinChangeInfo`
- `KeyCustomersSuppliers`
- `LastBid-Ask`
- `LobbyingData`
- `LobbyingResult`
- `MarketCapData`
- `MarketHoliday`
- `MarketHolidayData`
- `MarketNews`
- `MarketStatus`
- `MetricMap`
- `MetricSeriesMap`
- `MutualFundCountryExposure`
- `MutualFundCountryExposureData`
- `MutualFundEet`
- `MutualFundEetData`
- `MutualFundEetPai`
- `MutualFundEetPaiData`
- `MutualFundHoldings`
- `MutualFundHoldingsData`
- `MutualFundProfile`
- `MutualFundProfileData`
- `MutualFundSectorExposure`
- `MutualFundSectorExposureData`
- `NewsSentiment`
- `Newsroom`
- `NewsroomArticle`
- `Ownership`
- `OwnershipInfo`
- `PatternRecognition`
- `PresentationData`
- `PressRelease`
- `PriceMetricMap`
- `PriceMetrics`
- `PriceTarget`
- `Quote`
- `RecommendationTrend`
- `Report`
- `ReportDataMap`
- `RevenueBreakdown`
- `RevenueBreakdown2`
- `RevenueEstimates`
- `RevenueEstimatesInfo`
- `SECSentimentAnalysis`
- `ScanPattern`
- `SearchBody`
- `SearchFilter`
- `SearchResponse`
- `SectorMetric`
- `SectorMetricData`
- `Sentiment`
- `SentimentContent`
- `SimilarityIndex`
- `SimilarityIndexInfo`
- `SocialSentiment`
- `Split`
- `StockCandles`
- `StockPresentation`
- `StockSymbol`
- `StockTranscripts`
- `SupplyChainRelationships`
- `SupportResistance`
- `SymbolChange`
- `SymbolChangeInfo`
- `SymbolLookup`
- `SymbolLookupInfo`
- `TechnicalAnalysis`
- `TechnicalIndicator`
- `TickData`
- `Transactions`
- `TranscriptContent`
- `TranscriptParticipant`
- `Trend`
- `UpgradeDowngrade`
- `UsaSpending`
- `UsaSpendingResult`
- `UsptoPatent`
- `UsptoPatentResult`
- `VisaApplication`
- `VisaApplicationResult`

## 5. 使用建议（给 FionaTrade）

- 采集优先：`/company-news`、`/news`、`/stock/candle`、`/stock/tick`、`/stock/metric`。
- 补数策略：按 `ticker + date` 分片回补，失败重试并记录 `last_success_ts`。
- 速率治理：为 market-data 与 news 建立独立限流器；批量任务放低优先级队列。
- 监控字段：每个请求记录 `status_code`、耗时、返回条数、remaining 配额（若返回）。

## 6. 免责声明

- 本文档由官方 `docSchema` 自动整理，字段可能随 Finnhub 更新而变化。
- 如有不一致，以官方文档页面与账号订阅权限为准。