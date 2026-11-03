# AI 训练、回放评估与 Paper 接入运行指南

本指南描述仓库已经实现的数值模型工作流。模型预测短期中间价变化，只能减少已有策略的买卖参与度；订单簿、报价、同步风险预留、订单生命周期和成交模拟继续由原有交易模块负责。训练成功、模型注册、模拟盈利、Paper 授权是不同状态，不能相互替代。

以下命令是操作说明，默认新工作目录为 `artifacts/ai-release`，不代表这些命令已经执行或验收通过。实际执行记录、测试数量、指标和剩余阻塞以本次发布验证报告为准。本文不填写未测收益、准确率、延迟或券商联调结果。

已存在的 `artifacts/ai-v1`、`artifacts/ai-release` 是保留证据，不得按下面命令覆盖。本轮修复后的独立验收另使用 `artifacts/ai-rc3`、seed 44、声明起始日期 2026-08-03；具体结果由发布验证报告记录。重新操作时将示例中的数据与输出目录整体替换为尚不存在的新目录，并使用未消费的真实留出数据。

## 1. 现有模块审计与本次复用

最初的 MVP01 基线是一份架构方案，而非已有演示代码；初始工程审计见 [MVP01_AUDIT.md](MVP01_AUDIT.md)。AI 工作基于随后已经落地的交易研究平台扩展，没有另建假行情、假账户或 Python 伪成交后端。

| 模块 | 复用或新增内容 | 实际边界 |
|---|---|---|
| `engine/src/itch.cpp`、`book.cpp` | 复用真实 ITCH 5.0 解析、订单级订单簿与状态处理 | 当前为参考树/hash结构，没有零分配或 HFT 延迟承诺 |
| `engine/src/strategy.cpp`、`risk.cpp` | 复用成本感知报价、库存边界、同步预留、限流及亏损 Kill | AI 不能改变价格、数量、风控上限或重置 Kill |
| `engine/src/simulation.cpp` | 复用延迟激活、队列前置量、部分成交、撤单竞争和 FIFO 账本；增加信号 gate、窗口与 long-only | 公共 E/C 只是模拟撮合证据；P/Q 公共成交不是账户成交 |
| `engine/src/ml_io.cpp`、`trading-ml-replay` | 新增因果 L1 CSV 导出和严格 signal CSV 输入 | 普通 `trading-replay` 不接受 ML 专用参数 |
| `ml/features.py` | 新增离线与在线共用的有状态因果特征 | 不读取未来价格、标签、新闻或语言模型回答 |
| `ml/data.py`、`prepare.py` | 新增数据来源、SHA256、导出血缘及标签构造 | 来源授权是操作员声明，程序不能独立证明交易所许可 |
| `ml/training.py`、`train.py`、`model.py` | 新增 Ridge 基线训练、按时间拆分、训练期标准化和纯 JSON 推理模型 | 数值回归不是已校准成交概率，也不是盈利证明 |
| `ml/runtime.py`、`backtest.py` | 新增成本门槛、结构化信号、固定场景评估及 C++ 真实回放调用 | Python 不依据预测价格生成账户成交 |
| `ml/registry.py`、`retrain.py` | 新增不可覆盖的模型证据、晋级门槛、活动版本、回滚及受控重训tick | 本地操作员文件系统是信任边界，不是远程签名服务 |
| `execution/core_binding.py`、`auto_paper.py`、`ai_gate.py` | 复用 C ABI 交易内核与独立 Paper 执行器，按配置接入模型过滤 | 用户注册或网页点击不授予发单权限 |
| `execution/funds.py` | 增加账户资源和 long-only 权限检查 | 不支持借券核验，因此禁止做空 |
| 原 Web、数据库、队列和审计模块 | 保留用户、任务、回放结果及只读运营状态 | 不把数据库、REST 或训练进程放入 C++ 交易核心路径 |

当前仍需真实外部条件：有合法使用权的原始行情、可靠会话日期、完整会话和连续数据、真实 Paper 账户与 Gateway/SDK、股票交易权限、当前交易日实时行情来源、账户对账和实测故障恢复。仓库合成数据不能填补这些缺口；环境变量中的声明也不能替代合同、账户权限或实际接入验收。

