# 美股日内做市低延迟交易系统 — 技术设计文档

| 项目 | 内容 |
|---|---|
| 文档状态 | v0.1 Draft，供架构评审 |
| 日期 | 2026-11-21 |
| 适用范围 | 美股（NMS Stocks）连续交易时段日内做市；C++20/23；Linux |
| 配套代码 | `docs/design/reference/`：本文核心热路径代码的可编译参考实现与单元测试（**非生产代码**） |
| 输入材料 | 需求提示词 +《厚门模型（实战版）》文档（评估结论见 §1.5 与附录 A） |

---

## 0. 文档约定（阅读前必看）

### 0.1 数值标签

本文中的每一个延迟或吞吐数字都带有来源标签。**没有标签的数字不应被引用。**

| 标签 | 含义 | 可信度 |
|---|---|---|
| **【E】Estimated** | 基于公开资料、行业经验和硬件特性的估计；必须在目标硬件上实测 | 只能当作设计目标 / 量级参考 |
| **【M-VM】Measured on dev VM** | 作者在开发容器中实测（4 vCPU 共享云 VM，Xeon 2.1 GHz，**未做隔离核、未调优、合成负载**）。见附录 B | 能证明代码正确、给出量级，**不代表生产性能** |
| **【BD】Broker Dependent** | 由 Broker（IBKR）的基础设施、政策和账户类型决定；本系统无法控制，只能测量 | 必须用自己的账户实测 |
| **【XD】Exchange Dependent** | 由交易所撮合引擎、网关和机房决定 | 必须在机房实测 |

### 0.2 其他约定

- **价格**：`int64_t` 定点数，单位 1e-4 美元，与 ITCH 原生价格精度一致。热路径里不用 `double` 表示订单价格。
- **时间**：热路径计时用 TSC（invariant TSC）换算成 ns；跨机器对时用 PTP。下文提到的 `now_ns` 一律是单调时钟，不是 wall clock。
- **"Hot Path"**：指从行情包到订单包之间、处于同步依赖链上的代码。日志、监控、数据库、Dashboard 一律不在 Hot Path 上。
- **代码**：§6、§9、§10、§11 中的 C++ 片段摘自 `docs/design/reference/`，已用 g++ 13.3 和 clang 18（`-std=c++20 -O2 -Wall -Wextra -Wpedantic`）编译通过。订单簿已与 `std::map` 实现做了 200 万事件的逐笔对拍。
- **收益**：本文只讨论工程实现与风险控制，**不对任何策略的盈利能力做承诺**。做市策略在逆向选择、延迟劣势和极端行情下完全可能持续亏损。

---

## 1. 可行性分析

### 1.1 结论先行

> **"Interactive Brokers API + 真正端到端 < 1 ms" 在工程上不现实。**
>
> - **内部 C++ 链路**（行情包进入用户态 → 策略 → 风控 → 订单管理 → Broker Adapter 调用发送）可以做到 P50 < 300 μs、P99 < 1 ms，而且余量很大。合理实现的软件链路应在 **个位数到几十微秒**【E】。这一目标在 Architecture A 和 B 中**都适用，也都能达成**。
> - **端到端 Tick-to-Trade / Tick-to-ACK**：只要订单经过 IBKR，就会被 **TWS/IB Gateway 中间件、公网/VPN、IBKR 内部风控与路由**主导，量级是 **毫秒到数百毫秒**【BD】。这部分不在本系统控制范围内，**不能计入内部延迟预算后宣称系统"满足亚毫秒"**。
> - 真正的端到端亚毫秒甚至微秒级，需要 **Architecture B**：交易所机房托管（Colocation）+ 直连行情 + 原生订单协议（OUCH）+ 会员资格或 Sponsored Access + 内核旁路网卡。

### 1.2 IBKR 路径的瓶颈逐项分析

| # | 瓶颈 | 说明 | 量级 / 约束 | 标签 |
|---|---|---|---|---|
| 1 | **TWS / IB Gateway 中间件** | TWS API 并不直接连到 IBKR 服务器。客户端先通过 TCP 连到本地运行的 TWS 或 IB Gateway（Java 程序），再由它转发到 IBKR。API 协议是基于字段的文本编码，Gateway 内部的处理和 JVM GC 都会带来抖动 | 亚毫秒到数毫秒；GC 配置不当时尾延迟可到数十毫秒以上 | 【BD】【E】 |
| 2 | **公网 / VPN 物理距离** | 光纤中光速约 5 μs/km。1 ms 单程只够走约 200 km 光纤，这还没算路由器、排队和绕路 | 纽约/新泽西同城 RTT ≈ 1–5 ms；美国跨区 20–80 ms；中国/亚洲到美东 150–250+ ms | 【E】 |
| 3 | **IBKR 内部基础设施** | 账户级风控、保证金检查、SmartRouting 决策、到交易所的路由；IBKR 不公开延迟 SLA | 毫秒级，分布未知 | 【BD】 |
| 4 | **API 消息速率限制** | IBKR 文档规定客户端到 TWS 的消息上限约 **50 条/秒**，超限会触发 pacing violation，甚至断开连接。下单、改单、撤单都要占用这个额度 | **这是做市最硬的约束**：10 只股票双边报价，每侧每秒改价 1 次就要 20 条/秒 | 【BD】（以 IBKR 当前文档为准） |
| 5 | **撤单/报单比监控** | IBKR 会监控 Order Efficiency Ratio 等指标，过度撤改单可能导致账户受限或产生费用 | 阈值以 IBKR 当前政策为准 | 【BD】 |
| 6 | **行情能力** | IBKR 的 `reqMktData` 是聚合/采样的行情，不是逐笔；`reqMktDepth`（L2）有并发订阅数量限制；IBKR 不提供 NASDAQ TotalView-ITCH 原始组播 | 不能用来构建 order-level 订单簿；ITCH 需要单独向 NASDAQ 或数据供应商取得授权和物理接入 | 【BD】 |
| 7 | **路由与队列不可控** | 默认 SMART 路由会把订单发到任意交易场所，因此无法估计自己的队列位置。可以指定路由（Directed，如 NASDAQ/ISLAND），但费用和可用性因账户而异 | 队列位置模型在 A 中基本失效 | 【BD】 |
| 8 | **断线保护** | 需要确认 IBKR API 订单是否有交易所级 Cancel-on-Disconnect 等价物。若没有，断线期间挂单会继续留在市场 | 是 A 的主要残余风险，必须通过独立通道撤单 | 【BD】 |
| 9 | **账户规则** | 卖空需要 Locate/可借券；保证金账户日内交易规则（PDT 等）；做市商豁免（Reg SHO bona fide market making）不适用于普通客户 | 影响卖出侧报价能力 | 【BD】 |

**由此得出的关键推论：**

1. 第 4 条（约 50 msg/s）决定了 A 只能做**低频报价刷新**（秒级）和**少量标的**的做市原型。这不是高频做市。
2. 第 6 条意味着 A 的行情要么来自 IBKR 的低保真数据，要么单独采购 ITCH（或其他直连/聚合行情）。ITCH 无法通过公网组播接收：现实做法是用数据供应商的专线、交叉连接，或用 SoupBinTCP 版本的 ITCH 走广域网。
3. 第 1–3 条决定了 A 的 Tick-to-ACK 是毫秒级到百毫秒级，并且**分布未知，必须自己测量**（§15.6 给出了测量方法）。

### 1.3 两套架构的定位

**Architecture A — IBKR 可实现版本**

- 用途：策略研发、确定性回放、仿真、Paper Trading、低频/中频实盘、高频策略的**逻辑**原型验证（不验证延迟竞争力）。
- 部署：交易引擎和 IB Gateway 部署在美东（纽约/新泽西）的同一台主机，通过 localhost TCP 通信。**不要从中国/亚洲直连**，否则单程网络延迟就有 75–125 ms 以上【E】。
- 行情：可插拔，分三种来源：(a) 历史 ITCH 文件回放；(b) 数据供应商提供的实时 ITCH 或直连行情；(c) IBKR 行情（降级模式，只有 L1/L2）。
- 能做的策略形态：几只到十几只流动性好的股票、报价刷新间隔约秒级、价差相对更宽、以库存控制为核心。

**Architecture B — 真正低延迟 HFT 版本**

- 用途：生产级高频做市。
- 部署：在 NASDAQ 数据中心（新泽西 Carteret）托管机柜；ITCH 走 MoldUDP64 组播（A/B 双路）；订单走 OUCH over SoupBinTCP；以交易所会员身份接入，或通过 Sponsored Access 接入（须满足 SEC Rule 15c3-5 Market Access Rule 的事前风控要求）。
- 硬件：内核旁路网卡（如 AMD Solarflare 的 ef_vi/Onload，或 Exablaze/Cisco 系列）、Layer-1 交换机、PTP 授时、硬件时间戳抓包。
- **必须认识到**：美股是多交易所市场。只接 NASDAQ ITCH 只能看到 NASDAQ 一个场所的订单簿，拿不到全国最优价（NBBO）。Reg NMS 禁止锁定或交叉市场，所以生产级做市还需要 SIP 或其他交易所直连行情（NYSE、Cboe、IEX、MEMX 等）。本文以 NASDAQ 为主线展开，但所有接口都按多场所设计。
- **竞争现实**：顶级做市商使用 FPGA，tick-to-trade 在亚微秒级。纯软件的 B（几微秒）仍处于速度劣势，这种劣势会以逆向选择成本的形式体现。B 能否赚钱取决于策略和成本结构，而不是"做到了低延迟"本身。

### 1.4 A vs B 对比

| 维度 | Architecture A（IBKR） | Architecture B（Colo + DMA） |
|---|---|---|
| 内部链路延迟（包进入用户态 → 订单交给网络：A 为 T1→T8′，B 为 T1→T8，定义见 §4.1） | P50 ≈ 20–50 μs，P99 < 300 μs（主机未深度调优，含 IBKR Adapter 编码与 send()）【E】 | P50 ≈ 2–4 μs，P99 ≈ 5–15 μs（软件 + 内核旁路，含网卡发送）【E】 |
| 端到端 Tick-to-ACK | 美东同城部署 ≈ 数 ms 到数十 ms；亚洲 ≈ 数百 ms【BD】【E】 | 线上到线上 ≈ 数十 μs 量级（以撮合 ACK 为主）【XD】【E】 |
| 订单吞吐 | 受 API 限速约 50 msg/s（全账户共享）【BD】 | 受交易所端口限速和自身风控限制，通常数千到数万 msg/s/端口【XD】 |
| 行情保真度 | 依赖外部 ITCH 源；IBKR 行情为采样数据 | ITCH 逐笔 order-level，硬件时间戳 |
| 稳定性 / 抖动 | 公网和 Java Gateway 抖动大；IB Gateway 有每日重启和维护窗口【BD】 | 机房内网络确定性高；主要抖动来自自身软件和交易所拥塞 |
| 订单控制 | 间接：SMART 路由、改单语义由 IBKR 决定、无队列可见性 | 直接：OUCH 原生的改单、部分撤单（保留优先级）、Post-Only、Cancel-on-Disconnect、交易所 Kill Switch |
| 风控责任 | 本系统 + IBKR 账户级风控 | 本系统 + 15c3-5 事前风控（自有或 Sponsor 提供）+ 交易所级保护 |
| 监管 / 资质 | 普通券商客户 | 需要 Broker-Dealer 会员资格或 Sponsored Access 协议、合规体系 |
| 部署成本（年化） | 低：IBKR 账户 + 行情订阅 + 美东 VPS/裸金属，数百到数千美元/月【E】 | 高：机柜、交叉连接、端口费、行情费（含非展示用途费）、网卡/交换机、会员/Sponsor 费用、合规人员；通常数十万到数百万美元/年【E】（以交易所和供应商报价为准） |
| 研发重用 | 核心代码与 B 共享（Strategy / Risk / OMS / Book） | 替换 MD 接入层和 Execution Gateway 即可 |

### 1.5 关于上传的《厚门模型》

《厚门模型》是一套基于历史关键高低点、0.65 幂次迭代"共振"、起爆点公式 `(底部值-锚点)×10+锚点` 和双轨防守底的**多日波段**价位模型。文档自己写明其适用于 A 股、1 天至 3 周的波段，并且**"不适配日内交易"**。评估结论如下（详见附录 A）：

1. **它不能作为高频做市的 Hot Path 信号。** 它的输入是日线级别的拐点，输出是一组静态价位，每天最多更新一次。做市的报价决策需要的是毫秒到秒级的盘口信息。
2. **共振筛选在统计上区分度很低。** 我做了零假设检验：用**纯随机游走**价格（完全不含结构）生成拐点，再按文档规则筛选，有 **82%** 的样本至少找到 1 个"强共振空间对"，平均 1.85 个。换成 0.60 / 0.618 / 0.70 等系数，结果同样普遍【M-VM，蒙特卡洛 2000 次】。因此"找到共振"本身不构成证据。文档中"经万次历史回测验证的最优系数 0.65"的说法没有给出可复现的数据，而且文档末尾注明"部分内容可能由 AI 生成"。
3. **工程上的处理：** 把它实现为**可选的、Hot Path 之外的 `LevelContextEngine`**。每天开盘前用前一日数据算出价位表，以只读快照的形式发布给策略。策略只能以**有上限的方式**使用它：例如接近关键价位时加宽价差、缩小报价量，或在突破确认后给保留价一个有界偏移（不超过 N tick）。它**不能**改变任何风控限额。上线前必须通过附录 A.5 的显著性检验，未通过就保持关闭。
4. 文档中的"单资产持仓不超过总资金 20%"可以作为单标的名义敞口的**绝对上限**。但做市的实际库存限额应远低于这个值（见 §9.2 库存风险模型）。

### 1.6 推荐演进路线

```
Phase 0  确定性回放 + 回测（历史 ITCH 文件） ── 验证订单簿正确性、策略逻辑、风控
   │
Phase 1  Architecture A，Paper Trading ─────── 验证 OMS 状态机、断线恢复、实测 IBKR 延迟分布
   │
Phase 2  Architecture A，小资金实盘 ─────────── 低频报价，验证 PnL / 费用核对、Kill Switch 演练
   │
Phase 3  Architecture B（Colo + OUCH） ──────── 同一套 Strategy / Risk / OMS / Book 代码，替换接入层
```

Phase 0 到 Phase 3 共用同一套核心库。差异只在 `IMarketDataSource` 和 `ExecutionGateway` 的实现，以及部署配置。

---

## 2. 系统整体架构

### 2.1 总体结构（以 Architecture B 为基准）

```
┌───────────────────────────────────────── EXTERNAL ─────────────────────────────────────────┐
│  NASDAQ TotalView-ITCH 5.0     GLIMPSE 5.0        MoldUDP64        Other venues / SIP       │
│  (MoldUDP64 multicast A/B)     (snapshot, TCP)    Re-request (UDP) (NBBO, Reg NMS)          │
│                                                                                            │
│  Order entry:  NASDAQ OUCH (SoupBinTCP)  |  FIX  |  IBKR TWS API (via IB Gateway)          │
└──────┬──────────────────────────────────────────────────────────────────────▲─────────────┘
       │ UDP multicast (kernel bypass RX)                                       │ TCP (kernel bypass TX)
┌──────▼──────────────────────────────────────────────────────────────────────┴─────────────┐
│                      TRADING PROCESS  — single binary, hot path in-process                │
│                                                                                           │
│  ┌──────────────────────── Core 2: MARKET DATA GATEWAY (1 thread, busy poll) ───────────┐ │
│  │ RX Feed A ─┐                                                                          │ │
│  │ RX Feed B ─┴─► Line Arbitration ─► MoldUDP64 Sequencer ─► ITCH Decoder ─► Locate     │ │
│  │                 (first-wins)       (gap detect / reorder   (zero-copy,      Filter &   │ │
│  │                                     / re-request)           64B event)     Shard Route│ │
│  └───────────────────────────────────────────────┬──────────────────────────────────────┘ │
│                                                  │ SPSC ring<MarketEvent> per shard        │
│                  ┌───────────────────────────────┼───────────────────────────────┐         │
│                  ▼                               ▼                               ▼         │
│  ┌─ Core 3: TRADING SHARD 0 ───────────────┐ ┌─ Core 4: SHARD 1 ─┐       ┌─ Core N ─┐       │
│  │ run-to-completion, shared-nothing        │ │      (same)       │  ...  │  (same)  │       │
│  │                                          │ └───────────────────┘       └──────────┘       │
│  │ Order Book Engine (L3 → L2 → TOB)        │                                                │
│  │   ▼                                      │                                                │
│  │ Market State Engine (status/halt/LULD/   │                                                │
│  │   staleness/feed-lag)                    │                                                │
│  │   ▼                                      │                                                │
│  │ Signal Engine (microprice, imbalance,    │                                                │
│  │   trade flow, vol, momentum)             │                                                │
│  │   ▼                                      │                                                │
│  │ MM Strategy → Pricing Engine → Inventory │                                                │
│  │   ▼ desired quotes                       │                                                │
│  │ Quote Reconciler (desired vs live)       │                                                │
│  │   ▼ OrderRequest                         │                                                │
│  │ PRE-TRADE RISK (synchronous, mandatory)  │                                                │
│  │   ▼                                      │                                                │
│  │ Order Manager (state machine, cl_ord_id) │                                                │
│  │   ▼                                      │                                                │
│  │ Execution Gateway<OUCH> encode + send ───┼──► NIC TX                                     │
│  │   ▲                                      │                                                │
│  │ ExecReport poll ◄────────────────────────┼─── NIC RX (order entry session)               │
│  │   ▼                                      │                                                │
│  │ OMS update → Position → PnL → KillSwitch │                                                │
│  │   (all synchronous, same thread)         │                                                │
│  └──────────┬───────────────────────────────┘                                                │
│             │ SPSC rings: log records / telemetry / state snapshots (never block)            │
│  ┌──────────▼────────────── NON-HOT PATH (other cores, same process) ──────────────────────┐ │
│  │ Core 7: Async Logger (binary journal)   Core 8: Telemetry / Histogram merge             │ │
│  │ Core 1: Risk Coordinator (cross-shard budgets, global PnL), Supervisor / Control channel│ │
│  │         Configuration Manager (versioned snapshots, RCU-style swap)                     │ │
│  └──────────┬──────────────────────────────────────────────────────────────────────────────┘ │
└─────────────┼────────────────────────────────────────────────────────────────────────────────┘
              ▼ async, out of process
   Monitoring & Alerting (Prometheus/Grafana/PagerDuty)   Historical store (journals, pcap)
   Independent Watchdog process (heartbeat, out-of-band cancel-all / exchange kill switch)
   Replay / Backtest / Simulation cluster (same core libraries, offline)
   Level Context Engine (厚门, pre-market batch) ──► immutable daily snapshot ──► Strategy (read-only)
```

### 2.2 Architecture A 的差异部分

```
                      ┌──────────────── Trading Process (美东主机) ────────────────────┐
 ITCH vendor feed ───►│ MD Source (pluggable):                                          │
 (SoupBinTCP/专线)     │   ItchTcpSource | ItchFileReplay | IbkrMarketDataSource(降级)   │
 IBKR market data ───►│        │ SPSC                                                    │
                      │        ▼                                                         │
                      │ Trading Shard (single shard): Book→Signal→Strategy→Risk→OMS     │
                      │        │ SPSC<OrderRequest>          ▲ SPSC<ExecutionReport>      │
                      │        ▼                             │                            │
                      │ IBKR Adapter thread  (IbkrGateway : ExecutionGateway)             │
                      │   - IBKR C++ API (EClientSocket / EReader) 隔离在本线程            │
                      │   - 全局 50 msg/s 限速器 (SlidingWindow + 优先级: cancel 优先)     │
                      │   - orderId ↔ cl_ord_id 映射, execId 去重, 断线重连与对账         │
                      └────────┬──────────────────────────────────────────────────────────┘
                               │ localhost TCP (TWS API)
                      ┌────────▼────────┐   Internet / VPN   ┌──────────────┐     ┌───────────┐
                      │ IB Gateway (JVM)│ ─────────────────► │ IBKR servers │ ──► │ Exchanges │
                      └─────────────────┘   【BD】            │ risk/routing │     └───────────┘
                                                              └──────────────┘
```

IBKR 的 C++ API 内部使用 `std::string`、动态分配和阻塞 socket，**不能**运行在交易线程中。它被隔离在独立的 Adapter 线程里，通过两条 SPSC 队列与交易线程交互。因为 IBKR 链路本身是毫秒级，这一跳增加的几百纳秒可以忽略。

### 2.3 设计原则

1. **Hot Path 全部在进程内**：不使用 REST、JSON、RPC、数据库或消息中间件。线程间只用 SPSC 共享内存队列，每个状态只有一个写线程（single writer）。
2. **按标的分片、各分片互不共享（shared-nothing）**：每个交易分片线程独占一组标的的订单簿、策略、库存和订单状态，从而没有锁，也没有跨核写共享。
3. **风控同步执行，并且失败即拒绝（fail-closed）**：每笔订单在同一线程里先过 `pre_trade_check()` 才能发出。风控无法判断时一律拒绝。撤单永远放行。
4. **状态收敛模型**：策略只输出"期望报价"，由 Quote Reconciler 将期望状态与实际订单状态对比，生成最少的动作（新单、改单或撤单），并遵守"每个报价槽位同时最多一个在途请求"。
5. **多层防线**：进程内风控 → 进程外 Watchdog → Broker/Sponsor 风控 → 交易所级保护（Cancel-on-Disconnect、Kill Switch）→ 人工。
6. **确定性**：策略只读取事件流中的时间戳（可注入的 Clock），因此同一输入序列必然产生同一决策序列（见 §15.1）。

---

## 3. 模块职责

| 模块 | 职责 | 线程 | Hot Path | 输入 | 输出 |
|---|---|---|---|---|---|
| Market Data Gateway | 组播 RX（内核旁路轮询）、A/B 线路仲裁、硬件时间戳读取 | MD 核 | ✅ | UDP 包 | MoldUDP64 包 + rx_ts |
| Sequencer | MoldUDP64 序号校验、乱序重排（小窗口）、缺口检测、Re-request、GLIMPSE 快照恢复编排 | MD 核 | ✅ | MoldUDP64 包 | 按序 ITCH 消息；缺口事件 |
| ITCH Decoder | 解析 ITCH 5.0 消息（大端、零拷贝），按 Stock Locate 过滤并路由到分片 | MD 核 | ✅ | ITCH 消息字节 | `MarketEvent`（64B） |
| Order Book Engine | Order-level（L3）订单簿：Add/Replace/Cancel/Delete/Execute；维护 L2 价位和 O(1) 最优价 | 分片核 | ✅ | `MarketEvent` | 订单簿状态、TOB 变化标志 |
| Market State Engine | 交易状态（H/P/Q/T）、LULD、Reg SHO（SSR）、行情新鲜度、行情延迟（exch_ts vs PTP）、订单簿有效性 | 分片核 | ✅ | `MarketEvent`、系统事件 | `SymbolRisk.flags`、MarketSnapshot |
| Signal Engine | 增量计算 microprice、OBI、交易流不平衡、EWMA 波动率、短期动量、深度 | 分片核 | ✅ | 订单簿、成交事件 | `Features` |
| Market Making Strategy | 报价状态机（Quoting / OneSided / Pulled / Disabled）、行情过滤、刷新节奏 | 分片核 | ✅ | Features、Inventory、风控状态 | 期望报价（desired quotes） |
| Pricing Engine | Fair Price、保留价、价差、tick 对齐、不穿价 | 分片核 | ✅ | Features、库存 | `QuotePair` |
| Inventory Manager | 库存分区（Normal/Warning/Critical/Breach）、偏斜参数、单侧阻断标志 | 分片核 | ✅ | 成交、持仓 | 偏斜系数、`kSymBuy/SellBlocked` |
| Risk Engine (pre-trade) | 同步事前风控，详见 §9 | 分片核 | ✅ | `OrderRequest`、`RiskState` | `RiskVerdict` |
| Risk Coordinator | 跨分片的全局额度（Gross/Net/Loss）切片与再平衡、全局 PnL 汇总 | 控制核 | ❌（异步，有界延迟） | 各分片快照 | 预算更新、全局阻断标志 |
| Order Manager (OMS) | 订单状态机、cl_ord_id 生成、在途追踪、超时、ACK/Fill 处理、去重 | 分片核 | ✅ | 风控通过的请求、ExecReport | 网关调用、订单状态 |
| Execution Gateway | 统一接口 `ExecutionGateway`：OUCH / FIX / IBKR / Sim 的实现 | 分片核（OUCH）/ 独立线程（IBKR） | ✅ | `OrderRequest` | 协议报文；归一化 `ExecutionReport` |
| Position Manager | 持仓、均价、开放买/卖量；与 Broker 对账 | 分片核（实时）+ 控制核（对账） | ✅（成交更新） | ExecReport | `Position` |
| PnL Engine | 已实现/未实现 PnL、费用/返佣估算与核对、回撤 | 分片核 | ✅（成交与盯市增量） | 成交、TOB | `PnLState` |
| Kill Switch | 日内亏损/回撤熔断，锁存，只能由 Supervisor 复位 | 分片核（触发）+ 控制核（复位） | ✅（触发） | PnL、风险事件 | 全局阻断、Cancel-All |
| Supervisor / Control | 受认证的控制通道：启用/停用、Kill Switch 复位、参数变更审批 | 控制核 | ❌ | 运维指令 | 控制命令（SPSC） |
| Monitoring | 告警规则、健康状态、看板 | 进程外 | ❌ | Telemetry | 告警 |
| Telemetry | 计数器、HDR 直方图、各阶段延迟采样；汇总后导出 | Telemetry 核 | ❌ | 各线程无锁计数器 | 指标流 |
| Logging System | 二进制异步日志；审计记录不可丢 | Logger 核 | ❌（入队在 Hot Path，约 20–50 ns） | SPSC 日志记录 | 日志文件 / 日志库 |
| Replay Engine | 原始包/事件回放；确定性回放策略与风控 | 离线 | ❌ | pcap / 事件日志 | 决策流、对比报告 |
| Backtest Engine | 历史 ITCH + 撮合模拟 + 延迟模型 | 离线 | ❌ | ITCH 文件、参数 | PnL、markout、成交统计 |
| Simulation Engine | 交易所撮合模拟器（队列位置）、Broker 模拟器（延迟/拒单/断线注入） | 离线 / 测试 | ❌ | 订单、行情 | ExecReport |
| Configuration Manager | 版本化配置、不可变快照、启动时校验、受控热更新（仅非结构参数） | 控制核 | ❌ | 配置仓库 | 配置快照指针 |
| Level Context Engine（厚门，可选） | 盘前用日线计算价位表；每日不可变快照 | 盘前批处理 | ❌ | 日线 OHLCV | `LevelSnapshot`（只读） |
| Independent Watchdog | 独立进程：心跳监控、带外 Cancel-All、触发交易所 Kill Switch | 独立进程/主机 | ❌ | 心跳、券商连接 | 撤单 / 告警 |

