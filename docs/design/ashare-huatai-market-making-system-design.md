# A 股日内做市低延迟交易系统（华泰证券 API）— 技术设计文档

| 项目 | 内容 |
|---|---|
| 文档状态 | v0.2 Draft，供架构评审（由美股版 v0.1 改写为中国 A 股 + 华泰证券接入） |
| 日期 | 2026-11-21 |
| 适用范围 | 上交所 / 深交所连续竞价时段的日内双边报价（股票"底仓 T+0"、可转债与 T+0 ETF）；C++20/23；Linux |
| 配套代码 | `docs/design/reference/`：`ashare.hpp` + `test_ashare.cpp`（A 股专属部分）；`ref.hpp` / `risk.hpp` / `oms.hpp`（通用热路径部分）。**均非生产代码** |
| 通用设计 | `docs/design/hft-market-making-system-design.md`（美股版）。与市场无关的通用组件（SPSC 队列、内存池、GCRA 限速、异步日志、Linux 调优、Benchmark 方法）只在此处摘要，细节见该文 |
| 输入材料 | 需求提示词 +《厚门模型（实战版）》（该模型原本就面向 A 股，见 §1.6 与附录 A） |

---

## 0. 文档约定

### 0.1 数值与事实标签

| 标签 | 含义 |
|---|---|
| **【E】** | 估计值，必须在目标环境实测 |
| **【M-VM】** | 在作者的开发容器中实测（4 vCPU 共享云 VM，未调优），只能证明代码正确并给出量级 |
| **【BD】** | 取决于华泰证券（柜台、API、托管机房、合同条款），必须向华泰确认或实测 |
| **【XD】** | 取决于交易所（上交所/深交所的交易与行情系统） |
| **【REG】** | 监管或交易所规则。本文引用的规则已于 2026-11 用公开报道交叉核对过（来源见附录 D），**但实施细则的量化阈值、生效日期须以交易所和证监会正式文本为准** |

### 0.2 其他约定

- 价格：`int64_t` 定点，单位 1e-4 元。股票 tick 为 0.01 元（= 100），ETF/可转债 tick 为 0.001 元（= 10）。
- 时间：热路径用 TSC 单调时钟；交易所行情时间戳的精度通常为毫秒级（以接口规范为准），因此只能做毫秒级的行情延迟判断。
- 本文只讨论工程实现、合规与风险控制，**不对任何策略的盈利能力做承诺**。§1.5 的经济性分析恰恰说明，在 A 股做市的成本结构下，大部分标的上的经典做市在扣除费用后是亏钱的。

---

## 1. 可行性分析

### 1.1 结论先行

**(1) 延迟："华泰 API + 端到端 < 1 ms"**

- **内部 C++ 链路**（L2 行情进入用户态 → 订单簿 → 策略 → 风控 → OMS → 调用华泰 API 发送）可以做到 P50 < 300 μs、P99 < 1 ms，并且余量很大：估计 P50 为几微秒到几十微秒【E】。这在下文的 A、B 两种架构中都成立。
- **Architecture A**（自有服务器通过互联网或专线接入华泰 MATIC/MAPIs，行情来自 INSIGHT 或 MATIC 行情服务）：端到端由公网/专线、华泰行情分发和柜台处理主导，在**毫秒到数十毫秒**量级【BD】，不可能做到 < 1 ms。
- **Architecture B**（服务器放在华泰在交易所同城的托管机房，接内网 L2 组播行情和华泰的内存级/极速交易柜台）：Tick-to-Order（订单离开本机网卡）可以做到微秒级。Tick-to-ACK 还要经过 **柜台风控 → 券商交易网关 → 交易所撮合**，这一段没有公开 SLA。最好情况下可能接近亚毫秒，但**不能承诺**【BD】【XD】。

**(2) 比延迟更根本的是 A 股的市场结构约束**

| # | 约束 | 对做市的直接影响 | 依据 |
|---|---|---|---|
| 1 | **T+1**：股票（含股票型 ETF）当日买入，次一交易日才能卖出 | 当日买入的股票当日无法卖出，因此日内双边回转必须依赖**隔夜持有的底仓**；当日净买入会**强制变成隔夜风险**，无法在日内平掉 | 【REG】 |
| 2 | **不能用融券做日内回转**：证监会要求券商不得向利用融券实施日内回转交易（变相 T+0）的投资者提供融券；转融券业务已于 2024-07-11 暂停 | **没有空头一侧**：库存下限就是"卖光底仓"，不能为负 | 【REG】（附录 D） |
| 3 | **印花税**：卖出成交金额的 0.05%（2023-08-28 起减半征收），仅股票缴纳 | 加上佣金和过户费，一次回转成本约 7.2 bp（万 1 佣金、无最低收费时）。**股价高于约 14 元时，1 个 tick 的价差不足以覆盖成本**（§1.5） | 【REG】 |
| 4 | **订单类型**：没有 Post-Only，没有改单（Replace），只能撤单再重新下单；卖出委托会冻结股份，撤单确认后才解冻 | 每次改价至少要 2 条消息和 1 次往返；改价期间该侧没有报价；不穿价只能靠客户端自行检查 | 【REG】【XD】 |
| 5 | **程序化交易监管**（沪深北交易所《程序化交易管理实施细则》，2025-07-07 起施行）：事前报告制度；单账户每秒申报+撤单 ≥ 300 笔，或单日 ≥ 20,000 笔，即认定为**高频交易**（需额外报告服务器所在地、测试报告、应急方案，实施重点监管和差异化收费）；实时监控"瞬时申报速率异常、频繁瞬时撤单、频繁拉抬打压、短时间大额成交"四类异常行为 | 如果不想成为"高频交易"，每天的申报+撤单合计必须 < 20,000 笔。连续竞价约 14,220 秒，**平均每秒不到 1.4 笔**。改价（撤+下）的预算极其紧张；"1 秒内申报又撤单"的次数和全日撤单比例都必须受控 | 【REG】（附录 D） |
| 6 | **涨跌幅限制**（主板 ±10%，主板 ST 据报道已于 2026-07-06 起由 ±5% 调整为 ±10%；创业板/科创板 ±20%；北交所 ±30%；部分新股上市初期不设限）与**价格笼子**（主板：买入价 ≤ max(基准价 × 102%，基准价 + 10 个 tick)，卖出价 ≥ min(基准价 × 98%，基准价 − 10 个 tick)；科创板为拒单模式，创业板为缓存模式） | 涨跌停附近一侧流动性消失；被动报价一般不受价格笼子影响，但主动减仓单会受影响 | 【REG】 |
| 7 | **两地两所**：上交所和深交所的交易与行情系统分别在上海和深圳，两地相距约 1,200 km（同城内低延迟，跨城光纤 RTT 估计 15–25 ms【E】） | 低延迟部署需要**两套托管**（沪市标的放上海、深市标的放深圳）；跨市场信号存在十几毫秒的固有延迟 | 【XD】 |
| 8 | **非指定做市商**：交易所有正式的做市安排（如科创板做市商限于证券公司，ETF 有流动性服务商），普通投资者通过券商 API 下单不具备这些身份 | 没有做市返佣，也没有做市义务和相应豁免；本系统本质上是"被动流动性提供 + 库存管理" | 【REG】 |
| 9 | **行情粒度**：Level-1 快照约每 3 秒一次；Level-2 提供 3 秒快照（10 档）以及实时的逐笔委托/逐笔成交 | 必须使用 L2 逐笔数据重建订单簿（§7）；行情授权费用不低 | 【XD】 |

**(3) 由此得出的可落地业务形态**

| 形态 | 标的 | 机制 | 主要约束 |
|---|---|---|---|
| **形态 1：股票"底仓 T+0"双边挂单** | 流动性好、股价较低（tick 占价格比例高）的股票 | 隔夜持有底仓 B。日内卖出底仓、买回底仓，在 B ± band 范围内双边挂单；收盘前让持仓回到 B（或回到次日的目标底仓） | 底仓本身承担隔夜价格风险；印花税；T+1；卖出量不能超过昨仓 |
| **形态 2：T+0 品种双边报价** | 可转债（T+0，无印花税，±20%）、T+0 ETF（跨境、黄金、债券、货币类等，具体名单须核实） | 不需要底仓即可当日回转 | 可转债 tick 相对价格非常小（约 0.08 bp），价差和排队结构不同；ETF 有官方流动性服务商竞争 |
| ❌ 不可行 | 股票上的经典对称高频做市（频繁改价、靠空头对冲库存） | — | T+1、无融券、印花税、程序化交易监管 |

### 1.2 华泰接入路径的瓶颈逐项分析

| # | 瓶颈 | 说明 | 标签 |
|---|---|---|---|
| 1 | **接入产品形态** | 华泰面向专业/机构客户的平台为 **MATIC**。公开资料显示它集基础交易、两融、组合、ETF 套利、算法、量化、风控等功能于一体，提供 MAPIs 统一 API 服务（基础交易、增强交易、算法交易、数据服务、风控服务等），客户端量化模块 **MQuant** 支持 C++/Python。**具体可用的 API、版本、操作系统要求、开户门槛和费用，必须以华泰的接入文档和合同为准** | 【BD】 |
| 2 | **操作系统** | 有第三方实测资料提到 MATIC 客户端需要 Windows 7 及以上，且不支持在虚拟机中运行。如果交易 API 只有 Windows SDK，Linux 交易内核就必须通过**桥接进程**接入（§2.2），或者整体运行在 Windows 上 | 【BD】（需华泰确认是否提供 Linux SDK） |
| 3 | **行情** | 华泰 **INSIGHT** 金融数据服务平台整合了多家交易所的高频行情；MATIC 官网还提到"低延时的内网 Level-2 行情"。走互联网获取行情会有毫秒级以上的分发延迟；内网组播只在托管机房可用 | 【BD】 |
| 4 | **柜台** | 订单要先经过华泰柜台（资金/持仓冻结、事前风控），再由华泰的交易网关报到交易所。官网提到"内存级交易"能力，但不同产品（普通柜台、极速/内存柜台）的延迟差别很大 | 【BD】 |
| 5 | **报单速率与风控参数** | 华泰柜台自身的流控、撤单比限制、单笔限额等参数由券商设定 | 【BD】 |
| 6 | **托管机房** | 要获得交易所同城的托管资源，需要向华泰申请。高频交易还需向交易所报告服务器所在地 | 【BD】【REG】 |
| 7 | **断线保护** | 需确认华泰柜台是否支持"断线自动撤单"，以及是否有批量撤单 API。若都没有，就需要独立通道撤单（另一路 API 会话、华泰的客户端/APP、客户经理/交易室电话） | 【BD】 |

### 1.3 两套架构

**Architecture A — 华泰 MATIC/MAPIs 远程接入版**

- 用途：策略研发、回放回测、华泰仿真环境联调（如果华泰提供）、小资金实盘、"底仓 T+0"的低频版本。
- 部署：自有服务器（或 IDC 机柜）通过互联网/专线接入华泰。行情走 INSIGHT 或 MATIC 行情 API（L2）。如果交易 API 只支持 Windows，就采用"Linux 交易内核 + Windows 桥接机"的结构（§2.2、§16.1）。
- 能力：报价刷新在秒级到十秒级（同时也受每日消息预算约束），标的数量为几只到几十只。

**Architecture B — 华泰托管机房低延迟版**

- 用途：生产级的低延迟双边报价。
- 部署：**上海、深圳两地**的华泰托管机房（与交易所同城），接内网 L2 逐笔组播行情、华泰内存级/极速柜台 API（具体产品以华泰提供的为准），使用内核旁路网卡和 PTP 授时。
- 要求：按高频交易标准完成报告（如果会超过阈值）；为每个机房配备独立的风控和 Kill Switch。
- **现实提醒**：即使延迟做到微秒级，A 股的成本结构（印花税）和监管结构（报单预算）仍然决定了策略空间很窄。B 的投入必须以 A 阶段验证过的经济性为前提。

### 1.4 A vs B 对比

| 维度 | Architecture A（MATIC 远程） | Architecture B（华泰托管 + 内网 L2 + 极速柜台） |
|---|---|---|
| 内部链路（L2 包进入用户态 → 调用柜台 API） | P50 ≈ 10–50 μs，P99 < 300 μs【E】（含 Windows 桥接时额外增加 50–500 μs【E】） | P50 ≈ 2–5 μs，P99 ≈ 10–20 μs【E】 |
| 行情延迟（交易所发布 → 本机） | 毫秒级到数十毫秒（互联网 L2）【BD】 | 亚毫秒（同城组播）【BD】【XD】 |
| 订单 Tick-to-ACK | 数毫秒到数十毫秒【BD】 | 柜台 + 交易网关 + 撮合：无公开数据，须实测【BD】【XD】 |
| 消息吞吐 | 受华泰流控 + 监管预算约束（非高频账户 < 20,000 笔/日） | 技术上可以很高；监管上同样受约束（若超阈值则按高频交易管理） |
| 稳定性 | 公网抖动，跨机桥接 | 机房内网稳定，抖动主要来自柜台和交易所 |
| 订单控制 | 撤单+新单；状态推送依赖 API | 相同的语义（A 股没有改单），只是更快 |
| 部署成本 | 低：服务器 + 行情授权 + API 开通 | 高：两地托管机柜、内网行情、极速柜台使用费、硬件、合规报告 |
| 代码重用 | 与 B 共用 Strategy / Risk / OMS / Book | 替换行情源和 Gateway 实现即可 |

### 1.5 业务经济性：成本决定可做的标的（必须先算清楚）

**一次回转的成本**（买入 + 卖出，股票）：

$$
C_{rt} \approx \underbrace{2c}_{\text{佣金}} + \underbrace{2t}_{\text{过户费}} + \underbrace{s}_{\text{印花税（仅卖出）}} \;(+\; 2(e+g)\ \text{若经手费、证管费不含在佣金内})
$$

取 c = 0.01%（万 1，无最低佣金）、t = 0.001%、s = 0.05%，经手费和证管费含在佣金内，则 $C_{rt} \approx 7.2$ bp。

一个 tick 的价值 = 0.01 / P，即 100/P bp。**以最理想的情况（两侧都以挂单价成交、成交后价格不动）计算**，下表是一次回转覆盖成本所需的 tick 数（由 `breakeven_ticks()` 计算，名义金额约 2 万元）【M-VM】：

| 股价（元） | 1 tick（bp） | 覆盖成本所需 tick 数 | 回转成本（bp） |
|---|---|---|---|
| 3.00 | 33.33 | 1 | 7.20 |
| 5.00 | 20.00 | 1 | 7.20 |
| 8.00 | 12.50 | 1 | 7.20 |
| 10.00 | 10.00 | 1 | 7.20 |
| 13.00 | 7.69 | 1 | 7.21 |
| 15.00 | 6.67 | **2** | 7.21 |
| 20.00 | 5.00 | **2** | 7.20 |
| 50.00 | 2.00 | **4** | 7.20 |
| 100.00 | 1.00 | **8** | 7.20 |

**解读**：
1. 流动性好的 A 股，买一卖一价差通常只有 1 个 tick。股价超过约 14 元后，即使每次都完美地赚到 1 个 tick，也覆盖不了成本，更不用说逆向选择。**形态 1 只适用于低价、且价差经常大于 1 tick 的标的**。
2. 低价股的问题在于每个价位的排队非常长，被动成交概率很低（§8.7 的队列模型用于评估这一点）。
3. **最低佣金是致命的**：若有 5 元最低佣金，100 股 × 10 元的订单需要 **11 个 tick** 才能覆盖成本（无最低佣金时只需 1 个）【M-VM】。必须与华泰协商取消最低佣金，或者让单笔金额足够大。
4. **ETF**（无印花税，按万 0.5 佣金计）：价格 1.000 元时 1 tick = 10 bp，1 个 tick 即可覆盖成本。**可转债**（无印花税）：价格 120 元时 1 tick 只有 0.083 bp，需要约 13 个 tick（0.013 元）才能覆盖万 0.5 的佣金【M-VM】。这说明可转债的报价逻辑应以"元"为单位的价差来定，而不是以 tick 数来定。
5. 以上费率都是可配置参数。过户费、经手费的现行标准，以及各品种（ETF、可转债）的费用项目，须以中国结算、交易所和华泰的收费标准为准【REG】【BD】。

### 1.6 关于《厚门模型》：在 A 股版中的定位

《厚门模型》本身就是为 A 股、ETF、可转债（系数 0.7）设计的**多日波段**价位模型。文档自述"不适配日内交易"。在 A 股版架构中，它恰好可以对应一个美股版中没有的层次：