## 2. C++ 与 Python 的职责边界

```text
授权 ITCH 文件 + 来源清单
    → C++ Decoder / Book / Market State
    → 因果 L1 observations CSV
    → Python 共享 FeatureState
    → 训练期标签 / Ridge 拟合 / 固定验证与测试
    → model.json + 训练证据
    → Python 成本策略 → 结构化信号 + ordinal side-gate CSV
    → C++ Simulator → Pricing → Risk → OMS → 保守撮合 → FIFO PnL
    → 评估报告 → Registry 明确晋级

可选 Paper：
真实实时 MoldUDP64 → C++ Book → Python 同一 FeatureState / 数值模型
    → 仅允许或禁止某侧 → C++ 报价、风险预留和发送边界复检
    → Python 账户资金/权限/OMS/限流 → IBKR Paper API
    ← 券商私有执行回报 → 去重、账本、费用、对账、Kill
```

训练需要 `requirements-ml.txt` 的固定版本 NumPy/SciPy/scikit-learn 等依赖。已保存模型的数值推理使用标准库与纯 JSON 参数，不加载 pickle，也不在线训练。可按现有环境安装离线依赖：

```sh
.venv/bin/python -m pip install -r requirements-ml.txt
```

本文假设已经按项目部署文档构建 `build/trading-ml-replay` 和 `build/libvector_core.so`。二进制及模型哈希写入证据；改变构建产物需要重新核对血缘与评估。部署和账户隔离另见 [DEPLOYMENT.md](DEPLOYMENT.md)、[IBKR.md](IBKR.md) 与 [AUTOMATED_TRADING.md](AUTOMATED_TRADING.md)。

## 3. 建立有来源的数据集

### 3.1 授权真实数据

每个数据集只支持一个股票。训练至少需要十个独立、按日期排序的会话；最少划为 6 个训练、2 个验证、2 个测试会话。每个会话须有足够有效样本，重复会话、重复 ITCH hash、重复 CSV hash 被拒绝。文件须为数据集目录内的相对路径，不能使用 `..`、符号链接或外部绝对路径。

操作员准备 `dataset/source-manifest.json`，根字段精确为：

```json
{
  "schema_version": 1,
  "provenance": {
    "kind": "licensed_market",
    "source": "实际数据供应商、采集来源及授权记录引用",
    "authorized_use_attested": true
  },
  "captures": [
    {
      "capture_id": "实际唯一会话标识",
      "session_date": "2026-07-01",
      "symbol": "AAPL",
      "stock_locate": 1,
      "itch_path": "captures/实际文件.itch",
      "itch_sha256": "替换为该原始文件的64位小写SHA256"
    }
  ]
}
```

这只是字段示意，单个条目不满足训练数量要求。`stock_locate` 必须取自真实文件，不是股票代码的固定映射。`session_date` 必须是实际交易所会话日期；ITCH 时间戳本身不包含完整日期。只有确实核实使用权后才能填写真实授权声明，不要把合成数据改名成 `licensed_market`。

输入为未压缩的两字节大端长度前缀 ITCH 5.0，非 PCAP/gzip。C++ 导出器拒绝其他非零 stock locate；如从全市场文件筛选，必须保留会话状态和完整订单引用关系。长度前缀文件没有 MoldUDP64 传输序号，因此“文件解析成功”不能证明原始采集没有丢包。

### 3.2 仅用于软件验证的合成数据

没有授权真实文件时，可以验证协议、训练与评估流水线。以下命令创建独立新目录，不覆盖已有产物：

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m ml.fixtures \
  --output artifacts/ai-release/dataset \
  --sessions 20 --steps 2400 --seed 43 --start-date 2026-07-01