---

## 4. 延迟预算

### 4.1 测量点定义（所有延迟数字都以这些点为准）

| 点 | 定义 | 采集方式 |
|---|---|---|
| T0 | 触发行情包到达网卡（线上） | NIC 硬件 RX 时间戳 / 镜像口抓包 |
| T1 | 用户态拿到包 | 轮询返回后立刻读 TSC |
| T2 | `MarketEvent` 写入分片队列 | TSC |
| T3 | 分片线程取出事件 | TSC |
| T4 | 订单簿更新完成 | TSC |
| T5 | 策略决策完成（期望报价确定） | TSC |
| T6 | 事前风控通过 | TSC |
| T7 | OMS 构造订单完成，调用 Gateway 发送 | TSC |
| T8 | 订单包离开网卡（线上） | NIC 硬件 TX 时间戳 / 抓包 |
| T9 | 交易所 ACK 到达网卡 | 抓包 |
| T8′ | （A）`EClient::placeOrder()` 返回 | TSC |
| T9′ | （A）收到 `orderStatus(Submitted)` / `openOrder` 回调 | TSC |
| T10′ | （A）收到 `execDetails` 回调 | TSC |

### 4.2 七个延迟段：严格区分可控与不可控

| # | 延迟段 | 测量 | Architecture A | Architecture B | 可控性 |
|---|---|---|---|---|---|
| 1 | Market Data → Strategy Decision | T1→T5 | P50 5–20 μs / P99 < 100 μs【E】 | P50 0.5–1.5 μs / P99 < 5 μs【E】 | ✅ 系统可控 |
| 2 | Strategy → Risk Check | T5→T6 | < 1 μs【E】 | 30–100 ns【E】；risk 本体约 36–48 ns P50【M-VM】 | ✅ |
| 3 | Risk → Order Manager | T6→T7 | < 2 μs【E】 | 50–150 ns【E】 | ✅ |
| 4 | Order Manager → Broker Adapter（应用层） | T7→T8′（A）/ T7→T8（B） | 跨线程 + TWS API 编码 + send()：P50 10–30 μs / P99 < 200 μs【E】 | 编码 + 内核旁路 TX：P50 1–2 μs / P99 3–5 μs【E】 | ✅ |
| **内部合计** | **段 1–4** | T1→T8′ / T1→T8 | **P50 ≈ 20–50 μs，P99 < 300 μs**【E】→ **满足 P50<300 μs / P99<1 ms 的目标** | **P50 ≈ 2–4 μs，P99 ≈ 5–15 μs**【E】 | ✅ |
| 5 | Broker API / Internet / Broker Infra | T8′→（交易所） | localhost→IB Gateway(JVM)→公网/VPN→IBKR 风控路由：**毫秒到数百毫秒，分布未知** | Sponsor 风控网关（若有）：硬件亚微秒到数微秒，软件可达数十微秒 | ❌【BD】 |
| 6 | Exchange Acknowledgement | 交易所处理往返 | 包含在段 5 的观测中，无法单独分离 | T8→T9：**数十微秒量级**，拥塞时有尾部【XD】 | ❌【XD】 |
| 7 | 真正端到端 | T0→T8（Tick-to-Trade）；T0→T9 / T0→T9′（Tick-to-ACK） | Tick-to-Trade 无法定义到交易所线上；Tick-to-ACK ≈ **数 ms 到数百 ms**【BD】 | Tick-to-Trade（线上到线上）≈ **3–15 μs**；Tick-to-ACK ≈ **数十 μs**【E】【XD】 | 部分可控 |

> **结论**：P50 < 300 μs、P99 < 1 ms 的目标**只适用于段 1–4（内部链路）**。在 A 中，端到端时间被段 5 主导；在 B 中，内部链路应以更严格的 **P99 < 15 μs（线上到线上）** 作为工程目标，因为 300 μs 在 Colo 环境中毫无竞争力。

### 4.3 逐模块延迟预算 — Architecture B（软件 + 内核旁路，调优后的裸金属，热缓存）

| Stage | P50 | P99 | Worst Case | Controllable | 关键路径 | Optimization |
|---|---|---|---|---|---|---|
| NIC → Userspace | 0.8–1.2 μs【E】 | 2 μs【E】 | 10+ μs（PCIe 重放、SMI） | ✅（选型 + 调优） | ✅ | ef_vi/Onload 或 DPDK 轮询；网卡与线程同 NUMA；关闭中断合并；BIOS 关闭 SMI 源 |
| MD 核 → 分片核（SPSC 一跳） | 80–150 ns【E】（VM 实测 290–400 ns【M-VM】） | 300 ns【E】 | 数 μs | ✅ | ✅ | 同 socket；head/tail 分离缓存行；本地缓存对端索引；批量 pop |
| ITCH Decode | 10–20 ns【E】（吞吐 11.6–13.2 ns/msg【M-VM】） | 50 ns | 100 ns | ✅ | ✅ | 零拷贝、`memcpy` + `bswap` 读大端、`switch` 跳转表、不认识的消息直接跳过 |
| Sequencing | 5–10 ns | 30 ns | 有缺口时进入恢复流程（ms–s，此时停止报价） | ✅ | ✅ | 正常路径只做一次比较；乱序缓冲预分配 |
| Order Book Update | 50–150 ns【E】（VM 合成随机负载 P50 ≈ 200 ns【M-VM】） | 300–500 ns【E】（VM ≈ 500–590 ns【M-VM】） | 1–2 μs（TLB/缓存缺失、溢出路径） | ✅ | ✅ | 开放寻址哈希 + 对象池 + 平铺价位梯 + 位图；HugePages；预取 |
| Feature Calculation | 50–200 ns | 500 ns | 1 μs | ✅ | ✅ | 只在 TOB 或成交变化时增量更新；EWMA 用预计算的衰减系数 |
| Strategy Decision | 100–300 ns | 1 μs | 2 μs | ✅ | ✅ | 无虚函数；状态机用查表；冷路径（Pulled/Disabled）挪出热循环 |
| Pricing | 50–100 ns（`compute_quotes` ≈ 66–71 ns【M-VM】） | 300 ns | 500 ns | ✅ | ✅ | 不用 `std::pow`/`std::exp`（预计算表）；tick 对齐用整数运算 |
| Inventory Adjustment | 10–30 ns | 50 ns | 100 ns | ✅ | ✅ | 分区阈值预计算为整数；状态只在成交时更新 |
| Risk Check | 30–60 ns（`pre_trade_check` ≈ 36–48 ns P50【M-VM】） | 100 ns（VM ≈ 39–60 ns【M-VM】） | 200 ns | ✅ | ✅ | 单缓存行 `SymbolRisk`；全局阻断位一次 load；[[unlikely]]；令牌只窥视不扣减 |
| Order Construction（OMS + 编码） | 50–150 ns | 300 ns | 500 ns | ✅ | ✅ | 预填充报文模板，只改写价格/数量/token 字段；cl_ord_id 中编码对象池下标 |
| Broker Adapter（原生网关，同线程） | 20–50 ns | 100 ns | — | ✅ | ✅ | 静态分发（模板/concept），无虚调用 |
| Network Send | 1–2 μs（到线上）【E】 | 3–5 μs | 20+ μs（TCP 重传/窗口） | ✅（部分） | ✅ | TCPDirect / Onload / ef_vi；预热连接；Nagle 关闭 |
| Broker / Sponsor Processing | 硬件风控 < 1 μs；软件 10–50 μs | — | — | ❌【BD】 | ✅ | 选择硬件风控的 Sponsor 或自建会员接入 |
| Exchange ACK | 数十 μs | 拥塞时显著上升 | 开盘/收盘/新闻时尾部大 | ❌【XD】 | ✅ | 无法优化，只能测量并纳入策略的延迟模型 |
| Async Log enqueue | 15–30 ns | 60 ns | 队列满→fail-safe | ✅ | ⚠️ 旁路 | 写入 POD 记录；不做格式化 |

### 4.4 逐模块延迟预算 — Architecture A（IBKR，美东主机，内核网络栈）

| Stage | P50 | P99 | Worst Case | Controllable | 关键路径 | Optimization |
|---|---|---|---|---|---|---|
| MD 源 → 主机（ITCH 供应商 / IBKR 行情） | 视供应商与距离：同城 0.1–2 ms【E】 | — | 秒级（断流） | ❌（外部） | ✅ | 主机放在纽约/新泽西；选择延迟稳定的供应商 |
| NIC → Userspace（内核 TCP/UDP） | 10–30 μs【E】 | 50–100 μs | ms 级（VM steal、软中断） | 部分 ✅ | ✅ | 裸金属或独占实例；busy-poll（`SO_BUSY_POLL`）；IRQ 亲和 |
| Decode / Book / Signal / Strategy / Pricing / Risk / OMS | 合计 2–5 μs【E】 | 20–50 μs | 100+ μs | ✅ | ✅ | 与 B 相同的代码；主机调优程度较低 |
| 分片 → IBKR Adapter 线程（SPSC） | 0.2–1 μs | 5 μs | 50 μs | ✅ | ✅ | 绑核、自旋等待 |
| IBKR API 编码 + `send()` | 5–20 μs【E】 | 50–100 μs | ms | ✅（部分） | ✅ | 隔离在 Adapter 线程；预热；Nagle 关闭 |
| **全局限速排队** | 0（额度充足时） | **可达 1 s** | 数秒 | ✅（策略层面） | ✅ | 优先级：撤单 > 减仓 > 防御性改价 > 进攻性改价 > 新单 |
| localhost → IB Gateway（JVM） | 0.1–1 ms【BD】【E】 | 数 ms | GC：数十–数百 ms | ⚠️（JVM 参数可调） | ✅ | 固定堆、预分配；低停顿 GC；绑核 |
| IB Gateway → IBKR（公网/VPN） | 同城 1–5 ms RTT；亚洲 150–250+ ms【E】 | — | 丢包重传、路由变化 | ❌ | ✅ | 地理位置；稳定的线路 |
| IBKR 风控 / 路由 / 到交易所 | 【BD】未知 | — | — | ❌ | ✅ | 只能测量 |
| ACK 回到客户端（T9′） | **总计约数 ms 到数十 ms（美东）**【BD】【E】 | — | 秒级 | ❌ | ✅ | 测量 T8′→T9′ 的分布，按时段分桶 |

### 4.5 预算管理规则

1. 每个阶段在生产环境持续采样（对每笔订单记录 T1…T7 的 TSC 差值，写入每线程 HDR 直方图）。
2. 内部预算告警：段 1–4 的 P99 连续 1 分钟超过预算的 2 倍 → 告警；超过 5 倍 → 设置 `kBlkLatency`（停止新报价，只允许撤单）。
3. 外部延迟（段 5–6）只做测量和策略输入（例如 A 的延迟越高，最小报价寿命越长、价差越宽），**不计入**内部 SLA。

---

## 5. CPU / Thread Architecture

### 5.1 并发模型选型

| 模型 | 是否采用 | 用在哪里 | 理由 |
|---|---|---|---|
| **Single Thread Event Loop（run-to-completion）** | ✅ **核心** | 每个交易分片 | 订单簿、特征、策略、风控、OMS 状态全部留在同一核的 L1/L2 中，没有跨核一致性流量、没有锁、也没有队列跳转。时序天然确定，便于回放 |
| **Pipeline Architecture** | ⚠️ 只保留一级 | MD 网关 → 分片 | 解码和过滤全市场行情的工作量与分片计算解耦；一跳 SPSC 的代价约 100 ns【E】，换来的是吞吐和隔离。**不**把 Book / Strategy / Risk 拆成多级流水线：每多一级就多一次缓存行迁移，订单簿数据还会在两核间来回传递 |
| **Shared Nothing** | ✅ | 分片之间 | 每个分片独占一组标的；跨分片的全局限额用"预算切片"实现（§9.4），分片之间没有共享的可写状态 |
| **SPSC Queue** | ✅ 主力 | 所有线程间通信 | 单生产者单消费者只需 acquire/release，无需 CAS。实现见 §6.9 |
| **Lock-Free Ring Buffer** | ✅ | SPSC 的实现形态；日志环 | 预分配、2 的幂容量、无动态分配 |
| **Disruptor** | ⚠️ 可选 | MD 广播给多个消费者（分片 + 行情录制器） | 单生产者多消费者共享一个环，各消费者维护自己的序号，可省去 N 份拷贝。若消费者数 ≤ 3，"每个消费者一条 SPSC"更简单，两者都可以 |
| **MPMC Queue** | ❌（Hot Path） | 仅控制平面 | CAS 竞争导致尾延迟不可控；Hot Path 中不存在真正的多对多需求 |
| **Actor Model** | ❌（Hot Path） | 可用于控制平面/工具 | 通用 actor 框架通常伴随动态调度、邮箱分配和虚分发。本设计的分片线程本质上就是"一个绑核的 actor"，但用静态结构实现 |
| Mutex / CondVar / sleep | ❌（Hot Path） | 仅启动/关闭阶段 | 阻塞和上下文切换会带来 μs 到 ms 级抖动 |

### 5.2 选择 run-to-completion 而不是逐级流水线的定量理由

- 跨核传递一次数据的成本约为一次缓存行迁移（同 socket 约 60–100 ns，跨 socket 约 150–300 ns【E】），再加上队列操作。如果把 Book → Strategy → Risk → OMS 拆成 4 个核，**仅传递数据就增加约 0.3–0.6 μs**，而这些阶段本身的计算总共才约 0.5 μs。
- 订单簿是"写一次、读多次"的数据：由 Book 写，Signal、Strategy、Risk（collar 参考价）读。放在同一核上，读者命中的是 L1/L2；拆到不同核上，每次更新后的读取都会变成一致性缺失（coherence miss）。
- 在开发 VM 上实测，SPSC 单向延迟 P50 为 290–400 ns【M-VM】，比订单簿更新本身还慢。这说明在调优不足的环境里，跨核跳转的代价更高。

**什么时候应偏离这一设计**：如果某个分片的策略计算很重（例如 μs 级的模型推理），可以把它挪到独立核，让分片核只做 Book + Risk + OMS，由策略核异步推送"期望报价"。代价是决策所依据的行情会多出一跳的陈旧度，这一点必须在风控中以 `decision_md_seq` 校验（订单引用的行情序号太旧时拒绝）。

### 5.3 Core Mapping — Architecture B（双路服务器，网卡位于 NUMA 0，关闭超线程）

```
NUMA node 0 (NIC attached)                         NUMA node 1
┌──────────────────────────────────────────────┐  ┌───────────────────────────────┐
│ Core 0  OS housekeeping, ALL IRQs, kthreads  │  │ 不运行交易相关负载               │
│ Core 1  Control plane: Supervisor, Risk      │  │ （避免与交易核争用 LLC/内存带宽）  │
│         Coordinator, Config Manager           │  │ 可运行：离线工具、编译禁止        │
│ ─ isolated (isolcpus/nohz_full/rcu_nocbs) ─  │  │                               │
│ Core 2  MD Gateway: Feed A+B poll, arbitrate,│  │                               │
│         sequence, decode, filter, fan-out    │  │                               │
│ Core 3  Trading Shard 0 (+ own OUCH session) │  │                               │
│ Core 4  Trading Shard 1 (+ own OUCH session) │  │                               │
│ Core 5  Trading Shard 2 (+ own OUCH session) │  │                               │
│ Core 6  Hot spare shard / MD recorder        │  │                               │
│ ─ non-isolated but pinned ─                  │  │                               │
│ Core 7  Async Logger (drains all log rings)  │  │                               │
│ Core 8  Telemetry, histogram merge, exporter │  │                               │
│ Core 9+ spare                                │  │                               │
└──────────────────────────────────────────────┘  └───────────────────────────────┘
```

依据：

| 考量 | 决策 |
|---|---|
| **NUMA** | 网卡、所有 Hot Path 线程和它们的内存（HugePages 通过 `mbind`/`numactl --membind=0` 分配）全部位于 NUMA 0。跨 NUMA 访问会让每次缓存缺失多出约 50–100 ns【E】 |
| **Cache Locality** | 一个分片的工作集：约 50–100 只标的的订单簿热区（最优价附近的价位 + 位图）、`SymbolRisk`（64B/标的）、策略状态。热数据控制在 L2（1–2 MB）内。分片按**消息率**均衡分配，而不是按标的数量 |
| **Cross-Core Communication** | 每个事件只跨核一次（MD 核 → 分片核）。ExecReport 由分片线程在同一事件循环中轮询自己的 OUCH 会话，不跨核 |
| **False Sharing** | SPSC 的 head 和 tail 分别占独立缓存行；每线程计数器各占独立缓存行；全局阻断标志（`global_block`）独占一行，且很少被写 |
| **Context Switch** | 隔离核上只运行一个线程；`nohz_full` 去掉时钟中断；`rcu_nocbs` 把 RCU 回调挪走；用 `perf stat -e context-switches,cpu-migrations` 验证结果为 0 |
| **IRQ** | 所有设备中断绑定到 Core 0（`/proc/irq/*/smp_affinity_list`），关闭 `irqbalance`。内核旁路的收发队列由线程轮询，不产生中断 |
| **Network Queue** | MD 和订单入口使用不同的 VI/队列（甚至不同网卡端口）。每个分片独占一个订单会话及其 TX/RX 队列，因此没有多线程写同一 socket 的串行化问题 |
| **超线程** | BIOS 关闭，或保证隔离核的兄弟逻辑核空闲。共享执行单元和 L1 会带来抖动 |

**OUCH 会话分配**：交易所通常允许一个会员开多个 OUCH 端口。每个分片一个会话，好处是：(1) 无需跨线程串行化发送；(2) 一个会话出问题只影响该分片的标的；(3) Cancel-on-Disconnect 的作用范围清晰。

### 5.4 Core Mapping — Architecture A（美东裸金属或独占实例，8 核）

```
Core 0  OS + IRQ + sshd
Core 1  IB Gateway (JVM)            ← taskset 绑核；固定堆(-Xms=-Xmx)、AlwaysPreTouch、低停顿 GC【BD: vmoptions 可配置】
Core 2  MD Source thread            ← ITCH TCP/文件回放 / IBKR 行情适配
Core 3  Trading Shard (单分片)       ← Book→Signal→Strategy→Risk→OMS
Core 4  IBKR Adapter: EClient 发送线程 + 全局限速器 + 映射表
Core 5  IBKR EReader 接收线程        ← IBKR C++ API 自带线程，绑核
Core 6  Async Logger + Telemetry
Core 7  Supervisor / 本地 Watchdog
```

在虚拟机上 `isolcpus` 无法阻止 hypervisor 抢占 vCPU（steal time）。生产级 A 应使用裸金属或"独占物理核"的实例，并持续监控 `steal`。

### 5.5 事件循环骨架（分片线程）

```cpp
template <ExecutionGateway Gw>
void TradingShard<Gw>::run() noexcept {
  pin_this_thread(cfg_.core);                 // sched_setaffinity, SCHED_OTHER on isolated core
  while (running_.load(std::memory_order_relaxed)) [[likely]] {
    const uint64_t now = clock_.now_ns();     // TSC -> ns, one read per iteration
    MarketEvent ev;
    int n = 0;
    while (n < kMaxBatch && md_ring_.try_pop(ev)) { on_market_event(ev, now); ++n; }  // bounded batch
    gateway_.poll(*this);                     // exec reports -> on_exec_report() (same thread)
    control_ring_.drain(*this);               // supervisor commands (rare)
    if (timer_wheel_.due(now)) [[unlikely]] on_timers(now);   // ack timeouts, quote life, staleness
    if (n) reconcile_dirty_symbols(now);      // desired vs live quotes -> risk -> send
  }
}
```

- 有界批处理（`kMaxBatch`），防止行情突发时饿死 ExecReport 的处理。
- 对 TOB 有变化的标的先置"脏"标志，等本批事件处理完再统一调用 `reconcile`。这样同一批中多次 TOB 跳变只触发一次决策，也就不会发出马上就会被撤掉的报价。
- 注意：批处理会给批内较早的事件增加最多一批的处理时间，因此 `kMaxBatch` 应很小（例如 16–64），并且监控批大小的分布。

---

## 6. 核心数据结构

> 以下代码摘自 `docs/design/reference/{ref.hpp, risk.hpp, oms.hpp}`，均已编译，并对尺寸和对齐做了 `static_assert`。

### 6.1 设计取舍总表

| 主题 | 决策 | 理由 |
|---|---|---|
| **AoS vs SoA** | 订单簿订单、OMS 订单、`SymbolRisk` 用 **AoS**；深度数组、跨标的批量特征计算用 **SoA** | 处理一笔订单时会同时访问它的所有字段，AoS 下只需一次缓存行加载。计算不平衡度时只读数量数组，SoA 连续存放更利于预取和 SIMD |
| `std::vector` | 只在启动时 `reserve`/`resize`，运行期**不增长** | 连续内存；运行期禁止 `push_back` 触发扩容 |
| `std::array` | 固定上限的结构（价位梯、深度、位图） | 编译期大小、无间接寻址 |
| **Flat Array** | Stock Locate → 簿槽位（`uint16_t[65536]`）；价格 → 价位下标 | O(1)、无哈希 |
| **Hash Table** | 订单号 → 订单：**开放寻址 + 线性探测 + 回移删除** | `std::unordered_map` 每个节点都要单独分配内存、桶链指针跳转，对缓存不友好 |
| **Intrusive Container** | 价位内订单的 FIFO 双向链表，以 `uint32_t` 下标代替指针 | 无额外分配；下标只有 4 字节，比 8 字节指针省一半空间 |
| **Object Pool** | 订单簿订单、OMS 订单、日志记录 | 空闲链栈，O(1) 分配/释放，无系统调用 |
| **Memory Pool / Arena** | 启动时从 HugePages 一次性 `mmap`，单调递增地分配，日终整体释放 | 减少 TLB 缺失，运行期不进 malloc |
| **Pre-allocation** | 所有容量按"历史峰值 × 2"预分配；耗尽视为故障（停止报价并告警），绝不扩容 | 扩容意味着 ms 级停顿 |
| **Cache Line Alignment** | 每线程独占的写热点 `alignas(64)`；跨线程共享的变量各占一行 | 避免伪共享 |
| 动态内存分配 | Hot Path 禁止；CI 用 `LD_PRELOAD` malloc 钩子 + 回放测试断言分配次数为 0 | |
| virtual dispatch | Hot Path 用 C++20 **concept + 模板**做静态分发；虚接口只用于工具和 A | 虚调用阻止内联，并引入间接跳转预测 |
| `shared_ptr` | 禁止 | 引用计数是原子操作，跨核时会造成缓存行争用 |
| 异常 | Hot Path 代码 `noexcept`，以错误码 / `RiskVerdict` 返回；核心库用 `-fno-exceptions` 编译 | 异常抛出路径成本不可控（μs 级），而且难以审计 |
| `std::string` / `std::function` / iostream | 禁止 | 隐藏的分配和间接调用 |
| 浮点 | 订单价格、数量、PnL 一律用整数定点；只有信号特征用 `double`，禁用 `-ffast-math` 并设 `-ffp-contract=off` | 保证可复现；避免 0.1+0.2 一类误差进入订单（§8.13 有一个真实例子） |

### 6.2 基础类型与 MarketEvent

```cpp
namespace hft {
using Price    = int64_t;   // fixed point, 1e-4 USD (ITCH native scale)
using Qty      = int64_t;   // shares, signed for positions
using SymbolId = uint16_t;  // == ITCH Stock Locate (assigned per trading day)
enum class Side : uint8_t { Buy = 0, Sell = 1 };
inline constexpr std::size_t kCacheLine = 64;
inline constexpr uint32_t kNil = 0xFFFF'FFFFu;

enum class MdType : uint8_t {
  Add, Execute, ExecuteWithPrice, Cancel, Delete, Replace,
  Trade, BrokenTrade, TradingAction, SystemEvent, Ignored
};

struct alignas(kCacheLine) MarketEvent {
  uint64_t seq;         // MoldUDP64 sequence number of the message
  uint64_t exch_ts_ns;  // ITCH timestamp: ns since midnight (ET)
  uint64_t rx_ts;       // local RX timestamp (HW timestamp or TSC)
  uint64_t ref;         // order ref (Replace: original ref)
  uint64_t aux;         // Replace: new ref | Execute/Trade/Broken: match number
  uint32_t px;          // price, 1e-4 USD
  uint32_t qty;         // shares
  SymbolId sym;         // stock locate
  MdType   type;
  Side     side;
  uint8_t  flag;        // Execute w/ price: printable; TradingAction: state char
};
static_assert(sizeof(MarketEvent) == 64);   // exactly one cache line per ring slot
}
```

### 6.3 ITCH 消息与解码

ITCH 5.0 的所有整数都是**大端**，时间戳是 6 字节、表示自午夜起的纳秒数。消息头的公共部分为：`type(1) | stock_locate(2) | tracking(2) | timestamp(6)`，所以消息体从偏移 11 开始。