```
┌────────────────────── 波段层（日级，盘前计算） ──────────────────────┐
│ 厚门 LevelContext：空间对 / 起爆点 / 双轨防守底 / 目标位               │
│   → 底仓规划器：决定每只标的的目标底仓 B、底仓止损线（轨2防守底）       │
│   → 风险隔离：单标的底仓 ≤ 总资金 20%（来自模型文档，作为上限）         │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ 每日不可变快照（目标底仓、价位表）
┌───────────────────────────────▼──────────────────────────────────────┐
│ 日内层（毫秒级）：围绕底仓 B 的 T+0 双边挂单（形态 1）                  │
│   价位只能有界地影响：价差加宽、数量缩减、底仓偏斜方向                   │
└──────────────────────────────────────────────────────────────────────┘
```

**但是**，零假设检验仍然适用：在完全随机的价格走势上，按文档规则筛选，82.5% 的样本至少能找到 1 个"强共振空间对"【M-VM】。因此"找到共振"不能证明有效。在把它接入底仓决策之前，必须先用 A 股的历史日线完成附录 A.5 的检验；未通过就以固定规则（例如等权、按波动率定额）来设定底仓。

### 1.7 推荐演进路线

```
Phase 0  历史 L2 逐笔数据回放 + 回测 ─── 验证订单簿重建、成本模型、预算消耗、策略逻辑
   │
Phase 1  完成程序化交易报告；华泰仿真环境 / 极小资金联调（Architecture A）
   │                                  ─── 验证 OMS 状态机、柜台回报映射、断线恢复、实测延迟
Phase 2  Architecture A 小资金实盘：形态 1（底仓 T+0，低价股）+ 形态 2（可转债/T+0 ETF）
   │                                  ─── 校准成交率、markout、实际成本；Kill Switch 演练
Phase 3  Architecture B（沪、深两地托管）──── 只有在 Phase 2 证明扣费后仍有正期望时才投入
```

---

## 2. 系统整体架构

### 2.1 总体结构（Architecture B，一个城市一套；沪、深两套结构相同）

```
┌──────────────────────────────────────── EXTERNAL ────────────────────────────────────────┐
│  交易所 L2 行情（逐笔委托 / 逐笔成交 / 逐笔合并 / 3s 快照）── 经华泰内网组播或授权行情商    │
│  华泰柜台（内存级/极速柜台）── 华泰交易网关 ── 交易所交易系统（上交所 / 深交所）           │
└───────┬─────────────────────────────────────────────────────────────────▲────────────────┘
        │ UDP multicast (kernel bypass)                                    │ 柜台 API（TCP，SDK）
┌───────▼─────────────────────────────────────────────────────────────────┴────────────────┐
│                     TRADING PROCESS（同城一套，热路径全部在进程内）                        │
│                                                                                          │
│ ┌──────────── Core 2: L2 MARKET DATA GATEWAY ─────────────────────────────────────────┐  │
│ │ RX (A/B lines) → per-channel Sequencer (ApplSeqNum / BizIndex) → L2 Decoder          │  │
│ │ (SZSE Binary/STEP | SSE STEP/Binary | 华泰行情 SDK) → normalize to L2Event (64B)      │  │
│ │ → security filter → route by channel → shard                                         │  │
│ └───────────────────────────────┬─────────────────────────────────────────────────────┘  │
│                                 │ SPSC<L2Event> per shard (shard = set of channels)       │
│ ┌───────────── Core 3..N: TRADING SHARD（run-to-completion, shared-nothing）────────────┐  │
│ │ AshareBookBuilder (SZSE/SSE semantics, price-limit ladder) → Snapshot cross-check     │  │
│ │ → Market State (停牌/涨跌停距离/价格笼子基准/行情陈旧/竞价时段)                        │  │
│ │ → Signal Engine → Strategy (形态1: 底仓T+0 / 形态2: T+0品种) → Pricing (成本下限)     │  │
│ │ → T+1 Inventory & Cash (可卖/冻结) → Compliance Guard (消息预算/瞬时撤单/撤单比)        │  │
│ │ → PRE-TRADE RISK (sync) → Order Manager (撤单→确认→新单 状态机)                        │  │
│ │ → HuataiGateway (柜台 API) ──────────────────────────────────────────────────────────┼──► │
│ │ ◄── 委托确认 / 成交回报 / 撤单回报 / 废单 ── poll                                       │  │
│ │ → OMS → Position(T+1) → Cash → PnL(含印花税) → KillSwitch                              │  │
│ └───────────┬───────────────────────────────────────────────────────────────────────────┘  │
│             │ SPSC: logs / telemetry / compliance counters                                 │
│ ┌───────────▼──────── NON-HOT PATH ──────────────────────────────────────────────────────┐ │
│ │ Async Logger │ Telemetry │ Risk & Compliance Coordinator（账户级预算切片、全局 PnL）     │ │
│ │ Supervisor / Control │ Reference Data Loader（每日证券信息、涨跌停价、费率、底仓计划）    │ │
│ └────────────────────────────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────────────────────────┘
       ▼ async
  Monitoring / Alerting │ 合规报表（程序化交易统计）│ 历史 L2 存储 │ 回放/回测集群
  独立 Watchdog（另一路柜台会话：带外撤单）│ 盘前批处理：厚门 LevelContext → 底仓规划
```

**两地部署**：沪市标的的行情和订单在上海机房处理，深市标的在深圳机房处理。两个进程各自独立运行风控；账户级的约束（资金、日内亏损、每日消息预算）由 Coordinator 通过预算切片分配到两地（§9.4）。跨城链路只承载控制面和汇总数据，不在热路径上。

### 2.2 Architecture A 的差异部分（MATIC 远程接入）

```
            ┌────────────────── Linux 交易主机（自有机房 / IDC）─────────────────┐
 INSIGHT /  │ MD Source (pluggable): HuataiL2Source | L2FileReplay              │
 MATIC 行情 ►│      │ SPSC                                                       │
 (互联网/专线)│      ▼                                                            │
            │ Trading Shard: Book → Signal → Strategy → Risk → OMS              │
            │      │ SPSC<OrderRequest>              ▲ SPSC<ExecutionReport>      │
            │      ▼                                 │                           │
            │ HuataiGateway (proxy)                                              │
            │   ├─ 方案1：华泰提供 Linux SDK → 进程内 Adapter 线程                  │
            │   └─ 方案2：仅 Windows SDK → 二进制协议 over TCP（或共享内存）→ 桥接机  │
            └──────────────────────────────┬───────────────────────────────────┘
                                           │ (方案2) 同机柜 10GbE，固定长度二进制消息
                              ┌────────────▼─────────────┐
                              │ Windows 桥接机（物理机）   │  MATIC/MAPIs SDK（华泰）
                              │ 只做协议转换 + 映射表       │  不承载任何策略/风控逻辑
                              └────────────┬─────────────┘
                                           │ 互联网 / 专线【BD】
                                   华泰柜台 → 华泰交易网关 → 交易所
```

**桥接机原则**：(1) 它只负责字段映射和 SDK 回调转发，不做任何业务决策；(2) 交易内核里的风控是唯一的事前风控实现，桥接机上不允许重复实现；(3) 桥接协议使用定长二进制消息，带序号和心跳，任何一端断开都按 Gateway 断线处理；(4) 桥接机本身也是故障点，需要配置备机和独立撤单通道。

### 2.3 设计原则（A 股补充）

1. **热路径全部在进程内**：不使用 REST、JSON、RPC 或数据库（同美股版）。
2. **按交易所行情通道分片**：L2 逐笔数据的序号是**按通道连续**的（深交所的 ApplSeqNum、上交所逐笔合并的 BizIndex），把一个通道整体分给一个分片，可以保持顺序语义，分片之间也没有依赖。
3. **风控同步执行、失败即拒绝**；撤单永远放行（仍受消息预算约束，但预算中有为撤单预留的紧急额度）。
4. **合规与风控同等地位**：监管阈值（消息数、瞬时撤单、撤单比）在事前检查中硬性执行，而不是事后统计。
5. **持仓、资金以柜台为权威**：本地维护 T+1 可卖量和冻结资金，用于事前检查；每次回报和定期对账都与柜台核对。
6. **策略层不知道华泰的存在**：只依赖 `OrderRequest` / `ExecutionReport`。

---

## 3. 模块职责

| 模块 | 职责 | 线程 | Hot Path | 输入 | 输出 |
|---|---|---|---|---|---|
| L2 Market Data Gateway | 组播接收（内核旁路）/ SDK 回调接入、A/B 线路仲裁 | MD 核 | ✅ | UDP 包 / SDK 回调 | 原始逐笔消息 + rx_ts |
| Sequencer | 按通道检查序号连续性，缺口检测，重传或重建编排 | MD 核 | ✅ | 原始消息 | 按序消息；缺口事件（只影响该通道） |
| L2 Decoder | 深交所 Binary/STEP、上交所 STEP/Binary、华泰行情 SDK 结构 → 归一化的 `L2Event` | MD 核 | ✅ | 原始消息 | `L2Event`（64 B） |
| Order Book Engine | `AshareBookBuilder`：按深交所/上交所语义重建逐笔订单簿；价位梯按涨跌停价范围分配 | 分片核 | ✅ | `L2Event` | 订单簿、TOB 变化 |
| Snapshot Cross-Check | 用 3 秒 L2 快照（10 档）校验重建的订单簿 | 分片核（低频） | ⚠️ | 快照 | 一致/不一致标志 |
| Market State Engine | 交易时段（集合竞价/连续/午休/收盘竞价）、停复牌、涨跌停距离、价格笼子基准价、行情陈旧 | 分片核 | ✅ | 事件、时钟、参考数据 | 标的状态标志 |
| Signal Engine | microprice、OBI、主动买卖流、波动率、队列长度/位置、大单检测 | 分片核 | ✅ | 订单簿、成交 | Features |
| Market Making Strategy | 形态 1/2 的报价状态机、行情过滤、时段控制 | 分片核 | ✅ | Features、库存、预算 | 期望报价 |
| Pricing Engine | 公允价、保留价、价差（含成本下限）、tick/lot 对齐 | 分片核 | ✅ | Features、成本模型 | QuotePair |
| T+1 Inventory Manager | 昨仓、今买、今卖、冻结、可卖量；底仓偏离；当日净买入上限 | 分片核 | ✅ | 成交、委托 | `T1Position`、单侧阻断 |
| Cash Manager | 可用资金、买单冻结、卖出资金当日可用 | 分片核 | ✅ | 成交、委托 | `CashAccount` |
| Compliance Guard | 每日消息预算、每秒速率、瞬时撤单、撤单比、自成交（对倒）防护、不在涨跌停价挂单 | 分片核 | ✅ | 订单/撤单请求 | 允许/延后/拒绝 |
| Risk Engine (pre-trade) | 同步事前风控（§9） | 分片核 | ✅ | 请求、状态 | `AVerdict` |
| Order Manager (OMS) | A 股委托状态机（未报/已报/部成/已成/待撤/已撤/废单），撤单→确认→新单 | 分片核 | ✅ | 风控通过的请求、回报 | 柜台调用 |
| HuataiGateway | `ExecutionGateway` 的华泰实现：进程内 SDK 线程或 Windows 桥接代理；委托编号映射；回报去重 | 独立线程/桥接机 | ✅ | `OrderRequest` | 归一化 `ExecutionReport` |
| Position / Cash Reconciler | 与柜台查询结果对账（启动、重连、定时） | 控制核 | ❌ | 柜台查询 | 差异告警 |
| PnL Engine | 已实现/未实现损益，佣金、印花税、过户费、规费 | 分片核 | ✅（成交与盯市） | 成交、TOB | `PnLState` |
| Kill Switch | 日内亏损/回撤熔断，锁存，只能由 Supervisor 复位 | 分片核 + 控制核 | ✅（触发） | PnL、风险事件 | 阻断、全撤 |
| Risk & Compliance Coordinator | 账户级额度切片（资金、亏损、消息预算）跨分片/跨城分配；全局汇总 | 控制核 | ❌ | 各分片快照 | 预算更新 |
| Reference Data Loader | 盘前加载：证券信息、涨跌停价、tick、交易单位、T+0 标识、停牌、费率、底仓计划 | 盘前 | ❌ | 华泰/交易所参考数据 | 不可变快照 |
| Base Position Planner（厚门，可选） | 盘前生成目标底仓与底仓止损线 | 盘前批处理 | ❌ | 日线 | 底仓计划 |
| Supervisor / Control | 认证的控制通道：启停、复位、参数变更审批 | 控制核 | ❌ | 运维指令 | 控制命令 |
| Logging / Telemetry / Monitoring | 同美股版；增加程序化交易合规统计 | 非热路径核 | ❌ | SPSC | 日志、指标、合规报表 |
| Replay / Backtest / Simulation | L2 逐笔回放、撮合模拟（A 股规则）、柜台模拟（冻结、废单、延迟） | 离线 | ❌ | 历史 L2 | 报告 |
| Independent Watchdog | 独立进程 + 独立柜台会话：心跳、带外全撤 | 独立主机 | ❌ | 心跳 | 撤单、告警 |

---

## 4. 延迟预算

### 4.1 测量点

| 点 | 定义 |
|---|---|
| T0 | 行情包到达本机网卡（硬件时间戳；A 中为 SDK 回调进入用户态的时刻） |
| T1 | 用户态拿到消息（TSC） |
| T2–T7 | 同美股版：入分片队列、出队、订单簿更新、策略决策、风控通过、调用 Gateway 发送 |
| T8 | 订单离开本机（B：网卡硬件 TX 时间戳；A：SDK 发送函数返回，或桥接报文离开网卡） |
| T9 | 柜台"委托确认"回报到达（柜台已受理） |
| T10 | "交易所已报/交易所确认"回报到达（以华泰 API 提供的状态为准） |
| T11 | 成交回报到达 |
| Tx | 交易所行情时间戳（毫秒精度）——只用于粗粒度的行情延迟监控 |

### 4.2 七个延迟段

| # | 延迟段 | A（MATIC 远程） | B（华泰托管） | 可控性 |
|---|---|---|---|---|
| 1 | Market Data → Strategy Decision（T1→T5） | P50 5–20 μs / P99 < 100 μs【E】 | P50 0.5–1.5 μs / P99 < 5 μs【E】 | ✅ |
| 2 | Strategy → Risk（T5→T6） | < 1 μs | 40–100 ns（A 股风控实测 P50 约 42–45 ns【M-VM】） | ✅ |
| 3 | Risk → OMS（T6→T7） | < 2 μs | 50–150 ns | ✅ |
| 4 | OMS → Broker Adapter（T7→T8） | 进程内 SDK：10–50 μs；Windows 桥接：+50–500 μs【E】 | 柜台 SDK 调用 + 内核旁路发送：1–5 μs【E】【BD】 | ✅（部分） |
| **内部合计** | **段 1–4（T1→T8）** | **P50 ≈ 20–100 μs，P99 < 1 ms**（满足目标）【E】 | **P50 ≈ 2–5 μs，P99 ≈ 10–20 μs**【E】 | ✅ |
| 5 | 柜台 / 网络 / 券商交易网关 | 互联网或专线 + 柜台风控 + 交易网关：**毫秒级**【BD】 | 内存级柜台风控 + 交易网关：无公开数据，须实测【BD】 | ❌ |
| 6 | 交易所确认 | 包含在段 5 的观测中 | 【XD】须实测 | ❌ |
| 7 | 端到端 | Tick（交易所发布）→ ACK：**数毫秒到数十毫秒**（另加互联网行情延迟）【BD】 | Tick-to-Order：微秒级；Tick-to-ACK：须实测，**不承诺 < 1 ms**【BD】【XD】 | 部分 |

> **另一个必须看到的"延迟"**：在每日 2 万笔的消息预算下，平均每秒只能发出约 1.4 条消息。策略层的"决策到发单"往往不是被计算速度限制，而是被**预算节奏**限制（§10）。在 A 股，**报价质量更多取决于"在哪个时刻花掉有限的消息"**，而不是比别人快几微秒。

### 4.3 逐模块预算 — Architecture B