```

生成器输出实际 ITCH 消息和 `source-manifest.json`，随后必须经真实 C++ 订单簿导出；它不会直接制造训练标签或 PnL。来源永久标注 `synthetic`、`authorized_use_attested: false`。这些只是具有会话生命周期标记的短日内片段，不是二十个真实历史交易日，也不证明覆盖真实牛市、熊市或流动性压力。

不同 seed 和目录可用于软件回归；它们不创造新的真实市场证据。合成模型即使通过某些数值门槛也禁止晋级 Paper。原有 `artifacts/ai-v1` 不应覆盖；本指南路径用于新实验，不继承旧实验的任何通过结论。

### 3.3 C++ 导出与血缘验证

```sh
.venv/bin/python -m ml.prepare \
  --source-manifest artifacts/ai-release/dataset/source-manifest.json \
  --binary build/trading-ml-replay
```

输出包括 `dataset/manifest.json`、`dataset/market/*.csv`、`dataset/export-evidence.json`。清单绑定原始 ITCH 与导出 CSV 的双 SHA256；导出证据记录 C++ 二进制 hash。`ml.train` 和 `ml.backtest` 还会在临时目录重新运行 C++ 导出器，比较 CSV hash，防止仅提供两个无关文件的 hash 就冒充血缘验证。

CSV 精确结构：

```csv
ordinal,timestamp_ns,logical_timestamp_ns,stock_locate,bid,ask,bid_qty,ask_qty,valid,tradable
```

`ordinal` 是文件消息序号，价格单位为 1/10000 USD，数量为 L1 显示股数。字段不是成交量。导出器保留无效、停牌、状态切换和同时间戳事件；禁止把一个时间戳最后的盘口提前用于该时间戳第一条预测。C++ 可展示原始时间回退与因果运行最大时间，但当前 Python 训练流程明确拒绝回退或被修复的时钟，要求两列一致。

## 4. 在看结果之前固定准入策略

复制并审阅 `ml/admission.example.json`，确定实际费用、最小佣金、滑点缓冲、TTL、最大目标库存和评价门槛。示例只是完整字段模板，不是适用于所有账户的推荐参数。Paper 使用的七个模型策略字段必须与注册策略完全匹配；当前 Paper 额外强制 `allow_short: false`。

```sh
cp ml/admission.example.json artifacts/ai-release/admission.json
.venv/bin/python -m ml.registry --store artifacts/ai-release/registry \
  install-policy --file artifacts/ai-release/admission.json
```

保存输出 `data.admission_sha256`。必须在训练开始前安装并冻结用于 Paper 准入的策略；注册时间晚于训练开始的策略不能冒充事先承诺。模型效果不佳时降低费用、缩小损失统计范围、放宽最低成交数或反复试测试集，不构成有效验收。

主要门槛包括训练/验证样本量、验证 RMSE、相对零预测改善、方向准确率、留出会话数、模拟事件与成交数、净 PnL 下界、最大回撤、费用压力和模型年龄。全部字段显式存储并计算 hash，模型 manifest 绑定其准确版本。

## 5. 训练和防止未来数据泄漏

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m ml.train \
  --manifest artifacts/ai-release/dataset/manifest.json \
  --output artifacts/ai-release/training \
  --exporter build/trading-ml-replay \
  --horizon-ns 500000000 --max-label-delay-ns 250000000 \
  --window-events 32 --max-gap-ns 1000000000 \
  --alphas 0.1 1 10 --seed 43
```

实际实现为短期 mid return 的 Ridge 回归基线。六维输入是 spread bps、盘口不平衡度、microprice 偏移、过去收益、过去波动率和深度对数。默认窗口 32 个盘口变化事件；它不是固定 32 秒。普通连续时段为交易所本地 09:30 至 16:00，离开时段、无效/停牌或超过配置的变化间隔会清空预热状态。

只有时间戳推进时的第一条合格观察可以发预测；后续同时间戳观察只更新以后时点可用的历史。在线和离线使用相同 `FeatureState`。标签单独使用同一有效连续片段中达到未来 500ms 的第一条合格观察，最多接受额外 250ms 延迟；不能跨会话、停牌/reset 或前向填充失效盘口。标签中的未来价格绝不进入特征或直接成为撮合价。

数据按完整会话时间顺序划分 60%/20%/20%，不随机打乱。`StandardScaler` 和每个候选 Ridge 仅拟合训练数据；预声明 alpha 网格用验证 MSE 选择，训练内部 walk-forward 用于补充评估及同分选择。跨边界样本按标签结束时间清除，并加 `horizon + max_label_delay` embargo。最终测试指标在参数和模型身份封存后生成。

测试集消费记录保存在数据集目录的 `.ml_holdout_registry.json`，不随训练输出目录改变。相同完整实验可以做标记为 exact reproduction 的复跑；改变实验后重复使用相同源 hash 或同股票同日期测试会话会被拒绝。真实模型迭代必须取得更新、未消费的留出区间；不要删除台账或改文件名来绕过。

输出目录必须不存在。成功产物为：

```text
training/
  model.json          # 特征schema、标准化参数、系数、目标、数据身份和模型版本
  report.json         # 分割、purge、walk-forward、基线、测试指标、推理一致性
  config.json         # seed、alpha网格、特征与训练配置
  data-manifest.json  # 原始数据清单副本
  train.log           # 有时间戳的结构化训练事件；失败也记录
```

模型版本是内容生成的 `ridge-...` 标识。报告包含 Python/NumPy/sklearn 版本和训练/标准库推理一致性误差。SVD Ridge 本身主要是确定性求解；记录 seed 有助复现，但改变 seed 不意味着得到独立市场证据。训练完成仍输出 `eligible_for_paper: false`。

## 6. 真实 C++ 回放中的模型评估

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m ml.backtest \
  --manifest artifacts/ai-release/dataset/manifest.json \
  --training-run artifacts/ai-release/training \
  --output artifacts/ai-release/backtest \
  --binary build/trading-ml-replay \
  --admission artifacts/ai-release/admission.json --split test
```

`--split validation` 可用于明确的验证期研究；模型晋级需要未用于选择的 test 会话证据。测试模拟另有 `.simulation-holdouts` 台账，绑定会话源 hash、日期、模型与预声明评估计划；改变模型或策略后重复消费同一测试区间会失败。

执行前写入 `evaluation-plan.json`，固定以下五种场景，不能事后只挑最优场景：模型参与、成本感知双边基线、始终等待、双倍费用/最低佣金/缓冲、五倍激活与撤单延迟。当前比较每单 1 股，默认 1ms 激活和撤单延迟、每秒 5 个新单、每日损失上限 10000 美分；这些是模拟配置，不是测得的 IBKR 性能。

每个会话生成结构化 `*-signals.jsonl`、模型/基线/等待 CSV、源文件/CSV/模型/策略/信号 hash 绑定记录，以及各场景 `*-engine.json`。汇总为 `backtest/report.json`。结构化信号包括：

| 字段 | 含义 |
|---|---|
| `model_version`、模型/content/policy hash | 数值模型和政策的可追踪身份 |
| `source_sequence`、`source_timestamp_ns` | 预测所依据的因果市场观察 |
| `signal_time_ns`、`expires_ns`、`clock` | 生成时间、独占到期时间和时钟域 |
| `prediction_horizon_ns`、`predicted_return_bps` | 预测跨度和 mid return 估计，非保证收益 |
| `direction`、`action`、`allow_bid`、`allow_ask` | 只过滤侧别的指令；WAIT不创建订单 |
| `target_position` | 模型建议上限，不是实际成交或授权订单 |
| `inputs`、`required_edge_bps`、`reason_codes` | 输入特征、成本门槛及等待/拒绝原因 |
| `invalidation_conditions`、`execution_authorized` | 失效条件；信号自身始终不授予执行权限 |

C++ 接受的最小执行契约是：

```csv
ordinal,allow_bid,allow_ask,expires_ns
```

缺少信号、预热、过期、无效市场和窗口之外默认禁用两侧；新信号只能在对应 ordinal 的盘口更新之后影响新的报价决策。撤单有延迟，因此失效信号不等于原订单已经消失。C++ 继续处理全部尾段行情，报告窗口末库存和窗口后的成交；禁止截断回放藏掉不利成交。

离线信号生成不能预先知道未来真实模拟持仓，只输出方向许可。默认 `allow_short: false` 时调用 C++ `--long-only`，由模拟账本的实际库存减所有卖单预留独立检查卖出容量；未成交买单不能资助卖单。不足时整单拒绝，不虚构买入成交，也不自动补仓平掉不足一单大小的残余库存。

费用按每个订单累计 `max(最低佣金, 累计成交股数 × 每股费率)` 增量入账，零成交不收最低佣金。成本报价边距以偏移后的 reservation price 为参考，至少覆盖保守的一股部分成交最低佣金、每股费用和缓冲；返佣不会收窄报价。成本较高时可以全部 WAIT 或远离 BBO 而零成交，这属于合法结果，不应为了展示收益制造成交。

C++ 单独运行不验证 SHA256 来源绑定；该责任由 `ml.backtest` 的受控调用和不可变 manifest 承担，C++ JSON 明确显示 `capture_binding_verified_by_engine: false`。文件消息 ordinal 不是跨文件唯一身份，不能把另一份 capture 的信号直接拿来复用。

## 7. 模型注册和晋级

注册、评估与激活是分开的操作。下面 `<ADMISSION_SHA256>` 取自安装策略输出，`<MODEL_VERSION>` 取自实际训练产物，不能填写猜测值：

```sh
.venv/bin/python -m ml.registry --store artifacts/ai-release/registry register \
  --model artifacts/ai-release/training/model.json \
  --manifest artifacts/ai-release/dataset/manifest.json \
  --admission-sha256 '<ADMISSION_SHA256>'

.venv/bin/python -m ml.registry --store artifacts/ai-release/registry evaluate \
  --version '<MODEL_VERSION>' --report artifacts/ai-release/training/report.json

.venv/bin/python -m ml.registry --store artifacts/ai-release/registry validate-simulation \
  --version '<MODEL_VERSION>' --report artifacts/ai-release/backtest/report.json

.venv/bin/python -m ml.registry --store artifacts/ai-release/registry status
```

晋级顺序为 `candidate → evaluated → simulation_validated → operator_paper_approved`。如果门槛未过，命令返回非零并保留失败证据/候选状态。全 WAIT、成交数不足、无效估值、亏损 Kill 或压力门槛失败都不能激活；不要为了让页面显示绿色而放宽既定门槛。

只有成功完成所需验证后，才可设置模拟活动版本：

```sh
.venv/bin/python -m ml.registry --store artifacts/ai-release/registry activate \
  --version '<MODEL_VERSION>' --mode simulation
```

Paper 额外需要真实授权数据、原数据清单复核、训练前已安装的准入政策、未过期模型及操作员私有批准文件。文件须由当前操作员所有、禁止组/其他人读取，绑定精确模型、策略、门槛、数据、训练与模拟报告 hash、操作员身份和有效签发时间。批准文件不是自动训练的输出，也不能由网页普通用户签发。

```sh
.venv/bin/python -m ml.registry --store /operator/model-registry approve-paper \
  --version '<APPROVED_REAL_DATA_MODEL>' --operator-file /operator/private/approve-paper.json \
  --manifest /operator/authorized-dataset/manifest.json

.venv/bin/python -m ml.registry --store /operator/model-registry activate \
  --version '<APPROVED_REAL_DATA_MODEL>' --mode paper \
  --operator-file /operator/private/approve-paper.json
```

操作员根据 `ml/registry.py` 的 action/hash 验证合同生成和审阅批准文件。Paper批准与激活都要求 `action: "approve_paper"`，可复用尚在有效期且准确绑定同一模型证据的批准文件；回滚要求 `action: "rollback"`，还须绑定当前活动版本 `from_version`。合成模型永远不能通过 Paper 检查。`mode=live` 不受支持且始终拒绝。活动指针原子替换，历史版本和证据不可覆盖；回滚同样要求旧版本重新满足当前检查和操作员批准，不等于跳过安全检查：

```sh
.venv/bin/python -m ml.registry --store /operator/model-registry rollback \
  --version '<PREVIOUS_APPROVED_VERSION>' --mode paper \
  --operator-file /operator/private/rollback-paper.json
```

Registry 目录由操作员控制，Web 仅应读允许公开的状态投影，不能写模型、批准、门槛或活动指针。它抵御错误输入、路径链接及普通流程误用；拥有该目录写权限的管理员仍是信任主体，hash 不等于第三方数字签名。

## 8. 漂移检测和受控重训

`ml.retrain` 每次执行一个有边界的调度 tick，不是自动启动的常驻服务。由操作员的 cron/systemd timer 调用；训练工作不放入交易线程。默认周期 86400 秒，漂移窗口最少 200 个样本，标准化均值偏移阈值 2.0、PSI 阈值 0.2，训练子进程超时 1800 秒。参数必须在看新结果前确定，不能用反复重训搜索测试集。

先运行不执行训练的检查。`--manifest` 应指向已按第3节完成 prepare 的新增授权数据集；`--window` 为可选近期特征窗口：

```sh
.venv/bin/python -m ml.retrain \
  --store artifacts/ai-release/registry --base-version '<MODEL_VERSION>' \
  --manifest /operator/new-authorized-dataset/manifest.json \
  --exporter build/trading-ml-replay \
  --interval-seconds 86400 --min-samples 200 \
  --mean-z-threshold 2.0 --psi-threshold 0.2 --timeout-seconds 1800
```

默认只记录请求/状态，不训练。周期已到或漂移成立且数据通过检查后，明确添加 `--execute` 才调用真实 `ml.train`：

```sh
.venv/bin/python -m ml.retrain \
  --store artifacts/ai-release/registry --base-version '<MODEL_VERSION>' \
  --manifest /operator/new-authorized-dataset/manifest.json \
  --exporter build/trading-ml-replay --execute
```

不带 `--manifest` 时，即使到期也记录 `NO_NEW_AUTHORIZED_DATA`，不造数据继续。`--allow-synthetic` 只允许研究用候选重训，必须显式指定且永不授予 Paper 权限。新数据仍须满足至少十个会话、同股票、来源和双hash检查；已经尝试的数据集、已消费的测试源hash或同股票同日留出区间不能反复尝试。

可选 `--window /operator/recent-feature-window.json` 的精确字段为：

| 字段 | 要求 |
|---|---|
| `schema_version` | 整数1 |
| `model_version` | 准确基准模型版本 |
| `feature_schema_version` | `causal_book_v1` |
| `feature_names` | 与模型六维特征名及顺序完全一致 |
| `observed_at_ns` | 实际观察窗口的UTC epoch纳秒时间；默认最多一小时旧，拒绝未来时间 |
| `samples` | 最多10000行，每行六个有限数值；只能来自同一共享特征实现的已观察输入 |

漂移参考分布仅拟合训练集。标准化均值变化和 PSI 表示分布发生变化，不表示已经知道未来收益下降，也不自动修改模型。当前命令接收已采集窗口文件；没有把任意 Dashboard 指标当作正确六维样本的隐式转换器。窗口不足时返回 `insufficient_data`，不会伪报稳定或漂移。

Registry 写锁串行化同一 store 的重训，子进程使用受限环境、单线程 BLAS 与超时；推理读取不拿此训练写锁。训练沿用已封存模型的特征、目标、seed及alpha配置，输出到 `registry/retrain/runs/<request_id>/training`，同目录保存实际调用参数与stdout/stderr。请求、尝试记录和 `retrain/latest.json` 保留 `idle/requested/running/completed/failed/skipped/insufficient_data/blocked` 状态及原因。

重训成功最多注册一个新 `candidate`，`automatic_promotion` 始终为 false。它不会执行回测晋级、改活动指针、降低门槛或启动券商。新候选仍须经过第6、7节，重新审阅结果；旧活动模型不会因为新训练完成而被自动替换。

## 9. Paper 可选 AI 接入与独立账户限制

Paper AI 通过 `PAPER_AI_REGISTRY` 可选加载；默认空值保留原策略，不加载模型。加载使用 `ml.registry.load_approved_model(mode="paper")`，校验活动指针、模型字节 hash、政策、股票和目标期限。Paper 时钟为本地 monotonic，离线为 exchange logical；两者的时间数值不能直接混用。

`execution/ai_gate.py` 启动时及运行中每秒重新检查注册状态。版本变化、撤销或证据失效会锁存 WAIT，不在线热换模型；切换需要受控停机、对账和重新启动。共享特征实现保证相同有效观察的特征计算合同；跨语言、离线时间和实时接收时间不同，不能据此声称真实订单与历史模拟逐条一致。

对于已覆盖的事件序列，`tests/test_ml_signal_parity.py` 比较实际离线 signal 生成与 Paper 过滤输出：同时间戳后续盘口变化没有新合格特征时显式 WAIT；真正不变的盘口/冗余状态只保留原TTL，不延长寿命。这验证特征与参与过滤的指定语义，不证明未连接的券商成交、网络时序或所有在线路径与历史模拟等价。

示例配置仅表达字段，账户、实时来源和已批准 registry 路径必须使用部署时的真实值：

```dotenv
PAPER_AI_REGISTRY=/operator/model-registry
PAPER_AI_SIGNAL_TTL_MS=250
PAPER_AI_MIN_EXPECTED_EDGE_BPS=1.0
PAPER_ALLOW_SHORT=false
PAPER_FUNDS_MAX_AGE_MS=15000
PAPER_FUNDS_REFRESH_MS=5000
```

费用、最低佣金、滑点、TTL、最小预期边际、目标库存和做空许可必须与注册政策一致，不能加载后悄悄修改。`execution/paper.env.example` 提供原有 IBKR、合约、行情、C ABI、费用和风险变量；该文件不是已配置账户。密钥和真实环境文件放在操作员私有目录，不提交 Git。

启动接口保持：

```sh
# 在已装入受控环境变量的shell中执行；没有该flag不会连接券商或发单。
.venv/bin/python -m execution.auto_paper

# 仅在完成真实Paper验收与明确执行授权后使用。
.venv/bin/python -m execution.auto_paper --enable-paper \
  --status-file /operator/private/paper-status.json
```

预检查成功只说明本地配置、C ABI/SDK加载等检查通过，不代表已经验证券商、市场权限或账户连接。真正启用仍要求所有原 gate，包括 Paper 模式、独占账户、股票权限、日初零PnL条件、真实实时来源声明，以及完整券商对账结束且账户 flat/无挂单。既有库存接管和跨日历史PnL导入未实现时必须拒绝启动，不能忽略这些状态。

实时输入来自 `execution/live_feed.py`，要求当前纽约交易日、指定10字节会话、从 MoldUDP64 序号1和 System Event O 开始、来源时间新鲜。缺口、冲突、断线或时钟异常进入故障状态；没有自动历史文件补齐或中途无快照加入能力。把录制文件包装成“实时流”不满足授权与新鲜度要求。

账户资金由 `execution/funds.py` 独立保护，模型不得绕过：

- 只接受准确目标账户、USD 的 `AvailableFunds`、`SettledCash`、`BuyingPower`，并等待完整查询 End；缺失、过期、非有限数或错误币种拒绝。
- 可用预算取三者最小值，并扣本地已购买金额、正费用高水位和在途买单预留。刷新不恢复已经花掉的预算，不把卖出收入立刻当作可再投资结算现金。
- 股票交易权限需要独立操作员核验声明；余额足够不等于具备交易权限。
- 当前仅 cash-constrained long-only，`PAPER_ALLOW_SHORT=true` 被拒绝。每笔卖单须由实际持仓减全部在途卖单覆盖，待撤单仍占预留。
- 模型只把 C++ quote 的某侧关闭。实际 send 前重新检查模型有效性、资金、权限、限流和 C++ permit epoch/deadline；策略不能自行解除 Kill。

WAIT、预热、过期、成本不足和模型撤销须撤掉失效侧报价；撤单未确认前仍处理成交。IBKR 私有回报才改变真实 Paper 账户账本，公共 ITCH print 不得充当私有 fill。异常或结果未知进入 Kill/对账流程，不假定 SDK 抛错意味着订单没有发送。

本仓库实现和离线测试不等于已完成真实 IBKR SDK/账户联调。IBKR API、互联网、Gateway、券商路由和交易所 ACK 延迟独立于内部特征或 C++ 处理耗时，不承诺端到端小于1ms。真正超低延迟 HFT 仍需要共址、原生网关、专线及单独实测。

## 10. 评估结果的阅读方式与交付边界

训练报告的 MAE/RMSE、相关性和方向准确率衡量目标预测误差；零预测与 microprice 是数值基线。它们不含真实成交、费用和风险承载，不能直接换算收益。

模拟净收益为 `Realized + Unrealized - Fees + Rebates`，整数金额单位均为 1/10000 USD。`pnl_mark_valid=false` 时汇总净收益/未实现收益/回撤不可用，不能把最后一个旧估值当作有效收益；会话末停牌或收盘后估值失效可能正常触发此状态。必须同时检查成交数、残余库存、挂单、撤单竞争、费用和 Kill，而非只读净收益。

报告按会话独立从 flat 启动。残余库存不会被强制平仓，也不能把这些结果拼成连续真实账户净值。`turnover_units` 是两侧模拟成交额之和；没有本金基准时 `turnover_ratio` 和 `return_on_capital` 为 null，不编造年化收益或回报百分比。持仓时长按 flat→非flat→flat/换向事件计算；未结束持仓和窗口后成交单独列出。

`measured_internal_python_feature_and_policy` 只测离线 Python 特征与策略过程，包含 P50/P90/P99/P99.9/Max 和样本数；不包含 NIC、网络、券商或 ACK。`broker_latency.status=unmeasured` 与 `orders_sent=0` 必须保留，不把内部耗时包装成真实 tick-to-trade。

运行失败、准入失败、零成交、负收益和数据不足都是需要保留的结果。未具备授权市场数据时交付的是可验证的软件与训练工作流，不能升级描述为已验证商业交易收益。后续真实数据验收、资金/库存恢复、时延测量、券商故障演练与受控发布仍按项目发布检查单执行。

接口细节与检查代码：

- [C++ ML 回放合同](../engine/ML_REPLAY.md)、[C ABI 合同](../engine/C_ABI.md)。
- `ml/tests/test_features.py`、`ml/tests/test_training.py`：因果特征、标签、训练与hash血缘。
- `engine/tests/test_ml_cli.py`：实际二进制导出、signal TTL/窗口、FIFO、long-only、费用与风控。
- `execution/tests/test_funds.py`、`execution/tests/test_auto_paper.py`、`execution/tests/test_ai_gate.py`：资金、权限、模型过滤与Paper生命周期。
- `tests/test_ml_signal_parity.py`：同时间戳变化与不变状态的离线/Paper过滤对齐。

具体发布时应同时保存源manifest、原始文件hash、C++二进制hash、依赖版本、模型/config/report/log、信号与评估计划、registry事件和未通过原因。合成验收结果与真实市场/账户验收分开记录。
# 容器读取模型证据

Web 默认 `ML_MODEL_STORE_DIR` 为空，不加载任意宿主目录。可使用 `infrastructure/deployment/compose.ml.yml`，设置 `ML_MODEL_STORE_HOST_DIR` 指向受控模型库并只读挂载。运行容器 UID 为 10001；目录与文件所有者必须满足 Registry 的可信用户规则，且不能组/其他用户可写。不能通过放宽成 0777 解决权限问题。

本次本地验收使用独立的、仅含合成数据证据的 `artifacts/ai-rc3/model-store-web` 快照，所有者 10001，目录 0755、文件 0444，原始研究库不变。该公开可读权限只适合本次非敏感合成产物；真实私有模型应以专用 UID/组、最小读取权限和只读挂载部署。更新快照前应暂停写入或持有 Registry 写锁，复制完整版本、策略、证据和索引，加载核验通过后再切换目录。

训练数值库属于离线研究环境，安装 `requirements-ml.txt`。生产 Web 镜像仅包含标准库模型推理/验证代码，不包含 sklearn；训练不能在 Web 请求内执行。