| 类型 | 名称 | 长度 | 关键字段（偏移） | 订单簿动作 |
|---|---|---|---|---|
| `A` | Add Order | 36 | ref@11, side@19, shares@20, stock@24, price@32 | 新增订单 |
| `F` | Add Order (MPID) | 40 | 同 A，attribution@36 | 新增订单 |
| `E` | Order Executed | 31 | ref@11, shares@19, match@23 | 减少数量（成交） |
| `C` | Executed With Price | 36 | ref@11, shares@19, match@23, printable@31, price@32 | 减少数量；成交价可能不同于挂单价 |
| `X` | Order Cancel | 23 | ref@11, canceled_shares@19 | 部分撤单 |
| `D` | Order Delete | 19 | ref@11 | 删除订单 |
| `U` | Order Replace | 35 | orig_ref@11, new_ref@19, shares@27, price@31 | 删除原单，以新 ref 加入队尾（失去时间优先级） |
| `P` | Trade (non-cross) | 44 | ref@11, side@19, shares@20, stock@24, price@32, match@36 | 不改订单簿（匹配的是非展示订单），只更新成交统计 |
| `B` | Broken Trade | 19 | match@11 | 不改订单簿；回滚成交统计 |
| `H` | Trading Action | 25 | stock@11, state@19 | 交易状态：H/P/Q/T |
| `S` | System Event | 12 | event_code@11 | 开/收盘等系统事件 |

另外需要处理（不改订单簿）：`R`（Stock Directory：每日 locate 映射、Round Lot、LULD 参考信息）、`Y`（Reg SHO）、`L`、`V`/`W`（MWCB）、`K`、`J`（LULD Auction Collar）、`h`（Operational Halt）、`I`（NOII）等。

`ITCHMessage` 的两种表示：线上原始布局（仅用于文档和静态校验）与零拷贝视图。

```cpp
#pragma pack(push, 1)
struct ItchAddOrderWire {          // raw on-the-wire layout of 'A'; big-endian byte arrays
  char    type;                    // 'A'
  uint8_t stock_locate[2];
  uint8_t tracking[2];
  uint8_t timestamp[6];            // ns since midnight
  uint8_t order_ref[8];
  char    side;                    // 'B' / 'S'
  uint8_t shares[4];
  char    stock[8];                // right-padded with spaces
  uint8_t price[4];                // Price(4): 4 implied decimals
};
#pragma pack(pop)
static_assert(sizeof(ItchAddOrderWire) == 36);
static_assert(offsetof(ItchAddOrderWire, order_ref) == 11 && offsetof(ItchAddOrderWire, price) == 32);

struct ItchMessageView {           // zero-copy view over one de-framed message in the RX buffer
  const uint8_t* p;
  uint16_t len;
  char     type() const noexcept { return char(p[0]); }
  SymbolId locate() const noexcept { return be16(p + 1); }
  uint64_t timestamp_ns() const noexcept { return be48(p + 5); }
};
```

**不要**把 packed struct 中的多字节字段当作本机整数直接读取（字节序错误，并且未对齐访问在某些平台上是未定义行为）。热路径统一使用下面的 `beNN()` 读取函数：`memcpy` + `bswap` 会被编译器优化成一条 `movbe` 或 `mov` + `bswap` 指令。

```cpp
inline uint16_t be16(const uint8_t* p) noexcept { uint16_t v; std::memcpy(&v, p, 2); return __builtin_bswap16(v); }
inline uint32_t be32(const uint8_t* p) noexcept { uint32_t v; std::memcpy(&v, p, 4); return __builtin_bswap32(v); }
inline uint64_t be64(const uint8_t* p) noexcept { uint64_t v; std::memcpy(&v, p, 8); return __builtin_bswap64(v); }
inline uint64_t be48(const uint8_t* p) noexcept { return (uint64_t(be16(p)) << 32) | be32(p + 2); }

// Decode one ITCH 5.0 message (already de-framed from MoldUDP64). Zero-copy: reads the RX buffer in place.
inline bool decode_itch(const uint8_t* m, uint16_t len, uint64_t seq,
                        uint64_t rx_ts, MarketEvent& ev) noexcept {
  if (len < 11) [[unlikely]] return false;
  ev.seq = seq; ev.rx_ts = rx_ts;
  ev.sym = be16(m + 1);
  ev.exch_ts_ns = be48(m + 5);
  switch (m[0]) {
    case 'A': case 'F':
      if (len < 36) [[unlikely]] return false;
      ev.type = MdType::Add; ev.ref = be64(m + 11);
      ev.side = (m[19] == 'B') ? Side::Buy : Side::Sell;
      ev.qty = be32(m + 20); ev.px = be32(m + 32);
      return true;
    case 'E':
      if (len < 31) [[unlikely]] return false;
      ev.type = MdType::Execute; ev.ref = be64(m + 11); ev.qty = be32(m + 19); ev.aux = be64(m + 23);
      return true;
    case 'C':
      if (len < 36) [[unlikely]] return false;
      ev.type = MdType::ExecuteWithPrice; ev.ref = be64(m + 11); ev.qty = be32(m + 19);
      ev.aux = be64(m + 23); ev.flag = m[31]; ev.px = be32(m + 32);
      return true;
    case 'X':
      if (len < 23) [[unlikely]] return false;
      ev.type = MdType::Cancel; ev.ref = be64(m + 11); ev.qty = be32(m + 19);
      return true;
    case 'D':
      if (len < 19) [[unlikely]] return false;
      ev.type = MdType::Delete; ev.ref = be64(m + 11);
      return true;
    case 'U':
      if (len < 35) [[unlikely]] return false;
      ev.type = MdType::Replace; ev.ref = be64(m + 11); ev.aux = be64(m + 19);
      ev.qty = be32(m + 27); ev.px = be32(m + 31);
      return true;
    case 'P':
      if (len < 44) [[unlikely]] return false;
      ev.type = MdType::Trade; ev.ref = be64(m + 11);
      ev.side = (m[19] == 'B') ? Side::Buy : Side::Sell;
      ev.qty = be32(m + 20); ev.px = be32(m + 32); ev.aux = be64(m + 36);
      return true;
    case 'B':
      if (len < 19) [[unlikely]] return false;
      ev.type = MdType::BrokenTrade; ev.aux = be64(m + 11);
      return true;
    case 'H':
      if (len < 25) [[unlikely]] return false;
      ev.type = MdType::TradingAction; ev.flag = m[19];
      return true;
    case 'S':
      if (len < 12) [[unlikely]] return false;
      ev.type = MdType::SystemEvent; ev.flag = m[11];
      return true;
    default:
      ev.type = MdType::Ignored;   // R/Y/L/V/W/K/J/h/I/N/O -> cold-path handlers
      return true;
  }
}
```

**MoldUDP64 帧**：`session(10) | sequence(8) | message_count(2)`，之后是 `count` 个 `length(2) | message`。`count == 0` 表示心跳，`count == 0xFFFF` 表示会话结束。包头里的 `sequence` 是本包第一条消息的序号，后续消息依次加 1。

**注意事项**：
- `P` 消息的 Buy/Sell 字段在现行规范中**不一定反映真实的被动方方向**（请以当前 ITCH 5.0 规范说明为准）。推断主动方时应以 tick rule（成交价相对中间价的位置）为主。`E`/`C` 消息则可以精确推断：被成交的挂单在卖方，主动方就是买方。
- `C` 消息的 `printable='N'` 表示不应计入成交量统计，否则会重复计数。
- Stock Locate 每天重新分配，必须在每天开盘前根据 `R` 消息（或交易所下发的参考文件）重建 locate → 标的的映射。

### 6.4 订单簿结构

```cpp
inline constexpr uint32_t kLadder = 4096;           // price levels per side (power of 2)
inline constexpr uint16_t kOverflowLevel = 0xFFFF;  // order lives outside the ladder window

struct BookOrder {          // 32 bytes -> 2 per cache line
  uint64_t ref;             // ITCH order reference number (day-unique)
  uint32_t px;              // 1e-4 USD
  uint32_t qty;             // remaining displayed shares
  uint32_t prev, next;      // intrusive FIFO links (pool indices) within the level
  uint16_t level;           // ladder index or kOverflowLevel
  SymbolId sym;
  Side side;
  uint8_t pad_[3];
};
static_assert(sizeof(BookOrder) == 32);

struct PriceLevel {         // 24 bytes
  uint64_t qty = 0;         // aggregate displayed shares
  uint32_t count = 0;       // number of orders
  uint32_t head = kNil;     // oldest order (front of queue)
  uint32_t tail = kNil;     // newest order
  uint32_t pad_ = 0;
};

struct TopOfBook {
  uint32_t bid_px = 0, ask_px = 0;  // 0 = empty side
  uint64_t bid_qty = 0, ask_qty = 0;
  uint32_t bid_cnt = 0, ask_cnt = 0;
};
```

完整的 `OrderPool` / `OrderMap` / `Ladder` / `OrderBook` / `BookManager` 实现见 §7 和 `reference/ref.hpp`。

### 6.5 MarketSnapshot

```cpp
inline constexpr int kDepth = 5;
struct alignas(kCacheLine) MarketSnapshot {
  // line 0: top of book + status — touched by every decision
  Price    bid_px0, ask_px0;     // best bid / ask (0 = empty side)
  Qty      bid_qty0, ask_qty0;
  uint64_t last_exch_ts;         // ITCH ns since midnight of last applied event
  uint64_t last_seq;             // MoldUDP64 seq of last applied event
  uint32_t bid_cnt0, ask_cnt0;   // order count at best (queue length proxy)
  uint8_t  trading_state;        // 'T','H','P','Q'
  uint8_t  luld_flags;
  uint8_t  ssr;                  // Reg SHO Rule 201 in effect
  uint8_t  book_valid;
  uint32_t pad_;
  // lines 1..3: depth (SoA inside the struct: the imbalance loop reads qty arrays only)
  alignas(kCacheLine) Price bid_px[kDepth];
  Price    ask_px[kDepth];
  Qty      bid_qty[kDepth];
  Qty      ask_qty[kDepth];
  Price    last_trade_px;
  Qty      last_trade_qty;
};
static_assert(sizeof(MarketSnapshot) == 4 * kCacheLine);
static_assert(offsetof(MarketSnapshot, bid_px) == kCacheLine);
```

### 6.6 Quote / OrderRequest / Order / ExecutionReport

```cpp
enum class QuoteSlot : uint8_t { Bid = 0, Ask = 1 };
struct Quote {                   // desired quote for one side, written by strategy
  Price    px;
  Qty      qty;                  // 0 => side off
  uint64_t decided_tsc;
  uint64_t md_seq;               // market event seq the decision was based on
};

enum class OrderAction : uint8_t { New, Replace, Cancel };
enum class TimeInForce : uint8_t { Day, Ioc };
enum class Intent : uint8_t { Quote, ReduceInventory, Liquidate };
struct OrderRequest {
  uint64_t cl_ord_id;      // monotonic, unique per session
  uint64_t corr_id;        // correlation id of the triggering market event
  uint64_t orig_cl_ord_id; // Replace/Cancel target
  Price    px;
  Qty      qty;            // shares (> 0)
  Qty      orig_leaves;    // Replace: leaves of the order being replaced
  SymbolId sym;
  Side     side;
  OrderAction action;
  TimeInForce tif;
  Intent   intent;
  bool     post_only;
};

enum class OrdState : uint8_t {
  Free, PendingNew, Live, PendingReplace, PendingCancel, Filled, Canceled, Rejected, Unknown
};
struct alignas(kCacheLine) Order {   // OMS order
  uint64_t cl_ord_id;            // encodes pool index -> O(1) lookup on exec report
  uint64_t exch_order_id;        // venue / broker id (OUCH order ref, IBKR permId)
  uint64_t corr_id;
  Price    px, pending_px;       // live price / in-flight replace target
  Qty      qty, leaves, pending_qty;
  // ---- second cache line: timing / bookkeeping
  uint64_t sent_tsc;             // ack-timeout + latency stats
  Qty      cum_qty;
  SymbolId sym;
  Side     side;
  OrdState state;
  QuoteSlot slot;
  uint8_t  gw;                   // gateway / session id
  uint8_t  replace_count;
  uint8_t  pad_;
};
static_assert(sizeof(Order) == 128);

enum class ExecType : uint8_t {
  Ack, Reject, PartialFill, Fill, Canceled, CancelReject, Replaced, ReplaceReject,
  Expired, Broken, Restated, StatusUnknown
};
enum class Liquidity : uint8_t { Unknown, Added, Removed, Routed, Auction };
struct alignas(kCacheLine) ExecutionReport {   // normalized across OUCH / FIX / IBKR / Sim
  uint64_t cl_ord_id;
  uint64_t exch_order_id;
  uint64_t exec_id;              // OUCH match number / hash64(IBKR execId) for dedup
  uint64_t recv_tsc;
  Price    last_px;
  uint32_t last_qty;             // shares fit in 32 bits; keeps the report in one line
  uint32_t leaves;
  int32_t  reason;               // normalized reject/cancel reason
  SymbolId sym;
  Side     side;
  ExecType type;
  Liquidity liq;
  uint8_t  gw;
  uint8_t  pad_[6];
};
static_assert(sizeof(ExecutionReport) == 64);
```

**cl_ord_id 编码**：`cl_ord_id = (session_epoch << 40) | (generation << 20) | pool_index`。回报到达后可直接用 `pool_index` 定位 `Order`，再校验 `generation` 以防止旧回报命中已复用的槽位，整个过程 O(1) 且无需哈希。OUCH 的 Order Token 是 14 字节字母数字，可以直接承载这个编码。IBKR 的 `orderId` 是递增整数，采用 `orderId - base` 环形下标并校验存储的 id。

### 6.7 Position / InventoryState / RiskState / PnLState / StrategyState

```cpp
struct Position {
  Qty      qty = 0;              // signed
  Price    avg_px = 0;           // average cost of open position
  Qty      bought = 0, sold = 0; // day totals
  int64_t  realized = 0;         // 1e-4 USD
};

enum class InvZone : uint8_t { Normal, Warning, Critical, Breach };
struct InventoryState {
  Qty      q = 0;
  Qty      soft, hard, limit;    // |q| thresholds, soft < hard < limit
  Qty      hysteresis;           // de-escalate only below threshold - hysteresis
  InvZone  zone = InvZone::Normal;
  uint64_t zone_since_tsc = 0;
  uint64_t inventory_age_tsc = 0;
  double   skew = 0.0;           // q / limit in [-1,1], cached for pricing
};

// Hot per-symbol risk line: exactly 64 bytes, single writer (the shard thread).
struct alignas(kCacheLine) SymbolRisk {
  Qty      position;             // signed filled position
  Qty      open_buy, open_sell;  // sum of leaves of live/pending orders
  Qty      max_position;         // |worst-case position| limit
  Price    ref_px;               // collar reference
  Price    best_bid, best_ask;
  uint32_t last_md_ts32;         // low 32 bits of last MD ns (staleness < 4.29 s window)
  uint16_t open_orders;
  uint16_t flags;                // halted / book invalid / SSR / no-short / side blocked
};
static_assert(sizeof(SymbolRisk) == 64);

struct alignas(kCacheLine) RiskState {
  const std::atomic<uint64_t>* global_block;  // one read-mostly line shared by all shards
  SymbolRisk* sym;                            // indexed by book slot
  int64_t gross_worst_notional;               // shard budget usage
  int64_t net_notional;
  int64_t net_pnl;
  RiskLimits lim;
  Gcra order_rate{20, 10}, cancel_rate{40, 20}, replace_rate{40, 20};
  Gcra msg_rate{45, 5};                       // broker-wide message budget (A: < 50 msg/s)
  uint64_t last_cl_ord_id;
};

struct alignas(kCacheLine) PnLState {
  int64_t realized = 0, unrealized = 0;       // unrealized marked long@bid / short@ask
  int64_t fees = 0, rebates = 0;              // estimated at fill time, reconciled later
  int64_t net = 0;                            // realized + unrealized - fees + rebates
  int64_t peak_net = 0, drawdown = 0;
  uint64_t updated_tsc = 0;
};

enum class QuoteMode : uint8_t { Disabled, Pulled, OneSidedBid, OneSidedAsk, TwoSided };
struct alignas(kCacheLine) StrategyState {
  Quote    desired[2];           // [Bid, Ask] — written by strategy
  uint32_t live_order[2];        // OMS pool index, kNil if none
  uint64_t last_change_tsc[2];   // min quote life / hysteresis
  uint64_t cooldown_until_tsc;   // after pull / reject storm
  QuoteMode mode;
  uint8_t  dirty;                // TOB / inventory changed since last reconcile
  uint8_t  reject_streak;
};
```

### 6.8 Execution Gateway 抽象：静态接口 + 动态接口

```cpp
enum class SendResult : uint8_t { Ok, Throttled, Disconnected, Invalid };

// Static interface (hot path): no virtual dispatch, fully inlinable.
template <class G>
concept ExecutionGateway = requires(G g, const OrderRequest& r, ExecSinkProbe& sink) {
  { g.send_new(r) } noexcept -> std::same_as<SendResult>;
  { g.send_replace(r) } noexcept -> std::same_as<SendResult>;
  { g.send_cancel(r) } noexcept -> std::same_as<SendResult>;
  { g.cancel_all() } noexcept -> std::same_as<SendResult>;
  { g.poll(sink) } noexcept -> std::same_as<int>;   // drains exec reports into sink
  { g.healthy() } noexcept -> std::same_as<bool>;
};

// Dynamic interface (tools, Architecture A, tests).
class IExecutionGateway {
 public:
  virtual ~IExecutionGateway() = default;
  virtual SendResult send_new(const OrderRequest&) noexcept = 0;
  virtual SendResult send_replace(const OrderRequest&) noexcept = 0;
  virtual SendResult send_cancel(const OrderRequest&) noexcept = 0;
  virtual SendResult cancel_all() noexcept = 0;
  virtual int poll_into(void (*cb)(void*, const ExecutionReport&), void* ctx) noexcept = 0;
  virtual bool healthy() const noexcept = 0;
};
```

实现矩阵：

| 实现 | 线程模型 | 说明 |
|---|---|---|
| `OuchGateway` | 在分片线程内同步执行 | 预编码模板；SoupBinTCP 心跳/登录/序号；OUCH 消息：Enter `O`、Replace `U`、Cancel `X`；回报：Accepted `A`、Replaced `U`、Canceled `C`、Executed `E`、Rejected `J`、Broken Trade `B` 等（以所用 OUCH 版本规范为准） |
| `FixGateway` | 分片线程内同步执行，或独立线程 | 预分配的 FIX 编码器；不使用通用 FIX 引擎的动态分配路径 |
| `IbkrGateway`（代理） | 分片侧只做 SPSC 推入和弹出；独立 Adapter 线程运行 IBKR C++ API | 全局限速、`orderId` 映射、`execId` 去重、断线对账；改单用"同一 orderId 再次 `placeOrder`"，撤单用 `cancelOrder`，全撤用 `reqGlobalCancel` |
| `SimGateway` | 同步执行 | 回测/仿真：撮合模拟器 + 延迟模型 + 故障注入 |

**运行期选择**：启动时根据配置实例化 `std::variant<TradingShard<OuchGateway>, TradingShard<IbkrGateway>, TradingShard<SimGateway>>`，用一次 `std::visit` 进入对应的 `run()`。之后的内循环完全是单态代码，热路径上没有任何虚调用。Strategy 只依赖 `OrderRequest` / `ExecutionReport`，不知道 IBKR 的存在。

### 6.9 SPSC Ring

```cpp
template <typename T, std::size_t N>
class SpscRing {
  static_assert(std::has_single_bit(N), "N must be a power of two");
  static_assert(std::is_trivially_copyable_v<T>);
 public:
  bool try_push(const T& v) noexcept {
    const uint64_t h = head_.load(std::memory_order_relaxed);
    if (h - tail_cache_ == N) [[unlikely]] {             // looks full: refresh cached tail
      tail_cache_ = tail_.load(std::memory_order_acquire);
      if (h - tail_cache_ == N) return false;            // full: caller decides, never block
    }
    buf_[h & (N - 1)] = v;
    head_.store(h + 1, std::memory_order_release);
    return true;
  }
  bool try_pop(T& out) noexcept {
    const uint64_t t = tail_.load(std::memory_order_relaxed);
    if (t == head_cache_) [[unlikely]] {
      head_cache_ = head_.load(std::memory_order_acquire);
      if (t == head_cache_) return false;
    }
    out = buf_[t & (N - 1)];
    tail_.store(t + 1, std::memory_order_release);
    return true;
  }
 private:
  alignas(kCacheLine) std::atomic<uint64_t> head_{0};  // written by producer only
  alignas(kCacheLine) uint64_t tail_cache_{0};         // producer-private copy
  alignas(kCacheLine) std::atomic<uint64_t> tail_{0};  // written by consumer only
  alignas(kCacheLine) uint64_t head_cache_{0};         // consumer-private copy
  alignas(kCacheLine) std::array<T, N> buf_{};
};
```

要点：(1) head 和 tail 各占一个缓存行，消除伪共享；(2) 双方都缓存对端的索引，只有"看起来满/空"时才去读对端的原子变量，正常情况下几乎不产生跨核流量；(3) 队列满时**不阻塞**，由调用方决定处理方式：MD 队列满意味着分片处理跟不上，应标记订单簿 stale 并停止报价；日志队列满的处理见 §14.6。

### 6.10 内存：HugePage Arena

```cpp
class HugeArena {
 public:
  explicit HugeArena(std::size_t bytes) noexcept : size_(bytes) {
    base_ = static_cast<std::byte*>(::mmap(nullptr, bytes, PROT_READ | PROT_WRITE,
                                           MAP_PRIVATE | MAP_ANONYMOUS | MAP_HUGETLB, -1, 0));
    if (base_ == MAP_FAILED) { /* dev: fall back to 4K pages; production: abort startup */ }
    std::memset(base_, 0, bytes);   // prefault every page at startup, never on the hot path
    ::mlock(base_, bytes);          // plus mlockall(MCL_CURRENT | MCL_FUTURE) at process start
  }
  void* allocate(std::size_t n, std::size_t align = kCacheLine) noexcept {   // bump pointer
    std::size_t p = (used_ + align - 1) & ~(align - 1);
    if (p + n > size_) return nullptr;
    used_ = p + n;
    return base_ + p;
  }
  template <class T, class... A> T* make(A&&... a) noexcept;   // placement-new into the arena
  // ...
};
```

启动顺序：`mlockall` → 分配 Arena（NUMA 本地，`numactl --membind`）→ 构造所有池、表和簿 → 预触碰（prefault）→ 热身（回放一段行情让分支预测器和缓存进入稳态）→ 进入交易循环。

---

## 7. ITCH Order Book

### 7.1 结构总览

```
                   BookManager (one per shard)
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ slot_[65536] : uint16   Stock Locate -> book slot (kNoBook = filtered)   │  O(1) symbol filter
 │                                                                          │
 │ OrderMap  (open addressing, 16B slots, load <= 0.5, HugePages)           │
 │   ref(uint64) ──hash──► pool index(uint32)                               │  ~O(1) order lookup
 │                                                                          │
 │ OrderPool  BookOrder[cap] (32B each) + free-index stack                  │  O(1) alloc/free
 │                                                                          │
 │ books_[n] : OrderBook                                                    │
 │   ┌──────────── bids_ : Ladder ─────────────┐ ┌──── asks_ : Ladder ────┐ │
 │   │ lv_[4096] PriceLevel {qty,count,head,tail}│ │ same                   │ │
 │   │ words_[64] occupancy bitmap               │ │                        │ │
 │   │ summary_  (bit w <=> words_[w] != 0)      │ │                        │ │
 │   └───────────────────────────────────────────┘ └────────────────────────┘ │
 │   best_bid_, best_ask_ (cached ladder index)       O(1) best price        │
 │   base_ (lowest tick in window), tick_                                    │
 │   overflow (sorted small vector, cold) for prices outside the window      │
 └──────────────────────────────────────────────────────────────────────────┘

 Price level FIFO (intrusive, by pool index):
   PriceLevel.head ─► [order 17] ⇄ [order 902] ⇄ [order 33] ◄─ PriceLevel.tail
                      (oldest = first to execute under price-time priority)
```

### 7.2 关键设计决策

| 问题 | 方案 | 复杂度 | 为什么不用 STL 红黑树 |
|---|---|---|---|
| Order ID → Order | 开放寻址哈希（线性探测、Fibonacci hashing、回移删除、无墓碑），值为对象池下标 | 期望 O(1)；负载 ≤ 0.5 时平均探测 < 1.5 次 | `std::map` 查找是 O(log n)，而且每层都是一次缓存缺失 |
| Price → Price Level | 平铺价位梯：`idx = px / tick − base`（只在 Add/Replace 时计算一次，之后订单记录自己的 `level`） | O(1) | 红黑树插入/删除需要旋转和节点分配；遍历时指针跳转 |
| Best Bid / Ask | 缓存的最优下标 + 两级位图（64 × 64 位） | 查询 O(1)；最优价位清空后，用 2 次 `clz`/`ctz` 找下一档，O(1) | — |
| Insert（Add） | 池分配 → 哈希插入 → 挂到价位队尾 → 必要时置位图位 / 更新最优价 | O(1) | |
| Cancel（X，部分） | 哈希查找 → 价位和订单同时减量 | O(1) | |
| Delete（D） | 哈希查找 → 从链表摘除 → 价位清空时清位图位，如果它是最优价则重算 → 哈希删除 → 归还对象池 | O(1) | |
| Execute（E/C） | 同 X；成交量 ≥ 剩余量时按 D 处理 | O(1) | |
| Replace（U） | 删除原单 + 以新 ref、原方向加入新价位队尾（失去时间优先级） | O(1) | |
| L2 前 N 档 | 从最优价沿位图 `ctz`/`clz` 迭代 | O(N) | |
| 价位内队列位置 | 侵入式 FIFO，可以算出某订单之前的累计数量 | O(队列长度)，只在需要时计算 | |

**为什么用价位梯而不是哈希价格表**：做市只关心最优价附近几十档。价位梯让"按价格找价位"变成一次减法和一次数组访问；相邻价位在内存中也相邻，L2 迭代和不平衡度计算都对缓存友好。代价是每只标的约 2 × 4096 × 24 B ≈ 197 KB 内存，对于几百只标的可以接受（另外只有被访问过的价位会进入缓存）。