| Stage | P50 | P99 | Worst | Controllable | 关键路径 | Optimization |
|---|---|---|---|---|---|---|
| NIC → Userspace（内网 L2 组播） | 1 μs【E】 | 2 μs | 10+ μs | ✅ | ✅ | 内核旁路；同 NUMA |
| L2 Decode（Binary/STEP → L2Event） | 20–50 ns【E】 | 100 ns | 200 ns | ✅ | ✅ | 零拷贝；优先使用 Binary 协议（STEP 是文本 tag=value 格式，解析成本高得多） |
| Sequencing（按通道） | 5–10 ns | 30 ns | 有缺口时进入恢复 | ✅ | ✅ | 每通道一个计数器 |
| MD 核 → 分片核 | 80–150 ns【E】 | 300 ns | 数 μs | ✅ | ✅ | SPSC |
| Order Book（`AshareBookBuilder::apply`） | 100 ns【E】（VM 约 153–159 ns【M-VM】） | 400 ns（VM 约 432–462 ns【M-VM】） | 1–2 μs | ✅ | ✅ | 涨跌停价梯（无溢出）、开放寻址哈希、三级位图 |
| Feature / Strategy / Pricing | 200–500 ns | 1.5 μs | 3 μs | ✅ | ✅ | 同美股版 |
| T+1 / Cash / Compliance | 20–50 ns | 100 ns | — | ✅ | ✅ | 整数运算；计数器常驻 L1 |
| Risk（`ashare_pre_trade_check`） | 40–50 ns（VM 约 42–45 ns【M-VM】） | 100 ns（VM 约 46–48 ns【M-VM】） | 200 ns | ✅ | ✅ | 同美股版的检查顺序原则 |
| Order Construction + 柜台 API 调用 | 0.5–3 μs【BD】 | — | — | ⚠️ 取决于 SDK | ✅ | 预分配请求结构；SDK 调用放在专用线程或同线程，以 SDK 的线程模型为准 |
| Network Send | 1–2 μs【E】 | 3–5 μs | — | ✅ | ✅ | 内核旁路（若 SDK 允许，例如 Onload 透明加速） |
| 柜台风控 + 交易网关 | 【BD】 | 【BD】 | 【BD】 | ❌ | ✅ | 选择内存级/极速柜台 |
| 交易所撮合与确认 | 【XD】 | 【XD】 | 开盘、尾盘、行情剧烈时尾部显著 | ❌ | ✅ | 只能测量 |

### 4.4 逐模块预算 — Architecture A

| Stage | P50 | P99 | Worst | Controllable | 关键路径 | 说明 |
|---|---|---|---|---|---|---|
| 交易所 → 华泰行情分发 → 互联网 → 本机 | 毫秒到数十毫秒【BD】 | — | 秒级（断流） | ❌ | ✅ | 用 Tx 与本机 PTP/NTP 时间之差做粗粒度监控 |
| SDK 回调 → L2Event | 1–10 μs【E】 | 50 μs | — | ⚠️ | ✅ | SDK 回调线程只做拷贝和入队 |
| 内部计算（Book → Risk → OMS） | 2–5 μs | 20–50 μs | — | ✅ | ✅ | 与 B 相同的代码 |
| 分片 → Gateway 线程 / 桥接机 | 0.2–1 μs（进程内）/ 50–500 μs（跨机桥接）【E】 | — | — | ✅ | ✅ | 桥接机与交易机同机柜直连 |
| SDK 发送 → 华泰柜台 → 交易所 → 回报 | 数毫秒到数十毫秒【BD】 | — | 秒级 | ❌ | ✅ | 在 T8→T9→T10→T11 打点，按时段分桶统计 |
| **预算排队** | 0（额度充足时） | **秒级** | — | ✅（策略层面） | ✅ | §10 |

---

## 5. CPU / Thread Architecture

### 5.1 并发模型（与美股版一致，A 股补充）

| 模型 | 采用 | 位置 | A 股补充说明 |
|---|---|---|---|
| Run-to-completion 单线程事件循环 | ✅ 核心 | 每个交易分片 | 同美股版 |
| Pipeline（一级） | ✅ | L2 网关 → 分片 | 同美股版 |
| Shared Nothing | ✅ | 分片之间 | **分片单位 = 交易所行情通道**：一个通道的全部标的归同一个分片，通道内的序号语义（例如深交所待成交主动单的处理）因此留在一个线程内 |
| SPSC / Lock-Free Ring | ✅ | 所有线程间通信 | 同美股版（实现见美股版 §6.9） |
| Disruptor | ⚠️ 可选 | L2 广播给分片和录制器 | |
| MPMC / Actor / 锁 | ❌ 热路径 | 控制面 | |
| SDK 回调线程 | ⚠️ 受 SDK 约束 | Gateway | 柜台和行情 SDK 通常自带回调线程。**回调中只做拷贝和入队**，不做任何业务逻辑，然后由分片线程消费 |

### 5.2 Core Mapping — Architecture B（每个城市一台服务器，网卡位于 NUMA 0，关闭超线程）

```
Core 0   OS housekeeping + 所有 IRQ
Core 1   Control: Supervisor, Risk & Compliance Coordinator, Reference Data, Config
── isolated (isolcpus / nohz_full / rcu_nocbs) ──
Core 2   L2 MD Gateway: A/B 组播轮询、按通道排序、解码、按通道路由
Core 3   Trading Shard 0（通道组 0）
Core 4   Trading Shard 1（通道组 1）
Core 5   Huatai 柜台 SDK 线程（如果 SDK 要求独立的发送/回调线程）
Core 6   Hot spare / L2 录制器
── pinned, non-isolated ──
Core 7   Async Logger（含合规审计记录）
Core 8   Telemetry / 合规统计导出
```

- **柜台 SDK 的线程模型决定了 Core 5 的形态**：如果 SDK 允许在调用方线程内同步发送，并支持非阻塞轮询回报，就把它并入分片线程（最低延迟）；如果 SDK 自带阻塞式的回调线程，就把它绑定到 Core 5，与分片线程之间用一对 SPSC 通信（多一跳约 100–300 ns【E】）。
- 两地服务器配置相同；每台只处理本地交易所的标的。

### 5.3 Core Mapping — Architecture A（Linux 交易主机 8 核 + 可选 Windows 桥接机）

```
Linux:
Core 0  OS + IRQ        Core 1  Control / Supervisor / Reference Data
Core 2  MD Source（华泰行情 SDK 回调线程 + 解码入队）
Core 3  Trading Shard（单分片，覆盖沪深全部标的）
Core 4  HuataiGateway（进程内 SDK 线程，或桥接协议收发线程）
Core 5  Logger + Telemetry   Core 6  本地 Watchdog   Core 7  spare
Windows 桥接机（若需要）：SDK 回调线程 + 桥接协议线程，绑核，关闭电源管理和不必要的服务
```

### 5.4 事件循环骨架（A 股差异）

```cpp
template <ExecutionGateway Gw>
void TradingShard<Gw>::run() noexcept {
  while (running_) [[likely]] {
    const uint64_t now = clock_.now_ns();
    int n = 0; L2Event ev;
    while (n < kMaxBatch && md_ring_.try_pop(ev)) { on_l2(ev, now); ++n; }   // book + dirty marks
    if (quiet_for(now) > cfg_.pending_flush_ns) books_.flush_all();          // close SZSE matching bursts
    gateway_.poll(*this);                     // 委托确认 / 成交 / 撤单 / 废单 -> OMS, T+1, cash, PnL, kill
    control_ring_.drain(*this);
    session_.tick(now);                       // phase changes: 9:30, 11:30, 13:00, 14:57 -> pull quotes etc.
    if (timer_wheel_.due(now)) [[unlikely]] on_timers(now);   // ack timeouts, deferred cancels, budget pacing
    if (n) reconcile_dirty_symbols(now);      // skip symbols whose book is in transition (pending aggressor)
  }
}
```

---

## 6. 核心数据结构

> 通用的设计取舍（AoS/SoA、对象池、开放寻址哈希、缓存行对齐、禁止热路径分配/虚调用/`shared_ptr`/异常）与美股版 §6.1 完全相同，这里不再重复。下面只列 A 股专属的结构，代码摘自 `reference/ashare.hpp`，已编译和测试。

### 6.1 每日参考数据

```cpp
enum class Exchange : uint8_t { SSE, SZSE };
enum class Board : uint8_t { Main, Star, ChiNext, Bse };
enum class InstrType : uint8_t { Stock, Etf, ConvBond };

struct SecurityRef {         // loaded pre-open, immutable intraday
  uint32_t  sec_id;          // dense internal id == book slot
  Exchange  exch;
  Board     board;
  InstrType type;
  bool      t0;              // same-day round trip allowed (convertible bonds, some ETFs)
  bool      stamp_duty;      // stocks: seller pays stamp duty
  Price     tick;            // 1e-4 CNY: stock 0.01 -> 100, ETF/CB 0.001 -> 10
  Price     prev_close;
  Price     limit_up;        // 0 => no price limit today (e.g. first days after listing)
  Price     limit_down;
  Qty       buy_min, buy_step;    // main board 100/100, STAR 200/1, CB 10/10
  Qty       sell_lot;             // round lot for sells; odd remainder must go in one order
  int32_t   cage_bps;             // 200 => 2 % (0 => no cage)
  int32_t   cage_ticks;           // main board: 10 ticks alternative; STAR/ChiNext: 0
};
```

参考数据来源：华泰或交易所下发的每日证券信息（证券代码、板块、涨跌停价、停牌标志、T+0 标识等）、费率配置、底仓计划。盘前加载后做**一致性校验**（涨跌停价必须落在 tick 网格上、涨停价 > 跌停价、交易单位非零等），任何一项失败，该标的当日禁用。

### 6.2 归一化 L2 事件

```cpp
enum class L2Type : uint8_t { Order, Trade, Cancel };
enum class OrdKind : uint8_t { Limit, Market, BestOwn };   // SZSE OrdType '2' / '1' / 'U'
enum class Semantics : uint8_t { Szse, Sse };

inline constexpr uint64_t make_key(uint16_t channel, uint64_t seq) noexcept { return (uint64_t(channel) << 40) | seq; }

struct alignas(kCacheLine) L2Event {
  uint64_t seq;               // per-channel sequence (SZSE ApplSeqNum, SSE BizIndex in the merged feed)
  uint64_t exch_ts;
  uint64_t rx_ts;
  uint64_t key;               // Order / Cancel: order key
  uint64_t bid_key, ask_key;  // Trade: both sides
  uint32_t px;                // 1e-4 CNY (0 for market orders)
  uint32_t qty;               // Order: SZSE original qty / SSE resting remainder; Cancel: cancelled qty
  uint16_t channel;
  uint16_t book;              // dense security slot
  L2Type   type;
  Side     side;              // Order side; Trade: aggressor side (SSE BSFlag, SZSE inferred)
  OrdKind  kind;
  Semantics sem;
};
static_assert(sizeof(L2Event) == 64);
```

**解码器的职责**：把深交所 Binary（或 STEP）行情、上交所 STEP/Binary 行情（IS120 行情网关规范），或者华泰行情 SDK 的结构体，转换成 `L2Event`。字段的精确定义（消息类型编号、ExecType 取值、价格精度、时间戳格式）必须以**最新版的交易所接口规范**为准（附录 C），解码器要为每个版本建立回放回归测试。

### 6.3 订单簿结构（价位梯覆盖涨跌停价范围）

```cpp
struct AOrder {               // 32 bytes
  uint64_t key;               // (channel << 40) | per-channel order sequence
  uint32_t px;                // 1e-4 CNY
  uint32_t qty;               // remaining shares on the book
  uint32_t prev, next;        // intrusive FIFO within the level
  uint32_t level;
  uint16_t book;
  Side     side;
  uint8_t  pad_;
};
static_assert(sizeof(AOrder) == 32);

struct ALevel { uint64_t qty = 0; uint32_t count = 0, head = kNil, tail = kNil, pad_ = 0; };
```

**A 股专属优化**：有涨跌幅限制的标的，当日可成交的价格范围 `[跌停价, 涨停价]` 在盘前就已确定，因此价位梯可以**完整覆盖整个范围**：O(1) 价格→价位映射，不需要重定窗口，也没有溢出路径。价位数 = (涨停价 − 跌停价)/tick + 1。例如 10 元的主板股票 ±10%，tick 0.01 → 201 档；1,500 元的股票 ±10% → 30,001 档（每侧约 720 KB）。档数可能超过 4096，因此用**三级位图**（64³ = 262,144 档）查找最优价和逐档遍历深度，仍是 O(1)。无涨跌幅限制的标的（如部分新股上市初期）退化为围绕参考价的大窗口。

### 6.4 T+1 持仓与资金

```cpp
struct T1Position {
  Qty  yd_qty = 0;          // settled position at the open (sellable base for T+1 instruments)
  Qty  bought_today = 0;    // filled buys today (NOT sellable today unless t0)
  Qty  sold_today = 0;      // filled sells today
  Qty  frozen_sell = 0;     // leaves of live sell orders (counter freezes these shares)
  Qty  open_buy = 0;        // leaves of live buy orders
  bool t0 = false;
  Qty position() const noexcept { return yd_qty + bought_today - sold_today; }
  Qty sellable() const noexcept {
    const Qty base = t0 ? position() : yd_qty - sold_today;
    return base - frozen_sell;
  }
  Qty net_bought_today() const noexcept { return bought_today - sold_today; }  // > 0 adds overnight risk
};

struct CashAccount {        // all 1e-4 CNY
  int64_t available = 0;    // usable for new buys (same-day sell proceeds are reusable for buys)
  int64_t frozen = 0;       // reserved by live buy orders (notional + estimated fees)
};
```

**与柜台语义保持一致**：下卖单时冻结股份，撤单确认后解冻；下买单时冻结资金（含预估费用），成交后按实际金额扣减，撤单后解冻；卖出成交的资金当日可用于买入（但当日不可取出）。本地状态用于事前检查（避免柜台废单浪费消息预算），柜台的查询结果是权威数据，定期对账。

### 6.5 费用模型

```cpp
// Rates in units of 1e-8 (0.01 % == 10'000). Values are configuration, loaded daily and versioned.
struct FeeSchedule {
  int64_t commission_rate = 10'000;  // e.g. 0.01 % (万1) — negotiated with the broker
  int64_t commission_min = 0;        // per order, 1e-4 CNY (a 5-CNY minimum would kill small orders)
  int64_t stamp_duty_rate = 50'000;  // 0.05 % sell side, stocks only (halved from 0.1 % on 2023-08-28)
  int64_t transfer_rate = 1'000;     // 0.001 % both sides (verify current CSDC schedule)
  int64_t exchange_rate = 3'410;     // 经手费 0.00341 % both sides (since 2023-08-28)
  int64_t regulator_rate = 2'000;    // 证管费 0.002 % both sides
  bool    exch_fees_in_commission = true;  // many brokers bundle 经手费/证管费 into commission
};

inline int64_t fee_for_fill(const FeeSchedule& f, const SecurityRef& r, Side s, int64_t notional) noexcept {
  int64_t fee = std::max(notional * f.commission_rate / 100'000'000, f.commission_min);
  if (r.type == InstrType::Stock) fee += notional * f.transfer_rate / 100'000'000;
  if (!f.exch_fees_in_commission) fee += notional * (f.exchange_rate + f.regulator_rate) / 100'000'000;
  if (s == Side::Sell && r.stamp_duty) fee += notional * f.stamp_duty_rate / 100'000'000;
  return ceil_fen(fee);   // round up to 0.01 CNY (conservative)
}
```

费用在成交回报时按此估算并同步计入 PnL，日终再与柜台的清算数据核对。注意：实际的收费口径（按笔还是按成交汇总计算、取整方式、ETF 和可转债分别收哪些费用）以华泰的结算规则为准【BD】。

### 6.6 OMS 委托状态：与 A 股柜台状态的映射

A 股柜台的委托状态通常包括：未报、待报、已报、已报待撤、部成、部成待撤、已成、部撤、已撤、废单（具体名称和编码以华泰 API 为准）。OMS 内部状态与它们的映射如下：

| OMS 状态 | 柜台状态（典型） | 说明 |
|---|---|---|
| PendingNew | 未报 / 待报 | 已发给柜台，尚未报到交易所 |
| Live | 已报 / 部成 | 在交易所订单簿中（部成时 leaves > 0） |
| PendingCancel | 已报待撤 / 部成待撤 | 撤单已发出，等待交易所确认 |
| Filled | 已成 | 终态 |
| Canceled | 已撤 / 部撤 | 终态（部撤：部分成交后撤掉剩余部分） |
| Rejected | 废单（交易所拒绝）/ 柜台拒单（资金、持仓、风控不通过） | 终态；两类原因要分开统计 |
| Unknown | 超时未确认 / 断线期间 | 需要查询或对账 |

**没有 PendingReplace**：A 股没有改单。改价通过"撤旧单 → 等待撤单确认 → 下新单"完成（§8.9）。

`Order`、`ExecutionReport`、`MarketSnapshot`、`StrategyState`、`PnLState` 的布局沿用美股版 §6.6–§6.7。`ExecutionReport` 增加两项：`reject_source`（柜台/交易所）和 `counter_order_id`（柜台委托编号）。

### 6.7 Execution Gateway：华泰实现

接口与美股版相同（C++20 concept `ExecutionGateway`，外加用于工具的虚接口 `IExecutionGateway`），只是去掉 `send_replace`（A 股没有改单），并增加批量撤单：

```cpp
template <class G>
concept AshareExecutionGateway = requires(G g, const OrderRequest& r, ExecSinkProbe& sink) {
  { g.send_new(r) } noexcept -> std::same_as<SendResult>;
  { g.send_cancel(r) } noexcept -> std::same_as<SendResult>;
  { g.cancel_all() } noexcept -> std::same_as<SendResult>;   // batch cancel if the API offers it, else iterate
  { g.poll(sink) } noexcept -> std::same_as<int>;
  { g.healthy() } noexcept -> std::same_as<bool>;
};
```

