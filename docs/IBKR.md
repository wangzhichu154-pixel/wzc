# IBKR 执行边界与接入验收

## 实时 API 自动交易升级

新的实时 Bid/Ask 接入代码、已验证范围与多标的自动交易尚缺环节见 [实时自动交易接入约定](REALTIME_AUTOTRADING_PLAN.md)。该数据入口未接通订单发送，不改变下述单标的 Paper 工具的权限和限制。

## 当前实际交付

`execution/include/trading/execution_gateway.hpp` 提供独立 C++20 `IExecutionGateway` 契约、发送边界检查和显式不可用实现。`execution/ibkr_probe.py` 是使用官方 `ibapi` 的独立只读运维工具，能够请求账户值、持仓、开放订单和可获得的执行记录。

另外交付 `execution/auto_paper.py`：独立、显式启用的单标的 paper 自动执行代码。它使用官方 Python API 的真实 `placeOrder` / `cancelOrder` 接口，并通过 `libvector_core.so` C ABI 调用 C++ 订单簿、定价、同步风控和订单状态逻辑。历史 Replay、Web 研究任务和 paper 执行器彼此分离，Web 不提供启动发单接口。

**Paper 发送代码已实现并完成本地真实 C++ 核心集成测试；官方 SDK、真实 broker 和行情联调仍未验收。Live 交易始终拒绝。** 只读探针不调用任何写入接口；自动 paper 执行器仅在全部显式门槛满足后才调用写入接口。任何实现均不提供盈利承诺。

截至本次实现，未提供真实 TWS / IB Gateway、账户授权和有效 live 行情接入；没有运行真实账户查询或任何下单，没有测得 IBKR 延迟。官方 SDK 安装及版本兼容性未验证，测试中的 SDK 替身仅位于 `execution/tests/`，不能将它当作 IBKR 联调通过。

## SDK 与独立安装

