# 系统模块与数据边界

## 运行入口

生产运行入口是 `python server.py`，页面模板为 `templates/index.html`。`app.py` 为精简 Flask 兼容入口，不包含中长期基本面、国信交易和真实风控的完整路由，日常开发以 `server.py` 为准。

账户边界如下：

- 系统不维护本地模拟资金、模拟持仓或模拟成交。
- 风险诊断只接受 `broker.py` 从国信授权 API 查询到的资金、持仓和成交。
- `backtest.py` 的成交仅是历史策略验证结果，不进入账户风险诊断。
- `watchlist.py` 只保存观察代码，不表示账户持仓。

## 模块清单

| 模块 | 责任 | 主要输入 | 主要输出 | 优化时应关注 |
| --- | --- | --- | --- | --- |
| `config.py` | 集中参数 | 环境变量、固定阈值 | `LONG_TERM`、`RISK`、`BACKTEST` | 参数单位、实盘默认关闭 |
| `data_feed.py` | 行情、K 线、财务数据接口 | 股票代码、市场请求 | 标准化行情与财务字典 | 数据源降级、报告期、字段缺失 |
| `fundamental.py` | 中长期综合选股 | 全市场快照、逐股财务 API、K 线、板块资金 | 综合排名与精选十股 | 权重、财务因子、资金匹配口径 |
| `screener.py` | 兼容的独立技术筛选接口 | 行情、K 线 | 旧接口结果 | 页面主流程不再使用 |
| `strategy.py` | 唯一小资金策略的入场/退出算法 | K 线指标、风险参数 | `TradeSignal` | 信号确认、仓位计算 |
| `backtest.py` | 历史验证 | 策略、K 线、费用参数 | 收益、回撤、历史成交 | 防前视、基准、交易成本 |
| `ai_advisor.py` | 基于取得数据的文字解释 | 财务候选池、市场上下文 | 摘要、风险核验项 | AI 不可改固定评分 |
| `broker.py` | 国信授权 API 网关 | AK/SK、路径、委托模板 | 真实查询响应、受控委托 | 授权文档字段和签名校验 |
| `risk_control.py` | 真实账户风险评估 | 国信资金/持仓/成交响应 | 风控摘要、阈值告警 | 响应字段映射与单位 |
| `watchlist.py` | 自选观察列表 | 六位股票代码 | `.cache/watchlist.json` | 与账户数据隔离 |
| `server.py` | HTTP API 与业务编排 | 页面请求 | JSON/HTML 响应 | API 合约、异常隔离 |
| `templates/index.html` | 交互界面 | API 返回 | 研究和交易操作界面 | 不回退显示虚拟账户值 |

## 核心数据流

### 选股与建议

1. `server.py` 接收 `POST /api/long-term-screen`。
2. `fundamental.py` 从 `data_feed.py` 取得初筛池，并对全部初筛标的调用财务 API，不做 500 只截断。
3. 按 `LONG_TERM.weights` 生成基本面分，对合格池追加技术评分并按 `composite_weights` 排名。
4. 通过热门行业/概念成分匹配及资金流按 `selection_weights` 精选 10 只。
5. 页面自动请求 `POST /api/investment-advice`，将固定十股交给 DeepSeek 逐只给出建议。

### 真实账户风险

1. 页面配置国信资金、持仓、成交查询路径及授权参数。
2. `GET /api/risk-dashboard` 调用 `risk_control.RealAccountRiskAnalyzer`。
3. 分析器通过 `broker.GuosenOpenAPIClient` 请求真实账户接口。
4. 分析器将真实持仓与 `RISK` 阈值比对，返回仓位、盈亏和成交频率告警。
5. 未配置或查询失败时返回未取得真实账户数据，不生成本地替代值。

### 真实委托

1. 页面调用 `POST /api/broker/order/preview` 生成限价委托预检报文。
2. `broker.py` 校验股票代码、方向、价格和 A 股买入数量规则。
3. 只有服务端启动时显式设置 `GUOSEN_ENABLE_LIVE_TRADING=YES`，且请求携带确认口令，才可请求授权下单路径。
4. 成交与持仓的最终事实以随后从国信查询到的数据为准。

## HTTP 接口分组

| 路径 | 用途 |
| --- | --- |
| `POST /api/long-term-screen`、`GET /api/long-term-screen/status` | 全量选股、综合排名与精选十股进度 |
| `POST /api/investment-advice` | 对精选十股生成 DeepSeek/规则回退建议 |
| `GET /api/kline/{code}`、`POST /api/backtest/run` | 行情策略查看和可配置单股历史验证 |
| `POST /api/backtest/core`、`POST /api/backtest/optimize`、`GET /api/backtest/optimize/status` | 精选核心股批测与 DeepSeek 参数候选优化日志 |
| `GET /api/risk-dashboard` | 基于真实账户的风险诊断 |
| `POST /api/broker/config`、`POST /api/broker/test` | 国信运行时配置和联通测试 |
| `GET /api/broker/account`、`/positions`、`/trades`、`/orders` | 国信真实查询转发 |
| `POST /api/broker/order/preview`、`POST /api/broker/order` | 委托预检和受保护的真实提交 |
| `GET/POST /api/watchlist/*` | 独立自选观察列表 |

## 修改与测试顺序

| 改动目标 | 优先修改 | 必测内容 |
| --- | --- | --- |
| 更换基本面因子或权重 | `config.py`、`fundamental.py` | 缺失财务字段、排序可复核、报告期显示 |
| 调整入场/退出策略 | `strategy.py`、`backtest.py` | 历史样本外结果、费用和最大回撤 |
| 适配国信授权响应字段 | `broker.py`、`risk_control.py` | 资金/持仓/成交真实样例映射、失败不伪造 |
| 放开更多实盘指令 | `broker.py`、`server.py`、页面 | 预检、幂等、撤单、成交回报、停机开关 |