| 实现 | 说明 |
|---|---|
| `HuataiGateway`（进程内） | 华泰提供 Linux SDK 时使用：SDK 线程绑核；本地委托号 ↔ 柜台委托编号映射；成交回报按（柜台委托编号 + 成交编号）去重 |
| `HuataiBridgeGateway`（代理） | 只有 Windows SDK 时使用：定长二进制协议（带序号、心跳、ACK）发往桥接机；桥接机调用 SDK |
| `SimAshareGateway` | 回测/仿真：A 股撮合（价格优先、时间优先）、冻结与解冻、废单注入、延迟模型 |

**SDK 隔离规则**：柜台和行情 SDK 都被视为"不可信的第三方代码"。它们只能出现在 `broker/huatai/` 目录中，在隔离线程里运行；回调中禁止调用业务逻辑；SDK 抛出的任何异常都在边界处捕获并转换为 Gateway 断线事件。

---

## 7. A 股逐笔订单簿

### 7.1 深交所与上交所的逐笔语义差异（重建订单簿的关键）

| 方面 | 深交所（SZSE） | 上交所（SSE） |
|---|---|---|
| 序号 | 每个通道（ChannelNo）内 ApplSeqNum 连续，逐笔委托和逐笔成交共用同一序列 | 逐笔合并行情中，每个通道的 BizIndex 连续（以 IS120 规范为准） |
| 委托数量 | **原始委托数量**（包括会立即成交的主动单） | **只发布剩余挂单部分**（完成即时撮合之后，有剩余才发布） |
| 主动单在成交中 | 成交记录同时引用买卖双方的委托序号；主动方委托已先行发布 | 成交记录引用的主动方委托**可能从未进入订单簿**（全部成交时不会单独发布） |
| 撤单 | 以成交记录形式发布，ExecType 表示撤销，买卖序号中有一个非零，数量为撤销数量 | 逐笔委托中的删除类型（以规范为准） |
| 市价类委托 | OrdType：市价（1）、限价（2）、本方最优（U）；市价单价格字段不表示实际价格 | 剩余部分以实际价格作为挂单发布 |
| 即时成交剩余撤销 | 剩余部分以撤单记录发布 | 剩余部分不会进入订单簿，因此不发布 |
| 主动方判定 | 连续竞价阶段：成交双方中序号较大者为主动方 | 成交记录中的内外盘标志（BSFlag） |

> 上表是工程实现层面的归纳。**字段名、取值、例外情况（例如集合竞价阶段的处理）必须逐项对照交易所的最新接口规范**，并用真实历史数据回放验证（§7.6）。

### 7.2 重建算法

```cpp
bool AshareBookBuilder::apply(const L2Event& ev) noexcept {
  const uint8_t cs = chan_slot_[ev.channel];
  if (cs >= kMaxChannels) [[unlikely]] { ++st_.unknown_channel; return false; }   // unregistered channel
  Pending& p = pend_[cs];
  bool changed = false;
  if (p.active && !related(p, ev)) changed |= flush(p);     // the SZSE matching burst is over
  switch (ev.type) {
    case L2Type::Order: return on_order(ev, p) | changed;
    case L2Type::Trade:
      changed |= fill_side(p, ev.bid_key, ev.qty, ev.px, ev.sem);
      changed |= fill_side(p, ev.ask_key, ev.qty, ev.px, ev.sem);
      return changed;
    case L2Type::Cancel:
      if (p.active && ev.key == p.key) { p.active = false; return changed; }  // IOC remainder cancelled
      return reduce_or_remove(ev.key, ev.sem == Semantics::Sse ? 0xFFFF'FFFFu : ev.qty) | changed;
  }
  return changed;
}

bool AshareBookBuilder::on_order(const L2Event& ev, Pending& p) noexcept {
  AshareBook& b = books_[ev.book];
  if (ev.sem == Semantics::Sse) return insert(ev.key, ev.book, ev.side, ev.px, ev.qty);   // remainder only
  uint32_t px = ev.px;
  if (ev.kind == OrdKind::BestOwn) {           // 本方最优: priced at own-side best on entry
    px = b.best_px(ev.side);
    if (!px) return false;                     // exchange rejects it when own side is empty
  }
  const uint32_t opp = b.best_px(ev.side == Side::Buy ? Side::Sell : Side::Buy);
  const bool crossing = ev.kind == OrdKind::Market ||
                        (opp && (ev.side == Side::Buy ? px >= opp : px <= opp));
  if (crossing) {                              // hold as pending aggressor; never insert a crossed order
    p = Pending{ev.key, px, ev.qty, 0, ev.book, ev.side, ev.kind, true};
    return false;
  }
  return insert(ev.key, ev.book, ev.side, px, ev.qty);
}

bool AshareBookBuilder::flush(Pending& p) noexcept {
  p.active = false;
  if (!p.remaining) return false;
  // Limit: rests at its limit; 对手方最优 market: rests at the (single) matched price.
  const uint32_t px = p.kind == OrdKind::Market ? p.last_fill_px : p.limit_px;
  if (!px) return false;                       // market order with no fill: nothing rests
  /* if px still crosses the opposite best -> ++crossed_after_flush (data issue, alarm) */
  return insert(p.key, p.book, p.side, px, p.remaining);
}
```

**核心思想**：
1. **深交所的"待定主动单"（pending aggressor）**：会穿价的新委托（限价穿价单或市价单）**不立即放入订单簿**，否则订单簿会短暂交叉。它暂存在通道级的 pending 槽中，后续引用它的成交记录只减少它的剩余量。遇到第一条与它无关的消息时，说明这一轮撮合结束：剩余部分按限价（或"对手方最优"的成交价）挂入订单簿；如果是即时成交剩余撤销，会先收到它的撤单记录，pending 直接丢弃。
2. **上交所的"缺席主动方"**：成交记录中找不到的委托序号，就是那个已全部成交、从未入簿的主动方，直接忽略即可（单独计数，不算错误）。
3. **跨包的撮合序列**：深交所一轮撮合（委托 + 成交 + 撤单）在通道序号上是连续的，但可能被拆分到多个网络包中。因此 pending 槽可以跨包保留；如果通道静默超过 T 微秒，由定时器调用 `flush_all()`。**pending 存在期间，该标的的订单簿处于"过渡态"，策略不在它上面报价**。

### 7.3 复杂度

| 操作 | 复杂度 | 说明 |
|---|---|---|
| 委托号 → 订单 | 期望 O(1) | 开放寻址哈希，键为 (通道, 序号)；可选优化：由于通道内序号稠密递增，也可以用"序号 → 池下标"的直接数组（内存换掉哈希） |
| 价格 → 价位 | O(1) | 涨跌停价梯 |
| 最优价 | O(1) | 缓存最优价下标；价位清空时用三级位图在 O(1) 内找下一档 |
| 新增 / 部分撤单 / 删除 / 成交 | O(1) | |
| 前 N 档深度 | O(N) | 位图的 `prev_below` / `next_above` |
| 队列位置 | O(队列长度)，按需计算 | 价位内侵入式 FIFO |

### 7.4 用 3 秒快照校验

L2 每 3 秒推送一次包含 10 档价量的快照，这是**交易所免费附带的一致性校验源**：
- 收到快照时，取重建订单簿的前 10 档，与快照逐档比较价格和数量。
- 快照与逐笔数据之间没有严格的序号对齐，因此只允许**短暂**的不一致：连续 2 次快照都不一致，才判定为订单簿异常，将该标的置为 `BookInvalid`（撤销其报价）并触发重建。
- 重建方式：(1) 如果行情源提供逐笔重传或回放服务，就从当日起点重放该通道；(2) 否则只能以快照的 10 档作为近似初始状态，在窗口内继续接收逐笔数据，同时禁止在该标的上报价，直到连续 N 次快照一致。

### 7.5 内存规划（示例：每个分片 200 只标的）

| 项 | 估算 | 示例 |
|---|---|---|
| 价位梯 | Σ 2 × 档数 × 24 B | 低价股约 200–2,000 档 → 平均约 100 KB/标的 → 20 MB |
| 存活订单 | A 股低价股的排队很长，单只标的的存活委托可达数万甚至更多 → 用历史数据统计峰值 × 2 | 假设 5M → 对象池 160 MB |
| 哈希表 | 2^24 槽 × 16 B | 256 MB |
| 页 | 1 GB HugePages，NUMA 本地 | 约 0.5 GB |

### 7.6 正确性验证

- **对拍测试【M-VM】**：我写了一个价格-时间优先的撮合模拟器，按深交所语义（原始委托、成交、撤单记录、即时成交剩余撤销、对手方最优、本方最优）和上交所语义（只发布剩余部分、主动方缺席）分别生成逐笔消息流。共 78.4 万条消息、两只标的（深市主板 ±10%、沪市科创板 ±20%），每隔 7 步比对重建订单簿与模拟器的**双边前 10 档价量**，全部一致；未知委托号为 0，交叉为 0，越界为 0（上交所"缺席主动方"12.2 万次，属于正常现象）。
- **三级位图**：20 万档，40 万次随机置位/清位，与 `std::set` 对比 `highest/lowest/prev_below/next_above`，全部一致【M-VM】。
- **生产前**：用真实的历史 L2 逐笔数据（深交所、上交所各选若干交易日，覆盖开盘、涨跌停、临时停牌等情况）全量回放，与 3 秒快照逐次比对，要求连续交易时段零持续性不一致。
- **性能**：`AshareBookBuilder::apply` 在开发 VM 上 P50 约 153–159 ns、P99 约 432–462 ns、P99.9 约 561–650 ns【M-VM】（随机合成负载，g++ 与 clang++ 共 3 次运行）。

---

## 8. Market Making Strategy（A 股版）

> 同样**不构成**盈利承诺。§1.5 已经说明，成本结构使得大多数标的上的做市在扣费后为负期望。本节给出的是在这些约束下仍然**工程正确、合规、风险受控**的报价框架。

### 8.1 两种业务形态的决策流水线

```
L2Event ─► Book (pending-aware) ─► Market State（时段/停牌/涨跌停距离/陈旧）
        ─► Features（microprice、OBI、主动买卖流、σ、队列长度与位置、大单）
        ─► Regime filter（撤出 / 单边 / 加宽）
        ─► Fair price ─► Reservation price（相对底仓的库存偏斜）
        ─► Spread（σ、库存、流动性、毒性、**成本下限：含印花税的回转成本**）
        ─► Quote prices（tick 对齐、不穿价、不在涨跌停价、价格笼子）
        ─► Quote sizes（lot 规则、T+1 可卖量、当日净买入上限、资金）
        ─► Desired quotes ─► Reconciler（**预算节奏 + 最短挂单时间 + 撤单→确认→新单**）
        ─► pre_trade_check ─► OMS ─► HuataiGateway
```

### 8.2 特征（A 股补充）

- microprice、OBI、主动买卖流、EWMA 波动率、短期动量的定义同美股版 §8.2。
- **主动方判定**：深交所在连续竞价阶段用"成交双方中序号较大者"判定，上交所用 BSFlag。这比美股的 tick rule 更精确。
- **队列长度**：A 股低价股在最优价上的排队经常是几十万甚至上百万股。队列长度与平均每秒成交量之比（预计排到所需的时间）是决定"是否值得挂单"的关键特征。
- **大单/撤单行为**：在临近价位频繁出现、又频繁撤掉的大额挂单，往往不代表真实的买卖意愿。这类挂单的数量应在 OBI 中降低权重。
- **涨跌停距离**：`(涨停价 − mid)/tick` 和 `(mid − 跌停价)/tick`。临近涨跌停时，订单簿结构会发生质变（一侧排队、另一侧流动性枯竭）。

### 8.3 形态 1：股票"底仓 T+0"

**定义**：底仓目标 B（股数，由盘前的底仓计划给出）；偏离 `dev = position − B`；允许的日内带宽 band。

**约束**（由 `t0_quote_limits()` 计算，已测试）：
- 卖出量 ≤ 可卖量 = 昨仓 − 今日已卖 − 卖单冻结（**今日买入的股份不可卖**）。
- 卖出后 dev 不得低于 −band：`ask_qty ≤ band + dev − frozen_sell`。
- 买入后 dev 不得高于 +band：`bid_qty ≤ band − dev − open_buy`。
- 当日净买入 ≤ `max_net_buy_today`（这部分会变成**无法在当日平掉的隔夜风险**）。
- **收盘前（late session，例如 14:50 之后，可配置）**：只做回归底仓的一侧。dev > 0 时只卖（卖的是昨仓，总持仓因此回到 B）；dev < 0 时只买；dev = 0 时停止报价。

```cpp
inline T0Limits t0_quote_limits(const T1Position& p, Qty base_target, Qty band, Qty max_net_buy_today,
                                bool late_session) noexcept {
  const Qty dev = p.position() - base_target;      // > 0: above base (bought more than sold)
  T0Limits l{};
  l.max_ask_qty = std::max<Qty>(0, std::min(p.sellable(), band + dev - p.frozen_sell));  // stay >= base - band
  const Qty buy_room_band = std::max<Qty>(0, band - dev - p.open_buy);
  const Qty buy_room_overnight = p.t0 ? buy_room_band
                                      : std::max<Qty>(0, max_net_buy_today - p.net_bought_today() - p.open_buy);
  l.max_bid_qty = std::min(buy_room_band, buy_room_overnight);
  if (dev <= -band) l.max_ask_qty = 0;             // sold too much of the base: only buy back
  if (late_session) {                              // converge to base before the close, add no new risk
    if (dev >= 0) l.max_bid_qty = 0;
    if (dev <= 0) l.max_ask_qty = 0;
    l.max_ask_qty = std::min(l.max_ask_qty, std::max<Qty>(0, dev));
    l.max_bid_qty = std::min(l.max_bid_qty, std::max<Qty>(0, -dev));
  }
  l.bid_on = l.max_bid_qty > 0;
  l.ask_on = l.max_ask_qty > 0;
  return l;
}
```

**为什么底仓 T+0 在 T+1 下能成立**：持有昨仓 B 时，日内先卖出 x 股（用昨仓），再买回 x 股（今日买入的部分当日不可卖，但无所谓），收盘总持仓仍是 B，实现了一次回转。反过来先买后卖也可以：先买入 x 股（总持仓 B + x），再卖出 x 股**昨仓**，收盘仍为 B。所以，**底仓就是做市的"库存缓冲"**，它的价格风险是这一形态固有的成本，必须计入风险预算（§9.2）。

### 8.4 形态 2：T+0 品种（可转债、T+0 ETF）

- 当日买入可当日卖出（`t0 = true`），约束与美股版接近：库存在 [−已有持仓, +上限] 之间，不能为负（没有融券）。
- 可转债：tick 为 0.001 元，相对价格非常小。价差和偏斜应以"元"或"bp"为单位计算，再对齐到 tick。涨跌幅 ±20%（上市首日不同，首日还有临时停牌机制）【REG】。
- T+0 ETF：要注意官方流动性服务商的存在，它们的报价往往非常紧。被动挂单的成交概率和逆向选择要用回测和小规模实盘评估。

### 8.5 Fair Price / Reservation Price / Spread

公式与美股版 §8.3–§8.5 相同，库存变量改用"相对底仓的偏离"：

$$
q_n = \text{clamp}\!\left(\frac{dev}{band}, -1, 1\right), \qquad r = \text{fair} - q_n \cdot S_{max} \cdot \delta
$$

**价差下限必须包含回转成本**（A 股最关键的改动）：

$$
h \ge \text{fee\_edge} = \frac{C_{rt}(P)}{2} + \text{min\_edge}
$$

其中 $C_{rt}(P)$ 是每股的回转成本（含卖方印花税），由 `fee_for_fill()` 按当前价格计算。把它作为 `QuoteParams::fee_edge` 传给 `compute_quotes()`（沿用美股版代码）。结果是：对于高价股，价差下限会自动大于市场价差，因此**不会挂出注定亏钱的报价**。这类标的在盘前选股阶段就应该被排除。

### 8.6 Quote Size（A 股交易单位）

- 买入：主板/创业板为 100 股的整数倍；科创板至少 200 股，超过部分可按 1 股递增；可转债为 10 张的整数倍【REG】。
- 卖出：不足一手的零股部分必须一次性申报卖出。`lot_ok()` 实现了主板规则；科创板卖出规则（至少 200 股，余额不足 200 股时一次性卖出）需按规则补充。
- 数量取以下各项的最小值：策略期望量、`t0_quote_limits()` 的上限、可用资金所能支撑的量、`max_order_qty`；最后按交易单位向下取整。

### 8.7 成交概率与队列位置