优先安装运营者批准的官方 [TWS API](https://interactivebrokers.github.io/) 发布包中的 Python 客户端，并记录版本、来源与完整性校验。`execution/requirements-ibkr.txt` 固定了可选 PyPI 分发版 `ibapi==9.81.1.post1`；这是旧版本分发，**并不声明与当前 Gateway 兼容**。它不进入默认 Web 依赖或默认镜像，使用前必须通过环境对应的兼容性验收。未安装 SDK 时工具以非零状态退出，明确报告不可用。

使用独立虚拟环境，避免将 broker SDK 依赖混入 Web 服务：

```bash
python -m venv .venv-ibkr
.venv-ibkr/bin/python -m pip install -r execution/requirements-ibkr.txt
.venv-ibkr/bin/python -m execution.ibkr_probe --help
```

采用新官方发布包时，安装该包说明中的 Python 客户端并在运行记录中注明版本；不是悄悄升级生产依赖。

## 只读接入

1. 在 TWS / IB Gateway 中开启 API，并启用 **Read-Only API**。
2. 使用专用非零 client ID，限制 API 监听和受信任地址；不要把 broker socket 暴露到公网。
3. 先使用实际 paper 账户；账户号、host、port 从环境输入，不提交 Git。
4. 运行工具。以下环境变量需要操作者设置实际值：

```bash
export IBKR_EXECUTION_MODE=readonly
# 设置 IBKR_ACCOUNT、IBKR_HOST、IBKR_PORT、IBKR_CLIENT_ID。
.venv-ibkr/bin/python -m execution.ibkr_probe \
  --account-kind paper --timeout 20 --output /secure/path/ibkr-snapshot.json
```

默认 host 为本机、port 为 7497、client ID 为 71；这些是可覆盖的客户端默认值，不是账户模式证明。`--account-kind live` 只允许显式读取 live 账户，不开放交易。

工具校验配置账户确实出现在 `managedAccounts`，paper 模式额外要求 DU 前缀；**端口和前缀均不能独立证明账户模式**，因此产物始终包含 `account_mode_attested=false`。任何未来写入授权必须有独立、受控的环境与账户验证流程。

## 数据与错误语义

- 所有查询必须收到对应完成回调；连接中断、SDK 错误、超时或容量溢出均返回非零，不生成成功快照。
- 输出文件使用独占创建和 `0600` 权限；不覆盖既有文件或跟随目的文件符号链接。
- 标准输出只返回结果状态与文件路径，账户资料仅保存到操作者指定私有文件。
- 相同执行 ID、相同内容幂等；同一 ID 不同内容被拒绝，要求调查。
- 更正执行的完整 ID 被保留，不把后缀不同的更正简单去重，也不在探针内自动更正 PnL。
- 多个 IBKR 查询不构成同一时刻的原子快照。`query_completed=true` 不等于 `ledger_reconciled=true`。
- 执行历史范围取决于 API、Gateway 和权限，不能据此保证完整恢复历史账本。
- 完成查询不授权策略下单。输出始终为 `trading_enabled=false`、`ledger_reconciled=false`、`execution_history_complete=false`。
- 连接断开不会自动重连或重发任何经济意图。

## 自动 Paper 执行的操作边界

本实现仅支持单只 USD 股票、整数股、NASDAQ 的 ISLAND 直接路由、DAY 限价单、正常连续时段。实际 contract ID、symbol、币种、证券类型、路由 market rule 均从官方接口查询并校验。只改变配置中的股票代码而不校验合约不会放行。支持成本感知双边报价计算、受账户权限约束的报价参与、撤后再报、成交/佣金处理及持仓/PnL 风控；当前执行策略仅做多，初始零库存时不允许卖单，买入成交后才可能挂不超过可卖库存的 Ask。不支持任意多标的组合、复杂订单、自动清仓或自动故障恢复。

### 账户资源与交易权限

账户余额不使用示例数据。执行器通过官方 `reqAccountSummary` 请求精确账户的 **USD `AvailableFunds`、`SettledCash`、`BuyingPower`**；只有三项完整且收到对应 `accountSummaryEnd` 才提交快照。`BASE` 或其他币种不能被当成 USD，缺失字段不能推算为成功。新的唯一 request ID 标识每轮快照；重复同字段保守取较小值，迟到的旧请求不刷新状态，订阅在完成或超时后经共用 pacing 队列取消。

`PAPER_FUNDS_MAX_AGE_MS` 默认 15000，允许 1000–60000；`PAPER_FUNDS_REFRESH_MS` 默认 5000，要求 1000 到最大年龄的一半。新鲜度从**发出请求前的开始时间**计算，网络往返和排队不会延长有效期。请求异常、缺失、超时、过期均禁止新单，并安排撤销风险增加方向的旧报价；Kill 和撤单不等待模型或资金授权。

购买预算取三项中较低的 USD 值，并与该进程先前最低快照比较。再扣除本地累计买入成交金额、正佣金高水位和全部未终结买单预留。每单预留包括 `leaves × (limit price + 每股费用 + 完整最低佣金 + 滑点缓冲)`；发送前和 C++ 预留后分别同步检查。部分成交转为累计实际购买消耗，重复成交不再扣款；未知发送、未确认撤单继续占用，只有确定未发或已对齐的终态才释放剩余数量。卖出收益、返佣和后续较高快照**不会自动变成新的可用购买额度**。

这是刻意严格的**本地保守预算，不是 broker 实际现金余额**。Broker 快照可能已包含本地购买消耗，再扣本地累计值会重复保守扣减，导致提前停止买入；当前不支持日内资金循环、保证金融资或自动放大预算。需要完整资金流水对账的宽松模式尚未实现，不应通过删除私有账本或伪造快照绕过。

操作者必须在真实 broker 侧核实账户的股票交易权限，并设置 `IBKR_STOCK_TRADING_PERMISSION_ATTESTED=true`。API 合约查询和资金余额本身不证明交易许可，此标记是独立操作核验声明，不能由 AI 输出、网页用户或资金数字设置。Broker 仍可 Reject；首次接入需实际验证其语义。`PAPER_ALLOW_SHORT` 当前只允许字面值 `false`；没有借券/卖空许可核验，任何启用卖空的设置均拒绝启动。每笔卖单检查 `持仓 - 所有未终结卖单剩余数量 >= 新卖单数量`，并在 C++ 预留后重新检查，防止多单共同超卖。

### 可选 AI 参与过滤

`PAPER_AI_REGISTRY` 默认空，执行器不加载 AI。设置为操作者管理的注册库绝对目录后，启动先调用 `ml.registry.load_approved_model(..., mode="paper")`，核验真实授权数据、独立评估/仿真证据和明确的 operator paper 审批。Synthetic 工程模型始终拒绝 Paper；普通训练完成、回测结果或 Web 任务不会自动晋级或获得发单权限。

适配器再以 `O_NOFOLLOW` 打开有界正规 JSON 文件，校验读取的同一字节 SHA256，并用 `LinearModel.from_bytes` 解析，避免核验与加载使用两次不同内容。版本、symbol、预测 horizon、授权 provenance 与批准 manifest 一致。运行时 PolicyConfig 必须与批准的七字段 policy 和 `policy_sha256` **完全匹配**：

| Policy 字段 | 执行器来源 |
|---|---|
| `signal_ttl_ns` | `PAPER_AI_SIGNAL_TTL_MS × 1,000,000`，默认 250ms、范围 1–1000ms，且不超过预测 horizon |
| `min_expected_edge_bps` | `PAPER_AI_MIN_EXPECTED_EDGE_BPS`，默认 1.0、有限值 0–10000 |
| `fee_per_share_units` | `PAPER_FEE_PER_SHARE_UNITS` |
| `min_fee_per_order_units` | `PAPER_MIN_FEE_UNITS` |
| `slippage_buffer_units` | `PAPER_SLIPPAGE_BUFFER_UNITS` |
| `max_target_position` | `PAPER_INVENTORY_LIMIT`，与 C++ 硬库存上限相同 |
| `allow_short` | 固定 `false` |

Paper 时钟固定 `monotonic`，离线回放是 `exchange_logical`；manifest 明确绑定 `paper:monotonic;simulation:exchange_logical` 合同。每个已提交的目标 ITCH 消息在更新 C++ 簿后观察 BBO，使用真实 Mold 序列和 ITCH 日内纳秒构建共享 `FeatureState`；不以机器 monotonic 时间冒充交易所时段。BBO 未变的普通消息不推进特征窗口或续期信号；S/H/h 状态仍校验源时间与交易状态。窗口不足、同时间戳第二次变化不能生成新样本、特征/模型失败、预测不足以覆盖保守双边成本、市场不新鲜或 TTL 到期，均为 WAIT。

AI 只提供 `allow_bid` / `allow_ask`，参与既有 **C++ 报价价格、数量、订单状态和同步风控**。不修改价格、数量、账户权限、资金、库存/亏损限额或 Kill 状态。每轮和发送前重新检查，失效时安排撤销已不允许方向的旧报价；它不能阻止风险撤单。WAIT 可以留下既有库存，当前没有授权 AI 自主清仓。

批准内容每秒在本地重新验证；文件缺失、撤销或 active 版本变化会锁存 WAIT 并撤销报价，不热替换模型、不自动恢复许可。本版原有同日禁止重启交易的限制仍适用，需按下一交易会话的接入门槛受控重新启动。该检查使用外层 Python 本地磁盘，因此位于最终 C++ 一次性 permit/deadline/行情新鲜度检查**之前**。最后发送边界再次以当前时钟检查 permit、账户快照和信号 TTL，防止验证耗时穿透有效期。这个轮询撤销机制有最多约 1 秒检测间隔加控制循环排队时间，不是硬实时撤销保证；异常磁盘阻塞也需宿主监控处理。

私有 journal 的 `paper_armed` 保存批准版本/模型文件 SHA/policy SHA，`ai_send_candidate` 记录对应发单候选的结构化信号；候选日志不等于实际发送或成交。只读状态附带 AI 模式和最新信号，不提供 Web 启动接口。默认设置、`--enable-paper`、账户/行情证明、同日单会话和零仓初始对账要求全部保留。

### 成本感知报价

Paper 启动时通过新增的 `vc_set_quote_economics` C ABI 函数配置以下四个参数，单位均为 **1/10000 USD**：

| 环境变量 | 含义 | 默认值 |
|---|---|---:|
| `PAPER_FEE_PER_SHARE_UNITS` | 每股佣金保守估计 | 100（$0.01） |
| `PAPER_MIN_FEE_UNITS` | 单笔最低佣金保守估计 | 10000（$1.00） |
| `PAPER_SLIPPAGE_BUFFER_UNITS` | 每股滑点缓冲 | 100（$0.01） |
| `PAPER_MIN_NET_EDGE_UNITS` | 每股目标净边际缓冲 | 100（$0.01） |

四项必须是 `0..1,000,000,000` 的整数，Paper 配置中的最低佣金必须大于零。Python 在转换为 `int64` 前校验类型和范围，避免超大整数绕回低成本值；C++ 再次校验边界。默认值是可调整的保守假设，**不是 IBKR 账户费率查询结果**。操作者需依据自己的实际费用方案配置。

为了覆盖最小 **1 股部分成交**，报价半价差的成本下限为：

```text
每股费率 + 完整单笔最低佣金 + 每股滑点缓冲 + 每股目标净边际
```

最低佣金不会按原始大订单数量摊薄，因此即使只成交 1 股也不会在报价模型中摊成几乎为零。该公式采用相加的保守上界；实际账本仍按照执行与佣金回报计算，不会把报价缓冲再次作为真实费用扣除。Rebate 可更新已实现账本，但不会用于缩窄报价。

默认参数对应至少约 **$1.03 的单边成本宽度**（还需 tick 向外取整及其他策略约束），两边报价可能明显远离 BBO；低价股票可能根本无法形成有效正价报价。系统不会为强行成交而自动忽略费用或下调净边际。成交机会可能减少，这也说明 IBKR 小订单的费用结构不一定适合窄价差做市。

成本参数仅在**首次 arm 之前、未 armed 且没有开放风险预留**时设置；首次 arm 成功后在该进程中永久冻结，包括 Kill 之后也不能修改。没有完成成本配置的 C++ 核心不得生成可执行报价或 arm。旧共享库缺少新函数时，Python 明确拒绝启动，必须同时升级适配代码和编译库；ABI v1 结构体布局保持不变。

实际启用时，四项配置以明确单位写入私有 `paper_armed` 审计事件，便于复现报价参数。成本配置不会清除 Kill、释放现有风险预留或绕过账户/行情对账。

这个成本下限是报价决策约束，**不是盈利保证**。行情跳变、逆向选择、未成交另一侧、真实费用偏差及强制降仓仍可能使实际损益为负，原有亏损锁存、库存限额、行情新鲜度和 broker 对账门槛持续生效。

启动必须同时满足：

1. 命令行 `--enable-paper` 与 `IBKR_EXECUTION_MODE=paper`。
2. `IBKR_PAPER_ATTESTED=true`、`IBKR_EXCLUSIVE_ACCOUNT_ATTESTED=true`、`IBKR_DAY_START_ZERO_PNL_ATTESTED=true`、`IBKR_STOCK_TRADING_PERMISSION_ATTESTED=true`、`ITCH_LIVE_SOURCE_ATTESTED=true`；初始当日 PnL 必须由操作者在 broker 侧核实为零，不支持带历史损益启动。
3. 精确匹配配置账户和 Gateway `managedAccounts`；paper DU 前缀只是额外保护，账户模式仍须操作者在 Gateway 核实。
4. 独占 paper 账户、零持仓、零开放订单，且当前可查询执行历史为空。持仓、开放订单和执行查询均收到结束回调。
5. 官方 SDK 必需方法可用、C ABI 版本匹配、价格规则可验证。
6. 指定来源 IPv4 的 live UDP multicast，精确匹配 MoldUDP64 session 和当前美国东部交易日期。
7. 从**会话消息序号 1、System Event O**开始连续接收，观察到匹配股票目录，持续有效的交易状态与新鲜双边订单簿。
8. 应用事件队列已处理、broker 持仓/成交水位与本地账本对齐，账户初始损益已完成受控确认。
9. 当前精确账户的完整、新鲜 USD 资金快照，以及独立股票交易权限核验；卖空保持关闭。
10. 如启用 AI，注册库必须存在真实授权数据与明确 paper 批准，运行 policy 精确匹配；有有效参与信号仍须通过全部账户和 C++ 风控。

**中途启动、历史文件、旧时间戳、未知 session、gap、乱序跨缺口、非 flat 账户均不开放交易。** 当前没有 GLIMPSE 快照引导和缺口恢复，因此需要在交易所会话开始前接入并等待正常交易时段；不能在日中随便启动。行情时间与主机时间须经验证同步，时钟比较失败即停止。

默认执行预检仅加载 SDK 与共享库能力，不打开 broker 或行情连接，也不证明它们健康：

```bash
python -m execution.auto_paper
```

由操作者在完成接入验收后启用 paper；本次开发没有执行以下命令：

```bash
python -m execution.auto_paper --enable-paper \
  --status-file /run/vector-paper-status/status.json
```

配置模板为 `execution/paper.env.example`，通过环境变量或服务管理器注入。`execution/vector-paper.service` 是默认不安装、不启用的服务模板，不包含账号或密钥；配置未就绪时启动失败。不是 Web 登录后自动获得的发单权限。

## Paper 账本与失效行为

- 官方 SDK 回调线程只写有界队列；唯一 owner 线程处理 C++ 状态。回调积压时禁止新增订单，发送前再次检查。
- 每笔发单必须先获得 C++ 风险预留及一次性 permit；SDK 调用异常一律保守标记 Unknown，不自动重发经济意图。
- Broker order ID、client ID、perm ID 联合验证。待提交回报可暂时无 perm ID，但不能据此确认接受或终态。外部订单身份不能释放本地预留。
- 状态回调累计成交领先 execution 时冻结新单；超时进入对账要求。重复状态不能增加持仓。
- 使用完整 execution ID 幂等；成交更正锁存停机并要求人工对账，不当作第二笔新成交。
- FIFO 账本以 1/10000 USD 整数价格记账，长仓按 Bid、短仓按 Ask 估值。每笔执行先计保守佣金，正式佣金替换为总额；重复佣金不会重复扣款。
- Cancel 请求不释放额度，Cancel 确认也必须等待已知累计成交对应的 execution 补齐。迟到成交继续入账，并触发对账锁存。
- 所有应用级 SDK 请求共用消息预算，new/cancel 有独立预算；普通新单保留撤单容量。SDK 连接握手本身的底层帧不由应用桶计数。
- 审计磁盘故障先在内存锁存并安排撤单，不能因为日志错误跳过风险控制。
- SIGTERM、Kill、断线、gap、行情陈旧、PnL 限额、未知订单均停止新单；仍在有界退出期间处理成交和撤单回报。退出明确报告撤单是否已确认和是否 flat，不把断开 socket 当作撤单。
- 不会发送自动平仓市价单。Kill 后可能仍有库存，需操作者处理。
- SQLite journal 仅位于**IBKR 外层适配进程**，使用私有目录、WAL/FULL 和进程锁保存身份及审计；没有进入 C++ 内部风控/订单簿运算，但会增加完整 A 执行链路延迟。此实现不宣称亚毫秒 Tick-to-Send。
- 同一账户/client 的当日执行会话只允许启动一次，避免重启清空当日亏损。非空持仓、未知订单、历史损益接续和同日恢复未实现；不删除状态库来绕过限制。
- 当前损失保护覆盖本执行会话。运营者必须保证独占账户并确认当日初始损益；IBKR 查询历史不完备时，不能宣称已经覆盖全账户日内最大亏损。由其他客户端或人工产生交易时停机对账。

## 只读运营状态

可选 `--status-file` 每秒以 `0600` 原子替换输出 `schema_version=1`、`kind=ibkr-paper`、更新时间、状态、连接、armed、库存、开放订单、PnL、mark_valid、脱敏错误码、Kill 状态、资金门槛是否就绪及可选 AI 信号。资金门槛或 AI WAIT 显示 halted，而非正在报价。文件不含账户号、账户余额、凭据或执行明细。状态写入失败停止新单并尝试撤单。

Web 通过 `PAPER_STATUS_PATH` 只读观察，超过新鲜度阈值显示不可用。此状态与历史策略仿真结果不同，也不构成交易控制入口。

状态文件与账户账本严格分目录：

- `/var/lib/vector-paper/`：SQLite 订单/执行身份、审计与进程锁，仅执行服务使用；**不挂载给 Web 或 Worker**。
- `/run/vector-paper-status/`：只包含脱敏 `status.json`。systemd 模板通过 `RuntimeDirectory` 创建，目录 `0700`、状态文件 `0600`，原子替换；`RuntimeDirectoryPreserve=yes` 保持进程重启时目录 inode 稳定。

Docker 镜像内 backend 使用 UID/GID `10001:10001`。推荐由操作者确认专用 `vectorpaper` 服务账号的数字 UID 与读取方 UID `10001` 一致；不要盲目修改既有主机账号或共享其他宿主目录。只有独立状态目录以只读方式映射给容器，即使 UID 一致也不能因此暴露账户账本目录。

若不同 UID 必须使用受控 ACL／状态发布器，需确保每次 `0600` 新文件原子替换后仍能被授权读取；仅对旧 `status.json` 一次性 `setfacl` 会失效，普通默认 ACL 也不能假定绕过创建模式 `0600` 的限制。这种替代方式需要独立验证，本项目没有自动放宽目录权限或自动安装 ACL。

由操作者在执行服务及专用状态目录均准备好后，给现有部署**可选**叠加只读观察配置：

```bash
docker compose --env-file .env \
  -f docker-compose.yml \
  -f infrastructure/deployment/compose.paper-status.yml \
  up -d --no-deps backend
```

生产部署应保留已有 production/staging override，并将 `compose.paper-status.yml` 放在其后。该 override 仅为 backend 设置 `PAPER_STATUS_PATH=/run/vector-paper-status/status.json` 和只读目录绑定；没有 paper 服务定义、没有账户环境变量、不会启动或启用自动交易。源目录不存在时 `create_host_path=false` 使 Compose 明确失败，避免自动创建不受控目录。

必须挂载**专用状态目录**，而不是单个文件：单文件 bind mount 会在原子替换后继续读取旧 inode。若主机重启或人工删除后重新创建该 RuntimeDirectory，需要重建 backend 容器以重新绑定目录。验证读取权限和 `/execution/` 新鲜度后才视为观察链路完成；缺失、无权限或陈旧状态不能显示为交易健康。

## 真实部署前仍须完成的门槛

1. 验证批准的官方 Python SDK 与实际 Gateway 的版本、回调、签名和 Decimal 行为。Architecture B 原生 C++ 网关仍需独立开发与验收；本 Python 适配不冒充原生 HFT 网关。
2. 真实订单 ID / client ID / perm ID 与内部 ID 的持久映射、单写者分配和重启恢复。
3. C++ 同步风险检查及预留完成后，可信进程内构造 permit；不可接收浏览器提交的“风险已通过”标记。
4. 发出前再次检查 epoch、deadline、连接、账户模式、Kill 与恢复状态。
5. `PendingCancel`、`PendingReplace`、发送结果不明和 ACK 超时继续占用风险额度。
6. 将 SDK 回报规范化并完成部分成交、重复回报、成交更正、佣金延迟和迟到成交测试。
7. 断线后冻结新风险，以真实开放订单、成交和持仓对账；未解决 Unknown 前不启用。
8. Kill 锁存、普通撤单速率和应急额度、外部失效保护均需实测；平台“暂停研究任务”不代表交易所订单已撤销。
9. Paper 写入与 live 权限分离。当前命令行拒绝 Live，C++ gateway 契约也拒绝 Live；没有“设为 true 就实盘”的开关。
10. 完成 staging、受控 paper 会话和恢复演练后，才评审实际发送适配；没有任何配置开关可以跳过这些验收。
11. 实际账户 USD 三字段/End 行为、资金更新周期、买卖预留、超时撤单及股票权限需独立验证；不支持的币种、short、保证金融资和资金循环保持禁用。
12. AI 模式必须使用真实授权数据完成注册库门槛与 operator 审批，验证在线特征/过滤结果、成本压力、模型撤销及超时行为；本地测试模型不构成 Paper 批准或市场有效性证据。

## 本地可重复测试

```bash
python -m unittest discover -s execution/tests -p 'test_*.py' -v
cmake -S execution -B build-execution
cmake --build build-execution
ctest --test-dir build-execution --output-on-failure
```

Python 测试覆盖 SDK 缺失、只读模式、账户/客户端验证、重复和冲突执行、容量上限、私有输出和符号链接拒绝，以及**真实编译的 C++ 共享库**与测试 SDK 边界之间的 reserve→submit→partial fill→fee→cancel→late fill 流程；并覆盖 SDK 发送异常、审计磁盘故障、foreign client/perm ID、callback backlog、累计成交水位、持仓不一致、共享消息预算和状态文件。

成本相关测试还覆盖环境变量解析、负值/超限/小数/布尔拒绝、`ctypes` 整数绕回防护、旧共享库缺少函数时拒绝启动、未配置成本不能 arm、已 armed 时不能修改成本、实际 C++ 报价宽度覆盖保守成本，以及 rebate 不会缩窄报价。

资金测试覆盖完整 USD 快照、请求代次/End、重复较低值、新鲜度、累计购买/费用、未确认撤单预留和多卖单防超卖。AI 测试覆盖未注册/synthetic 拒绝、模型字节 hash 与符号链接、policy 绑定、因果窗口、同时间戳变化、状态时钟回退、TTL 不续期、WAIT 撤单、不变更 C++ 报价、资金不可绕过，以及 150ms 模型验证延迟不得穿过 100ms permit。`tests/test_ml_signal_parity.py` 独立比较在线/离线参与过滤；不声称模拟成交与真实 broker 结果相同。

测试中的注册库审批和 SDK 替身仅在测试目录注入。带 licensed 标签的测试系数明确为虚构工程夹具，缺少真实证据，不能通过生产注册库；它们不是已获批模型。

共享库必须先通过 `cmake -S engine -B build && cmake --build build` 生成；没有共享库时集成测试明确 skip，不能把该次结果当集成验收。测试替身只存在于测试目录。以上结果**不证明真实 IBKR 联调、实际成交、全账户恢复、低延迟或盈利成立**。