### 7.3 价格窗口、tick 与溢出

- 开盘前用参考价（前收盘或开盘价）设置 `base_ = ref_px/tick − 2048`。窗口覆盖 ±2048 tick：$50 的股票在 1 美分 tick 下为 ±$20.48，足以覆盖 LULD 价格带内的连续交易。
- 窗口外的订单（远离市场的限价单）进入**溢出区**（按价格排序的小 vector，冷路径），只维护聚合数量。它们几乎不影响做市决策。
- **重定窗口（re-center）**：当最优价接近窗口边缘（例如距边缘 < 256 tick）时，在分片线程的空闲周期执行重定窗口，把溢出区中落入新窗口的订单迁回价位梯。这一操作是 O(窗口)，触发时先标记该标的为 `kSymBookInvalid`（暂停报价），完成后恢复。每天极少发生。
- **tick 不能硬编码**：Reg NMS Rule 612 规定，价格 ≥ $1 的报价最小单位为 $0.01，< $1 为 $0.0001。SEC 在 2024 年的修订中还为部分报价价差很窄的股票引入了 $0.005 的报价单位（实施日期和适用名单以 SEC/交易所的最新规则为准）。系统必须每天从参考数据加载每只股票的 tick 表。
- 价格可能跨越 $1 的标的，使用 `tick_ = 1`（$0.0001），并相应扩大窗口或接受更频繁的溢出。
- `index_of()` 会校验 `px` 是否落在 tick 网格上（`t * tick == px`）。不在网格上的价格走溢出路径，并计数告警，因为这通常意味着 tick 表配置错误。

### 7.4 更新流程（核心代码）

```cpp
// Apply one event. Returns true when top-of-book of ev.sym changed.
bool BookManager::apply(const MarketEvent& ev) noexcept {
  switch (ev.type) {
    case MdType::Add: return add(ev.sym, ev.ref, ev.side, ev.px, ev.qty);
    case MdType::Execute:
    case MdType::ExecuteWithPrice:
    case MdType::Cancel: return reduce_or_remove(ev.ref, ev.qty);
    case MdType::Delete: return remove(ev.ref);
    case MdType::Replace: {
      const uint32_t oi = map_.find(ev.ref);
      if (oi == kNil) [[unlikely]] { ++unknown_ref_; return false; }
      const Side s = pool_[oi].side;                 // U carries no side: inherit it
      const bool a = remove(ev.ref);
      const bool b = add(ev.sym, ev.aux, s, ev.px, ev.qty);
      return a || b;
    }
    default: return false;  // Trade / Broken / status: no book change
  }
}

bool OrderBook::link_back(OrderPool& pool, uint32_t oi) noexcept {   // append to FIFO tail
  BookOrder& o = pool[oi];
  Ladder& lad = side(o.side);
  PriceLevel& l = lad[o.level];
  o.prev = l.tail; o.next = kNil;
  if (l.tail != kNil) pool[l.tail].next = oi; else l.head = oi;
  l.tail = oi;
  l.qty += o.qty;
  if (l.count++ == 0) {                                     // level becomes non-empty
    lad.mark(o.level);
    int& best = o.side == Side::Buy ? best_bid_ : best_ask_;
    const bool better = best < 0 || (o.side == Side::Buy ? int(o.level) > best : int(o.level) < best);
    if (better) best = o.level;
    return better || int(o.level) == best;
  }
  return int(o.level) == (o.side == Side::Buy ? best_bid_ : best_ask_);
}

bool OrderBook::unlink(OrderPool& pool, uint32_t oi) noexcept {      // remove from FIFO
  BookOrder& o = pool[oi];
  Ladder& lad = side(o.side);
  PriceLevel& l = lad[o.level];
  if (o.prev != kNil) pool[o.prev].next = o.next; else l.head = o.next;
  if (o.next != kNil) pool[o.next].prev = o.prev; else l.tail = o.prev;
  l.qty -= o.qty;
  int& best = o.side == Side::Buy ? best_bid_ : best_ask_;
  const bool at_best = int(o.level) == best;
  if (--l.count == 0) {
    lad.unmark(o.level);
    if (at_best) best = o.side == Side::Buy ? lad.highest() : lad.lowest();  // 2x clz/ctz
  }
  return at_best;
}

// Two-level bitmap: 4096 levels = 64 words x 64 bits, summary word marks non-empty words.
int Ladder::highest() const noexcept {
  if (!summary_) return -1;
  const int w = 63 - std::countl_zero(summary_);
  return (w << 6) + 63 - std::countl_zero(words_[w]);
}
int Ladder::lowest() const noexcept {
  if (!summary_) return -1;
  const int w = std::countr_zero(summary_);
  return (w << 6) + std::countr_zero(words_[w]);
}
```

开放寻址哈希的删除（回移删除，保证没有墓碑，探测链不会随时间退化）：

```cpp
bool OrderMap::erase(uint64_t k) noexcept {
  std::size_t i = home(k);
  while (t_[i].key != k) { if (!t_[i].key) return false; i = (i + 1) & mask_; }
  for (std::size_t j = (i + 1) & mask_; t_[j].key; j = (j + 1) & mask_) {
    const std::size_t h = home(t_[j].key);
    // entry j may move into hole i only if its home is NOT cyclically in (i, j]
    const bool stays = (i <= j) ? (i < h && h <= j) : (i < h || h <= j);
    if (!stays) { t_[i] = t_[j]; i = j; }
  }
  t_[i].key = 0;
  return true;
}
```

### 7.5 容量规划

| 项 | 估算方法 | 示例（200 只标的） |
|---|---|---|
| 订单簿价位梯 | 标的数 × 197 KB | ≈ 40 MB |
| 存活订单峰值 | 回放历史 ITCH 统计宇宙内标的的存活订单峰值，再 × 2 | 假设 2M → 对象池 64 MB |
| OrderMap | 容量 = 2^ceil(log2(峰值 × 2))，16 B/槽 | 2^23 槽 = 128 MB |
| 页 | 1 GB HugePages，NUMA 本地 | 共约 0.25 GB → 1 个 1 GB 页 |

ITCH 的订单号在全市场范围内当日唯一，所以 OrderMap 和 OrderPool 在每个分片内共享，订单簿按标的划分。MD 网关会按 locate 过滤掉宇宙外的标的。所有修改类消息（E/X/D/U）都带有 locate，因此可以安全过滤，不会出现"收到对未知订单的修改"。

### 7.6 订单簿完整性保护

| 检查 | 触发条件 | 动作 |
|---|---|---|
| 未知订单号 | E/X/D/U 引用了不存在的 ref（`unknown_ref_` 计数增加） | 订单簿可能已损坏 → 该标的置 `kSymBookInvalid`，撤销其报价 → 用快照恢复 |
| 交叉订单簿 | 连续交易时段（state = `T`）中 best_bid ≥ best_ask | 单一交易所的订单簿在连续交易时不应出现交叉 → 同上 |
| 数量下溢 | 撤单/成交数量 > 剩余数量 | 计数告警，按删除处理（防御性），并标记待核查 |
| 对象池耗尽 | `alloc()` 返回 `kNil` | 全局停止报价（`kBlkMdGap`），这是容量规划失误 |
| 序号缺口 | §13.1 | 所有标的置 invalid → 恢复 |

### 7.7 正确性验证

- **对拍**：参考实现中，订单簿与一个基于 `std::map<price, qty>` 的朴素实现，在 200 万条随机 Add/Execute/Cancel/Delete/Replace 事件上，每 997 个事件比对一次 64 只标的的最优价和最优量，全部一致【M-VM】。
- **生产前**：用 NASDAQ 官方发布的历史 ITCH 样本文件全量回放，在若干时间点用 GLIMPSE 快照（或独立实现）校验全簿；校验收盘时每只标的的成交量是否与官方统计一致。
- **Fuzz**：用 libFuzzer 对 `decode_itch` + `apply` 做截断、乱码和重复消息的模糊测试，要求不崩溃、不越界、不出现静默错误（必须计数）。

---

## 8. Market Making Strategy

> 本节给出完整的报价逻辑框架和工程实现。它**不构成**盈利承诺：做市的主要风险是逆向选择（被更快或更知情的对手成交）、库存风险和尾部事件，任何参数都必须经过回测、仿真和小规模实盘验证。

### 8.1 决策流水线（每次 TOB/成交变化触发，同一线程内完成）

```
MarketEvent ─► Book update ─► Market State (halt/LULD/SSR/stale/feed-lag)
           ─► Features (mid, microprice, OBI, trade imbalance, σ, momentum, depth)
           ─► Regime filter (pull? one-sided? widen?)
           ─► Fair price ─► Reservation price (inventory skew)
           ─► Spread (σ, inventory, liquidity, flow toxicity, fee floor)
           ─► Quote prices (tick snap, no-cross) + sizes (inventory, zone)
           ─► Desired quotes ─► Quote Reconciler (hysteresis, min life, budget)
           ─► OrderRequest(s) ─► pre_trade_check ─► OMS ─► Gateway
```

### 8.2 特征（增量计算）

记最优买价 $P_b$、最优卖价 $P_a$，对应数量 $Q_b$、$Q_a$，tick 为 $\delta$。

| 特征 | 定义 | 实现要点 |
|---|---|---|
| Mid | $m = (P_b + P_a)/2$ | |
| Microprice | $\mu = \dfrac{P_a Q_b + P_b Q_a}{Q_b + Q_a}$ | 买盘量越大，越接近卖价（预示价格可能上行） |
| 订单簿不平衡（OBI） | $I = \dfrac{\sum_{k} w_k (Q_b^k - Q_a^k)}{\sum_k w_k (Q_b^k + Q_a^k)} \in [-1,1]$，$w_k$ 随档位递减 | 只在前 N 档变化时更新；$w_k$ 预计算 |
| 交易流不平衡 | $TI = \dfrac{V_{buy} - V_{sell}}{V_{buy} + V_{sell}}$，按时间衰减窗口计算 | 主动方用 E/C 被成交挂单的方向精确判定；P 消息用 tick rule |
| 波动率 | $\sigma^2 \leftarrow \lambda \sigma^2 + (1-\lambda)\dfrac{(\Delta m)^2}{\Delta t}$（每秒尺度） | 不规则采样的 EWMA；衰减系数按 $\Delta t$ 查表 |
| 短期动量 | mid 收益率的 EWMA（如半衰期 1 s / 10 s） | |
| 深度 | 前 N 档总量；与当日同时段的中位数之比 | 用于检测"深度骤降" |
| 队列位置 | 自己订单前方的估计数量（§8.7） | 只在 B 中有意义 |
| 行情延迟 | `local_ptp_now − exch_ts` | 超过阈值就降级 |

### 8.3 Fair Price

$$
\text{fair} = m + w_\mu(\mu - m) + w_I \cdot I \cdot \tfrac{P_a - P_b}{2} + w_{TI} \cdot TI \cdot \delta + w_{mom} \cdot \text{mom} \cdot \tau
$$

再截断到 $[P_b, P_a]$ 之内：**任何信号都不允许把公允价推出盘口**。这一约束防止单个特征失控时产生激进报价。权重由离线回归（对未来 $\tau$ 时刻 mid 变化的预测）得到，按标的或按流动性分组，每天盘前加载。

### 8.4 Reservation Price 与 Inventory Skew

理论参考是 Avellaneda–Stoikov 模型：

$$
r = s - q\gamma\sigma^2(T-t), \qquad \delta^a + \delta^b = \gamma\sigma^2(T-t) + \tfrac{2}{\gamma}\ln\!\left(1+\tfrac{\gamma}{k}\right)
$$

日内连续做市没有自然的终止时间 $T$，工程上通常改用固定的风险持有期 $\tau$，并把偏斜量限制在可解释的 tick 数之内：

$$
q_n = \text{clamp}\left(\frac{q}{q_{limit}}, -1, 1\right), \qquad r = \text{fair} - q_n \cdot S_{max} \cdot \delta
$$

- $S_{max}$：库存达到上限时保留价的最大偏移（tick 数），例如 2–3 tick。
- **库存 > 0（多头）**：$r$ 下移，Bid 和 Ask 都下移。Ask 更接近（甚至等于）最优卖价，更容易卖出；Bid 远离市场，买入积极度降低。
- **库存 < 0（空头）**：$r$ 上移。Bid 更积极，更容易买入；Ask 远离市场，卖出积极度降低。
- 非线性选项：$q_n^3$ 或分段函数，使库存接近上限时偏斜加速（与 §9.2 的分区联动）。

### 8.5 Spread：多因子决定报价宽度

$$
h = \max\Big( \underbrace{k_\sigma \sigma\sqrt{\tau}}_{\text{波动}} + \underbrace{k_q |q_n| \delta}_{\text{库存}} + \underbrace{k_L \tfrac{\delta}{\max(1, D/D_0)}}_{\text{流动性}} + \underbrace{k_f |TI| \delta}_{\text{订单流毒性}},\; h_{min}\delta,\; \text{fee\_edge} \Big)
$$

$$
h_{bid} = h\,(1 + \max(q_n, 0)), \qquad h_{ask} = h\,(1 + \max(-q_n, 0))
$$

| 因素 | 方向 | 原因 |
|---|---|---|
| 波动率 ↑ | 价差 ↑ | 持有期内价格变动的风险更大 |
| 库存 \|q\| ↑ | 价差 ↑，且加风险一侧更宽 | 进一步增加库存的边际成本上升 |
| 流动性（深度 D）↓ | 价差 ↑ | 深度薄时价格冲击和跳变的概率更高 |
| 订单流单边性 \|TI\| ↑ | 价差 ↑ | 单边订单流往往伴随知情交易，逆向选择风险更高 |
| 费用下限 `fee_edge` | 价差下限 | 至少覆盖 佣金 + 交易所费用 − 返佣 + 最低边际；IBKR 费用结构下这一下限通常比交易所会员高很多【BD】 |
| 市场价差 | 参考 | 不应窄于市场已有价差太多，否则只会频繁被吃并承担逆向选择 |

### 8.6 Quote Size

- 基础量 `base_size`，按 Round Lot 取整。Round Lot 的定义已由 SEC 修订为按价格分层（以 SIP/交易所当前执行的规则为准），从参考数据加载。
- 加风险一侧按库存线性缩减（**整数运算**）：`bid_qty = base × (limit − max(q,0)) / limit`，再向下取整到 lot。
- 再乘以分区系数：Warning 区 × 0.5，Critical 区 × 0（不再报加风险一侧）。
- 深度骤降或波动率尖峰时全体缩量。
- 上限受 `max_order_qty` 和最坏情况持仓检查的双重约束（§9）。

### 8.7 成交概率与队列位置

**强度模型**（离线标定）：距最优价 $d$ 个 tick 的挂单，单位时间被成交的强度 $\lambda(d) = A e^{-kd}$，持有期 $\tau$ 内成交的概率为 $P_{fill} = 1 - e^{-\lambda(d)\tau}$。$A$、$k$ 按标的和时段（开盘、午间、收盘）分桶估计。

**队列模型**（B，order-level 订单簿）：
- 自己的订单被确认（Accepted）时，记录该价位当时的总量作为前方数量 $Q_{ahead}$（保守地假设自己在队尾）。
- 该价位发生成交（E/C）时：按价格-时间优先原则，成交从队首开始消耗，因此 $Q_{ahead}$ 减去成交量。
- 该价位发生撤单（X/D）时：无法确定被撤订单在自己前面还是后面，按比例估计 $Q_{ahead} \leftarrow Q_{ahead}(1 - x/Q_{level})$。
- 成交概率 $P(V_\tau \ge Q_{ahead} + q_{own})$，其中 $V_\tau$ 是该价位在 $\tau$ 内的成交量（复合泊松分布，离线标定）。

**用途**：(1) 决定是挂在最优价还是退后一档；(2) 改价决策：如果当前订单的队列位置很好（$Q_{ahead}$ 小），除非风险要求，否则不要因为微小的公允价变化去改价，因为改价会丢失优先级；(3) 回测撮合模拟器中的成交判定。

**A 的限制**：SMART 路由下订单可能去任意交易场所，队列模型不适用。若要使用，需要定向路由到 NASDAQ，并接受相应的费用和成交差异【BD】。

### 8.8 报价刷新策略（Quote Reconciler）

策略每次决策只更新"期望报价"`desired[side]`。Reconciler 负责比较期望状态与实际状态，并决定是否发出动作：

| 规则 | 说明 |
|---|---|
| **单槽单在途** | 每侧（Bid/Ask）同一时刻最多一个在途请求。在途期间期望值可以继续变化，回报到达后再与最新期望值比较，**不排队累积命令** |
| **价格滞回** | 价格差 ≥ 1 tick 才考虑改价 |
| **防御优先、进攻延迟** | 新价格**更保守**（远离市场、风险更低）时立即执行；新价格**更激进**时，需要满足最小报价寿命 `min_quote_life`（例如 B：几十 ms；A：≥ 1 s），避免被抖动带着频繁追价 |
| **数量变化** | 减量：OUCH 用 Cancel 的 "intended order size" 做部分撤单，**通常保留优先级**（以交易所规则为准）；IBKR 用改单（是否保留优先级取决于交易所）【BD】。加量：改单会失去优先级，可以考虑另挂一笔 |
| **预算感知** | 限速额度紧张时（§10）按优先级分配：撤单 > 减仓 > 防御改价 > 进攻改价 > 新单。额度不足时跳过进攻性动作 |
| **合并** | 同一批事件中多次 TOB 变化，只在批末做一次决策（§5.5） |
| **冷却** | 被拒单或撤报价后，进入 `cooldown_until` 冷却期 |

### 8.9 Cancel / Replace 状态机（每个报价槽）

```
                 send New (risk ok)
   ┌───────┐ ─────────────────────────► ┌────────────┐   Reject      ┌─────────────────┐
   │ EMPTY │                            │ PENDING_NEW│ ────────────► │ EMPTY + cooldown│
   └───────┘ ◄─────────────┐            └─────┬──────┘               └─────────────────┘
       ▲                   │                  │ Ack                (timeout → UNKNOWN)
       │ Canceled /        │                  ▼
       │ Filled(leaves=0)  │            ┌──────────┐  send Replace   ┌────────────────┐
       │                   └─────────── │   LIVE   │ ──────────────► │ PENDING_REPLACE│
       │                                └──┬────┬──┘ ◄────────────── └───────┬────────┘
       │                       PartialFill │    │      Replaced /             │ ReplaceReject
       │                       (leaves>0)  │    │      ReplaceReject          │ → LIVE (old px/qty)
       │                       stays LIVE ─┘    │ send Cancel                 │ Fill during pending:
       │                                        ▼                             │ apply fill, keep pending
       │                                 ┌───────────────┐
       └──────────────────────────────── │ PENDING_CANCEL│ ── CancelReject("too late")
                     Canceled            └───────────────┘    → wait for Fill / final status
                                                             (timeout → UNKNOWN → status query)

UNKNOWN: 停止该槽的新动作；发送状态查询（OUCH 无查询时依赖交易所回报/重连重放，
         IBKR 用 reqOpenOrders）；N 次未决 → 该标的 Pulled，升级告警。
```

**竞态处理要点**：
- **撤单途中被成交**：先收到 Fill，再收到 CancelReject 或 Canceled（剩余部分）。成交永远优先计入持仓，任何状态下收到 Fill 都要处理。
- **改单途中被成交**：OUCH 的 Replace 会基于剩余量处理（以规范为准）；OMS 先计入成交，再根据 Replaced 回报中的新 leaves 校正。
- **最坏情况持仓计算**：在途 New/Replace 的数量计入 `open_buy`/`open_sell`；Replace 时取新旧数量中较大者，直到回报确认。
- **重复回报**：按 `exec_id` 去重（§13）。

### 8.10 行情过滤与报价撤出（Regime Filter）

| 条件 | 检测 | 动作 |
|---|---|---|
| 交易暂停 / 熔断 | ITCH `H`（H/P 状态）、`V`/`W`（MWCB） | 立即撤销该标的（或全市场）报价；恢复后先观察 N 秒再报价 |
| LULD | 价格接近价格带；`J`、限价状态 | 加宽价差或撤出；价格带外的报价会被拒绝 |
| 价差骤然扩大 | 当前价差 > 同时段中位数 × K | 跟随加宽，或撤出 |
| 深度骤降 | 前 N 档深度 < 中位数 × k | 缩量、加宽 |
| 波动率尖峰 | σ 短期/长期之比 > 阈值 | 加宽、缩量；极端时撤出 |
| 高频撤单（闪烁挂单） | 价位上的撤单率异常 | 微观价格和 OBI 的可信度下降：降低 $w_I$ 权重 |
| 行情延迟 / 陈旧 | feed lag > 阈值；最后更新距今 > 阈值 | 撤出（风控同时会阻断新单） |
| 开盘 / 收盘竞价附近 | 时钟（如 9:30 后 N 分钟、16:00 前 N 分钟，可配置） | 不报价或缩量；收盘前进入平仓窗口 |
| 外部市场锁定/交叉 | NBBO（需要 SIP 或多交易所行情） | 遵守 Reg NMS，不锁定/交叉外部市场 |

### 8.11 合规约束（硬编码进风控而不是策略）

- 不锁定/交叉市场（Reg NMS Rule 610）：Post-Only 订单的价格必须严格优于对手方 NBBO 一个 tick 以上。只有 NASDAQ 行情时，只能保证不穿 NASDAQ 的盘口，这就是 B 需要 NBBO 的原因。
- 报价单位（Rule 612），按标的加载 tick 表。
- 卖空：Reg SHO 要求 locate；Rule 201（SSR）触发后卖空价必须高于 NBB。IBKR 账户需要可借券，B 中如果不是做市商注册身份，同样需要 locate。
- Market Access Rule（15c3-5）：B 中必须有事前风控，并与 Sponsor 的风控协调。
- 自成交防护（Self-Trade Prevention）：同一账户的买卖单可能互相成交（构成 wash trade 风险）。OUCH 提供 STP 相关字段时应启用（以所用规范为准）；系统内部也要检查自己的 Bid 不会 ≥ 自己的 Ask。

### 8.12 厚门价位的有界接入（可选，默认关闭）

`LevelContextEngine` 盘前生成每只标的的价位表（核心空间对锚点、起爆点、轨 1/轨 2 防守底、趋势目标位），形式为不可变快照 `LevelSnapshot`。策略只能以下列**有界**方式使用：

| 用法 | 规则 | 上限 |
|---|---|---|
| 价位附近防御 | mid 距离任一关键价位 < `k·σ·√τ` 时：价差 × (1+α)，数量 × (1−β) | α ≤ 0.5，β ≤ 0.5 |
| 突破偏置 | mid 上穿起爆点，并且"当日累计成交量 / 历史同时段平均" ≥ 1.3（把文档中"比前一日放大 30%"翻译为日内可比的口径）→ fair 加正偏移 | ≤ 1 tick，持续 ≤ T 分钟 |
| 防守底跌破 | mid 跌破轨 2 防守底 → 多头库存目标下调（库存偏斜中心移向 ≤ 0） | 不改变 `inv_limit` |

**禁止**：用厚门价位改变任何风控限额；用它触发市价单；在附录 A.5 的检验未通过之前在实盘开启。

### 8.13 Pricing 核心代码

```cpp
struct Features { double mid, microprice, imbalance, trade_imbalance, momentum, sigma, depth_shares; };
struct QuoteParams {
  double tick, half_spread_min_ticks, k_vol, k_inv_spread, k_liq, k_flow;
  double w_micro, w_imb, w_flow, w_mom, horizon_s;
  double skew_ticks_at_limit;   // reservation shift (ticks) at |q| == limit
  double fee_edge;              // $/share needed to break even (fees - rebates + min edge)
  int64_t base_size, lot, inv_limit;
};
struct QuotePair { Price bid_px, ask_px; Qty bid_qty, ask_qty; bool bid_on, ask_on; };

inline QuotePair compute_quotes(const Features& f, double best_bid, double best_ask,
                                int64_t inv, const QuoteParams& p) noexcept {
  const double half_book = 0.5 * (best_ask - best_bid);
  // 1) fair value: mid + bounded short-horizon adjustments, clamped inside the touch
  double fair = f.mid + p.w_micro * (f.microprice - f.mid) + p.w_imb * f.imbalance * half_book +
                p.w_flow * f.trade_imbalance * p.tick + p.w_mom * f.momentum * p.horizon_s;
  fair = std::clamp(fair, best_bid, best_ask);
  // 2) inventory skew -> reservation price
  const double qn = std::clamp(double(inv) / double(p.inv_limit), -1.0, 1.0);
  const double reservation = fair - qn * p.skew_ticks_at_limit * p.tick;
  // 3) half spread: volatility + inventory + liquidity + flow toxicity, floored by tick & fees
  double hs = p.k_vol * f.sigma * std::sqrt(p.horizon_s) + p.k_inv_spread * std::fabs(qn) * p.tick +
              p.k_liq * p.tick / std::max(1.0, f.depth_shares / double(p.base_size)) +
              p.k_flow * std::fabs(f.trade_imbalance) * p.tick;
  hs = std::max({hs, p.half_spread_min_ticks * p.tick, p.fee_edge});
  const double hs_bid = hs * (1.0 + std::max(qn, 0.0));    // widen the risk-increasing side
  const double hs_ask = hs * (1.0 + std::max(-qn, 0.0));
  // 4) snap to tick grid away from reservation; never cross the touch (post-only)
  double bid = std::floor((reservation - hs_bid) / p.tick + 1e-9) * p.tick;
  double ask = std::ceil((reservation + hs_ask) / p.tick - 1e-9) * p.tick;
  bid = std::min(bid, best_ask - p.tick);
  ask = std::max(ask, best_bid + p.tick);
  // 5) sizes shrink linearly on the risk-increasing side — integer math only
  //    (double 1.0-0.8 = 0.19999… would floor 100 shares down to 0)
  const int64_t q_long  = std::clamp<int64_t>(inv, 0, p.inv_limit);
  const int64_t q_short = std::clamp<int64_t>(-inv, 0, p.inv_limit);
  auto lots = [&](int64_t room) { return (p.base_size * room / p.inv_limit) / p.lot * p.lot; };
  QuotePair q;
  q.bid_px = to_fixed(bid); q.ask_px = to_fixed(ask);
  q.bid_qty = lots(p.inv_limit - q_long);
  q.ask_qty = lots(p.inv_limit - q_short);
  q.bid_on = q.bid_qty > 0; q.ask_on = q.ask_qty > 0;
  return q;
}
```