- A 股的 L2 逐笔数据可以重建完整的订单级队列（深交所发布全部委托；上交所发布剩余挂单），因此可以估计自己订单前面的排队量。但自己的委托在行情中是匿名的，需要用"下单时刻 + 价格 + 数量"做近似匹配（深交所），或者保守地假设自己排在队尾。
- 预计排到时间 ≈ 前方排队量 / 该价位的成交速度。如果远大于策略的持有期（例如低价股排队 50 万股、每秒成交 2 千股 → 约 250 秒），被动挂单基本没有意义。
- 强度模型 $\lambda(d) = A e^{-kd}$ 同美股版，按标的、时段分桶标定。

### 8.8 报价刷新：预算优先

在每日约 2 万笔的预算下，报价刷新逻辑必须"花在刀刃上"：

| 规则 | 说明 |
|---|---|
| **改价成本 = 2 条消息** | 撤单 + 新单。只有当新价格与旧价格相差 ≥ 1 tick，**并且**预期收益超过预算的影子价格时才改价 |
| **防御优先** | 价格向不利方向移动（旧报价变得过于激进、可能被吃掉）时立即撤单，可以动用紧急预算 |
| **最短挂单时间** | 非紧急撤单必须等待挂单 ≥ `min_rest`（默认 ≥ 1 秒）之后才能发出，避免"1 秒内申报又撤单"（§10.4） |
| **预算节奏** | `DailyMessageBudget` 按交易时间线性释放额度（§10.3）。早盘用完预算会导致午后无法撤单，这本身就是风险 |
| **偏好"不动"** | A 股的时间优先很有价值（排队位置）。如果当前报价仍在可接受范围内，就保持不动 |
| **单侧报价** | 预算或库存约束紧张时，只保留对降低风险有帮助的一侧 |

### 8.9 撤单 → 确认 → 新单 状态机（每个报价槽）

```
            send New (risk ok, budget ok)
 ┌───────┐ ───────────────────────────────► ┌──────────────┐  柜台拒单 / 废单   ┌──────────────────┐
 │ EMPTY │                                  │ PENDING_NEW  │ ─────────────────► │ EMPTY + cooldown │
 └───────┘ ◄─────────────┐                  │ (未报/待报)   │                    └──────────────────┘
     ▲                   │                  └──────┬───────┘
     │ 已撤/部撤          │                         │ 已报
     │ 已成               │                         ▼
     │                   │                  ┌──────────────┐  部成：leaves>0，保持 LIVE
     │                   └───────────────── │     LIVE     │
     │                                      │ (已报/部成)   │
     │                                      └──────┬───────┘
     │                 min_rest 已满足（或紧急），且预算允许 │ send Cancel
     │                                             ▼
     │                                      ┌──────────────────┐   撤单失败（已成交等）
     └───────────────────────────────────── │ PENDING_CANCEL   │ ── → 等待成交 / 终态回报
                     撤单确认（股份/资金解冻）   │ (已报待撤/部成待撤)│
                                            └──────────────────┘
 改价 = LIVE → PENDING_CANCEL → (撤单确认) → EMPTY → PENDING_NEW(新价格)
 卖单必须等撤单确认、股份解冻之后才能下新卖单（否则柜台会以"可卖不足"拒单，白白浪费消息预算）
 买单可以选择"先下新单后撤旧单"（需要额外资金冻结，期间有双倍敞口），默认不使用
```

### 8.10 交易时段控制（A 股特有）

| 时段 | 时间（以交易所规则为准） | 系统行为 |
|---|---|---|
| 开盘集合竞价 | 9:15–9:25（9:20–9:25 不接受撤单） | 不报价；**9:20 之前**确认没有遗留委托 |
| 竞价后 | 9:25–9:30 | 接收开盘价，更新特征，不报价 |
| 连续竞价（上午） | 9:30–11:30 | 9:30 + N 秒（等待开盘的剧烈波动过去）开始报价 |
| 午间休市 | 11:30–13:00 | 默认在 11:29 前撤掉所有报价（午间可能出现消息，委托会持续有效到下午） |
| 连续竞价（下午） | 13:00–14:57 | 13:00 + N 秒恢复报价 |
| 收盘集合竞价 | 14:57–15:00（不接受撤单） | **14:56 前必须撤掉全部报价**，否则它们会参与收盘竞价且无法撤销；late session 窗口（如 14:50 起）只做回归底仓的一侧 |
| 盘后固定价格交易 | 科创板/创业板 15:05–15:30 | 不参与 |

时段切换由 `SessionScheduler` 驱动，切换时刻写入日志；时钟必须与交易所时间同步（PTP/NTP），并且留有安全余量。

### 8.11 行情过滤与报价撤出

| 条件 | 动作 |
|---|---|
| 停牌（含临时停牌）/ 行情中断 | 撤销该标的报价；复牌后观察 N 秒 |
| 距涨停或跌停 ≤ K 个 tick | 撤出（**不在涨跌停价挂单**，避免被困在排队中，也避免触及"涨跌停价频繁申报撤单"类的监控） |
| 价差骤然扩大、深度骤降、波动率尖峰 | 加宽、缩量或撤出（同美股版） |
| 订单簿处于过渡态（pending 主动单）或快照校验失败 | 不报价 |
| 行情延迟超过阈值 | 撤出 |
| 大额主动单连续冲击（短时间内连续上涨或下跌超过 X tick） | 撤出；**本方也不追单**，避免构成"拉抬打压" |

### 8.12 合规硬约束（在风控中执行，策略无法绕过）

1. **禁止自成交（对倒）**：自己的买价必须严格低于自己的卖价，任何订单都不得与自己的挂单成交。
2. **禁止"拉抬打压"式行为**：不发出连续推高或压低价格的主动单。主动减仓单受单笔数量、单位时间主动成交金额和价格偏离的上限约束。
3. **禁止虚假申报**：不以成交为目的的申报不允许出现。所有报价都以真实可成交为前提（价格在合理范围、数量在可承受范围），并且受最短挂单时间约束。
4. **消息预算**：每日申报+撤单 < 2 万笔（除非已按高频交易完成报告），每秒远低于 300 笔。
5. **撤单比与瞬时撤单**：受 `CancelPolicy` 约束，阈值取保守值，并且必须对照交易所细则正式文本校准。
6. **程序化交易报告**：系统上线前、策略类型或交易软件发生重大变化时，按规定通过华泰向交易所报告。

### 8.13 厚门价位的接入（形态 1 的底仓层）

| 用途 | 规则 | 默认 |
|---|---|---|
| 目标底仓 B | 价格位于起爆点之上且量能确认 → B 取上限的 x%；跌破轨 2 防守底 → 次日 B 降为 0（只卖不买，逐步清掉底仓） | 关闭（固定 B） |
| 底仓止损 | 收盘价跌破轨 2 防守底 → 次日开盘后，在合规和流动性允许的范围内减持底仓 | 关闭 |
| 日内防御 | mid 接近关键价位 → 价差 × (1+α)、数量 × (1−β)，α、β ≤ 0.5 | 关闭 |
| 风险隔离 | 单标的底仓市值 ≤ 总资金 20%（来自模型文档的上限，做市的实际限额应更低） | **启用**（作为硬上限） |

所有"关闭"项都必须先通过附录 A.5 的检验才能开启。

### 8.14 策略层面的工程风险（A 股）

| 风险 | 表现 | 对策 |
|---|---|---|
| 底仓价格风险 | 底仓随市场下跌（与做市收益无关） | 底仓规模受 §9.2 隔夜限额约束；可选：用股指期货对冲 beta（需要期货账户，属于独立的风险域） |
| 涨跌停被困 | 跌停时卖不出，涨停时买不回 | 不在涨跌停附近报价；底仓按"连续 N 个跌停"做压力测试 |
| 逆向选择 | 被大单扫过之后价格继续单边运动 | markout 监控；连续亏损自动加宽或停报 |
| 预算耗尽 | 午后无法撤单 | 节奏控制 + 紧急储备 + 预算低水位时只保留单侧 |
| 撤单确认慢 | 改价期间失去报价，或卖单冻结导致无法重新下单 | 状态机严格按"撤单确认后再下新单"执行；ACK 超时处理（§13） |
| 合规误判 | 正常的做市行为被监控规则视为异常 | 保守参数；每日合规统计复盘；保持与华泰合规部门的沟通 |

---

## 9. Risk Engine（A 股版）

### 9.1 风控分层

```
L0  Strategy sanity（策略自检）
L1  Pre-Trade Risk + Compliance Guard（分片线程内同步执行，fail-closed）            ★ 核心
L2  At-Trade / Post-Trade（成交时同步更新：T+1 持仓、资金、PnL、Kill Switch）
L3  External：华泰柜台风控（资金/持仓/限额/撤单比等）；交易所实时监控（程序化异常交易）；
             独立 Watchdog（另一路柜台会话）
L4  Human：Supervisor 控制台；华泰交易室/客户经理电话（带外撤单）
```

在 A 股，L3 中的**交易所实时监控是"被动"防线**：它的作用是发现异常并采取监管措施，而不是保护你的资金。因此本系统必须保证：**任何异常交易行为的阈值在本地 L1 中就被拦截**，而不是等交易所的警示。

### 9.2 库存风险模型（T+1 改造）

**定义**

| 符号 | 含义 |
|---|---|
| `base` (B) | 目标底仓（盘前确定） |
| `dev` | 偏离 = position − B |
| `band` | 日内允许的偏离带宽（对称） |
| `soft_limit` / `hard_limit` | \|dev\| 的告警/临界阈值，例如 band 的 50% / 80% |
| `max_net_buy_today` | 当日净买入上限：T+1 下这部分**一定会变成隔夜持仓** |
| `overnight_limit` | 隔夜持仓（底仓 + 当日净买入）市值上限 |
| inventory velocity | 单侧成交速度（§9.3 #19） |

**分区与动作**

| 区域 | 条件 | 报价动作 | 风控动作 |
|---|---|---|---|
| Normal | \|dev\| < soft | 双边报价，线性偏斜 | — |
| Warning | soft ≤ \|dev\| < hard | 加风险一侧：价差加宽、数量减半；回归一侧：偏斜加速 | 告警 |
| Critical | hard ≤ \|dev\| < band | 停止加风险一侧（单边报价） | 置单侧阻断标志 |
| Band 边界 | \|dev\| ≥ band | 只允许回归底仓的订单 | 单侧阻断 |
| 隔夜越限 | 当日净买入达到 `max_net_buy_today`，或隔夜市值达到 `overnight_limit` | **停止所有买单**；卖出昨仓以回到底仓 | 告警；**当日无法通过卖出今日买入的股份来平仓**，因此这一限额必须保守 |
| Kill | 多标的越限、日内亏损熔断、合规阈值触发 | 全撤 | Kill Strategy（锁存） |

**状态机**（与美股版结构相同，库存变量换成 dev，另外增加"隔夜越限"这一由当日净买入驱动的维度）：

```
           |dev| ≥ soft             |dev| ≥ hard              |dev| ≥ band
 NORMAL ───────────────► WARNING ───────────────► CRITICAL ───────────────► BAND_EDGE（只允许回归底仓）
   ▲  ◄─────────────────    ▲   ◄────────────────     │
   │   |dev| < soft − h         |dev| < hard − h       │
   │                                                  │
   │ 次日（新的底仓计划）               net_buy_today ≥ max_net_buy_today 或 隔夜市值 ≥ overnight_limit
   └──────────────────────────────── OVERNIGHT_CAP（禁止买入；卖出昨仓回到底仓）◄─────┘
                                            │ 多标的越限 / 亏损熔断 / 合规阈值
                                            ▼
                                     KILL STRATEGY（锁存）
```

**压力测试（盘前，每日）**：对每只标的的隔夜持仓计算"连续 2 个跌停"情景下的损失 = 市值 × (1 − 0.9²)（主板，若是 ±20% 的板块则为 1 − 0.8²）。全部标的损失之和必须 ≤ 预设的压力损失上限，否则按比例缩减底仓计划。

### 9.3 风控规则全集（A 股）

| # | 规则 | 执行时点 | 示例阈值 | 违反后动作 |
|---|---|---|---|---|
| 1 | Max Order Size（数量、金额） | 事前同步 | 单笔 ≤ 10 万元 | 拒单 |
| 2 | Max Position / Max Net Buy Today / Overnight Limit | 事前同步 | §9.2 | 拒单 |
| 3 | Max Gross / Net Exposure | 事前同步（预算切片）+ 异步汇总 | 资金的 X% | 拒加风险单 |
| 4 | Max Open Orders | 事前同步 | 每标的 2（一买一卖） | 拒新单 |
| 5 | 每秒申报+撤单 | 事前同步（滑动窗口） | ≤ 100（远低于高频认定的 300） | 拒单 |
| 6 | 每日申报+撤单 | 事前同步（`DailyMessageBudget`） | < 20,000（非高频账户），含紧急储备 | 拒新单；撤单动用储备 |
| 7 | 瞬时撤单 / 撤单比 | 事前同步（`CancelPolicy`） | 最短挂单 1 s；瞬时撤单配额；撤单比上限（以细则校准） | 延后或拒绝非紧急撤单 |
| 8 | 价格笼子 | 事前同步 | §1.1 | 拒单（避免柜台或交易所拒单浪费预算） |
| 9 | 涨跌停价 / 不在涨跌停价挂单 | 事前同步 | — | 拒单 |
| 10 | tick 网格、交易单位、零股规则 | 事前同步 | — | 拒单 |
| 11 | 不穿价（没有 Post-Only） | 事前同步 | 买价 < 卖一，卖价 > 买一 | 拒单 |
| 12 | 自成交（对倒）防护 | 事前同步 | 买价 < 自己的最低卖价，卖价 > 自己的最高买价 | 拒单 + 告警 |
| 13 | 可卖量（T+1，无融券） | 事前同步 | 卖出量 ≤ 可卖量 | 拒单 |
| 14 | 可用资金 | 事前同步 | 金额 + 预估费用 ≤ 可用资金 | 拒单 |
| 15 | Price Collar / Fat Finger | 事前同步 | 偏离参考价 ≤ max(1%, 5 tick) | 拒单 |
| 16 | Duplicate Order | 事前同步 | 本地委托号严格递增；相同内容短时间重复 | 拒单 + 告警 |
| 17 | Stale Market Data / 订单簿无效 / 通道缺口 | 事前同步 | 行情超过阈值未更新；快照校验失败；缺口 | 拒单、撤报价 |
| 18 | 交易时段 | 事前同步 | 只在连续竞价时段下单 | 拒单 |
| 19 | Inventory Velocity | 成交时同步 | 1 秒内同方向成交超过 30% × band | 撤该侧报价 + 冷却 |
| 20 | 主动成交金额（防"短时间大额成交"） | 事前同步 | 每 N 秒的主动成交金额上限 | 拒主动单 |
| 21 | Max Intraday Loss / Daily Drawdown | 成交与盯市时同步 | §11 | Kill Switch |
| 22 | Broker（柜台）断线 / 行情断线 | 网关置位，事前检查 | — | 拒新单 |
| 23 | 柜台拒单率 / 废单率 | 回报时同步 | 最近 50 笔中 > 10% | 冷却 / 全局阻断 |
| 24 | 延迟尖峰 | 回报时同步 | ACK 延迟 EWMA > 基线 × K | 加宽或撤报价 |
| 25 | 交易所监管告知（警示、问询） | 人工录入 → 控制面 | — | 立即 Kill，等待合规审查 |

### 9.4 账户级约束的切片

资金、日内亏损限额、每日消息预算都是**账户级**的，而系统分布在多个分片和两个城市：
- Coordinator 把账户级额度切分给各分片：`Σ 切片 ≤ 账户限额`。每个分片在 L1 中同步检查自己的切片，因此结果是保守的。
- 每 1–10 ms（同城）或 100 ms 级（跨城）按使用情况再平衡。
- **不得通过拆分多个账户来规避高频交易认定阈值**。这在合规上不可接受，监管也可能把关联账户合并计算。

### 9.5 事前检查实现

