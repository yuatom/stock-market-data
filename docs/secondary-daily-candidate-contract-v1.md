# secondary-daily-candidate/v1 输出契约

用途：离线候选解析与代码评审，不是生产 market-data-series 契约。新增不兼容字段类型/语义须提升版本。此契约不是源数据授权书。

## 基础结果：normalize_sina / normalize_yahoo
- `schema_version` 固定 `secondary-daily-candidate/v1`。
- `provider` 为 `sina_daily_candidate` 或 `yahoo_chart_candidate`；两者不得拼接成单一历史。
- `symbol` 显式请求标的；新浪 `identity_verification=request_bound_only`，Yahoo 为 `payload_symbol`。不表示受认证身份。
- `session=regular`、`interval=1d` 是候选分类，不证明官方结算值。
- `price_encoding=decimal_string`。open/high/low/close 为十进制字符串或 null；close 必须非空正数。最多64位有效数字，非零数量级指数绝对值≤100。Sina保留JSON数字及字符串精度；直接传Yahoo dict之前已转float的损失无法恢复。
- `volume` 为0至2^63−1整数或null，不填零替代缺失。
- `bars`：1至5000行、按trade_date升序、日期唯一且早于调用方as_of_date。此日期门不校验交易所日历或报告精确截止时刻。OHLC范围必须自洽。
- `quality_flags`：相邻收盘比严格低于0.6或高于1.4时标记 `large_price_discontinuity_unexplained` 与trade_date；不识别拆股，也不证明其余序列连续。
- 固定安全状态：`market_fact_authority=false`、`volume_eligible=false`、`technical_eligible=false`、`official_close_claim=false`、`promotion_required=true`、`adjustment_semantics=unverified_provider_observation`、`technical_block_reason=adjustment unverified`。
- `require_technical_eligibility` 对当前全部候选抛ValueError，不存在更改布尔字段即晋升路径。

## 绑定结果：normalize_capture
包含基础结果及：
- `capture`：原始bytes的SHA256、字节长度、原始URL、带时区的started_at/finished_at。校验成功HTTP、固定端点/显式标的、无凭据/端口/重定向、参数无重复；raw上限8,000,000字节。
- `capture.binding=local_bytes_and_declared_request`，`authenticated_origin=false`，`historical_availability_proven=false`。哈希及外部传入元数据只证明局部一致性，不证明真实来源或发布时间。
- `action_coverage_verified=false` 永不因事件列表或adjclose存在而晋升。
- 新浪 `corporate_actions.status=not_provided_by_endpoint`，不输出虚构事件或调整后收盘。
- Yahoo `adjusted_close_observations` 为null（未提供）或与源timestamp逐项对应的 `{trade_date,value}` 列表；value十进制字符串或null，绝不覆盖bars.close。
- Yahoo `corporate_actions.status` 区分缺失 `absent_not_proof_of_no_actions` 与提供 `provided_coverage_unverified`；splits/dividends/capitalGains分别列出。事件ID与秒级整数date一致，按交易所历史时区映射日期，早于as_of_date。
- 拆股保留numerator/denominator十进制字符串及ratio原字符串，只检查正数与比例自洽；股息/分配保留amount，未知/非法值拒绝。提供事件不等于事件覆盖完整，更不是OHLC复权语义证明。

所有异常输入拒绝，不默认为空行情。JSON重复成员拒绝，JSONP只接受裸JSON、严格var(...);或实测固定无执行注释包装。模块无网络、文件写入、Store更新、路由或生产晋升功能。

## 可复现合成fixture
`tests/fixtures/secondary_daily/synthetic_v1.json` 使用虚构TEST证券、2020年日期及人工数字；不是实盘原始响应，也不是SOXX拆股证据。覆盖固定包装、长十进制、缺值、大整数成交量、4:1合成事件、结构断点和始终拒绝晋升。运行依赖：Python 3.9+ 标准库及系统 IANA 时区数据库（包含 America/New_York）；无第三方 Python 包、密钥、网络或生产配置依赖。当前macOS已具备时区数据库；其他环境若缺失须先补时区数据，不能静默改用UTC。运行：

```sh
python3 -m unittest discover -s tests -p 'test_secondary_daily*.py'
```