单元测试结果【M-VM】（$50.00 × $50.01 的盘口，inv_limit = 5000，base = 500）：

| 库存 | Bid | Ask | 解读 |
|---|---|---|---|
| 0 | 49.99 × 500 | 50.02 × 500 | 公允价略高于 mid（正的 OBI/microprice），所以 Ask 更远 |
| +4000（多头 80%） | 49.96 × **100** | **50.01** × 500 | 买入积极度降低、买量缩小；Ask 贴近最优卖价以促进卖出 |
| −4000（空头 80%） | **50.00** × 500 | 50.06 × **100** | 对称 |

> 一个真实的教训：这段代码的第一版用 `double` 计算数量比例，`1.0 − 0.8 = 0.19999…`，导致 100 股被向下取整成 0，测试捕获了这个问题。**凡是会进入订单的数值，一律用整数运算**。

`compute_quotes` 在开发 VM 上的 P50 约为 66–71 ns【M-VM】。`std::sqrt` 可以预计算（σ√τ 只在 σ 更新时重算），除法可以用倒数乘法替代。

### 8.14 策略层面的工程风险

| 风险 | 表现 | 工程对策 |
|---|---|---|
| 逆向选择 | 成交后价格持续朝不利方向移动 | 记录每笔成交在 100 ms / 1 s / 5 s / 30 s 的 markout（成交后盯市损益），按标的和时段监控；markout 持续为负 → 自动加宽或停报 |
| 延迟劣势 | 行情变化后自己的旧报价被更快的对手吃掉（stale quote sniping） | 防御性改价立即执行；行情延迟超阈值时撤出；A 中加大价差以覆盖 IBKR 延迟 |
| 库存累积 | 单边行情中持续成交在同一侧 | §9.2 分区状态机 + 硬限额 + 平仓逻辑 |
| 参数漂移 | 盘中特征分布变化 | 参数只在盘前加载；盘中只允许 Supervisor 受控调整（审计） |
| 模型失效 | 新闻、并购、指数调整等事件 | 事件日历在盘前禁用相关标的；人工 Kill |
| 自激振荡 | 自己的报价改变 OBI → 触发自己改价 | 计算 OBI 时扣除自己的挂单（B 中可以估计；A 中做不到） |

---

## 9. Risk Engine

### 9.1 风控分层

```
L0  Strategy sanity       (策略内自检：NaN/越界/价格不在盘口附近)            ── 可被绕过，不作依赖
L1  Pre-Trade Risk        (分片线程内同步执行，每笔订单必经，fail-closed)    ── 核心防线 ★
L2  At-Trade / Post-Trade (成交时同步更新持仓、PnL、Kill Switch；异步汇总)   ── 状态驱动 L1
L3  External              (独立 Watchdog 进程；Broker/Sponsor 15c3-5 风控；
                           交易所 Cancel-on-Disconnect / Kill Switch)         ── 进程崩溃也有效
L4  Human                 (Supervisor 控制台、带外撤单、电话联系 Broker)
```

**原则**：
1. L1 与下单在**同一线程、同一调用链**中执行，不经过任何队列，因此不存在"风控还没算完订单就已经发出"的窗口。
2. **Fail-closed**：风控需要的状态无效（行情陈旧、持仓未对账、配置未加载）时，拒绝所有增加风险的订单。
3. **撤单永不因风险原因被拒绝**，只受交易所或 Broker 的消息额度约束，而且这部分额度会专门预留给撤单。
4. 风控状态由分片线程单独写入；其他线程只读快照，或者通过控制队列提交变更请求。

### 9.2 库存风险模型与状态机

**定义**

| 符号 | 含义 | 示例 |
|---|---|---|
| `inventory` (q) | 当前已成交的带符号持仓（股数） | +1200 |
| `inventory_limit` | 单标的的绝对持仓上限；以最坏情况计算（持仓 + 所有在途订单全部成交） | 5000 股，并且受名义上限约束（如 $250k，取较小者） |
| `soft_limit` | 进入 Warning 区的阈值 | 50% × limit |
| `hard_limit` | 进入 Critical 区的阈值 | 80% × limit |
| `inventory_skew` | $q_n = q/\text{limit}$，用于保留价偏移和不对称价差（§8.4、§8.5） | 0.24 |
| hysteresis | 降级需要回落到阈值以下的幅度，防止状态抖动 | 5% × limit |
| inventory age | 库存距上次接近 0 的时长 | 用于超时升级 |

**分区与动作**

| 区域 | 条件 | 报价动作 | 风控动作 |
|---|---|---|---|
| **Normal** | \|q\| < soft | 正常双边报价，线性偏斜 | — |
| **Warning** | soft ≤ \|q\| < hard | 加风险一侧：价差 × (1+α)、数量 × 0.5；减风险一侧：报价更积极（偏斜加速） | 告警（info） |
| **Critical** | hard ≤ \|q\| < limit | **停止加风险一侧的报价**（单边报价）；减风险一侧贴近或改善最优价；库存持续时间超过 `T_crit` 时，允许带价格保护的 IOC 主动减仓（Intent=ReduceInventory） | 置 `kSymBuyBlocked` / `kSymSellBlocked`（L1 拒绝加风险订单） |
| **Breach** | \|q\| ≥ limit（只会由在途订单的超额成交造成，因为事前检查用的是最坏情况） | **Cancel All（该标的）**；受控平仓（分批 IOC，价格 collar） | 锁定该标的；告警（page） |
| **Kill** | 该标的平仓失败，或多个标的同时 Breach，或全局净/总敞口越限 | 全局 Cancel All | **Kill Strategy**（置全局 `kBlkStrategyOff`，并可能触发 Kill Switch） |

**状态机**

```
                    |q| ≥ soft                    |q| ≥ hard                     |q| ≥ limit
   ┌────────┐  ─────────────────►  ┌─────────┐  ─────────────────►  ┌──────────┐ ──────────────► ┌────────┐
   │ NORMAL │                      │ WARNING │                      │ CRITICAL │                 │ BREACH │
   └────────┘  ◄─────────────────  └─────────┘  ◄─────────────────  └──────────┘                 └───┬────┘
        ▲        |q| < soft − h                   |q| < hard − h          │                          │
        │                                                                │ age > T_crit             │ cancel-all(sym)
        │                                                                ▼                          │ + controlled flatten
        │                                                       主动减仓（IOC, collar）              ▼
        │                                                                                    ┌──────────────┐
        └─────────── 平仓完成且 |q| < soft − h，并且 Supervisor 解锁该标的 ◄────────────────── │ SYMBOL LOCKED│
                                                                                             └──────┬───────┘
                                                              平仓失败 / 多标的 Breach / 全局越限    │
                                                                                                    ▼
                                                                                          ┌──────────────────┐
                                                                                          │ KILL STRATEGY    │
                                                                                          │ (global, latched)│
                                                                                          └──────────────────┘
```

**实现要点**：
- 分区只在**成交回报**时重算（库存只会在成交时变化），代价是 O(1) 的整数比较。
- 最坏情况持仓：`worst_long = q + open_buy`，`worst_short = q − open_sell`。事前检查保证 `|worst| ≤ limit`，因此 Breach 只可能来自"已撤单但在途期间成交"这类竞态，这是设计内的低概率事件。
- **库存速度（应对"库存快速累积"）**：除了库存水平，还要监控单侧成交速度 `|Δq| / Δt`（例如 1 s 和 10 s 窗口，用 GCRA 式计数实现）。如果 1 秒内同一方向成交超过 `velocity_limit`（如 30% × limit），即使尚未进入 Warning，也立即撤销该侧报价并冷却 N 秒。这通常意味着有知情对手在扫单，或者行情发生了跳变而本方报价已经陈旧。
- 收盘前（如 15:50 ET，可配置）进入**平仓窗口**：所有标的的 limit 线性收缩到 0，状态机会自然推动库存归零；收盘前 N 分钟仍未平掉的部分走受控平仓，或按策略设定隔夜持有（需要单独的隔夜风险限额）。

### 9.3 风控规则全集

| # | 规则 | 范围 | 执行时点 | 示例阈值 | 违反后的动作 |
|---|---|---|---|---|---|
| 1 | Max Order Size | 单笔 | 事前同步 | 1000 股，且名义金额 ≤ $50k | 拒单 |
| 2 | Max Position | 单标的 | 事前同步（最坏情况） | 5000 股 / $250k | 拒单 |
| 3 | Max Gross Exposure | 分片预算 / 全局 | 事前同步（分片预算）+ 异步汇总（全局） | $5M | 拒加风险单；全局越限 → 只允许减仓 |
| 4 | Max Net Exposure | 同上 | 同上 | $2M | 同上 |
| 5 | Max Open Orders | 单标的 / 全局 | 事前同步 | 4 / 标的；200 / 全局 | 拒新单 |
| 6 | Max Orders / s | 会话 / 全局 | 事前同步（GCRA） | 依交易所或 Broker 限额的 80% | 拒单（策略降级） |
| 7 | Max Cancels / s | 同上 | 同上 | 同上，但为撤单预留额度 | 排队（撤单不得丢弃，详见 §10.5） |
| 8 | Price Collar | 单笔 | 事前同步 | 偏离参考价（mid）≤ max(100 bps, 5 tick) | 拒单 |
| 9 | Fat Finger | 单笔 | 事前同步 | 数量、名义金额、价格 ≤ 0、价格不在 tick 网格 | 拒单 + 告警 |
| 10 | Duplicate Order | 单笔 | 事前同步 | `cl_ord_id` 必须严格递增；同标的同方向同价同量在 X μs 内重复 | 拒单 + 告警（通常意味着策略 bug） |
| 11 | Stale Market Data | 单标的 / 全局 | 事前同步 | 最后行情更新距今 > 阈值（按标的活跃度设定）；feed lag > 阈值 | 拒加风险单；撤报价 |
| 12 | Sequence Gap | 全局 | 由 MD 线程置位，事前同步检查 | 任意缺口 | 拒所有新单；撤报价；进入恢复 |
| 13 | Max Inventory | 单标的 | 成交时同步更新分区，事前同步检查 | §9.2 | 单侧阻断 / 平仓 |
| 14 | Max Intraday Loss | 全局 / 分片 | 成交和盯市时同步更新，事前同步检查 | §11 | Kill Switch |
| 15 | Daily Drawdown | 全局 | 同上 | 盘中 NetPnL 从峰值回撤 > X | ReduceOnly → Kill |
| 16 | Broker Disconnect | 会话 | 网关线程置位 | 心跳超时 / 断线 | 拒新单；重连后对账前不恢复 |
| 17 | Market Data Disconnect | 全局 | MD 线程置位 | MoldUDP64 心跳超时（例如 > 2 s 无包） | 同 12 |
| 18 | Reject Rate | 会话 / 标的 | 回报时同步更新 | 最近 100 单中拒单率 > 10%，或连续 5 次拒单 | 标的冷却 / 全局阻断 + 告警 |
| 19 | Latency Spike | 全局 | 回报时同步更新（ACK 延迟），Telemetry 汇总内部延迟 | ACK 延迟 EWMA > 基线 × 5，或内部 P99 超预算 5 倍 | 置 `kBlkLatency`；加宽或撤报价 |
| 20 | Short Sale | 单笔 | 事前同步 | 不可借券 → 禁止卖空；SSR 生效 → 卖空价必须 > NBB | 拒单 |
| 21 | Trading Status | 单标的 | 事前同步 | 暂停 / LULD / 非连续交易时段 | 拒单 |
| 22 | System Health | 全局 | 事前同步（标志位） | 日志背压、时钟失步、CPU 过载（队列积压） | 拒新单 |
| 23 | Self-Cross | 单标的 | 事前同步 | 自己的 Bid ≥ 自己的 Ask | 拒单 |
| 24 | Decision Staleness | 单笔 | 事前同步 | 订单所依据的行情 `md_seq` 落后当前超过 N 条，或时间超过 M μs | 拒单（防止基于过期决策下单） |

### 9.4 跨分片的全局限额：预算切片

全局的 Gross/Net/Loss 限额如果每笔都要跨分片汇总，就会引入共享可写状态。方案如下：

1. Risk Coordinator 把全局限额切成分片预算：`Σ budget_i ≤ global_limit`。
2. 每个分片的 L1 只检查**本分片预算**。这是同步的，而且结果是保守的：任何时刻全局使用量都不会超过全局限额。
3. Coordinator 每 1–10 ms 读取各分片的使用量快照（单写者、seqlock 读），把预算从空闲分片再平衡给接近用满的分片。再平衡通过控制 SPSC 队列下发，分片在事件循环中应用。
4. 全局 PnL 由 Coordinator 汇总；如果全局亏损触及限额，就置全局 `kBlkKill`。各分片同时执行本分片的亏损切片检查（同步）。由于切片之和不超过全局限额，即使 Coordinator 延迟了，也不会超亏。代价是保守：某个分片可能在全局未超亏时先触发自己的切片限额。

Architecture A 只有单分片，所有限额都是精确的同步检查。

### 9.5 检查顺序与实现

**顺序原则**：
1. **一次 load 覆盖所有全局停止条件**：`global_block` 是一个 64 位字，每一位代表一种全局阻断原因。正常情况下这是一个高度可预测的分支。
2. **单标的状态**：`SymbolRisk` 正好一个缓存行，后续所有检查都命中这一行。
3. **便宜的字段校验**（数量、价格、重复、collar、不穿价）。
4. **算术型限额**（最坏情况持仓、敞口、亏损）。
5. **限速最后检查**，而且只"窥视"不扣减：被前面规则拒绝的订单不应消耗令牌。订单真正发送成功后才 `commit()`。

```cpp
[[nodiscard]] inline RiskVerdict
pre_trade_check(const OrderRequest& r, const RiskState& rs, uint64_t now_ns) noexcept {
  const uint64_t blk = rs.global_block->load(std::memory_order_acquire);
  const SymbolRisk& s = rs.sym[r.sym];

  if (r.action == OrderAction::Cancel) [[unlikely]] {
    // never block a cancel for risk reasons; only the broker-wide budget applies
    return rs.msg_rate.can_acquire(now_ns) ? RiskVerdict::Ok : RiskVerdict::MsgRate;
  }

  // (1) global stop conditions — one load, one predictable branch in the normal case
  constexpr uint64_t kHard = kBlkKill | kBlkStrategyOff | kBlkGatewayDown | kBlkMdGap |
                             kBlkClockBad | kBlkLatency | kBlkRejectRate | kBlkLogBackpressure;
  if (blk & kHard) [[unlikely]] {
    if (blk & kBlkKill) return RiskVerdict::KillSwitch;
    if (blk & kBlkGatewayDown) return RiskVerdict::GatewayDown;
    if (blk & kBlkMdGap) return RiskVerdict::BookInvalid;
    return RiskVerdict::StrategyDisabled;
  }
  // (2) per-symbol trading state + market data freshness
  if (s.flags & (kSymHalted | kSymBookInvalid)) [[unlikely]]
    return (s.flags & kSymHalted) ? RiskVerdict::SymbolHalted : RiskVerdict::BookInvalid;
  if (uint32_t(now_ns) - s.last_md_ts32 > rs.lim.md_stale_ns) [[unlikely]]
    return RiskVerdict::StaleMarketData;

  // (3) field sanity / fat finger / duplicate / collar / no-cross
  if (r.qty <= 0 || r.qty > rs.lim.max_order_qty) [[unlikely]]
    return r.qty <= 0 ? RiskVerdict::BadQty : RiskVerdict::MaxOrderSize;
  if (r.px <= 0) [[unlikely]] return RiskVerdict::BadPrice;
  if (r.cl_ord_id <= rs.last_cl_ord_id) [[unlikely]] return RiskVerdict::Duplicate;
  {
    const Price dev = r.px > s.ref_px ? r.px - s.ref_px : s.ref_px - r.px;
    const Price collar = std::max<Price>(s.ref_px * rs.lim.collar_bps / 10'000, rs.lim.collar_min_abs);
    if (dev > collar) [[unlikely]] return RiskVerdict::PriceCollar;
  }
  const bool buy = r.side == Side::Buy;
  if (r.post_only) {
    if (buy ? (s.best_ask > 0 && r.px >= s.best_ask) : (s.best_bid > 0 && r.px <= s.best_bid)) [[unlikely]]
      return RiskVerdict::WouldCross;
  }

  // (4) worst-case position / inventory / exposure / loss (assume every open order fills)
  const Qty add = r.qty - (r.action == OrderAction::Replace ? r.orig_leaves : 0);
  const Qty worst = buy ? s.position + s.open_buy + add : s.position - s.open_sell - add;
  const bool increases_risk = buy ? worst > 0 : worst < 0;
  if ((worst > s.max_position) | (worst < -s.max_position)) [[unlikely]] return RiskVerdict::MaxPosition;
  if (increases_risk && ((blk & kBlkReduceOnly) || (s.flags & (buy ? kSymBuyBlocked : kSymSellBlocked)))) [[unlikely]]
    return RiskVerdict::InventoryZone;
  if (!buy && worst < 0 && (s.flags & (kSymNoShort | kSymSsr))) [[unlikely]] {
    if (s.flags & kSymNoShort) return RiskVerdict::ShortSaleRestricted;
    if (r.px <= s.best_bid) return RiskVerdict::ShortSaleRestricted;  // Rule 201: above NBB (use NBB in prod)
  }
  if (r.action == OrderAction::New && s.open_orders >= rs.lim.max_open_orders_per_sym) [[unlikely]]
    return RiskVerdict::MaxOpenOrders;
  if (increases_risk) {
    const int64_t notional = add * r.px;
    if (rs.gross_worst_notional + notional > rs.lim.max_gross_notional) [[unlikely]] return RiskVerdict::GrossExposure;
    const int64_t net = rs.net_notional + (buy ? notional : -notional);
    if (net > rs.lim.max_net_notional || net < -rs.lim.max_net_notional) [[unlikely]] return RiskVerdict::NetExposure;
    if (rs.net_pnl <= -rs.lim.daily_loss_limit) [[unlikely]] return RiskVerdict::DailyLoss;
  }

  // (5) rate limits — peek only; tokens are spent in commit() after a successful send
  const Gcra& lim = r.action == OrderAction::New ? rs.order_rate : rs.replace_rate;
  if (!lim.can_acquire(now_ns)) [[unlikely]]
    return r.action == OrderAction::New ? RiskVerdict::OrderRate : RiskVerdict::ReplaceRate;
  if (rs.msg_rate.available(now_ns) <= 2) [[unlikely]] return RiskVerdict::MsgRate;  // reserve for cancels
  return RiskVerdict::Ok;
}

// The signature requested in the spec; the enum version is preferred (reason codes for logs).
inline bool pre_trade_check_bool(const OrderRequest& r, const RiskState& rs, uint64_t now) noexcept {
  return pre_trade_check(r, rs, now) == RiskVerdict::Ok;
}
```

**提交（commit）步骤**（发送成功后，同一线程执行）：扣减令牌 → `open_buy/open_sell += add` → `open_orders++` → `gross_worst_notional += …` → `last_cl_ord_id = r.cl_ord_id`。如果网关返回 `Throttled` 或 `Disconnected`，就不提交，撤销预占，确保风控状态与实际在途订单严格一致。

**Branch Prediction / Hot Path / Cache 设计说明**：

| 技术 | 应用 |
|---|---|
| Early Return | 每条规则失败时立即返回，失败分支用 `[[unlikely]]` 标注，编译器会把它们放到冷区，正常路径是一条顺序直线 |
| 位掩码合并 | 8 种全局阻断条件合并成一次 `blk & kHard` 测试；6 种标的状态合并成一次 `flags & mask` |
| 无分支比较 | `(worst > max) \| (worst < -max)` 用按位或，避免两个分支 |
| 缓存布局 | 整个检查只访问 3 个缓存行：`global_block`（只读共享，很少失效）、`SymbolRisk`（单写者、L1 常驻）、`RiskState` 热字段 |
| 整数运算 | 除了 collar 的 `ref_px * bps / 10000`，其余全是加减和比较；collar 的值可以在 `ref_px` 更新时预计算并缓存进 `SymbolRisk`，从热路径中去掉除法 |
| 时间戳 | `last_md_ts32` 只存低 32 位：无符号回绕减法在 4.29 s 内是正确的，这足以判断阈值在百毫秒级的陈旧度，同时把结构压进一个缓存行 |

**实测**：正常通过路径（全部检查通过）P50 36–48 ns、P99 39–60 ns【M-VM，含约 22–24 ns 的计时开销】。

**单元测试覆盖**（`reference/test_bench.cpp`）：正常通过、穿价、collar、超量、最坏情况持仓、陈旧行情、ReduceOnly 下拒绝加风险单但放行减风险单、Kill 状态拒单、Kill 状态下撤单放行。

---

## 10. Order Throttling

### 10.1 要求与选型

| 要求 | 方案 |
|---|---|
| O(1) | 每次检查只做常数次整数运算 |
| 无锁 / 单线程 | 每个限速器只属于一个线程（分片或 IBKR Adapter）；全局额度用预算切片 |
| 不用 mutex / sleep / 阻塞等待 | 额度不足时立即返回 false，由调用方决定**丢弃、合并到下一次 reconcile、还是降级** |
| 平滑限速 + 允许突发 | **令牌桶**（经典实现）或 **GCRA**（等价的时间域实现） |
| 严格遵守外部规则（"任意 1 秒窗口不超过 N 条"） | **滑动窗口日志**（N 个时间戳的环） |

### 10.2 Token Bucket：经典实现（整数"纳令牌"，惰性补充）

**补充算法**：不需要定时器线程。每次调用时根据距上次调用的时间差补充令牌：

$$
\text{tokens} \leftarrow \min(\text{capacity},\ \text{tokens} + \Delta t_{ns} \times \text{rate}_{/s})
$$

1 个令牌记为 $10^9$ 个"纳令牌"，这样 $\Delta t_{ns} \times \text{rate}_{/s}$ 直接得到纳令牌数，**不需要除法、不需要浮点**。$\Delta t$ 先截断到"把桶从空补满所需的时间"，同时防止长时间空闲后的 64 位溢出。

```cpp
class TokenBucket {
 public:
  static constexpr uint64_t kOne = 1'000'000'000ull;           // 1 token = 1e9 nano-tokens
  constexpr TokenBucket(uint64_t rate_per_sec, uint64_t burst) noexcept
      : rate_(rate_per_sec), cap_(burst * kOne), tokens_(burst * kOne),
        max_elapsed_(burst * kOne / rate_per_sec + 1) {}       // division only at construction
  bool try_acquire(uint64_t now_ns, uint64_t n = 1) noexcept {
    refill(now_ns);
    const uint64_t cost = n * kOne;
    if (tokens_ < cost) return false;   // caller decides: drop / defer / downgrade; never sleep
    tokens_ -= cost;
    return true;
  }
  uint64_t available(uint64_t now_ns) noexcept { refill(now_ns); return tokens_ / kOne; }
 private:
  void refill(uint64_t now_ns) noexcept {
    if (now_ns <= last_ns_) return;                 // monotonic clock; ignore non-increasing stamps
    uint64_t el = now_ns - last_ns_;
    last_ns_ = now_ns;
    if (el > max_elapsed_) el = max_elapsed_;       // bucket is full anyway; prevents overflow
    tokens_ = std::min(cap_, tokens_ + el * rate_);
  }
  uint64_t rate_, cap_, tokens_, max_elapsed_, last_ns_ = 0;
};
```

### 10.3 GCRA：同一语义，只需一个状态字

GCRA（Generic Cell Rate Algorithm）维护"理论到达时间" TAT。发射间隔 $T = 1/\text{rate}$，突发容忍度 $\tau = (B-1)T$。请求在 $t$ 时刻到达，**允许的条件是 $\max(TAT, t) - t \le \tau$**，允许后 $TAT \leftarrow \max(TAT, t) + T$。它与容量为 B、速率为 rate 的令牌桶完全等价，但状态只有一个 64 位整数，而且天然支持"先窥视、后提交"（风控需要这个特性）。

```cpp
class Gcra {
 public:
  constexpr Gcra(uint64_t rate_per_sec, uint64_t burst) noexcept
      : interval_ns_(1'000'000'000ull / rate_per_sec), tolerance_ns_((burst - 1) * interval_ns_) {}
  bool can_acquire(uint64_t now, uint64_t n = 1) const noexcept {       // pure: no side effect
    const uint64_t tat = tat_ns_ > now ? tat_ns_ : now;
    return tat + (n - 1) * interval_ns_ - now <= tolerance_ns_;
  }
  void commit(uint64_t now, uint64_t n = 1) noexcept {
    const uint64_t tat = tat_ns_ > now ? tat_ns_ : now;
    tat_ns_ = tat + n * interval_ns_;
  }
  bool try_acquire(uint64_t now, uint64_t n = 1) noexcept {
    if (!can_acquire(now, n)) return false;
    commit(now, n);
    return true;
  }
  uint64_t available(uint64_t now) const noexcept {                     // whole tokens left
    const uint64_t tat = tat_ns_ > now ? tat_ns_ : now;
    const uint64_t used = tat - now;
    return used > tolerance_ns_ + interval_ns_ ? 0 : (tolerance_ns_ + interval_ns_ - used) / interval_ns_;
  }
 private:
  uint64_t tat_ns_ = 0;        // theoretical arrival time
  uint64_t interval_ns_;       // T = 1/rate
  uint64_t tolerance_ns_;      // (burst-1) * T
};
```

### 10.4 滑动窗口（严格遵守"任意窗口 W 内最多 N 条"）

令牌桶允许"桶满时突发 B 条，之后按速率补充"。在两个相邻的 1 秒窗口交界处，这仍可能超过"任意 1 秒最多 N 条"这类外部规则。对这类规则，用精确的滑动窗口日志：记录最近 N 次被接受请求的时间戳，只有当最老的那一条已经超出窗口时才允许下一条。