```cpp
[[nodiscard]] inline AVerdict
ashare_pre_trade_check(const AOrderReq& o, const ASymbolState& s, const AAccountState& a, uint64_t now) noexcept {
  const uint64_t blk = a.global_block->load(std::memory_order_acquire);
  if (o.cancel) [[unlikely]] {   // cancels: only the message budgets apply (emergency may use the reserve)
    if (!a.per_second->can_acquire(now)) return AVerdict::PerSecond;
    return a.budget->can_spend(a.session_elapsed_ns, o.emergency) ? AVerdict::Ok : AVerdict::DailyBudget;
  }
  constexpr uint64_t kHard = kBlkKill | kBlkStrategyOff | kBlkGatewayDown | kBlkMdGap | kBlkClockBad |
                             kBlkLatency | kBlkRejectRate | kBlkLogBackpressure;
  if (blk & kHard) [[unlikely]] return (blk & kBlkKill) ? AVerdict::KillSwitch : AVerdict::Blocked;
  if (a.phase != SessionPhase::Continuous) [[unlikely]] return AVerdict::SessionClosed;
  if (s.flags & (kSymHalted | kSymBookInvalid)) [[unlikely]]
    return (s.flags & kSymHalted) ? AVerdict::Halted : AVerdict::BookInvalid;
  if (uint32_t(now) - s.last_md_ts32 > a.md_stale_ns) [[unlikely]] return AVerdict::Stale;

  const SecurityRef& r = *s.ref;
  const bool buy = o.side == Side::Buy;
  if (!on_tick(r, o.px)) [[unlikely]] return AVerdict::OffTick;
  if (!price_limit_ok(r, o.px)) [[unlikely]] return AVerdict::PriceLimit;
  if (r.limit_up && !s.allow_quote_at_limit && (o.px == r.limit_up || o.px == r.limit_down)) [[unlikely]]
    return AVerdict::AtLimitPrice;
  if (!price_cage_ok(r, o.side, o.px, buy ? s.best_ask : s.best_bid)) [[unlikely]] return AVerdict::PriceCage;
  if (!lot_ok(r, o.side, o.qty, s.pos.sellable())) [[unlikely]] return AVerdict::BadLot;
  if (o.passive && (buy ? (s.best_ask && o.px >= s.best_ask) : (s.best_bid && o.px <= s.best_bid))) [[unlikely]]
    return AVerdict::WouldCross;                                    // A-shares have no post-only order type
  if (buy ? (s.own_best_ask && o.px >= s.own_best_ask) : (s.own_best_bid && o.px <= s.own_best_bid)) [[unlikely]]
    return AVerdict::SelfTrade;                                     // never trade against ourselves (对倒)

  if (!buy) {
    if (o.qty > s.pos.sellable()) [[unlikely]] return AVerdict::InsufficientSellable;  // T+1, no short
  } else {
    const int64_t notional = o.px * o.qty;
    if (notional + fee_for_fill(a.fees, r, Side::Buy, notional) > a.cash.available) [[unlikely]]
      return AVerdict::InsufficientCash;
    if (s.pos.position() + s.pos.open_buy + o.qty > s.max_position) [[unlikely]] return AVerdict::MaxPosition;
    if (!s.pos.t0 && s.pos.net_bought_today() + s.pos.open_buy + o.qty > s.max_net_buy_today) [[unlikely]]
      return AVerdict::OvernightCap;
    if (blk & kBlkReduceOnly) [[unlikely]] return AVerdict::ReduceOnly;   // buys add risk (no shorts exist)
    if (a.net_pnl <= -a.daily_loss_limit) [[unlikely]] return AVerdict::DailyLoss;
  }
  if (!a.per_second->can_acquire(now)) [[unlikely]] return AVerdict::PerSecond;
  if (!a.budget->can_spend(a.session_elapsed_ns, false)) [[unlikely]] return AVerdict::DailyBudget;
  return AVerdict::Ok;
}
```

**与美股版的关键差异**：
1. **ReduceOnly 只拦截买单**：A 股中不存在空头（不能融券做日内回转），卖出永远是降低风险的方向，所以只阻断买入。
2. **可卖量与资金检查放进本地**：柜台也会检查，但被柜台拒单意味着白白消耗了一条消息预算，并且多了一次往返。
3. **预算检查放在最后**，并且只"窥视"不扣减，发送成功后才扣减（与美股版的令牌提交模式相同）。
4. 撤单只受预算约束；`emergency = true` 的撤单（风险撤单、Kill 撤单）可以动用紧急储备。

**单元测试（全部通过）【M-VM】**：正常买卖、穿价、tick 网格、跌停价挂单、交易单位、自成交、T+1 可卖不足、当日净买入上限、资金不足、收盘集合竞价时段、ReduceOnly 拦截买单但放行卖单、Kill 状态拒绝新单但放行紧急撤单。正常通过路径 P50 约 42–45 ns、P99 约 46–48 ns【M-VM】。

---

## 10. Order Throttling（A 股版：监管预算优先）

### 10.1 三层限速

| 层 | 目的 | 实现 | 默认值 |
|---|---|---|---|
| 每秒 | 远离"每秒 300 笔"的高频认定线和"瞬时申报速率异常"监控 | `SlidingWindowLimiter<100>`（精确：任意 1 秒内 ≤ 100 笔） | 100 |
| 每日 | 低于"每日 20,000 笔"的高频认定线 | `DailyMessageBudget`：按连续交易时间线性释放额度，留紧急储备 | 上限 18,000，储备 1,000，初始突发 500 |
| 平滑 | 防止局部突发（与券商流控匹配） | GCRA（美股版 §10.3） | 按华泰流控参数的 80% 设定 |

此外还有 `CancelPolicy`（最短挂单时间、瞬时撤单配额、撤单比上限），它限制的是撤单的**时机**，而不仅是数量。

### 10.2 每秒限速：滑动窗口

代码与美股版 §10.4 相同：保存最近 N 次被接受请求的时间戳，只有当最老的一条已经超出 1 秒窗口时才放行下一条。O(1)，N = 100 时内存为 800 字节，没有锁，也没有 sleep。

### 10.3 每日预算：按交易时间释放

```cpp
class DailyMessageBudget {
 public:
  DailyMessageBudget(uint32_t cap, uint32_t emergency_reserve, uint32_t burst, uint64_t session_ns) noexcept
      : cap_(cap), reserve_(emergency_reserve), burst_(burst), session_ns_(session_ns) {}
  // elapsed_ns: continuous-trading time elapsed today (lunch break excluded)
  uint32_t allowance(uint64_t elapsed_ns) const noexcept {
    const uint64_t pace = uint64_t(cap_ - reserve_ - burst_) * std::min(elapsed_ns, session_ns_) / session_ns_;
    return burst_ + uint32_t(pace);
  }
  bool can_spend(uint64_t elapsed_ns, bool emergency) const noexcept {
    if (emergency) return used_ < cap_;             // cancels during kill / risk reduction may use the reserve
    return used_ < allowance(elapsed_ns);
  }
  void spend() noexcept { ++used_; }
  uint32_t used() const noexcept { return used_; }
 private:
  uint32_t cap_, reserve_, burst_;
  uint64_t session_ns_;
  uint32_t used_ = 0;
};
```

**额度释放算法**：允许使用量 = 初始突发额度 + (上限 − 储备 − 突发) × 已过连续交易时间 / 连续交易总时长（9:30–11:30 加 13:00–14:57，共 14,220 秒，午休不计）。例如上限 18,000、储备 1,000、突发 500 时：开盘时可用 500，交易时间过半时可用 8,750，收盘时可用 17,000；最后 1,000 只留给紧急撤单【M-VM：单元测试验证】。没用完的额度会自动累积到后面使用。如果改用按时段加权的释放曲线（开盘和尾盘多分配一些），需要用历史成交量曲线来标定。

**影子价格**：策略在决定是否改价时，用"剩余额度 / 剩余时间"计算一条消息的影子价格。额度越紧，改价门槛越高。

### 10.4 撤单策略：避免"频繁瞬时撤单"

```cpp
class CancelPolicy {
 public:
  enum class Decision : uint8_t { Allow, Defer, Deny };
  Decision check(uint64_t now, uint64_t entered_ts, bool emergency) const noexcept {
    if (emergency) return Decision::Allow;                       // risk reduction always wins
    if (now - entered_ts < min_rest_ns_) return instant_ < max_instant_ ? Decision::Allow : Decision::Defer;
    if (orders_ >= min_sample_ && uint64_t(cancels_ + 1) * 100 > uint64_t(ratio_pct_) * orders_) return Decision::Deny;
    return Decision::Allow;
  }
  void on_order() noexcept { ++orders_; }
  void on_cancel(uint64_t now, uint64_t entered_ts) noexcept { ++cancels_; if (now - entered_ts < min_rest_ns_) ++instant_; }
  // ...
};
```

- 交易所细则对"频繁瞬时撤单"的描述是：全日多次出现"1 秒内申报又撤单"，并且全日撤单比例达到一定标准【REG】。具体的次数和比例阈值**我没有在公开报道中找到**，必须查阅细则正文后再设定。默认值取保守值：`min_rest = 1 s`、每日瞬时撤单配额很小、撤单比上限（例如 60%，待校准）。
- **紧急撤单（风险驱动）永远放行**：合规风险可以事后解释，资金风险不行。这类撤单单独计数，并在日终的合规报告中逐笔列明原因。
- `Defer`：撤单进入定时轮，在最短挂单时间满足后自动发出（前提是届时仍需要撤）。

### 10.5 优先级

额度紧张时的分配顺序：**紧急撤单 > 回归底仓的减仓单 > 防御性撤单 > 新报价 > 进攻性改价**。被限速的新单不排队，等下一次 reconcile 时基于最新行情重新决策。

---

## 11. Daily Loss Kill Switch（A 股版）

### 11.1 PnL 定义

$$
\text{NetPnL} = \text{Realized} + \text{Unrealized} - \text{Fees} + \text{Rebates}
$$

| 组成 | A 股口径 |
|---|---|
| Realized | 平均成本法。由于底仓的存在，"已实现"按**当日回转部分**计算：当日卖出量与当日买入量中较小的部分，按当日的买卖均价差计算；底仓本身的价格变动计入 Unrealized |
| Unrealized | 全部持仓（底仓 + 当日净买入）相对**昨收盘价**（底仓部分）或**买入成本**（当日买入部分）的盯市损益。保守地以买一价估值；如果处于跌停排队中，按跌停价估值并加上流动性折扣 |
| Fees | 佣金 + 印花税（卖出，股票）+ 过户费 + 经手费/证管费（若单独收取）；成交时估算，日终核对 |
| Rebates | 普通投资者在 A 股**没有做市返佣**，取 0 |

**注意**：Kill Switch 关心的是**当日总损失**，所以底仓的盯市亏损也必须计入。它与做市本身无关，却是这一形态固有的风险。日内亏损限额要同时覆盖"做市亏损 + 底仓波动"，或者把两者分开设限：底仓的风险由隔夜限额和压力测试来管理，日内限额只针对回转部分。本文推荐分开设限，两个维度各自触发 Kill。

### 11.2 状态机

与美股版 §11.2 相同：ARMED → WARNING（60%）→ REDUCE_ONLY（85%，A 股中等于"禁止买入"）→ TRIPPED（100%）→ LOCKED。日内不自动降级；只有 Supervisor 能把 LOCKED 复位（已认证 + 持仓已与柜台对账 + 行情健康）；状态持久化，重启后仍然锁定。`KillSwitch` 类直接复用（`reference/risk.hpp`，已测试）。

### 11.3 触发后的动作（A 股差异）

| 步骤 | 动作 | A 股注意事项 |
|---|---|---|
| 1 | Reject New Orders | `global_block \|= kBlkKill`，同步完成 |
| 2 | Cancel All Orders | 使用华泰的批量撤单接口（如果有），否则逐笔撤单；撤单动用紧急预算；**收盘集合竞价期间（14:57–15:00）无法撤单**，因此 late session 必须提前撤完 |
| 3 | Disable Strategy | 同步完成 |
| 4 | Trigger Kill Switch（全局传播） | 另一城市的分片、Coordinator、Watchdog；Watchdog 通过独立柜台会话再次全撤 |
| 5 | Persist Risk Event | fsync 落盘后才进入 LOCKED |
| 6 | Notify Monitoring | 最高级别告警 |
| 7（A 股新增） | **生成次日处置计划** | T+1 下当日买入的股份无法当日卖出，Kill 之后剩余的超额持仓只能在**次日**处理。系统生成"次日开盘后的减仓计划"（数量、价格保护、时间窗口），由人工确认后执行 |
| 8（可选） | 对冲 | 如果开通了股指期货账户，可以用期货卖出对冲底仓的 beta 风险。这属于独立的风险域，需要单独的风控和授权，不在本系统默认范围内 |

### 11.4 "Strategy 不能自行复位"的保证

与美股版 §11.5 相同：对象所有权隔离（策略只拿到只读视图）、控制通道认证（Supervisor 签发的令牌）、持久化锁存（重启不能绕过）、外部 Watchdog 冗余、复位条件由代码强制检查。A 股另加一条：**复位前必须完成与华泰柜台的持仓和资金对账**，因为 T+1 下的持仓状态（昨仓、今买、可卖）比美股更复杂。

---

## 12. Critical Path：L2 Tick → Order

### 12.1 逐步分析（Architecture B，以深交所为例）

| # | 步骤 | 线程 | 发生了什么 | 估计耗时 |
|---|---|---|---|---|
| 0 | 包到达网卡 | NIC | 内网 L2 组播；硬件时间戳 T0 | — |
| 1 | 轮询收包 | MD 核 | 内核旁路 RX；T1 | ~1 μs【E】 |
| 2 | A/B 仲裁 + 按通道排序 | MD 核 | 每个 ChannelNo 有一个 expected_seq；重复丢弃，缺口进入恢复流程 | < 20 ns |
| 3 | 解码 | MD 核 | Binary 消息 → `L2Event`（逐笔委托 / 逐笔成交 / 撤单记录） | 20–50 ns |
| 4 | 路由 | MD 核 | 证券过滤 + 通道 → 分片；SPSC（T2） | 10–20 ns |
| 5 | 跨核 | — | 缓存行迁移 | 80–150 ns |
| 6 | 订单簿 | 分片核 | `AshareBookBuilder::apply`：pending 主动单的处理、成交扣减、撤单、价位与位图（T4） | 100–200 ns |
| 7 | 状态 | 分片核 | 时段、停牌、涨跌停距离、价格笼子基准、行情陈旧 | 10–30 ns |
| 8 | 标脏 | 分片核 | TOB 或成交变化 → dirty；pending 存在 → 该标的处于"过渡态" | < 5 ns |
| 9 | 特征 | 分片核 | 批末对脏标的增量计算 | 50–200 ns |
| 10 | 定价 | 分片核 | `compute_quotes`（成本下限）+ `t0_quote_limits`（T+1 约束）（T5） | 80–150 ns |
| 11 | Reconcile | 分片核 | 期望 vs 实际；改价成本 = 2 条消息；min_rest；预算影子价格 → 0–1 个动作（撤单或新单） | 20–50 ns |
| 12 | 风控 + 合规 | 分片核 | `ashare_pre_trade_check` + `CancelPolicy`（T6） | 40–60 ns |
| 13 | OMS | 分片核 | 本地委托号、状态 → PendingNew / PendingCancel；冻结可卖量或资金（本地镜像） | 20–50 ns |
| 14 | 柜台 API | 分片核 / SDK 线程 | 填充 SDK 请求结构并调用发送（T7）；若 SDK 线程独立，再加一跳 SPSC | 0.5–3 μs【BD】 |
| 15 | 发送 | NIC | T8 | 1–2 μs【E】 |
| 16 | 提交 | 分片核 | 扣减每秒和每日预算；`CancelPolicy::on_order/on_cancel` | 10–20 ns |
| 17 | 柜台 → 交易网关 → 交易所 | 外部 | 柜台风控、报盘、撮合 | 【BD】【XD】 |
| 18 | 回报 | 分片核 | 委托确认 → 已报 → 成交 / 撤单确认；OMS、T+1、资金、PnL、Kill 同步更新 | 100–300 ns |

T1→T8 估计为 P50 2–5 μs【E】，其中柜台 SDK 调用是最大的不确定项【BD】。

### 12.2 Architecture A 的差异

步骤 0–3 变为"华泰行情 SDK 回调 → 拷贝 → 入队"（行情本身已经经过了互联网分发，带有毫秒级延迟）；步骤 14 变为"进程内 SDK 调用"或"桥接协议 → Windows 桥接机 → SDK"；步骤 17 是互联网/专线加柜台处理，毫秒级【BD】。内部链路仍然满足 P50 < 300 μs、P99 < 1 ms，但端到端由外部主导。

### 12.3 关键流程时序图

**(1) Market Data Flow**
```
交易所 L2 ─► 华泰内网组播 / INSIGHT ─► NIC/SDK ─► MD Gateway ─► 通道 Sequencer ─► Decoder ─► SPSC
  ─► Shard: AshareBookBuilder (pending aggressor) ─► Snapshot cross-check (每 3 s)
  ─► Market State ─► Features ─► Strategy (形态1/2) ─► desired quotes
```

**(2) Order Flow**
```
desired quote ─► Reconciler（预算影子价格、min_rest、单槽单在途）─► OrderRequest
  ─► ashare_pre_trade_check ──reject──► log + 计数
        │ ok
        ▼
  OMS（本地委托号、冻结镜像、PendingNew）─► HuataiGateway ─► 华泰柜台（资金/持仓冻结、柜台风控）
  ─► 华泰交易网关 ─► 交易所 ─► 预算提交（每秒 / 每日）
```

**(3) Execution Report Flow**
```
柜台推送：委托确认(已报/柜台拒单) │ 成交回报 │ 撤单回报 │ 废单
  ─► 归一化 ExecutionReport ─► 去重（柜台委托编号 + 成交编号）─► OMS 状态机
  ─► T1Position（今买/今卖/冻结）─► Cash（冻结→扣减/解冻）─► PnL（费用含印花税）
  ─► 库存分区 / 隔夜上限 ─► KillSwitch.on_pnl ─► Strategy dirty ─► Logger（关键记录）
```

