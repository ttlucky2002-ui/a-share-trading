# 国信证券真实账户接入与上线步骤

## 当前实现边界

本系统已实现国信开放 API 的运行时配置、AK/SK HMAC 网关调用、真实资金/持仓/成交查询接入位、真实风险评估、限价订单预检和带开关的订单提交。

系统无法从普通客户端登录信息推导交易接口。你必须先取得国信授权项目中提供的 API 地址、账户权限、报文字段和密钥，才能连接真实账户。具体交易 API 名称和响应字段以你的授权项目在线文档为准。

官方资料可确认的流程如下：

- 国信开放 API 平台要求先创建应用，并将 API 集合关联到应用；授权项目是获准访问 API 集合的载体。
- 官方快速入门说明：账号申请需联系对接人，开发方先在测试环境联调，线上访问需要提交申请并等待项目联系人审批。
- 官方 HMAC 文档定义了 `X-GS-API-AK`、`X-GS-API-DATE`、`X-GS-API-ALGORITHM`、`X-GS-API-SIGNATURE`，以及项目开启请求体校验时使用的 `X-GS-API-BODY-DIGEST`。
- iQuant 是基于 Python、支持自动交易且本地运行的策略平台；它是另一条客户端策略运行路线，不等于本 Web 服务可直接调用的开放 HTTP 交易接口。

## 第一步：向国信申请能力

1. 联系你的国信客户经理或国信开放 API 对接人，说明需要证券账户的自动化交易及查询能力。
2. 确认账户类型、可开通的开放 API 或 iQuant 权限、测试环境、生产审批条件、IP 白名单或证书要求。
3. 本项目按开放 API 方式集成；若最终仅获批 iQuant，应在 iQuant 内运行策略，而不是把普通登录账号填入本页面。
4. 在开放 API 控制台创建应用，并申请与你的资金账号绑定的查询和委托 API 集合。

至少申请以下业务能力：

| 能力 | 本系统用途 |
| --- | --- |
| 联通或账户状态查询 | 验证鉴权与环境 |
| 资金查询 | 展示总资产、可用资金、总盈亏 |
| 持仓查询 | 计算真实总仓位、单票仓位和持仓盈亏 |
| 成交查询 | 评估真实成交频率并进行复核 |
| 委托查询 | 核对委托状态 |
| 下单接口 | 提交真实限价委托 |

## 第二步：取得授权项目参数

审批及测试环境开通后，在授权项目的在线文档中记录：

1. 测试环境和生产环境的 HTTPS 根地址。
2. 应用的 AK、SK 及密钥轮换流程。
3. 资金账号字段及是否需要额外股东号、市场代码或二次认证。
4. 资金、持仓、成交、委托、下单各自的 HTTP 路径、方法、请求字段、返回字段和单位。
5. 下单的订单类型值、买卖方向值、市场代码格式、撤单和订单状态查询方式。
6. HMAC 是否包含额外 signed headers，以及下单报文是否启用请求体摘要。

重要：当前 `risk_control.py` 支持一组常用中英文别名。如果国信授权响应使用其他字段名或收益率以小数而非百分数返回，需要按实际样例调整字段映射和单位后再使用风控数值。

当前 `broker.py` 将资金、持仓、成交和委托查询按 `GET` 请求实现，并支持可配置的资金账号查询参数名。如果你的授权文档规定查询采用 `POST`、要求额外签名请求头或需要二次认证，必须先按该文档扩展适配器，再进行真实账户联调。

## 第三步：在测试环境配置系统

在新的 PowerShell 会话中配置测试环境，不要把密钥写入代码：

```powershell
$env:GUOSEN_API_BASE_URL="https://测试环境网关"
$env:GUOSEN_API_AK="测试AK"
$env:GUOSEN_API_SK="测试SK"
$env:GUOSEN_ACCOUNT_ID="资金账号"
$env:GUOSEN_ACCOUNT_QUERY_FIELD="授权接口要求的账号查询参数名（若需要，如 accountId）"
$env:GUOSEN_STATUS_PATH="/授权文档中的联通或状态路径"
$env:GUOSEN_ACCOUNT_PATH="/授权文档中的资金查询路径"
$env:GUOSEN_POSITIONS_PATH="/授权文档中的持仓查询路径"
$env:GUOSEN_TRADES_PATH="/授权文档中的成交查询路径"
$env:GUOSEN_ORDERS_PATH="/授权文档中的委托查询路径"
$env:GUOSEN_ORDER_PATH="/授权文档中的下单路径"
$env:GUOSEN_ORDER_TEMPLATE='{"account":"{{account_id}}","symbol":"{{market_code}}","side":"{{side}}","orderType":"{{order_type}}","price":{{price}},"quantity":{{quantity}}}'
# 仅在授权项目明确要求请求体摘要时设置：
$env:GUOSEN_BODY_DIGEST="YES"
python server.py
```

订单模板只是占位示例，必须替换为授权文档中的字段和值枚举。

## 第四步：联调查询与真实风控

1. 打开页面的“国信下单”，确认测试环境路径已加载。
2. 依次执行“测试连接”和“查询资金”；再调用 `/api/broker/positions` 与 `/api/broker/trades`，取得真实样例响应。
3. 打开“风控诊断”，确认页面标明来源为真实账户，且总资产、仓位、盈亏和成交与国信客户端一致。
4. 如字段无法识别，在 `risk_control.py` 的 `ACCOUNT_FIELDS`、`POSITION_FIELDS`、`TRADE_FIELDS` 中按授权响应增加映射，并重新核对每项单位。
5. 不要在真实持仓未核对一致时开启下单。

## 第五步：联调订单全生命周期

1. 使用“生成下单预检”核对下单报文，系统此时不会发送真实委托。
2. 在国信允许下单的测试环境提交测试订单，核验订单编号、委托查询、成交查询和持仓变化。
3. 单独验证拒单、部分成交、撤单、当日买入不可卖以及价格/数量非法的处理。
4. 当前代码提供订单提交入口，但尚未实现撤单界面、回报推送、幂等订单键和自动对账。在这些功能补齐前，不建议无人值守执行实盘策略。

## 第六步：生产启用与停机

测试及审批通过后，换为生产网关和生产密钥，仍先保持实盘关闭完成资金、持仓和成交核对。确认一致后才启用：

```powershell
$env:GUOSEN_ENABLE_LIVE_TRADING="YES"
python server.py
```

生产提交还必须在页面输入 `CONFIRM_LIVE_ORDER`。建议先用符合交易规则的最小限价订单验证全链路，并立即在国信客户端复核委托、成交和持仓。

紧急停机方式：

```powershell
Remove-Item Env:GUOSEN_ENABLE_LIVE_TRADING -ErrorAction SilentlyContinue
```

随后重启服务；未显式启用时系统只允许订单预检。

## 安全检查

- AK、SK、资金账号和授权响应不得提交到版本库或粘贴进公开日志。
- 使用测试和生产分离的应用及密钥，配置最小权限和允许访问的网络来源。
- 保留真实委托、成交回报、风控判定和人工操作日志，以便对账和审计。
- 将券商客户端或官方渠道作为最终资金、委托和成交事实来源。

## 官方资料

- [国信证券开放 API 平台文档](https://openapi.guosen.com.cn/doc/)
- [国信开放 API 快速入门](https://openapi.guosen.com.cn/doc/quickstart/)
- [国信开放 API HMAC(AK/SK)详解](https://openapi.guosen.com.cn/doc/signature/gsApiSignature/)
- [国信 iQuant 产品页](https://www.guosen.com.cn/gs/iquant/index.html)