```cpp
template <std::size_t N>
class SlidingWindowLimiter {
 public:
  explicit SlidingWindowLimiter(uint64_t window_ns) noexcept : window_ns_(window_ns) {}
  bool can_acquire(uint64_t now) const noexcept { return count_ < N || now - ts_[head_] >= window_ns_; }
  void commit(uint64_t now) noexcept {
    ts_[head_] = now;                 // overwrite the oldest
    head_ = (head_ + 1) % N;          // N is a compile-time constant
    if (count_ < N) ++count_;
  }
  bool try_acquire(uint64_t now) noexcept { if (!can_acquire(now)) return false; commit(now); return true; }
 private:
  std::array<uint64_t, N> ts_{};
  std::size_t head_ = 0, count_ = 0;
  uint64_t window_ns_;
};
```

O(1)，内存为 N × 8 字节（N = 50 时只有 400 字节）。

### 10.5 组合与优先级

```
                       ┌─────────────────────────── 每个分片 / IBKR Adapter ─────────────────────────┐
OrderRequest ─► 分类 ─► │ New      : order_rate (GCRA)   ┐                                            │
                       │ Replace  : replace_rate (GCRA) ├─► msg_rate (会话/账户总额度, GCRA)          │
                       │ Cancel   : cancel_rate (GCRA)  ┘    + SlidingWindow<N> (外部硬规则)         │
                       │                                                                            │
                       │ 预留规则：New/Replace 只能在 msg_rate.available() > R_cancel 时使用          │
                       │          （R_cancel = 当前在途订单数，保证任何时刻都能撤掉所有挂单）         │
                       └────────────────────────────────────────────────────────────────────────────┘
```

- **撤单绝不丢弃**：额度不足时，撤单进入一个固定大小的待发队列（容量 = 最大挂单数），在下一个额度可用的时刻优先发出。这是唯一允许"延后发送"的消息类型。由于预留规则存在，正常情况下不会发生。
- **New/Replace 被限速时不排队**：策略在下一次 reconcile 时会基于最新行情重新决策，旧决策直接丢弃，避免发出过期报价。
- **Architecture A**：账户级 `SlidingWindowLimiter<45>`（取 IBKR 文档中 50 msg/s 的 90%），并且所有 API 消息都要计入（包括行情请求，如果使用 IBKR 行情的话）【BD】。
- **Architecture B**：按交易所端口规则配置；另外按标的设置 `per_symbol` GCRA，防止单只标的的振荡吃光全部额度。
- 时间源：TSC 换算出的单调 ns，**禁止使用 wall clock**（NTP/PTP 调整会让限速器失效或卡死）。

单元测试【M-VM】：GCRA(10/s, burst 5) 在 t=0 恰好放行 5 次，之后每 100 ms 放行 1 次；TokenBucket 行为一致；`SlidingWindowLimiter<50>(1 s)` 在 3 秒内以 1 kHz 尝试，恰好放行 150 次。

---

## 11. Daily Loss Kill Switch

### 11.1 PnL 定义

$$
\text{NetPnL} = \text{RealizedPnL} + \text{UnrealizedPnL} - \text{Fees} + \text{Rebates}
$$

| 组成 | 计算 | 更新时点 |
|---|---|---|
| RealizedPnL | 平均成本法：减仓成交时 `(fill_px − avg_px) × closed_qty × sign` | 成交回报（同步） |
| UnrealizedPnL | **保守盯市**：多头按 best bid、空头按 best ask 估值（即清算价值，而不是按 mid）。如果对手盘为空或行情陈旧，就用最后可信价格并施加惩罚（haircut） | 持仓标的 TOB 变化时增量更新（同步） |
| Fees | 佣金（Broker）+ 交易所取流动性费 + SEC Section 31 费（卖出）+ FINRA TAF（卖出）+ 清算费。成交时按费率表**估算**；IBKR 的 `commissionReport` 异步到达后再核对，取 `max(估算, 实际)` | 成交回报（估算，同步）；核对（异步） |
| Rebates | 按回报中的流动性标志（Added）和费率表估算；月度阶梯费率按**保守档**估算 | 成交回报（同步） |

全部用 1e-4 美元的 `int64_t` 定点数。费率表属于配置项，每日加载并记录版本号。

**Daily Drawdown**：`drawdown = peak_net − net`，其中 `peak_net` 是当日 NetPnL 的高水位。它与亏损限额分开设置（例如亏损限额 $50k，回撤限额 $30k）。

### 11.2 状态机

```
                 loss ≥ 60% limit          loss ≥ 85% limit            loss ≥ 100% limit
 ┌─────────┐   ───────────────────►  ┌─────────┐  ─────────────►  ┌─────────────┐  ─────────────►  ┌──────────┐
 │  ARMED  │                         │ WARNING │                  │ REDUCE_ONLY │                  │ TRIPPED  │
 └─────────┘                         └─────────┘                  └─────────────┘                  └────┬─────┘
   ▲   (日内不自动降级：一旦升级，只能停留或继续升级，避免在阈值附近来回切换)                             │
   │                                                                                    执行动作 1→6，完成后
   │                                                                                                    ▼
   │       Supervisor reset：已认证的操作员 + 理由 + 持仓已对账 + 行情健康                         ┌──────────┐
   └────────────────────── (可选：双人复核；新交易日自动回到 ARMED) ◄───────────────────────────── │  LOCKED  │
                                                                                                  └──────────┘
 WARNING     : 告警；价差 ×1.5；报价量 ×0.5
 REDUCE_ONLY : 置 kBlkReduceOnly —— L1 只放行减风险订单；告警（page）
 TRIPPED     : 见 11.3
 LOCKED      : 所有新单被拒；只有 Supervisor 能解除；状态持久化，进程重启后仍然是 LOCKED
```

### 11.3 触发后的动作（严格顺序）

| 步骤 | 动作 | 同步/异步 | 说明 |
|---|---|---|---|
| 1 | **Reject New Orders** | 同步，在触发它的同一个调用中 | `global_block \|= kBlkKill`（原子 `fetch_or`）。从这一刻起，所有分片的 L1 第一行检查都会拒单 |
| 2 | **Cancel All Orders** | 同步发起 | 本分片立即对所有在途/挂单发撤单（撤单绕过风控，并且有预留额度）；B 中还可以使用交易所的 Mass Cancel 功能（若可用）；A 中调用 `reqGlobalCancel()` |
| 3 | **Disable Strategy** | 同步 | 所有标的 `mode = Disabled`；策略的 reconcile 不再生成任何请求 |
| 4 | **Trigger Kill Switch（全局传播）** | 同步置位 + 异步传播 | 其他分片在下一次循环中看到 `kBlkKill`，各自执行步骤 2–3；Coordinator 通知独立 Watchdog；B 中可以通过 Sponsor/交易所的 Kill Switch 接口做账户级封禁 |
| 5 | **Persist Risk Event** | 异步，但**必须落盘确认** | 写入审计日志（带 fsync）：时间、触发值、持仓、挂单、PnL 分解、配置版本。落盘确认之前，状态不能从 TRIPPED 迁移到 LOCKED |
| 6 | **Notify Monitoring** | 异步 | 最高级别告警（电话/短信 + 看板） |

步骤 1–3 在触发线程内同步完成，耗时为微秒级（主要是撤单报文的编码和发送）。持久化和通知不在 Hot Path 上，但它们决定了状态能否进入 LOCKED。

### 11.4 伪代码

```cpp
// Synchronous, called by the shard thread on every fill and on every mark-to-market update.
void Shard::on_pnl_changed() noexcept {
  pnl_.net = pnl_.realized + pnl_.unrealized - pnl_.fees + pnl_.rebates;
  pnl_.peak_net = std::max(pnl_.peak_net, pnl_.net);
  pnl_.drawdown = pnl_.peak_net - pnl_.net;
  risk_.net_pnl = pnl_.net;                                   // L1 reads this on the next order

  const bool dd_breach = pnl_.drawdown >= cfg_.max_drawdown;
  if (kill_.on_pnl(pnl_.net) || (dd_breach && kill_.force_trip())) [[unlikely]]
    on_kill_tripped();
}

void Shard::on_kill_tripped() noexcept {                       // cold path, still synchronous
  // (1) already done inside KillSwitch: global_block |= kBlkKill
  for (uint32_t oi : oms_.live_orders()) send_cancel_bypass_risk(oi);        // (2)
  if (gateway_.supports_mass_cancel()) gateway_.cancel_all();
  strategy_.disable_all();                                                   // (3)
  control_out_.push(ControlMsg::KillPropagate{shard_id_, pnl_.net});        // (4) -> coordinator/watchdog
  audit_.push_critical(RiskEvent::KillSwitch{now(), pnl_, positions_snapshot()});  // (5) fsync'd by logger
  telemetry_.alert(Alert::Critical, "KILL_SWITCH", pnl_.net);               // (6)
}

// Logger thread acks durability -> shard marks Locked.
void Shard::on_audit_durable(uint64_t event_id) noexcept {
  if (event_id == kill_event_id_) kill_.on_actions_complete();   // TRIPPED -> LOCKED
}
```

`KillSwitch` 类的核心逻辑（摘自 `reference/risk.hpp`，已测试）：

```cpp
enum class KillState : uint8_t { Armed, Warning, ReduceOnly, Tripped, Locked };

class KillSwitch {
 public:
  bool on_pnl(int64_t net_pnl) noexcept {
    if (state_ >= KillState::Tripped) return false;           // latched
    const int64_t loss = -net_pnl;
    if (loss >= c_.daily_loss_limit) {
      state_ = KillState::Tripped;
      blk_->fetch_or(kBlkKill, std::memory_order_acq_rel);   // (1) reject new orders from now on
      return true;
    }
    const KillState next = loss * 100 >= c_.daily_loss_limit * c_.reduce_pct ? KillState::ReduceOnly
                         : loss * 100 >= c_.daily_loss_limit * c_.warn_pct   ? KillState::Warning
                                                                            : KillState::Armed;
    if (next > state_) {                                      // never auto de-escalate intraday
      state_ = next;
      if (state_ == KillState::ReduceOnly) blk_->fetch_or(kBlkReduceOnly, std::memory_order_acq_rel);
    }
    return false;
  }
  bool force_trip() noexcept {                               // drawdown / operator / watchdog
    if (state_ >= KillState::Tripped) return false;
    state_ = KillState::Tripped;
    blk_->fetch_or(kBlkKill, std::memory_order_acq_rel);
    return true;
  }
  void on_actions_complete() noexcept { if (state_ == KillState::Tripped) state_ = KillState::Locked; }

  // Reachable only from the Supervisor control channel (authenticated + audited).
  struct SupervisorToken { uint64_t operator_id; uint64_t nonce; };
  bool supervisor_reset(const SupervisorToken& t, bool positions_reconciled, bool md_healthy) noexcept {
    if (state_ != KillState::Locked || !positions_reconciled || !md_healthy || t.operator_id == 0) return false;
    state_ = KillState::Armed;
    blk_->fetch_and(~(kBlkKill | kBlkReduceOnly), std::memory_order_acq_rel);
    return true;
  }
 private:
  KillConfig c_;
  std::atomic<uint64_t>* blk_;
  KillState state_ = KillState::Armed;
};
```

### 11.5 "Strategy 不能自行复位"如何保证

C++ 的访问控制不是安全边界，因此靠的是**架构隔离**：

1. **对象所有权**：`KillSwitch` 归 Risk 层所有；Strategy 只拿到 `const RiskView&`，编译期就没有可写接口。
2. **通道隔离**：`supervisor_reset` 只能由控制线程调用，而控制线程只消费来自 Supervisor 进程的认证消息（Unix domain socket + 双向认证，或本机 mTLS）。`SupervisorToken` 由 Supervisor 进程签发（HMAC：操作员 ID + nonce + 时间戳），Strategy 代码路径上不存在签发它的密钥。
3. **持久化锁存**：TRIPPED/LOCKED 状态写入审计日志。进程启动时先读当日的风控事件日志，如果当天已经触发过 Kill，就以 LOCKED 状态启动，**重启不能绕过 Kill**。
4. **外部冗余**：独立 Watchdog 进程同样会监控 PnL（来自 Broker 的成交回报副本或 drop copy），并且具有带外撤单能力。即使交易进程本身出错，外部仍然能执行 Kill。
5. **复位条件**（代码中强制）：状态必须是 LOCKED（动作已完成且已落盘）；持仓已与 Broker 对账；行情健康；有操作员身份。可选：双人复核；复位后自动进入 REDUCE_ONLY 观察期，再由人工切换到 ARMED。

**单元测试**【M-VM】：60% → Warning；85% → ReduceOnly（加风险单被拒、减风险单放行）；亏损回落后**不降级**；100% → Tripped 并置位 `kBlkKill`；Kill 状态下新单被拒、撤单放行；LOCKED 之前复位被拒绝；LOCKED 之后满足条件的复位成功并清除阻断位。

---

## 12. Critical Path：Market Tick → Order

### 12.1 逐步分析（Architecture B）

| # | 步骤 | 线程 / 核 | 发生了什么 | 触及的数据 | 估计耗时 |
|---|---|---|---|---|---|
| 0 | 包到达网卡 | NIC | 硬件打 RX 时间戳（T0），DMA 写入预先注册的 RX 环（HugePage） | RX 描述符环 | — |
| 1 | 轮询收包 | MD 核 | `ef_eventq_poll` / `rte_eth_rx_burst` 返回；读取 TSC（T1） | 事件队列、包缓冲 | 0.8–1.2 μs（线上→用户态）【E】 |
| 2 | 线路仲裁 | MD 核 | A/B 两路先到者胜：`seq < expected` 的包直接丢弃（已由另一路送达） | `expected_seq` | < 10 ns |
| 3 | 序号校验 | MD 核 | `seq == expected` → 继续；`seq > expected` → 缺口处理（§13.1）；`count == 0` → 心跳 | Sequencer 状态 | 5–10 ns |
| 4 | 解帧 + 解码 | MD 核 | 遍历 MoldUDP64 消息块，对每条调用 `decode_itch()`（原地零拷贝读取） | 包缓冲 | 10–20 ns/msg |
| 5 | 过滤 + 路由 | MD 核 | `slot_[locate]` 查表：不在宇宙内 → 丢弃；否则按 `shard_of[locate]` 推入对应分片的 SPSC（T2） | 64 KB 查表数组、SPSC 环 | 10–20 ns |
| 6 | 跨核 | — | SPSC 缓存行从 MD 核迁移到分片核 | 1–2 个缓存行 | 80–150 ns【E】 |
| 7 | 出队 | 分片核 | `try_pop`（T3）；更新 `SymbolRisk.last_md_ts32` | SPSC、SymbolRisk | 10 ns |
| 8 | 订单簿更新 | 分片核 | `BookManager::apply()`：哈希查找/插入 → 价位 → 位图 → 最优价；返回"TOB 是否变化"（T4） | OrderMap、OrderPool、Ladder | 50–150 ns |
| 9 | 市场状态 | 分片核 | 交易状态、TOB 合理性（不交叉）、feed lag = PTP_now − exch_ts | MarketSnapshot | 10–20 ns |
| 10 | 标脏 | 分片核 | TOB 变化（或成交） → `strategy_state.dirty = 1`；批内后续事件继续处理 | StrategyState | < 5 ns |
| 11 | 特征 | 分片核 | 批末对脏标的增量更新 microprice、OBI、σ、TI | Features | 50–200 ns |
| 12 | 行情过滤 | 分片核 | 暂停/价差/深度/波动/陈旧 → 决定 TwoSided / OneSided / Pulled | Features、阈值 | 10–30 ns |
| 13 | 定价 | 分片核 | `compute_quotes()` → 期望报价（T5） | Features、InventoryState、QuoteParams | 50–100 ns |
| 14 | Reconcile | 分片核 | 期望 vs 实际：槽位是否空闲？价差 ≥ 1 tick？防御还是进攻？额度够吗？→ 生成 0–2 个 `OrderRequest` | StrategyState、Order | 20–50 ns |
| 15 | 事前风控 | 分片核 | `pre_trade_check()`（T6） | global_block、SymbolRisk、RiskState | 30–60 ns |
| 16 | OMS | 分片核 | 分配 Order 槽位，生成 cl_ord_id（内含池下标），状态 → PendingNew/PendingReplace，记录 `sent_tsc` | OrderPool | 20–50 ns |
| 17 | 编码 | 分片核 | 在预填充的 OUCH 报文模板中改写 token、价格、数量；SoupBinTCP 帧头（T7） | 报文模板（L1 常驻） | 30–80 ns |
| 18 | 发送 | 分片核 | 内核旁路 TCP 发送（TCPDirect/Onload） | TX 环 | 1–2 μs（到线上）【E】 |
| 19 | 风控提交 | 分片核 | 扣令牌、`open_buy/sell`、`open_orders`、敞口预占 | RiskState、SymbolRisk | 10–20 ns |
| 20 | 日志入队 | 分片核 | 决策、风控和订单记录（POD）写入日志 SPSC | 日志环 | 20–40 ns |
| 21 | 线上 | NIC | 硬件 TX 时间戳（T8） | — | — |
| 22 | 交易所 | 外部 | 撮合引擎处理 → ACK（T9）【XD】 | — | 数十 μs |
| 23 | 回报 | 分片核 | `gateway_.poll()` → 解码 Accepted → Order 状态 → Live；记录 ACK 延迟 | Order | 50–100 ns |

**合计**：T1→T7 ≈ 0.5–1.5 μs；T0→T8 ≈ 3–5 μs（P50）【E】。

**关键洞察**：
- 用户态计算本身（步骤 2–17）不到 1 μs，**网卡收发（步骤 1 和 18）合计约 2–3 μs，是软件方案的大头**。所以 B 的 P0 优化是网卡和内核旁路，而不是在策略代码上抠纳秒。
- 步骤 20 的日志入队放在发送之后，保证日志不阻塞关键路径。如果日志环满了，订单已经发出，这时设置 `kBlkLogBackpressure`，阻止后续新单（§14.6）。

### 12.2 Architecture A 的差异

```
步骤 1–15 同上（内核网络栈：T0→T1 ≈ 10–30 μs）
16. OMS：cl_ord_id 分配，状态 PendingNew
17'. 推入 SPSC<OrderRequest> → IBKR Adapter 线程                     0.2–1 μs
18'. Adapter：SlidingWindow(45/s) 检查 → 通过则映射 cl_ord_id→orderId
     → EClient::placeOrder(orderId, contract, order)（T8′）           5–20 μs
19'. TWS API 报文 → localhost TCP → IB Gateway (JVM)                  【BD】0.1–1 ms
20'. IB Gateway → 公网/VPN → IBKR：风控、SmartRouting → 交易所          【BD】ms 级
21'. orderStatus("Submitted") / openOrder 回调（T9′）→ 归一化为 ExecutionReport::Ack
     → SPSC<ExecutionReport> → 分片线程 → OMS 状态 → Live
```

**A 的关键路径分析结论**：内部部分（T1→T8′）约 20–50 μs；从 T8′ 开始全部是 Broker 的时间。因此在 A 中，**优化 IBKR 之前的 C++ 代码几乎不会改变端到端结果**，工程重点应该放在：正确性、限速调度、断线恢复，以及测量 IBKR 的延迟分布并把它反馈给策略参数。

### 12.3 关键流程时序图

**(1) Market Data Flow**
```
NASDAQ ─► Switch(L1) ─► NIC(RX ts T0) ─► MD Gateway[poll T1] ─► A/B Arbitration ─► Sequencer
   ─► ITCH Decoder ─► Locate Filter ─► SPSC[T2] ─► Shard[T3] ─► Order Book[T4] ─► Market State
   ─► Signal Engine ─► (dirty) ─► Strategy ─► Pricing ─► Inventory ─► desired quotes[T5]
   (side branches: Recorder ◄─ raw packets ; Logger ◄─ sampled events)
```

**(2) Order Flow**
```
Strategy(desired) ─► Quote Reconciler ─► OrderRequest ─► Pre-Trade Risk[T6] ──reject──► log + counters
                                                             │ ok
                                                             ▼
                                   Order Manager (alloc Order, cl_ord_id, PendingNew)[T7]
                                                             ▼
                        B: OuchGateway encode ─► NIC TX[T8] ─► NASDAQ
                        A: SPSC ─► IBKR Adapter ─► RateLimiter ─► placeOrder[T8′] ─► IB Gateway ─► IBKR ─► Venue
                                                             ▼
                                                  Risk commit (tokens, open qty, exposure)
```

**(3) Execution Report Flow**
```
Venue ─► (B) OUCH session ─► Shard poll        (A) IBKR ─► IB Gateway ─► EReader ─► Adapter normalize ─► SPSC ─► Shard
      ─► Normalize ExecutionReport ─► Dedup(exec_id) ─► OMS state machine ─► Position ─► PnL(realized/unrealized/fees)
      ─► Inventory zone ─► KillSwitch.on_pnl() ─► SymbolRisk/RiskState update ─► Strategy dirty ─► (re-quote)
      ─► Logger (critical record)
```

**(4) Cancel / Replace Flow**
```
Strategy: new desired px ─► Reconciler: slot LIVE & idle? Δpx ≥ 1 tick? defensive or min_life passed? budget ok?
   ─► Risk (Replace: worst-case uses max(old,new) leaves) ─► OMS: LIVE → PENDING_REPLACE (pending_px/qty)
   ─► Gateway send Replace
Venue ─► Replaced ─► OMS: px=pending_px, leaves=new ─► LIVE ─► Reconciler compares with latest desired
      ─► ReplaceReject ─► OMS: back to LIVE (old px) ─► cooldown / reason handling
      ─► Fill while pending ─► apply fill first; keep PENDING_REPLACE; correct leaves on Replaced
Cancel: LIVE → PENDING_CANCEL ─► Canceled ─► EMPTY | CancelReject("too late") ─► await Fill / final status
```

**(5) Partial Fill Flow**
```
Venue ─► Exec(last_qty < leaves) ─► Dedup ─► OMS: leaves -= last_qty, cum += last_qty (state stays LIVE)
      ─► Position/avg_px ─► PnL ─► Inventory zone (may block one side) ─► SymbolRisk.open_* -= last_qty
      ─► Strategy dirty ─► Reconciler: keep remainder? top-up? (avoid churn: only if size gap ≥ threshold)
```

**(6) Risk Event Flow**
```
Trigger (any of): reject-rate / latency spike / stale MD / gap / inventory breach / drawdown / log backpressure
   ─► Owner thread sets flag (global_block bit or SymbolRisk.flags) synchronously
   ─► Next pre_trade_check sees it (same or other shard, ≤ 1 loop iteration)
   ─► Strategy reacts (pull / one-sided / widen) ─► Logger(critical) ─► Telemetry ─► Alerting
   ─► Clear: condition resolved + hysteresis/observation window (+ Supervisor if latched)
```

**(7) Kill Switch Flow**
```
Fill / mark ─► PnL.net ≤ −DailyLossLimit ─► KillSwitch: TRIPPED
   ─► (1) global_block |= kBlkKill  ─► all new orders rejected everywhere
   ─► (2) cancel all live orders (bypass risk, reserved budget; mass cancel / reqGlobalCancel)
   ─► (3) strategy.disable_all()
   ─► (4) propagate: other shards, Risk Coordinator, Watchdog (out-of-band), exchange/sponsor kill (B)
   ─► (5) audit record fsync ─► durability ack ─► LOCKED
   ─► (6) critical alert
LOCKED ─► only Supervisor (authenticated, reconciled, MD healthy) ─► ARMED / REDUCE_ONLY
```

**(8) Recovery Flow**
```
Start/Reconnect ─► SAFE mode ─► load config & risk-event journal (Kill latched?) ─► sessions login
   ─► fetch broker truth: open orders, executions, positions ─► reconcile vs journal
   ─► cancel unknown orders ─► rebuild books (GLIMPSE snapshot + live ITCH / vendor snapshot)
   ─► recompute PnL ─► self-checks (book, clock, logger, watchdog) ─► Supervisor confirm
   ─► REDUCE_ONLY observation ─► ARMED
```

---

## 13. Failure Handling

### 13.1 故障总表