**(4) Cancel / New（改价）Flow**
```
新期望价 ─► Reconciler：|Δpx| ≥ 1 tick？收益 > 2 × 消息影子价格？挂单 ≥ min_rest（或紧急）？
  ─► Cancel(旧单) ─► LIVE → PENDING_CANCEL
柜台/交易所 ─► 撤单确认（股份或资金解冻）─► EMPTY ─► 与最新期望比较 ─► New(新价) ─► PENDING_NEW
         ─► 撤单失败（已全部成交）─► 等待成交回报 ─► Filled
         ─► 撤单途中成交 ─► 先计入成交，再处理撤单确认中的剩余量（部撤）
```

**(5) Partial Fill Flow**
```
成交回报（部分）─► 去重 ─► OMS: leaves -= qty（部成，仍为 LIVE）
  ─► T1Position.bought_today/sold_today ─► frozen_sell / open_buy 同步减少 ─► Cash ─► PnL
  ─► dev / 隔夜上限检查（可能触发单侧阻断）─► Reconciler：剩余部分保持不动（保留排队位置）
```

**(6) Risk / Compliance Event Flow**
```
触发：预算低水位 / 瞬时撤单配额用尽 / 拒单率 / 行情陈旧 / 通道缺口 / 快照不一致 / 库存越限 / 亏损
  ─► 所在线程同步置位（global_block 或 标的 flags）─► 下一次 pre_trade_check 生效
  ─► 策略撤出 / 单边 / 加宽 ─► Logger（关键记录）─► 告警 ─► 合规日报中列示
```

**(7) Kill Switch Flow**
```
PnL ≤ −限额 ─► TRIPPED ─► (1) kBlkKill ─► (2) 全撤（批量撤单 / 逐笔，紧急预算）─► (3) 停用策略
  ─► (4) 传播：另一城市、Coordinator、Watchdog（独立会话再次全撤）─► (5) fsync ─► LOCKED
  ─► (6) 告警 ─► (7) 生成次日处置计划（T+1 下今日买入的部分当日无法卖出）
```

**(8) Recovery Flow**
```
启动/重连 ─► SAFE 模式 ─► 读取当日风控事件（Kill 是否已锁存）─► 柜台登录
  ─► 查询柜台：委托、成交、持仓（昨仓/可卖）、资金 ─► 与本地日志对账（以柜台为准，差异告警）
  ─► 撤销所有未知委托 ─► 行情：订阅 + 等待 3 s 快照 + 逐笔重放（如果可用）─► 订单簿校验
  ─► 恢复当日已用的消息预算计数（从日志或柜台统计中恢复，**不能归零**）
  ─► Supervisor 确认 ─► REDUCE_ONLY 观察 ─► ARMED
```

---

## 13. Failure Handling（A 股版）

| 故障 | 检测 | 立即动作 | 恢复 | 升级 |
|---|---|---|---|---|
| **逐笔通道序号缺口** | 某个 ChannelNo 的序号跳跃 | **只有该通道的标的**置为 BookInvalid 并撤报价（这一点优于 ITCH 的全市场缺口）；缓存后续消息 | 另一条线路补齐 → 继续；行情源的重传/回放服务 → 补齐；否则以快照作为近似并进入观察期（§7.4） | 长时间无法恢复 → 该通道当日停止报价 |
| **UDP 丢包 / 乱序** | 同上 / 序号小幅乱序 | 小窗口重排缓冲 | 同上 | — |
| **行情延迟** | Tx 与本地时钟之差超过阈值（毫秒级精度）；MD 队列积压 | 加宽或撤出 | 恢复后观察 N 秒 | 持续 → 停止 |
| **行情停止** | 通道心跳超时；快照停止 | 该通道撤报价 | 恢复后校验 | 超时 → Kill |
| **快照校验失败** | 连续 2 次不一致 | 该标的 BookInvalid | 重建（§7.4） | 多标的失败 → 停止 |
| **华泰 API / 柜台断线** | SDK 断线回调、心跳超时、桥接机失联 | 置 `kBlkGatewayDown`；所有委托标记为 Unknown；**断线期间挂单仍在交易所**：Watchdog 通过独立会话全撤；同时准备人工通道（华泰客户端/APP、交易室电话） | 重连 → 查询委托/成交/持仓/资金 → 对账 → 撤销未知委托 → 人工确认后恢复 | 无法确认撤单 → 人工介入 |
| **柜台拒单** | 资金不足、可卖不足、超出柜台限额等 | 槽位 → EMPTY + 冷却；**本地镜像与柜台不一致**通常意味着对账问题 → 触发一次查询对账 | 冷却结束 | 拒单率熔断 |
| **交易所废单** | 价格超出涨跌停、价格笼子、非交易时段、停牌、交易单位错误等 | 同上；按原因分类计数（这类废单说明本地检查有漏洞，必须修复） | — | 废单率熔断 |
| **委托确认超时** | PendingNew 超过 T_ack | 该委托标记 Unknown；该标的停止新动作；查询委托状态 | 收到迟到的确认或查询结果 → 归位 | 多笔 Unknown → 全局阻断 |
| **撤单确认超时** | PendingCancel 超过阈值 | 不重复发送撤单（浪费预算）；查询状态 | 同上 | — |
| **部分成交** | 成交回报中 leaves > 0 | 正常业务：更新 T+1、资金、PnL | — | — |
| **重复成交回报** | （柜台委托编号 + 成交编号）已处理过 | 丢弃 + 计数，**绝不重复计入持仓** | 定期与柜台成交查询对账 | 不一致 → 停止该标的 |
| **策略异常** | 不变量检查（NaN、价格越界、买价 ≥ 卖价、数量越界） | L1 兜底拒单；该标的停用 | 人工 | 多标的 → 全局停用 |
| **策略失控** | 改价频率异常、预算消耗速度异常、价格振荡、库存速度超标 | 该标的停用；全局预算低水位 → 只撤不报 | 回放复现后由人工启用 | Watchdog 带外 Kill |
| **风控失效** | 状态无效（参考数据未加载、持仓未对账） | Fail-closed：只允许撤单 | 修复后重启 | 直接 Kill |
| **交易所监管告知 / 异常交易警示** | 华泰合规部门或交易所通知（人工录入） | 立即 Kill，暂停程序化交易 | 合规审查完成后由 Supervisor 恢复 | — |
| **预算耗尽** | 每日已用量接近上限 | 只允许紧急撤单 | 次日 | — |
| **NIC / 桥接机故障** | 链路断开、桥接心跳 | 同柜台断线 | 切换备机 → 对账 | 双故障 → Kill |
| **CPU 过载** | 队列积压、循环延迟 | 撤报价、降级非关键工作 | 恢复 | 持续 → 停止 |
| **时钟偏差** | NTP/PTP 偏移超过阈值 | **时段控制依赖时钟**（14:57 等关键时刻）：偏移超过安全余量时提前撤出所有报价 | 时钟恢复 | — |
| **系统重启** | 启动 | SAFE 模式；从日志恢复**当日已用消息预算**和 Kill 状态 | §12.3 (8) | 当日已触发 Kill → 以 LOCKED 启动 |

---

## 14. Performance Optimization

### 14.1 与美股版相同的部分

P0/P1/P2 的分级、Linux/BIOS 配置清单、编译选项、"什么时候停止优化"的原则，与美股版 §14.1–§14.5 完全相同（零分配、对象池、缓存行对齐、SPSC、绑核、TSC、isolcpus/nohz_full、HugePages、内核旁路等）。

### 14.2 A 股特有的取舍

| 项 | Arch A | Arch B | 说明 |
|---|---|---|---|
| L2 解码优先使用 Binary 而非 STEP | P1 | **P0** | STEP 是 FIX 风格的文本 tag=value 格式，解析成本比定长二进制高一个数量级；如果华泰或行情商提供 Binary/结构体形式，优先使用 |
| 柜台 SDK 线程模型适配 | **P0** | **P0** | SDK 回调中只做拷贝和入队；评估 SDK 是否在热路径上有分配或锁（作为黑盒测量它的调用延迟分布） |
| Windows 桥接 | **P0**（如果只有 Windows SDK） | ❌（托管机房应争取 Linux SDK） | 桥接机使用物理机，绑核，关闭电源管理，与交易机直连 |
| 涨跌停价梯 | **P0** | **P0** | 消除溢出和重定窗口的复杂性 |
| 硬件（FPGA）行情解码 | ❌ | P2 | A 股托管机房有成熟的商用方案，但在每日 2 万笔的预算下，收益非常有限 |
| 内核旁路 | ❌ | P0/P1 | 取决于 SDK 是否兼容（例如 Onload 透明加速 socket） |

**关键判断**：在 A 股，非高频账户的交易频率被监管限制在平均每秒约 1 笔的量级。因此，**订单路径上的纳秒级优化价值有限**；行情路径（订单簿重建的正确性和时效性）与决策质量（花掉每一条消息的时机）更重要。B 架构的价值主要在于更早看到行情、更快撤掉危险报价，而不是发更多的单。

### 14.3 日志、监控与合规记录

架构与美股版 §14.6 相同（每线程 SPSC → 异步 Logger → 二进制日志；热路径只写 POD 记录和原始 TSC；Correlation ID 贯穿全链路；关键记录不可丢，环满即停止新单）。A 股额外要求：

| 记录 / 统计 | 用途 |
|---|---|
| 每笔申报、撤单的时间戳（精确到毫秒以上）、挂单时长、撤单原因（策略/风险/紧急） | 程序化交易合规统计；"瞬时撤单"自查 |
| 每秒、每日的申报+撤单计数，并与高频认定阈值比较 | 合规日报；接近阈值时告警 |
| 撤单比、瞬时撤单次数、主动成交金额 | 异常交易行为自查 |
| 柜台拒单与交易所废单的分类计数 | 本地检查漏洞的发现 |
| 程序化交易报告中申报的信息与实际运行配置（交易软件版本、策略类型、服务器位置）的一致性 | 报告变更管理 |

监控看板增加"合规"页：预算使用曲线（实际 vs 释放额度）、瞬时撤单计数、撤单比、距离高频认定阈值的余量。

---

## 15. Benchmark Plan

### 15.1 Replay 与确定性测试

与美股版 §15.1 相同的确定性要求（单一输入流、注入时钟、禁止依赖迭代顺序、固定浮点选项、整数价格、固定种子、配置版本化）。A 股的数据源：
- **历史 L2 逐笔数据**：深交所、上交所的逐笔委托/成交（或逐笔合并）以及 3 秒快照，从授权的数据商获取。注意数据授权条款是否允许用于回测和研发。
- **录制**：B 中在托管机房抓取原始组播包（带硬件时间戳）；A 中记录 SDK 回调的原始结构和本地时间戳。
- **回放模式**：MD Replay（订单簿 vs 快照）、Strategy Replay（加上当时的柜台回报，复现实盘那一天）、Risk/Compliance Replay（同一订单流在新规则或新参数下的判定结果）、Order Simulation（A 股撮合模拟器）。

**A 股撮合模拟器的要点**：价格优先、时间优先；T+1 冻结与可卖量；资金冻结；撤单需要确认（带延迟）；废单规则（涨跌停、价格笼子、交易单位、时段）；集合竞价时段不可撤单；虚拟订单的队列位置按"下单时刻的价位排队量"初始化，并随成交向前推进。

### 15.2 测试层级与指标

| 层级 | 对象 | 指标 |
|---|---|---|
| L0 微基准 | L2 解码、`AshareBookBuilder::apply`、`ashare_pre_trade_check`、预算与撤单策略、`compute_quotes` | P50/P90/P99/P99.9/Max（ns） |
| L1 组件 | 全天 L2 回放：订单簿更新吞吐、快照一致性比例 | msgs/s；不一致次数 |
| L2 进程内管线 | 回放 + 各阶段 TSC 打点 | T1→T7 分布 |
| L3 线上到线上（B） | 包回放 + 抓包 | T0→T8 分布 |
| L4 外部（A/B） | 华泰仿真环境 / 极小资金实盘 | T8→T9（柜台确认）、T9→T10（交易所确认）、撤单确认延迟，按时段分桶 |
| **L5 合规仿真** | 用全天回放驱动完整策略 | **每日消息总数、每秒峰值、瞬时撤单次数、撤单比**，必须全部低于设定值 |
| **L6 经济性回测** | 撮合模拟 + 完整费用模型 | 每笔回转的净收益分布、markout、底仓盯市波动、预算的边际价值 |

方法要求（不报告平均值、避免协同遗漏、单独测量计时开销、冷热缓存分开测试、记录环境信息）与美股版 §15.3 相同。

### 15.3 验收标准（示例，B 架构目标硬件）

| 指标 | 目标 |
|---|---|
| `AshareBookBuilder::apply` | P50 ≤ 150 ns，P99 ≤ 500 ns |
| `ashare_pre_trade_check` | P50 ≤ 60 ns，P99 ≤ 120 ns |
| T1→T7（不含柜台 SDK） | P50 ≤ 2 μs，P99 ≤ 10 μs |
| 订单簿 vs 快照 | 连续交易时段零持续性不一致 |
| 合规仿真 | 每日消息 < 设定上限；每秒峰值 < 设定值；瞬时撤单 ≤ 配额 |
| A 架构内部链路 | T1→T8 P50 < 300 μs，P99 < 1 ms |

### 15.4 开发 VM 上的参考测量【M-VM】

`test_ashare.cpp` 的测量结果（4 vCPU 共享云 VM，未调优，g++ 13.3 与 clang++ 18 共 3 次运行取区间，仅供参考量级）：

| 测试 | n | P50 | P90 | P99 | P99.9 | Max |
|---|---|---|---|---|---|---|
| `AshareBookBuilder::apply`（深+沪语义混合，78.4 万条消息） | 783,831 | 153–159 ns | 241–267 ns | 432–462 ns | 561–650 ns | 32–46 μs |
| `ashare_pre_trade_check`（通过路径） | 1,000,000 | 42–45 ns | 45–47 ns | 46–48 ns | 50–61 ns | 54–229 μs |

（以上数字都包含约 22–24 ns 的 rdtsc 计时开销。）

---

## 16. 部署拓扑

### 16.1 Architecture A：华泰 MATIC 远程接入

```
┌──────────────────────── 自有机房 / IDC 机柜（建议选在上海或深圳，靠近华泰接入点）─────────────────┐
│                                                                                                  │
│  ┌─────────────────────────────┐   10GbE 直连    ┌──────────────────────────────┐                 │
│  │ Linux 交易主机（8+ 核，物理机）│ ◄────────────► │ Windows 桥接机（物理机，可选）  │                 │
│  │ MD Source / Shard / Gateway  │  定长二进制协议  │ 华泰 MATIC / MAPIs SDK         │                 │
│  │ Logger / Telemetry / Control │                 │ 只做协议转换                   │                 │
│  └──────────────┬──────────────┘                 └───────────────┬──────────────┘                 │
│                 │ 华泰行情 SDK（INSIGHT / MATIC 行情）                │ 柜台 API                        │
└─────────────────┼────────────────────────────────────────────────┼────────────────────────────────┘
                  │ 互联网 / 专线【BD】                               │ 互联网 / 专线【BD】
                  ▼                                                  ▼
            华泰行情服务                                   华泰柜台 ─► 华泰交易网关 ─► 上交所 / 深交所

┌──────────── 运维区（独立主机）────────────┐
│ 监控告警 │ 日志归档 │ 合规日报 │ 配置仓库   │
│ 独立 Watchdog（另一路柜台会话：带外全撤）   │
│ 运维控制台（VPN + MFA）                    │
└───────────────────────────────────────────┘
```

要点：
- 账户：在华泰开通相应的机构/专业投资者账户和 MATIC 权限，**完成程序化交易报告后才能上线**【REG】【BD】。
- 如果华泰提供 Linux SDK，就去掉桥接机，在交易主机的进程内运行 SDK 线程。
- 至少准备一条与主交易会话无关的带外撤单通道（独立会话的 Watchdog + 人工渠道）。

### 16.2 Architecture B：华泰托管机房（沪、深两地）