| 故障 | 检测 | 立即动作（Fail Safe） | 恢复 | 升级到 Kill |
|---|---|---|---|---|
| **ITCH Sequence Gap** | MoldUDP64 `seq > expected` | 置 `kBlkMdGap`：所有标的停止新报价并撤销现有报价（ITCH 没有按标的的序号，任何缺口都可能影响任意标的）；缓存后续包 | ① 另一路（B 线）是否已补齐 → 继续；② 向 Re-request 服务器请求缺失区间（小缺口，ms 级）；③ 超时或缺口过大 → GLIMPSE 快照重建全簿，再回放缓存的增量 → 校验 → 清除阻断 | 恢复失败超过 T 秒 → 当日停止该会话的交易并告警 |
| **UDP Packet Loss** | 同上（以缺口形式出现）；同时统计 NIC drop 计数 | 同上 | 同上；如果是持续丢包，检查 RX 环大小、CPU 是否跟得上、交换机拥塞 | 丢包率超阈值 → 停止交易 |
| **Out-of-Order Packet** | `seq > expected`，但缺失的包在短时间内到达 | 先放入重排缓冲（预分配，窗口 K 个包、等待 ≤ X μs），**不立即**判定为缺口 | 缺失包在窗口内到达 → 按序处理；超时 → 按缺口处理 | — |
| **行情延迟** | feed lag = PTP_now − exch_ts > 阈值（例如 B：500 μs；A：50 ms）；MD 队列积压 | 加宽或撤报价；严重时置全局阻断 | 延迟回落并保持 N 秒 → 恢复 | 持续 > T → 告警 + 停止 |
| **行情停止** | MoldUDP64 心跳超时（例如 > 2 s 无任何包，含心跳） | 置 `kBlkMdGap`，撤销所有报价 | 心跳恢复 → 检查序号连续性 → 如有缺口按缺口流程处理 | 超时 > T → Kill 并人工介入 |
| **IB API Disconnect** | `connectionClosed()` 回调；错误码 1100（IB ↔ TWS 连接丢失）、2110（TWS ↔ 服务器）；心跳（`reqCurrentTime`）超时 | 置 `kBlkGatewayDown`；所有订单标记为 Unknown；**断线期间挂单仍在市场上**：Watchdog 通过备用连接（另一个 IB Gateway 实例 / clientId）发 `reqGlobalCancel()`，并人工备选（IBKR 网页/手机端） | 重连 → `reqOpenOrders`/`reqAllOpenOrders`、`reqExecutions`、`reqPositions` → 与 OMS 逐笔对账 → 撤掉所有未知订单 → 人工确认（或在规则允许时自动）恢复。注意 1101（恢复但数据丢失）与 1102（恢复且数据保留）需要分别处理【BD】 | 撤单无法确认 → 人工介入 |
| **Broker Reject** | 回报 Reject（OUCH `J`；IBKR `error()` 回调中的订单相关错误） | 槽位 → EMPTY + 冷却；按原因分类：瞬时（限速、pacing）→ 退避；永久（不可卖空、保证金不足、价格非法）→ 该标的的该方向停用 | 冷却结束 → 恢复 | 拒单率熔断（§9.3 #18） |
| **订单 ACK 超时** | 定时轮：`now − sent_tsc > T_ack`（B：如 5 ms；A：如 2 s） | 订单 → Unknown；该标的停止新动作；尝试撤单（交易所会对未知订单回 CancelReject，这本身也是信息）；计入延迟熔断 | 收到迟到的 ACK 或拒单 → 状态归位；IBKR：`reqOpenOrders` 查询 | 多个订单 Unknown → 全局阻断 |
| **部分成交** | Fill 回报中 `leaves > 0` | 正常业务：更新持仓、PnL、库存分区；槽位保持 Live（剩余量） | Reconcile 决定是否补量（避免频繁补量导致的过度撤改） | — |
| **重复 Execution Report** | `exec_id` 已见过（OUCH match number；IBKR `execId`） | 丢弃 + 计数；**绝不重复计入持仓** | IBKR 修正回报：`execId` 的后缀会变化（以 IBKR 文档为准），要按"同一笔成交的修正"处理（替换，而不是累加） | 对账不一致 → 停止该标的 + 人工核查 |
| **Strategy Exception** | Hot Path 不使用异常；改为不变量检查：NaN/Inf、价格 ≤ 0、Bid ≥ Ask、数量越界 | L1 风控兜底拒单；该标的置 Disabled；计数告警 | 人工排查后由 Supervisor 启用 | 多标的触发 → 全局停用 |
| **Risk Engine Failure** | 风控状态无效（配置未加载、持仓未对账、内部断言失败） | **Fail-closed**：拒绝所有加风险订单，只允许撤单；进程崩溃时依靠外部防线（L3） | 修复后重启，走恢复流程（§13.3） | 直接 Kill |
| **NIC Failure** | 链路断开（ethtool/驱动事件）；RX 计数停滞；TX 错误 | MD：切换到另一路/另一块网卡；订单入口：交易所 Cancel-on-Disconnect 会撤销挂单（B） | 用备用网卡重建会话 → 对账 → 人工确认后恢复 | 双网卡同时故障 → Kill |
| **CPU Overload** | MD 队列深度 > 阈值；事件循环迭代时间 P99 超标；批大小持续达到上限 | 订单簿已落后于市场，标记 stale → 撤报价；降级非关键工作（降低特征更新频率等） | 队列排空且延迟恢复 → 恢复 | 持续 → 告警 + 停止 |
| **Clock Drift** | PTP offset（`phc2sys`/`ptp4l` 状态）> 阈值（如 B：1 μs，A：1 ms）；TSC 与 PTP 的相对漂移 | 时间戳和延迟统计不可信 → 置 `kBlkClockBad`（可配置为只告警）；限速器和超时**使用 TSC 单调时钟，不受影响** | PTP 重新锁定 → 清除 | 只影响审计时间戳的准确性 → 合规风险告警 |
| **策略失控（Runaway）** | 单标的改单频率 > N 次/秒且持续；消息额度持续接近上限；OER 突增；报价在两个价位间振荡（A→B→A 模式计数）；同方向连续成交且库存速度超标 | 该标的 Disabled + 撤单；多标的同时触发 → 全局 `kBlkStrategyOff` | 人工排查（回放当日日志复现）后由 Supervisor 启用 | 触发全局停用后仍有异常发单（理论上不应发生）→ Watchdog 带外 Kill |
| **Broker API 延迟异常** | T8′→T9′ 的 EWMA / P99 超过基线 × K（A）；ACK 延迟尖峰（B） | 置 `kBlkLatency`：停止进攻性报价，加宽价差，增大 `min_quote_life` | 延迟回落并保持 N 秒 → 逐步恢复 | 持续 → 停止交易 |
| **System Restart** | 进程启动 | 以 **SAFE 模式**启动：不报价 | §13.3 恢复流程 | 当日已触发 Kill → 以 LOCKED 启动 |
| **日志背压** | 日志 SPSC 满 | 置 `kBlkLogBackpressure`：停止新单（审计日志不允许丢失） | 队列回落 → 清除 | 持续 → 停止 |
| **独立 Watchdog 失联** | 交易进程发出的心跳没有被确认 | 告警；交易可以继续，但限额收紧（可配置） | — | — |

### 13.2 序号缺口恢复流程

```
MD 线程                                  分片线程                             外部
   │ seq > expected                         │                                   │
   ├─► global_block |= kBlkMdGap ──────────►│ L1 拒绝新单；撤销所有报价           │
   │   缓存后续包（预分配环，容量 C）          │                                   │
   │                                        │                                   │
   ├─ 另一路是否已在 X μs 内补齐？ ── 是 ──► 按序处理 → 清除 kBlkMdGap            │
   │      │ 否                                                                   │
   │      ▼                                                                      │
   ├─► Re-request(session, seq, count) ───────────────────────────────────────►│ MoldUDP64 重传服务器
   │◄──────────────────────────────── 重传包（单播） ◄──────────────────────────│
   │   全部补齐且在时限内？ ── 是 ──► 应用重传 + 缓存 → 清除 kBlkMdGap
   │      │ 否（或缺口 > N 条、缓存将满）
   │      ▼
   ├─► 所有标的置 kSymBookInvalid；GLIMPSE 登录 ──────────────────────────────►│ GLIMPSE（SoupBinTCP）
   │◄──────────── 全簿快照 + 快照对应的 ITCH 序号 S ◄────────────────────────────│
   │   清空订单簿 → 应用快照 → 回放缓存中 seq > S 的消息 → 订单簿自检（不交叉、无未知订单号）
   │   → 清除 kSymBookInvalid / kBlkMdGap → 策略进入"观察期"（N 秒只更新特征，不报价）
```

### 13.3 系统重启 / 恢复流程

```
1. 进程启动 → mlockall → 分配 Arena → 读取配置快照（版本号写入日志）
2. 读取当日风控事件日志：
     当日已有 KillSwitch 事件 → KillSwitch 状态 = LOCKED
     已有锁定标的 → 恢复锁定状态
3. 连接 Broker / 交易所会话（SoupBinTCP 登录时带上次序号以获取补发回报；IBKR 用 nextValidId）
4. 拉取 Broker 侧的权威状态：未结订单、当日成交、持仓
5. 与本地审计日志重建的状态对账：
     持仓不一致 → 以 Broker 为准，记录差异并告警（page），该标的锁定，等待人工确认
     有未知的未结订单 → 一律撤单
6. 行情：GLIMPSE 快照 + 实时 ITCH 重建订单簿（A：回放或供应商快照）
7. PnL：用当日全部成交重算已实现 PnL 和费用 + 当前盯市
8. 自检：订单簿一致性、风控状态有效、时钟同步、日志可写、Watchdog 心跳正常
9. 保持 SAFE 模式 → Supervisor（人工）确认 → 进入 REDUCE_ONLY 或 ARMED
```

**原则**：**持仓与订单以 Broker/交易所为权威数据源**；本地日志用于重建和审计。任何不一致都不能自动"抹平"，必须留痕并人工确认。

### 13.4 故障注入测试（必须进入 CI / 仿真）

在 `SimGateway` 和 MD 回放器中注入以下故障：丢包（随机/突发）、乱序、重复包、MD 停流、ACK 延迟/丢失、重复回报、修正回报、撤单途中成交、改单途中成交、拒单风暴、断线（发送前/后）、时钟跳变、日志盘写满。断言：持仓永远正确、风控永不放行越限订单、Kill 永远可以触发、恢复后状态与 Broker 一致。

---

## 14. Performance Optimization

### 14.1 分级总表

| 优化项 | Arch A | Arch B | 预期收益 | 说明 / 评价 |
|---|---|---|---|---|
| Zero Allocation Hot Path | **P0** | **P0** | 消除 μs–ms 级尾延迟 | 回放测试中断言分配次数为 0 |
| Object Pool / Pre-allocation | **P0** | **P0** | 同上 | §6 |
| Cache Alignment / 避免 False Sharing | **P0** | **P0** | 跨核写共享的代价是 100 ns 级 | 实现成本低，收益确定 |
| 无锁 SPSC / 单写者 | **P0** | **P0** | 去掉锁和上下文切换 | |
| 异步日志（Hot Path 无 IO） | **P0** | **P0** | 同步 IO 是 ms 级抖动 | §14.6 |
| CPU Pinning | **P0** | **P0** | 消除迁移和缓存失效 | `pthread_setaffinity_np` |
| TSC 计时 | **P0** | **P0** | `clock_gettime`（vDSO）约 20 ns，TSC 约 7–10 ns，并且可以直接打点 | 需要 invariant TSC；启动时校准；跨核一致性要验证 |
| Memory Locking + Prefault | P1 | **P0** | 消除缺页（μs 级） | `mlockall` + 启动时触碰所有内存 |
| isolcpus / nohz_full / rcu_nocbs | P1 | **P0** | 消除定时器中断和 RCU 回调（μs 级抖动） | 虚拟机上作用有限 |
| IRQ Affinity | P1 | **P0** | 中断离开交易核 | 关闭 irqbalance |
| NUMA Binding | P1（单路可忽略） | **P0** | 跨 NUMA 访问 +50–100 ns/缺失 | `numactl --cpunodebind --membind` |
| Busy Polling | P1（`SO_BUSY_POLL`） | **P0**（内核旁路天然轮询） | 去掉中断唤醒（数 μs） | 代价是 100% 占用 CPU |
| Kernel Bypass（ef_vi/Onload/TCPDirect、ExaNIC） | ❌（与 IBKR 无关） | **P0** | 线上→用户态从 5–15 μs 降到约 1 μs【E】 | B 中收益最大的单项 |
| HugePages | P1 | **P0** | 订单哈希表随机访问时减少 TLB 缺失 | 1 GB 页；关闭 THP（防止合并时的停顿） |
| Branch Prediction（`[[likely]]`、冷热分离） | P1 | P1 | 每次误预测约 15–20 个周期 | 先用 `perf stat` 看误预测率再决定 |
| PGO / LTO | P2 | P1 | 典型收益 5–15%【E】 | 用真实行情回放生成 profile |
| Hardware Timestamping | P2 | **P0**（测量必需） | 不降低延迟，**但没有它就无法正确测量** | |
| PTP | P2（NTP/chrony 足够） | **P0** | 跨机时间一致性（审计、feed lag） | GPS 授时的 grandmaster + 支持 PTP 的交换机 |
| Zero Copy | P2 | **P0** | 解码直接读 RX 环，省一次拷贝（约 10–50 ns） | ef_vi/DPDK 可以做到 |
| SIMD | P2 | P2 | 小 | ITCH 是逐条可变长消息，SIMD 很难发挥作用；多标的批量特征计算和深度求和有一点价值。**基本属于过度优化** |
| AF_XDP | P2 | P1（没有专用网卡时） | 介于内核栈和 ef_vi/DPDK 之间 | 开源、无厂商锁定；尾延迟不如专用方案 |
| DPDK | ❌ | P1/P2 | 与 ef_vi 相当 | 订单入口需要用户态 TCP 栈（复杂度高）；更适合只收 UDP 的行情 |
| Solarflare / Onload | ❌ | **P0**（或同类方案） | | Onload 可以透明加速现有的 socket 代码，迁移成本最低 |
| RDMA | ❌ | ❌ | — | 交易所接口不使用 RDMA；只在自建的机房内多机通信中才有意义。**本场景属于过度优化** |
| FPGA（行情解析/风控/下单） | ❌ | P2 | 亚微秒 tick-to-trade | 成本、开发周期、团队能力要求都高；只有在软件方案已经验证了策略的经济性之后才值得考虑 |
| Intel CAT（缓存分区） | ❌ | P2 | 隔离 LLC 干扰 | 同机还有其他负载时才需要 |
| 关闭 Spectre/Meltdown 缓解 | ❌ | P2（谨慎） | 系统调用路径更快 | 安全权衡；内核旁路后收益很小 |

### 14.2 分级结论

**P0 必须实现**
- Arch A 与 B 共同：零分配 Hot Path、对象池/预分配、缓存行对齐、SPSC 单写者、异步日志、绑核、TSC 计时、整数价格。
- Arch B 额外：内核旁路网卡（这是 B 的分水岭）、isolcpus/nohz_full/rcu_nocbs、IRQ 亲和、NUMA 绑定、HugePages、mlockall + 预取、硬件时间戳、PTP、零拷贝解码、BIOS 调优（关闭 C-state/P-state 切换和超线程，开启性能模式，尽量减少 SMI）。

**P1 推荐实现**
- PGO/LTO、分支布局优化、AF_XDP（没有专用网卡时）、DPDK（只收行情时）、`SO_BUSY_POLL`（A）。

**P2 极限优化（先测量、再决定）**
- FPGA、SIMD、Intel CAT、关闭安全缓解、定制内核。RDMA 在本场景中属于过度优化。

### 14.3 Linux / BIOS 配置清单（Arch B 示例，需按硬件验证）

```bash
# Kernel cmdline (example; core 0-1 housekeeping, 2-8 trading)
isolcpus=managed_irq,domain,2-8 nohz_full=2-8 rcu_nocbs=2-8 irqaffinity=0-1
intel_idle.max_cstate=0 processor.max_cstate=1 idle=poll   # trade power for latency; verify thermals
nmi_watchdog=0 nosoftlockup skew_tick=1 tsc=reliable
transparent_hugepage=never default_hugepagesz=1G hugepagesz=1G hugepages=8
audit=0

# Runtime
systemctl stop irqbalance
for i in /proc/irq/*/smp_affinity_list; do echo 0-1 > $i 2>/dev/null; done
cpupower frequency-set -g performance
sysctl -w vm.swappiness=0 kernel.numa_balancing=0 vm.stat_interval=120
sysctl -w kernel.sched_rt_runtime_us=-1        # only if running SCHED_FIFO; prefer SCHED_OTHER on isolated cores

# Verification (must be part of the deployment checklist)
cat /sys/devices/system/cpu/isolated /sys/devices/system/cpu/nohz_full
perf stat -e context-switches,cpu-migrations -C 2-8 sleep 60      # expect ~0
cat /proc/interrupts                                                # no increments on 2-8
echo osnoise > /sys/kernel/tracing/current_tracer                   # measure OS noise on isolated cores
```

BIOS：Performance profile、关闭超线程（或保证兄弟逻辑核空闲）、关闭 C1E/C-states、固定 Turbo 或关闭 Turbo（以稳定性优先）、关闭不必要的 SMI 源（USB legacy 等）、内存性能模式。注意：`isolcpus` 在新内核中被认为是遗留接口，可以改用 cgroup v2 cpuset 的 isolated partition，两者择一即可。

### 14.4 编译与代码层

- 编译选项：`-O2`（或 `-O3`，以基准测试结果为准）、`-march=<目标 CPU>`（**不要**在构建机上用 `native` 编译生产二进制）、`-fno-exceptions -fno-rtti`（核心库）、`-fno-plt`、LTO/PGO；**禁止 `-ffast-math`**，并设 `-ffp-contract=off`，保证回放的确定性。
- `[[gnu::hot]]`/`[[gnu::cold]]`、`__builtin_expect`/`[[likely]]`；冷路径（恢复、错误处理）放进单独的 `noinline` 函数，不污染指令缓存。
- 预取：在哈希查找之前对目标槽位 `__builtin_prefetch`（对批处理有效）。
- 热身：开盘前用回放数据跑一遍完整路径，让分支预测器、指令缓存和 TLB 进入稳态。下单路径用"模拟下单"热身（编码但不发送），注意不要把真实订单发出去。

### 14.5 什么时候停止优化

优化前后必须用 §15 的方法测量 P50/P99/P99.9，以及**端到端**（T0→T8）的分布。如果某项优化只改善了 P50 而没有改善 P99/P99.9，或者节省的量级远小于外部延迟（A 中所有亚微秒级优化都属于这一类），就停止。

### 14.6 日志、监控与遥测（Hot Path 零阻塞）

**架构**

```
Trading thread                       Logger thread (Core 7)                    Storage / Ops
 ┌──────────────┐  SPSC<LogRecord>   ┌──────────────────────────────┐
 │ log.emit(rec)│ ─────────────────► │ drain all rings (round robin)│ ──► binary journal (mmap/O_DIRECT,
 │  ~20–40 ns   │   (per producer)   │ TSC → ns realtime (calibrated)│      rotate by size/time)
 └──────────────┘                    │ critical records: fsync batch │ ──► durability ack → shard (§11.3)
                                     │ sampling → telemetry          │
                                     └──────────────────────────────┘
 per-thread counters / HDR histograms ──(relaxed loads)──► Telemetry thread (Core 8) ──► Prometheus/Grafana, Alerting
                                                                                         offline decoder → text/Parquet
```

**记录格式**（POD，定长头 + 定长或短变长体；Hot Path 中**不做任何字符串格式化**）

```cpp
struct alignas(8) LogHeader {
  uint64_t tsc;          // raw TSC; logger converts to ns UTC using PTP-disciplined calibration
  uint64_t corr_id;      // [shard:8 | seq:56], assigned when the market event enters the shard
  uint32_t type;         // MarketEvent / Decision / RiskDecision / Order / Cancel / Exec / Reject / Position / PnL / Kill / Health
  uint16_t len;          // payload bytes
  uint16_t strategy_id;
  uint64_t order_id;     // cl_ord_id (0 if n/a)
  SymbolId sym;
  uint8_t  severity;
  uint8_t  flags;        // bit0: critical (must be durable)
};
```

| 记录类型 | 内容 | 是否关键（不可丢） |
|---|---|---|
| Market Event | 触发决策的事件（全量原始行情由录制器或抓包单独保存） | 否（采样） |
| Strategy Decision | 特征值、fair、reservation、期望报价、模式 | 否（可采样，但默认全量） |
| Risk Decision | verdict、关键输入（worst、敞口、PnL） | **拒单是关键记录** |
| Order / Cancel / Replace | 完整请求 + cl_ord_id + corr_id | **是** |
| Execution / Reject | 归一化回报 + 原始回报引用 | **是** |
| Position / PnL | 每次成交后的快照 | **是** |
| Kill Switch / Risk Event | 完整上下文 | **是**（fsync 后回执） |
| System Health | 队列深度、延迟分位数、丢包、时钟偏移 | 否 |

**规则**：
1. 每个生产线程独占一条 SPSC，日志线程轮询消费（多条 SPSC 的组合，而不是一个 MPMC）。
2. 环的容量按"开盘前 5 分钟峰值 × 10"来设定。**普通记录**在环满时丢弃并计数；**关键记录**的环满意味着审计能力受损，应置 `kBlkLogBackpressure` 并停止新单（fail-safe）。
3. 纳秒时间戳：Hot Path 只记录原始 TSC，由日志线程用（TSC ↔ PTP 时间）的线性校准换算成 UTC ns。校准参数每秒更新一次，并写入日志，以便离线重算。
4. Correlation ID 贯穿全链路：行情事件 → 决策 → 风控 → 订单 → 回报 → 成交 → PnL，可以从任意一笔成交反查到触发它的那个行情包。
5. 原始行情的完整录制（pcap 级）由独立的抓包设备/进程负责（§15.1），不占用交易进程的日志带宽。

**监控指标与告警**

| 类别 | 指标 | 告警示例 |
|---|---|---|
| 延迟 | 各阶段 T1…T7 的 HDR 直方图；ACK 延迟；feed lag | P99 > 预算 × 2，持续 1 分钟 |
| 行情 | 包速率、缺口数、重传数、丢包、各标的最后更新时间 | 任意缺口；心跳丢失 |
| 交易 | 报价在场率、改单率、撤单率、OER、拒单率、成交率、markout | 拒单率 > 5%；markout 持续为负 |
| 风险 | 持仓、库存分区、敞口使用率、PnL、回撤、Kill 状态 | 进入 Warning/ReduceOnly |
| 系统 | 队列深度、批大小、上下文切换、CPU 温度/频率、PTP offset、日志环水位 | 上下文切换 > 0（隔离核）；PTP offset 超标 |

Dashboard、时序数据库、历史存储全部在进程外异步运行，**与 Hot Path 之间唯一的接口是只读的计数器和直方图快照**。

---

## 15. Benchmark Plan

### 15.1 Replay 与确定性测试基础设施

Benchmark、回测、Bug 复现和回归测试共用同一套回放基础设施。

**录制（Capture）**

| 组件 | 内容 | 方式 |
|---|---|---|
| ITCH Recorder | 原始 MoldUDP64 包 + 硬件 RX 时间戳 | B：交换机镜像口或 L1 交换机复制 → 独立抓包服务器（pcapng，ns 精度）；A：交易进程内的 Recorder 线程写二进制日志 |
| Binary Market Data Capture | 解码后的 `MarketEvent` 流（64 B/条，可以直接 mmap 回放） | 离线由 pcap 转换生成 |
| 交易事件日志 | 所有**输入**：回报、定时器触发、控制命令、配置版本、Level 快照版本 | 交易进程的审计日志 |
| 历史数据 | NASDAQ 公开发布的历史 ITCH 5.0 样本文件 | 用于 Phase 0 的开发和基准测试 |

**确定性要求**

同一段输入必须产生**逐条相同**的策略决策序列。实现方法：
1. **单一输入流**：每个分片的所有输入（行情、回报、定时器、控制命令）都以带序号的事件形式进入事件循环，并全部写入日志。回放时按原序注入。
2. **注入时钟**：策略和风控只通过 `Clock` 接口取时间。实盘用 TSC，回放用事件自带的时间戳。代码中**禁止**直接调用 `clock_gettime`/`rdtsc`（用静态检查规则强制）。
3. 禁止依赖：`unordered_map` 的迭代顺序、未初始化内存、线程调度顺序、地址值（例如用指针做哈希键）。
4. 浮点：相同的二进制和编译选项（`-ffp-contract=off`、不用 `-ffast-math`）；订单相关的数值全部用整数。
5. 随机数：固定种子，种子写入日志。
6. 配置和参数快照带版本号，回放时加载同一版本。

**回放模式**

| 模式 | 输入 | 输出 | 用途 |
|---|---|---|---|
| **MD Replay** | pcap / MarketEvent 流 | 订单簿状态、TOB 序列 | 订单簿正确性、解码吞吐、延迟剖析 |
| **Strategy Replay（闭环复现）** | 行情 + 当时记录的回报（按原时刻注入） | 决策序列，与实盘日志逐条比对 | **Bug 复现**：完整重现实盘那一天 |
| **Risk Replay** | 实盘订单请求序列 | 每笔的 verdict，与实盘比对 | 风控规则变更的回归验证 |
| **Order Simulation（开环回测）** | 行情 + 撮合模拟器 + 延迟模型 | 模拟成交、PnL、markout | 回测与参数研究 |

**撮合模拟器**（Backtest/Simulation Engine）：基于 order-level 订单簿，把自己的虚拟订单插入价位队尾（到达时刻 = 决策时刻 + 延迟模型采样）；该价位的成交按 FIFO 消耗前方队列，轮到虚拟订单时就成交；前方的撤单按比例估计。延迟模型：A 使用实测的 IBKR 延迟分布，B 使用实测的机房延迟分布。**局限**：虚拟订单不改变回放中的行情（无冲击假设），会高估成交质量。这一偏差必须在报告中写明，并用小规模实盘的 markout 进行校准。

**回归测试**：CI 对固定的若干交易日回放，计算决策流的哈希值并与基线比较。任何不一致都必须由提交者解释（是预期中的策略变更，还是确定性被破坏）。

### 15.2 测试层级

| 层级 | 对象 | 工具 | 负载 | 指标 |
|---|---|---|---|---|
| L0 微基准 | `decode_itch`、`BookManager::apply`、`compute_quotes`、`pre_trade_check`、GCRA、SPSC、OUCH 编码 | Google Benchmark（吞吐）+ 自研 TSC 逐次采样直方图（分位数） | 真实 ITCH 样本中提取的消息序列；合成的最坏情况（深队列、随机访问） | ns/op 的 P50/P90/P99/P99.9/Max；吞吐（ops/s） |
| L1 组件 | ITCH Decode Throughput、Order Book Updates/s、Strategy Decisions/s、Risk Checks/s、Order Manager Throughput | 全天 ITCH 文件回放（单核满速） | 全天回放 + 开盘 5 分钟压缩回放（×10 速度） | msgs/s；每条延迟直方图；缓存缺失（`perf stat`） |
| L2 进程内管线 | MD 线程 → 分片 → 风控 → OMS → SimGateway | 回放 + 每阶段 TSC 打点（T1…T7） | 实时速率 / ×10 突发 | 各段延迟分布、批大小、队列深度 |
| L3 线上到线上 | 整机（网卡 → 网卡） | 包回放器（独立机器，按原时间间隔或加速发送）+ 硬件时间戳抓包（镜像口） | 实时 / 突发 / 持续饱和 | **T0→T8 Tick-to-Trade 分布** |
| L4 抖动/耐久 | 整机 24 h | `osnoise`/`hwlat` tracer、温度、频率监控 | 循环回放全天行情 | P99.9/Max 随时间的漂移；上下文切换；异常停顿 |
| L5 外部（A） | IBKR Paper / 实盘小单 | 在 T8′、T9′、T10′ 打点 | 交易时段内定时发单（远离市场的限价单，随后立即撤单） | Broker 往返延迟的分布，按时段分桶 |

### 15.3 方法要点

1. **不报告平均值**，只报告 P50、P90、P99、P99.9、Max 和样本数，并附直方图（HDR Histogram，3 位有效数字）。
2. **避免协同遗漏（Coordinated Omission）**：L2/L3 使用开环负载（按预定时间表发送，不等待上一条处理完）；延迟从**计划发送时刻**开始计算。
3. 计时开销要单独测量并报告（本文中 `rdtsc + lfence` 约 23 ns【M-VM】），不要把它混进被测函数的耗时。
4. 冷/热缓存分别测试：热缓存代表稳态；冷缓存（每次测量前用无关数据冲刷缓存）代表长时间空闲后的第一笔订单，这对低活跃标的很重要。
5. 环境写入报告：CPU 型号、微码、BIOS 设置、内核版本与参数、编译器与选项、网卡固件、隔离配置。**云 VM 上的数字只用于功能验证**，不能作为性能结论。
6. 每个结果都要标注【M】的环境，或者【E】。

### 15.4 验收标准（Architecture B，目标硬件，调优后）

| 指标 | P50 | P99 | P99.9 | Max（24h） |
|---|---|---|---|---|
| ITCH Decode | ≤ 20 ns/msg | ≤ 50 ns | ≤ 100 ns | — |
| Order Book apply | ≤ 150 ns | ≤ 500 ns | ≤ 1 μs | ≤ 10 μs |
| pre_trade_check | ≤ 50 ns | ≤ 100 ns | ≤ 200 ns | — |
| T1→T7（用户态内部） | ≤ 1.5 μs | ≤ 5 μs | ≤ 10 μs | ≤ 50 μs |
| T0→T8（线上到线上） | ≤ 5 μs | ≤ 15 μs | ≤ 30 μs | ≤ 100 μs |
| 吞吐（单 MD 核） | 持续 ≥ 5M msg/s，无丢包 | | | |

Architecture A 的验收：T1→T8′ 满足 P50 < 300 μs、P99 < 1 ms（预计余量很大）；T8′→T9′ **只测量、不验收**，按时段给出分布报告。

### 15.5 回归门禁

专用的性能测试主机（与生产同型号，而不是 CI 云主机）在每晚构建后运行 L0–L2：任一关键指标的 P99 恶化超过 10% 时，阻止发布。L3/L4 在每次发布前运行。

### 15.6 IBKR 延迟测量方法（Architecture A）

- 在交易时段的不同时间点（开盘后、午间、收盘前），用远离市场的限价单（不会成交）执行"下单 → 收到 Submitted → 撤单 → 收到 Cancelled"的循环，频率要远低于限速。
- 记录 T8′（`placeOrder` 返回）、T9′（Submitted）、撤单发出时刻、Cancelled 回调时刻，统计分布。
- 注意 IBKR 的 Paper Trading 环境与实盘的路由和延迟不同，**Paper 的数据不能代表实盘**【BD】。
- 测得的分布作为 A 的延迟模型输入回测，并用于设定 `min_quote_life`、价差下限和 ACK 超时。

### 15.7 开发 VM 上的参考测量（示例报告格式）

以下数据由 `docs/design/reference/test_bench.cpp` 在本文作者的开发容器中测得（4 vCPU 共享云 VM，Intel Xeon 2.1 GHz，Ubuntu 24.04，g++ 13.3 `-O2 -march=native`，**未隔离、未调优**，4 次运行取区间）。**它们只用来证明代码可用、给出量级，不是生产性能**。

| 测试 | n | P50 | P90 | P99 | P99.9 | Max | 备注 |
|---|---|---|---|---|---|---|---|
| rdtsc+lfence 计时开销 | 1M | 22–24 ns | 23–32 ns | 24–44 ns | 29–59 ns | 23 μs–224 μs | 下列各项都包含这部分开销 |
| ITCH decode（吞吐） | 1M | 11.6–13.2 ns/msg（约 76–87 M msg/s） | | | | | 合成的 A/D/E 混合负载 |
| `BookManager::apply` | 3M | 195–213 ns | 330–382 ns | 496–589 ns | 768–998 ns | 0.5–3.1 ms | 64 只标的，2 万存活订单，**随机访问**（缓存不友好）；Max 来自 VM 抢占 |
| `pre_trade_check`（通过） | 2M | 36–48 ns | 37–53 ns | 39–60 ns | 50–84 ns | 145 μs–2.6 ms | |
| `compute_quotes` | 1M | 66–71 ns | 67–82 ns | 69–100 ns | 74–157 ns | 65 μs–1.6 ms | |
| SPSC 单向（RTT/2） | 200k | 290–400 ns | 360–450 ns | 430–540 ns | 5.5–8.1 μs | 0.15–1.1 ms | 跨 vCPU；裸金属同 socket 预期约 60–150 ns【E】 |

**解读**：(1) 计算本身的量级与 §4 的估计一致；(2) Max 列中百微秒到毫秒级的值都来自未隔离 VM 上的抢占和中断，这正说明了 §14 中隔离、绑核和裸金属的必要性；(3) 订单簿的测试负载是随机访问，比真实 ITCH 的局部性更差，生产数据需要用真实 ITCH 回放（L1）重新测量。

---

## 16. 部署拓扑

### 16.1 IBKR Prototype Deployment（Architecture A）

**阶段 A0：研发 / 回测（任意位置）**

```
┌─────────────────────────── Dev workstation / CI ───────────────────────────┐
│  Historical ITCH files ─► Replay Engine ─► Trading Core (SimGateway)       │
│                                          ─► Backtest reports / regression   │
└────────────────────────────────────────────────────────────────────────────┘
```

**阶段 A1/A2：Paper / 小资金实盘（美东同城）**

```
                        美东机房 / 云区域（纽约·新泽西，靠近 IBKR 与交易所）
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │  Trading Host（裸金属或独占物理核实例，8+ 核）                                         │
 │  ┌────────────────────────────────────────────────────────────────────────────────┐ │
 │  │ Core1: IB Gateway (JVM) ◄── localhost TCP ──► Core4/5: IBKR Adapter + EReader   │ │
 │  │ Core2: MD Source (ITCH vendor / IBKR data)    Core3: Trading Shard              │ │
 │  │ Core6: Logger + Telemetry                     Core7: Supervisor + local watchdog│ │
 │  └────────────────────────────────────────────────────────────────────────────────┘ │
 │        │ ITCH (vendor: 专线/交叉连接/SoupBinTCP)          │ TWS API → Internet/VPN     │
 └────────┼──────────────────────────────────────────────────┼──────────────────────────┘
          ▼                                                  ▼
   Market data vendor                                  IBKR servers ──► Exchanges
                                                        【BD】
 ┌───────────────────── Ops (separate host / region) ─────────────────────┐
 │ Monitoring (Prometheus/Grafana/Alertmanager) · Log archive · Config repo│
 │ Independent Watchdog（第二个 IB Gateway 实例 / clientId：带外 reqGlobalCancel）│
 │ Operator console（VPN + MFA）                                             │
 └────────────────────────────────────────────────────────────────────────┘
```

要点：
- **地理位置**：交易主机和 IB Gateway 必须位于美东。从亚洲运营时，只把运维控制台放在本地，交易进程留在美东。
- IB Gateway 的每日重启/维护窗口要排进运行计划；重启前进入 SAFE 模式，重启后走 §13.3 的对账流程【BD】。
- Watchdog 与交易主机分离，并使用独立的 Broker 会话，确保交易主机宕机时仍能撤单。
- 凭证：IBKR 账户凭证和 API 访问用操作系统密钥管理，不写入配置仓库。

### 16.2 Colocation HFT Deployment（Architecture B）

```
                        NASDAQ Data Center (Carteret, NJ) — colocation cage
 ┌───────────────────────────────────────────────────────────────────────────────────────────┐
 │  Exchange handoff                                                                          │
 │   ITCH MoldUDP64 Feed A ──┐   ITCH Feed B ──┐   OUCH ports (per shard) ◄──┐   GLIMPSE/RR ◄──┐ │
 │                           ▼                 ▼                            │                 │ │
 │  ┌──────────────────────────────────────────────────┐                    │                 │ │
 │  │ Layer-1 switch (fan-out / mirror, ~ns)            │── mirror ──► Capture appliance      │ │
 │  └───────┬──────────────────────────┬────────────────┘             (HW timestamps, pcapng) │ │
 │          │                          │                                                       │ │
 │  ┌───────▼────────────┐   ┌─────────▼──────────┐                                            │ │
 │  │ Trading Server #1  │   │ Trading Server #2  │  (hot standby / 不同标的组)                  │ │
 │  │ NIC0: MD A/B (bypass)  │ same               │                                            │ │
 │  │ NIC1: Order entry ─────┼────────────────────┼─► (Sponsor 15c3-5 risk gateway, 若适用) ──┘ │
 │  │ NUMA0: trading cores│   │                    │                                           │ │
 │  └───────┬────────────┘   └─────────┬──────────┘                                            │ │
 │          │ PTP (from GPS grandmaster via PTP-aware switch)                                   │ │
 │  ┌───────▼──────────────────────────▼─────────┐                                             │ │
 │  │ Mgmt switch (OOB): IPMI/BMC, SSH, telemetry │── VPN ──► Ops center                        │ │
 │  └─────────────────────────────────────────────┘                                            │ │
 │  Other venue feeds / SIP via cross-connect or extranet (for NBBO)                            │ │
 └───────────────────────────────────────────────────────────────────────────────────────────┘
          │ dedicated circuits (dark fiber / metro wave)
          ▼
   Ops / DR site: monitoring, historical store, replay/backtest cluster, config & release mgmt
```

要点：
- **网络分离**：行情、订单入口、管理/遥测各走独立网卡或独立端口；管理网络与交易网络物理隔离。
- **冗余**：A/B 行情双路（不同网卡更佳）；每台服务器配置双电源；备用服务器预装并热身，标的组按"主备"或"按标的拆分"两种方式部署。
- **时钟**：GPS 授时的 PTP grandmaster → 支持 PTP 的交换机 → 网卡的 PHC → `phc2sys` 同步系统时钟。同时监控 offset。
- **抓包设备**独立于交易服务器：它是延迟测量（T0/T8/T9）和合规审计的数据源。
- **交易所级保护**：每个 OUCH 会话启用 Cancel-on-Disconnect（若该端口提供）；与 Sponsor 或交易所的 Kill Switch 接口打通到 Watchdog。
- **变更管理**：盘中禁止发布；发布在盘后进行，先在备用服务器上用当日行情回放验证，再切换。

### 16.3 环境矩阵

| 环境 | 行情 | 执行 | 资金 | 目的 |
|---|---|---|---|---|
| DEV | 历史 ITCH 文件 | SimGateway | 无 | 开发、单元测试 |
| SIM | 历史/实时行情回放 | SimGateway（撮合模拟 + 故障注入） | 无 | 回测、故障演练 |
| PAPER | 实时 | IBKR Paper | 模拟资金 | A 的端到端联调 |
| PROD-A | 实时 | IBKR Live | 小额 | 低频实盘 |
| PROD-B | Colo ITCH | OUCH | 生产 | HFT 实盘 |

---

## 17. 工程目录

```
trading-system/
├── CMakeLists.txt / CMakePresets.json     # presets: debug-asan, release, release-pgo, bench
├── cmake/                                 # toolchains (-march per target), warnings, sanitizers
├── third_party/                           # pinned: googletest, google-benchmark, hdrhistogram, (ibkr-api)
│
├── core/                                  # header-mostly, no exceptions, no allocation after init
│   ├── types/                             # Price/Qty/SymbolId, fixed-point helpers, enums
│   ├── memory/                            # HugeArena, ObjectPool, cache-aligned helpers
│   ├── concurrency/                       # SpscRing, Disruptor-style broadcast, seqlock
│   ├── time/                              # TSC clock, calibration, Clock interface (live/replay)
│   └── util/                              # likely/unlikely, bit ops, static checks
│
├── market_data/                           # Market Data Gateway
│   ├── transport/                         # ef_vi / AF_XDP / DPDK / kernel UDP receivers (compile-time selected)
│   ├── moldudp64/                         # framing, A/B arbitration, sequencer, re-request client
│   ├── glimpse/                           # snapshot client (SoupBinTCP) for recovery
│   └── sources/                           # IMarketDataSource: ItchMulticast, ItchTcp, FileReplay, IbkrMarketData
│
├── itch/                                  # ITCH 5.0 message layouts + decode_itch + reference-data handlers (R/H/Y/...)
├── orderbook/                             # OrderMap, OrderPool, Ladder, OrderBook, BookManager, integrity checks
├── market_state/                          # trading status, LULD, SSR, staleness, feed lag
├── signal/                                # microprice, OBI, trade flow, EWMA vol, momentum (incremental)
├── strategy/
│   ├── market_making/                     # quote modes, regime filters, quote reconciler, slot state machine
│   └── level_context/                     # 厚门 LevelSnapshot reader (read-only, bounded usage) — optional
├── pricing/                               # fair value, reservation price, spread, tick snapping, sizing
├── inventory/                             # zones, hysteresis, flatten logic
├── risk/                                  # pre_trade_check, limits, GCRA/TokenBucket/SlidingWindow, KillSwitch,
│                                          # budget slicing, risk coordinator
├── order_manager/                         # Order pool, cl_ord_id codec, state machines, timeouts, dedup
├── execution/                             # ExecutionGateway concept + IExecutionGateway + normalization
├── broker/
│   ├── ouch/                              # SoupBinTCP + OUCH encoder/decoder, pre-encoded templates
│   ├── fix/                               # allocation-free FIX encoder/decoder
│   ├── ibkr/                              # IbkrGateway proxy + adapter thread (IBKR C++ API isolated here)
│   └── sim/                               # SimGateway: matching simulator, latency model, fault injection
├── position/                              # positions, broker reconciliation
├── pnl/                                   # realized/unrealized, fee & rebate schedules, drawdown
├── control/                               # supervisor channel (authenticated), config manager, kill reset
├── monitoring/                            # telemetry thread, counters, HDR histograms, exporters, alerts rules
├── logging/                               # binary log records, async logger, durability acks, offline decoder
├── replay/                                # pcap → MarketEvent converter, deterministic replay driver, diff tool
├── simulation/                            # exchange simulator (queue model), broker simulator, scenario DSL
├── backtest/                              # backtest runner, markout analysis, parameter sweeps (offline)
├── research/
│   └── level_context_batch/               # 厚门 pre-market level computation + significance tests (Python/C++)
├── apps/
│   ├── trader/                            # main binary: wiring, startup sequence, std::variant<Shard<Gw>...>
│   ├── watchdog/                          # independent process: heartbeats, out-of-band cancel-all
│   ├── supervisor/                        # operator CLI/console (auth, audit)
│   └── tools/                             # book dumper, log decoder, latency report, reconcile tool
├── benchmark/                             # L0 micro (Google Benchmark + TSC histograms), L1/L2 pipeline benches
├── tests/
│   ├── unit/                              # per module
│   ├── property/                          # orderbook vs reference model, risk invariants
│   ├── fuzz/                              # libFuzzer: decoder, book, gateways
│   ├── replay_regression/                 # golden decision-stream hashes for fixed days
│   └── fault_injection/                   # gap, disconnect, duplicate exec, kill switch drills
├── config/
│   ├── schemas/                           # validated schema (startup fails on invalid config)
│   ├── environments/{dev,sim,paper,prod-a,prod-b}/
│   └── reference_data/                    # tick tables, round lots, fee schedules (daily, versioned)
├── infra/
│   ├── host_tuning/                       # kernel cmdline, sysctl, irq affinity scripts, verification checks
│   ├── deploy/                            # systemd units, release & rollback scripts
│   ├── monitoring/                        # dashboards, alert rules
│   └── ptp/                               # ptp4l/phc2sys configs
└── docs/
    ├── design/                            # this document + reference prototype
    ├── runbooks/                          # gap recovery, broker disconnect, kill switch reset, restart
    └── adr/                               # architecture decision records
```

**依赖规则**（CI 中用 include 规则强制）：`strategy/`、`pricing/`、`risk/` 不得依赖 `broker/`；Hot Path 目录不得包含 `<iostream>`、`<regex>`、`<map>`、`<unordered_map>`、`<functional>`、`<memory>`（`shared_ptr`）等头文件；`broker/ibkr/` 是唯一允许链接 IBKR API 的目录。

**构建与质量**：GCC 13+/Clang 18+，C++20 起步（C++23 的 `std::expected`、`std::byteswap` 可用时启用）；Debug 构建启用 ASan/UBSan/TSan；clang-tidy 与自定义规则（禁止在 Hot Path 目录中出现 `new`/`throw`/`virtual`）；每次合并都运行单元、属性、模糊和回放回归测试；性能测试在专用主机上每晚运行。

---

## 附录 A：《厚门模型》分析与集成规范

### A.1 模型要点（来自上传文档）

| 元素 | 规则 |
|---|---|
| 斐数组 | 近 1–2 年 10–15 个关键高低点（优先涨跌幅 ≥ 15% 的节点），去重排序 |
| 底部组 | 回调后 3–5 个交易日内反弹 ≥ 8% 的底部价格 |
| 空间对 | 两两配对，检查 `低位锚 / 0.65^n ≈ 高位锚`（n = 1..4），偏差率 ≤ 1% |
| 起爆点 | `(底部值 − 空间锚点) × 10 + 空间锚点`，要求底部值 > 锚点（不倒挂） |
| 防守底 | `(A − B) / 10 + B`；轨 1：A = B/0.65；轨 2：A = 相近的趋势高点 |
| 目标位 | `高位锚 / 0.65^n`，n = 1..3 |
| 量能确认 | 突破时成交量较前一日放大 30% 以上 |
| 适用范围（文档自述） | A 股、ETF；1 天至 3 周的波段；**不适配日内交易** |

我按文档中的公式复算了海王生物的示例，数字一致：6.34/0.65 = 9.75、6.34/0.65² = 15.01、6.34 × 0.65 = 4.12（与 4.11 的偏差为 0.27%）、起爆点 4.81、超短期防守底 4.93/5.07、短期防守底 6.40/6.36、中期防守底 10.28。

### A.2 与本系统场景的错配

1. **时间尺度**：日线拐点和波段持仓，对应的是做市的秒级以下决策和日内平仓。
2. **市场**：模型针对 A 股（T+1、涨跌停、散户主导的波动结构），本系统针对美股（T+0、LULD、做市商与 HFT 主导、多交易所）。
3. **输出形态**：静态价位，没有对"未来几秒价格变化"的预测，而这才是做市需要的东西。

### A.3 统计有效性的担忧（零假设检验）

**问题**：如果在完全随机的价格中也能经常找到"强共振空间对"，那么"找到共振"本身就不构成任何证据。

**方法**（脚本：`docs/design/reference/houmen_null_test.py`，本文作者已运行）：
- 生成 500 个交易日的几何随机游走（日波动率 3%，零漂移）；没有任何结构。
- 用 15% 阈值的 ZigZag 提取拐点，取最近不超过 15 个（与文档的"10–15 个关键高低点"一致），去重排序。
- 按文档规则统计"低位锚 / c^n 与高位锚的偏差 ≤ 1%"的配对数（n = 1..4）。
- 重复 2000 次，并对比不同的系数 c。

**结果**【M-VM，蒙特卡洛，seed = 42】：

| 系数 c | 平均"强共振空间对"数 | 至少找到 1 对的样本比例 |
|---|---|---|
| 0.60 | 1.29 | 69.9% |
| 0.618 | 1.55 | 76.2% |
| **0.65** | **1.85** | **82.5%** |
| 0.70 | 2.56 | 89.9% |

**解读**：在不含任何结构的随机价格上，82.5% 的样本至少能找到一个 c = 0.65 的"强共振空间对"。系数越接近 1，匹配越多（因为相邻锚点的比值更容易落进容差带）。因此，"0.65 比 0.618 更好"不能用"共振出现得更多"来证明，必须看共振价位**之后**的价格行为是否显著异于随机。此外，起爆点公式对输入非常敏感：它对底部值的导数是 10，所以底部值相差 1 美分，起爆点就相差 10 美分。拐点选取中的主观性（"精准度高的价格节点"）会被放大 10 倍。

### A.4 集成规范（若决定使用）

```
盘前批处理（research/level_context_batch，非交易进程）
  输入：复权日线 OHLCV（≥ 250 日）
  1. 拐点：确定性的 ZigZag(15%)，取最近 ≤ 15 个，round 到 tick
  2. 底部组：拐点低点中满足"3–5 日内反弹 ≥ 8%"者
  3. 空间对：O(n²·4)，n ≤ 15 → ≤ 420 次比较（微秒级）
  4. 起爆点 / 防守底（轨1、轨2）/ 目标位
  5. 输出 LevelSnapshot{symbol, version, levels[], level_type[], computed_from_date}
     → 校验（排序、正数、与前收盘价的距离合理）→ 签名 → 发布到 config/reference_data
交易进程
  启动时加载 LevelSnapshot（只读、不可变）；盘中不重新计算
  策略按 §8.12 的有界规则使用；默认 feature flag = OFF
```

### A.5 上线前检验（未通过则保持关闭）

1. **事件研究**：在美股历史数据上，统计价格触及"厚门价位"后 1 s / 10 s / 1 min / 5 min 的收益率与波动率分布，与以下两种对照组比较：(a) 随机价位；(b) 用 0.60/0.618/0.70 计算的价位。要求差异在多重检验校正后仍然显著。
2. **做市指标**：在回测中比较开启和关闭 §8.12 规则时的 markout、逆向选择成本、库存波动和 PnL 分布（样本外、分时段）。
3. **稳健性**：参数扰动（ZigZag 阈值 12–18%、容差 0.5–2%）下，结论不能翻转。
4. 只要任何一项不通过，就保持关闭。这是工程纪律问题，不是对模型作者的否定。

---

## 附录 B：参考实现（`docs/design/reference/`）

| 文件 | 内容 |
|---|---|
| `ref.hpp` | 基础类型、大端读取、`MarketEvent`、`decode_itch`、`SpscRing`、`OrderPool`、`OrderMap`、`Ladder`、`OrderBook`、`BookManager`、`TokenBucket`、`Gcra`、`SlidingWindowLimiter` |
| `risk.hpp` | `OrderRequest`、`RiskVerdict`、`SymbolRisk`、`RiskState`、`pre_trade_check`、`KillSwitch`、`compute_quotes` |
| `oms.hpp` | `MarketSnapshot`、`Quote`、`Order`、`ExecutionReport`、`Position`、`InventoryState`、`PnLState`、`StrategyState`、`ExecutionGateway` concept、`IExecutionGateway`、`SimGateway`、`HugeArena` |
| `test_bench.cpp` | 正确性测试（订单簿与 `std::map` 对拍 200 万事件、风控、Kill Switch、限速器、报价偏斜）+ 微基准（逐次 TSC 直方图） |
| `test_oms.cpp` | 数据结构尺寸与对齐检查、concept 约束检查 |
| `houmen_null_test.py` | 附录 A.3 的零假设蒙特卡洛 |
| `CMakeLists.txt` | 构建脚本 |

构建与运行：

```bash
cd docs/design/reference
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
./build/test_bench          # prints "ALL OK" plus latency histograms
./build/test_oms
python3 houmen_null_test.py
```

**它不是生产代码**：没有网络收发、没有 OUCH/IBKR 协议实现、没有恢复流程、订单簿的重定窗口未实现、只做了单线程的正确性测试。它的作用是让本文中的数据结构和算法"可编译、可测试、可测量"，作为后续工程实现的起点。

---

## 附录 C：必须核实的外部假设清单

以下事项会随时间变化，或者依赖具体合同，**开发前必须逐项向官方来源核实**：

| # | 事项 | 影响 | 来源 |
|---|---|---|---|
| 1 | IBKR API 消息速率上限（本文按约 50 msg/s 设计）与 pacing 规则 | A 的报价能力 | IBKR TWS API 官方文档 |
| 2 | IBKR 的 Order Efficiency Ratio 政策与撤单费用 | A 的撤改频率 | IBKR 客户协议 / 通知 |
| 3 | IBKR 是否为 API 订单提供 Cancel-on-Disconnect 等价机制；GTD 等短时效订单的可用性 | A 的断线风险 | IBKR 文档 / 客服确认 |
| 4 | IB Gateway 的每日重启、维护窗口 | A 的运行计划 | IBKR |
| 5 | IBKR 费用结构（佣金、交易所费用透传、返佣） | `fee_edge` 价差下限 | IBKR 费率页面（按账户类型） |
| 6 | ITCH 5.0 / MoldUDP64 / GLIMPSE / OUCH 的当前版本规范（字段、消息类型，`P` 消息 Buy/Sell 字段的语义） | 解码与订单协议 | NASDAQ Trader 技术规范 |
| 7 | Reg NMS Rule 612 的 tick size 修订（$0.005 档）实施日期与适用名单；Round Lot 定义修订；access fee cap 修订 | tick 表、报价逻辑、费用 | SEC 最终规则 / 交易所通告 |
| 8 | 卖空规则（Reg SHO locate、Rule 201）与账户的可借券情况 | 卖出侧报价 | Broker / SEC |
| 9 | 账户层面的日内交易规则（PDT 等） | A 的账户资格 | Broker / FINRA |
| 10 | NASDAQ 端口限速、Cancel-on-Disconnect、Kill Switch、Mass Cancel、STP 的具体能力 | B 的限速与保护 | NASDAQ 会员文档 |
| 11 | 行情授权与费用（TotalView 展示/非展示用途） | 成本、合规 | NASDAQ 数据政策 |
| 12 | 15c3-5 事前风控在 Sponsored Access 下的责任划分 | B 的风控架构 | Sponsor 合同 / 合规 |

---

*文档结束。所有【E】数字都是待验证的设计目标；所有【M-VM】数字都来自未调优的开发虚拟机，只用于量级参考；所有【BD】/【XD】项都必须在真实账户和真实机房中测量。*