```
            上海（沪市标的）                                         深圳（深市标的）
┌──────────────────────────────────────────┐            ┌──────────────────────────────────────────┐
│ 华泰托管机房（与上交所同城）                │            │ 华泰托管机房（与深交所同城）                │
│                                          │            │                                          │
│  内网 L2 组播（A/B）──┐                    │            │  内网 L2 组播（A/B）──┐                    │
│                       ▼                   │            │                       ▼                   │
│  ┌────────────────────────────────┐       │            │  ┌────────────────────────────────┐       │
│  │ Trading Server SH               │       │            │  │ Trading Server SZ               │       │
│  │ NIC0: L2 组播（内核旁路）         │       │            │  │ NIC0: L2 组播                    │       │
│  │ NIC1: 华泰极速柜台 API ───────────┼──► 柜台 ─► 交易网关 ─► 上交所  │ NIC1: 柜台 API ─► 柜台 ─► 深交所 │
│  │ 本地风控 + Kill + 预算切片        │       │            │  │ 本地风控 + Kill + 预算切片        │       │
│  └──────────────┬─────────────────┘       │            │  └──────────────┬─────────────────┘       │
│  备机（热备）     │ PTP/NTP、带外管理         │            │  备机（热备）     │                         │
└─────────────────┼────────────────────────┘            └─────────────────┼────────────────────────┘
                  │            跨城专线（控制面 + 汇总；不在热路径上）       │
                  └────────────────────────┬─────────────────────────────┘
                                           ▼
             运维/合规中心：Coordinator（账户级预算切片）、监控、合规日报、历史 L2 存储、回放集群
```

要点：
- 托管资源、内网行情、极速柜台都需要向华泰申请，费用和可用产品以华泰的报价为准【BD】。
- 如果会达到高频交易认定标准，就必须按高频交易报告（包括服务器所在地、测试报告、应急方案）【REG】。
- 两地各自独立运行风控和 Kill Switch；跨城链路中断时，两地按各自持有的预算切片继续运行（保守），或者按配置降级为只撤不报。
- 发布在盘后进行，先用当日 L2 回放验证，再切换。

### 16.3 环境矩阵

| 环境 | 行情 | 执行 | 资金 | 目的 |
|---|---|---|---|---|
| DEV | 历史 L2 | SimAshareGateway | 无 | 开发、单元测试 |
| SIM | 历史 L2 回放 | 撮合模拟 + 故障注入 | 无 | 回测、合规仿真、故障演练 |
| UAT | 实时 | 华泰仿真/测试环境（若提供） | 模拟 | 柜台联调、回报映射验证 |
| PROD-A | 实时（互联网 L2） | 华泰 MATIC | 小额 | 小资金实盘 |
| PROD-B | 内网 L2 | 华泰极速柜台 | 生产 | 低延迟实盘 |

---

## 17. 工程目录

在美股版 §17 的目录基础上，增加 A 股专属模块（`core/`、`logging/`、`monitoring/`、`replay/` 等通用部分不变）：

```
trading-system/
├── core/ ... logging/ ... monitoring/ ... replay/ ...     # 与美股版相同（通用内核）
│
├── ashare/
│   ├── refdata/                 # 每日证券信息、涨跌停价、tick/交易单位、T+0 标识、停牌；校验
│   ├── session/                 # 交易时段调度：集合竞价、午休、14:57 收盘竞价、时钟安全余量
│   ├── l2/
│   │   ├── szse_binary/         # 深交所 Binary 行情解码（逐笔委托 / 逐笔成交 / 快照）
│   │   ├── szse_step/           # 深交所 STEP（备用）
│   │   ├── sse_is120/           # 上交所 IS120 行情（STEP/Binary，逐笔合并 / 快照）
│   │   ├── huatai_sdk/          # 华泰行情 SDK 适配（INSIGHT / MATIC 行情）→ L2Event
│   │   └── sequencer/           # 按通道的序号检查、重排、缺口恢复
│   ├── orderbook/               # AshareBookBuilder、涨跌停价梯、三级位图、快照校验
│   ├── inventory/               # T1Position、底仓 T+0 约束（t0_quote_limits）、隔夜上限、压力测试
│   ├── cash/                    # 资金冻结镜像、对账
│   ├── fees/                    # 费率表（版本化）、fee_for_fill、break-even 工具
│   ├── compliance/              # DailyMessageBudget、每秒限速、CancelPolicy、自成交防护、
│   │                            # 主动成交金额上限、合规统计与日报
│   ├── risk/                    # ashare_pre_trade_check、规则配置
│   └── strategy/
│       ├── base_t0/             # 形态 1：股票底仓 T+0
│       ├── t0_instruments/      # 形态 2：可转债、T+0 ETF
│       └── base_planner/        # 底仓规划（固定规则 / 厚门 LevelContext，可选）
├── broker/
│   ├── huatai/
│   │   ├── gateway/             # HuataiGateway：委托号映射、回报归一化、去重、状态映射
│   │   ├── sdk_adapter/         # 隔离华泰 SDK 的唯一目录（异常边界、回调线程 → SPSC）
│   │   └── bridge/              # Linux ↔ Windows 桥接协议（若仅有 Windows SDK）+ Windows 侧服务
│   └── sim_ashare/              # A 股撮合模拟器、柜台模拟（冻结、废单、延迟、断线）
├── research/
│   ├── level_context_batch/     # 厚门价位计算 + 显著性检验（A 股日线）
│   └── economics/               # 成本、成交率、markout、预算边际价值分析
├── tests/
│   ├── ashare_book_vs_sim/      # 订单簿 vs 撮合模拟器（深/沪语义）
│   ├── ashare_book_vs_snapshot/ # 真实 L2 回放 vs 3 s 快照
│   ├── compliance_replay/       # 全天回放下的消息数、瞬时撤单、撤单比
│   └── t1_cash_reconcile/       # T+1 与资金冻结的状态机测试
└── docs/design/                 # 美股版（通用内核）+ 本文（A 股 / 华泰）+ reference/
```

**依赖规则**：`broker/huatai/sdk_adapter/` 是唯一允许链接华泰 SDK 的目录；`ashare/strategy/` 不得依赖 `broker/`；`ashare/compliance/` 和 `ashare/risk/` 的检查不能被策略配置关闭（只能由 Supervisor 在受控流程中调整参数）。

---

## 附录 A：《厚门模型》在 A 股版中的分析与集成

### A.1 模型要点

同美股版附录 A.1：斐数组（近 1–2 年 10–15 个关键高低点）、底部组（回调后 3–5 日反弹 ≥ 8%）、空间对（`低位锚/0.65^n ≈ 高位锚`，n = 1..4，偏差 ≤ 1%）、起爆点 `(底部值 − 锚点) × 10 + 锚点`、双轨防守底 `(A − B)/10 + B`、目标位 `高位锚/0.65^n`、量能确认（较前一日放大 30%）、风险隔离（单资产 ≤ 20%）。可转债的迭代系数为 0.7。文档中海王生物的示例数字经复算一致。

### A.2 适配性

- **市场一致**：模型面向 A 股、ETF、可转债，与本系统的市场一致。
- **时间尺度不同**：模型是 1 天到 3 周的波段，本系统的日内层是毫秒到分钟级。因此它只能用于**底仓层**（日级决策），不能用于日内报价。
- **与 T+1 的契合**：A 股的 T+1 本来就要求有隔夜底仓，而模型输出的"起爆点 / 防守底 / 目标位"恰好对应"何时建底仓 / 何时减底仓 / 何时止盈"。这是它在本架构中最合理的位置。

### A.3 统计有效性（零假设检验，结论不变）

用纯随机游走（日波动率 3%、零漂移、500 个交易日）生成价格，按 15% 阈值的 ZigZag 提取拐点，再按文档规则筛选【M-VM，2000 次蒙特卡洛，seed = 42，脚本 `reference/houmen_null_test.py`】：

| 系数 c | 平均"强共振空间对"数 | 至少找到 1 对的比例 |
|---|---|---|
| 0.60 | 1.29 | 69.9% |
| 0.618 | 1.55 | 76.2% |
| **0.65** | **1.85** | **82.5%** |
| 0.70（文档中可转债所用系数） | 2.56 | 89.9% |

在没有任何结构的价格上也几乎总能找到"共振"，所以"找到共振"本身不是证据。另外，起爆点公式对底部值的敏感度为 10 倍（底部值相差 0.01 元，起爆点就相差 0.10 元），而拐点的选取又带有主观性，误差会被放大。

### A.4 集成规范（底仓规划器）

```
盘前批处理（research/level_context_batch，非交易进程）
  输入：前复权日线 OHLCV（≥ 250 日）、成交量
  1. 拐点：确定性的 ZigZag(15%)，取最近 ≤ 15 个，对齐到 tick
  2. 底部组：3–5 日内反弹 ≥ 8% 的低点
  3. 空间对（股票 c = 0.65，可转债 c = 0.70）、起爆点、轨 1/轨 2 防守底、目标位
  4. 底仓规则（示例，须通过 A.5 检验才能启用）：
       收盘价 > 起爆点，且成交量 ≥ 前一日 × 1.3 → 目标底仓 = 上限 × w
       收盘价 < 轨 2 防守底                     → 目标底仓 = 0（次日只卖不买）
       否则                                     → 维持
     硬上限：单标的底仓 ≤ 总资金 20%（文档的风险隔离原则），并受 §9.2 隔夜限额和压力测试约束
  5. 输出 BasePlan{symbol, target_base, stop_level, levels[], version} → 校验 → 签名 → 发布
交易进程：盘前加载 BasePlan（只读）；盘中不重新计算
```

### A.5 上线前检验（未通过则使用固定规则的底仓）

1. **事件研究（A 股日线）**：价格突破起爆点（并有量能确认）后 1/5/10/20 日的收益分布，与随机价位、其他系数（0.60/0.618/0.70）计算的价位做对照，要求经多重检验校正后差异显著。
2. **防守底的止损效果**：与固定百分比止损、ATR 止损做对比（回撤、收益、换手）。
3. **对底仓 T+0 组合的影响**：在回测中比较"厚门底仓"与"固定底仓"下的总 PnL（做市回转 + 底仓盯市）、回撤和压力损失。
4. **稳健性**：ZigZag 阈值 12–18%、容差 0.5–2% 范围内结论不翻转；样本外、分市场状态（上涨/震荡/下跌）检验。

---

## 附录 B：参考实现

| 文件 | 内容 |
|---|---|
| `ashare.hpp` | A 股参考数据、三级位图、涨跌停价梯订单簿、`L2Event`、`AshareBookBuilder`（深/沪语义）、`T1Position`、`CashAccount`、`FeeSchedule` / `fee_for_fill` / `breakeven_ticks`、价格笼子 / 涨跌停 / 交易单位检查、`DailyMessageBudget`、`CancelPolicy`、`ashare_pre_trade_check`、`t0_quote_limits` |
| `test_ashare.cpp` | 位图 vs `std::set`；订单簿 vs 撮合模拟器（深交所、上交所语义，78.4 万条消息，10 档逐档比对）；T+1；费用与盈亏平衡表；价格笼子、交易单位；预算与撤单策略；A 股事前检查；底仓 T+0 约束；微基准 |
| `ref.hpp` / `risk.hpp` / `oms.hpp` | 通用部分（SPSC、GCRA/TokenBucket/滑动窗口、KillSwitch、`compute_quotes` 等），与美股版共用 |
| `houmen_null_test.py` | 附录 A.3 的零假设蒙特卡洛 |

```bash
cd docs/design/reference
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
./build/test_ashare     # A-share components, prints "ALL OK"
./build/test_bench      # generic hot-path components
python3 houmen_null_test.py
```

**它不是生产代码**：没有真实的行情协议解码器（深交所 Binary、上交所 IS120 的字段级实现）、没有华泰 SDK 适配、没有恢复流程。订单簿重建的正确性只在模拟器生成的数据上验证过，**必须再用真实的历史 L2 数据验证**。

---

## 附录 C：必须核实的事项清单

| # | 事项 | 为什么重要 | 向谁确认 / 查哪里 |
|---|---|---|---|
| 1 | 《程序化交易管理实施细则》正文：四类异常交易行为的**量化阈值**（瞬时申报速率、1 秒内撤单的次数、撤单比例等）、报告内容、差异化收费的具体安排 | `CancelPolicy`、每秒限速、预算参数 | 上交所/深交所官网的细则原文及配套指南；华泰合规部门 |
| 2 | 程序化交易报告的流程与时点；交易软件和策略变更时的报告义务 | 上线门槛 | 华泰 |
| 3 | 华泰 MATIC/MAPIs：可用的 API（交易、行情、查询、批量撤单）、是否提供 **Linux SDK**、线程模型、流控参数、断线自动撤单、仿真环境、开通门槛和费用 | Gateway 设计、桥接需求 | 华泰机构业务 / 技术支持 |
| 4 | 华泰托管机房、内网 L2、极速/内存柜台的可用性、延迟指标和费用 | Architecture B 的可行性 | 华泰 |
| 5 | 佣金是否有最低收费；经手费、证管费是否包含在佣金内；过户费的现行标准；ETF、可转债的费用项目 | 盈亏平衡（§1.5） | 华泰合同；中国结算、交易所收费标准 |
| 6 | 价格笼子：主板对超出范围的申报采用哪种处理模式（拒单还是缓存）；ETF、可转债是否适用；基准价在无对手盘时的取法 | 风控检查 | 交易所交易规则 |
| 7 | 主板 ST 涨跌幅调整为 10% 的生效日期（公开报道为 2026-07-06） | 参考数据 | 交易所公告 |
| 8 | 融券、转融券的现行政策（2024 年起收紧） | 是否存在空头一侧 | 证监会 / 华泰 |
| 9 | T+0 ETF 的具体名单；可转债交易规则（tick、交易单位、涨跌幅、首日规则） | 形态 2 | 交易所规则 |
| 10 | L2 行情接口规范的当前版本（深交所 STEP/Binary、上交所 IS120），以及逐笔数据的字段语义（ExecType、委托数量含义、市价单价格字段等） | 订单簿重建 | 交易所技术服务页面 |
| 11 | L2 行情的授权、展示与非展示用途费用；历史数据用于研发回测的授权 | 成本与合规 | 上证信息 / 深证信息 / 授权数据商 |
| 12 | 各交易时段的精确规则（集合竞价阶段可否撤单、盘后固定价格交易） | 时段控制 | 交易所交易规则 |

---

## 附录 D：本次核对使用的公开来源（2026-11 检索）

- 程序化交易管理实施细则、高频交易认定标准（每秒 300 笔 / 每日 2 万笔）、四类异常交易行为：
  [每日经济新闻](https://www.nbd.com.cn/articles/2025-04-03/3818683.html)、
  [21 世纪经济报道](https://www.21jingji.com/article/20250404/f16ffa519a5ccd89d208e2d07747833c.html)、
  [证券时报](https://www.stcn.com/article/detail/1641763.html)、
  [证券时报（频繁瞬时撤单构成要件）](https://stcn.com/article/detail/2419844.html)、
  [央广网](https://finance.cnr.cn/ycbd/20250403/t20250403_527122910.shtml)
- 华泰 MATIC / MAPIs / MQuant / INSIGHT：
  [华泰 MATIC 官网](https://matic.htsc.com.cn/usercenter/)、
  [知乎：华泰证券量化 API 接口 SDK — MATIC](https://zhuanlan.zhihu.com/p/675563991)、
  [雪球](https://xueqiu.com/2339396598/274742578)、
  [中证网：华泰证券 HUATECH 伙伴大会](https://www.cs.com.cn/qs/202001/t20200113_6016708.html)、
  [中国证券网：华泰量化接入解决方案](https://news.cnstock.com/news,qy-201903-4346957.htm)
- 价格笼子（主板 2% 与 10 个最小变动单位孰高/孰低；科创板拒单、创业板缓存）：
  [证券时报](https://www.stcn.com/article/detail/836464.html)、
  [财联社](https://www.cls.cn/detail/1272183)
- 主板 ST 涨跌幅由 5% 调整为 10%：
  [第一财经](https://www.yicai.com/news/102696166.html)、
  [东方财富](https://finance.eastmoney.com/a/202506293443252963.html)
- 印花税减半、经手费调整（2023-08-28）：
  [证监会](https://www.csrc.gov.cn/csrc/c100028/c7426794/content.shtml)、
  [新华网](https://www.news.cn/2023-08/28/c_1212260850.htm)
- 融券与转融券限制（禁止利用融券实施日内回转交易；2024-07-11 暂停转融券）：
  [证监会](https://www.csrc.gov.cn/csrc/c100028/c7493852/content.shtml)、
  [财联社](https://www.cls.cn/detail/1729975)
- 可转债交易规则（首日 +57.3%/−43.3%，次日起 ±20%）：
  [深交所业务规则](https://docs.static.szse.cn/www/lawrules/rule/bond/W020250327514604502531.pdf)
- L2 行情接口规范：
  [上交所 IS120 行情网关接口](https://www.sse.com.cn/services/tradingtech/development/c/10827383/files/4a5c0f16fef7493a81be3b5c420c1dab.pdf)、
  [深交所 STEP 行情数据接口规范](https://www.szse.cn/marketServices/technicalservice/interface/P020240531618144293021.pdf)

---

*文档结束。【E】为待验证的目标；【M-VM】来自未调优的开发虚拟机；【BD】须向华泰确认；【XD】须在交易所环境实测；【REG】须以正式规则文本为准。*
