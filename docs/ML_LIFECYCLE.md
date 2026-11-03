# 模型生命周期与再训练

本模块管理本地 JSON 线性模型、不可变证据和操作员批准。训练、历史模拟、注册表审批、连接券商是不同操作。训练成功不会下单，注册 candidate 不会激活；合成数据永远不能进入券商 paper，`live` 在注册表接口中始终拒绝。真实行情授权、券商 paper 联调和外部执行条件仍需独立验收，文件中的授权声明不是交易所授权证明。

## 状态与证据

```text
train output → candidate → evaluated → simulation_validated
                                           │
                                           ├→ active simulation（显式 activate）
                                           └→ operator_paper_approved
                                                    └→ active paper（另行显式 activate）
periodic / drift → retrain intent → actual offline train → new candidate
```

每次正向迁移重新验证绑定。评估不通过时保留报告和失败尝试，不改变已通过的阶段或活动指针。`read_status` 单独返回最近一次 `evaluation_passed`、`simulation_passed`，因此旧阶段已通过、最新一次尝试失败可以同时存在；不能仅凭阶段文字放行。

```text
MODEL_STORE/
  policies/<admission_sha256>.json
  versions/<model_version>/{model.json,manifest.json}
  evidence/<report_sha256>.json
  events/<model_version>/{evaluated,simulation_validated,operator_paper_approved}.json
  events/<model_version>/latest-<training|simulation>-attempt.json
  events/<model_version>/<training|simulation>-attempt-*.json
  active-simulation.json                  # 未激活时不存在
  active-paper.json                       # 未批准时不存在
  history/<mode>-<generation>.json
  index.json
  holdouts.json
  retrain/{latest,schedule,attempts}.json
  retrain/requests/<request_id>.json
  retrain/runs/<request_id>/{invocation.json,stdout.log,stderr.log,training/}
```

证据以内容哈希命名并原子创建，已有模型文件不覆盖。活动指针使用临时文件、`fsync`、原子替换，历史版本与切换记录保留。故障中止可能留下未完成临时目录或已消费的数据集；遇到这种情况应由操作员检查日志，不通过删除锁或 ledger 自动重试。

## 精确绑定与准入配置

`model_version` 是模型 canonical JSON 内容身份。`model_sha256` 是 **model.json 实际文件字节** 的 SHA256，包含序列化空格和换行；两者不可互换。报告、活动指针与 paper 推理使用同一文件摘要。训练来源还绑定 dataset、数据 manifest、各 ITCH/CSV 文件摘要，以及实际 C++ 导出器摘要。注册表要求训练报告与模型均声明并一致证明 `market_exports_verified=true`，拒绝未经导出验证的旧产物。

准入文件为 `{"schema_version":1,"policy":{...},"gates":{...}}`，完整字段见 `ml.registry.DEFAULT_GATES` 与 `ml.runtime.PolicyConfig.policy_document()`。必须在训练开始前安装并冻结；训练后补装的阈值可用于研究记录，不能取得 paper 批准。禁止根据同一个 holdout 的结果反复调门槛。

交易 policy 精确绑定 `signal_ttl_ns`、`min_expected_edge_bps`、`fee_per_share_units`、`min_fee_per_order_units`、`slippage_buffer_units`、`max_target_position`、`allow_short`。`clock` 明确不参与 policy 哈希：历史模拟使用 `exchange_logical`，paper 使用 `monotonic`，manifest 记录这一约定；这不是允许忽略其他配置差异。标的和目标 horizon 独立绑定，signal TTL 不得超过 horizon；paper 禁止做空。

默认准入门槛如下。它们是工程发布门槛，不能据此声称有收益优势；改变门槛需要新的事前政策。

| 配置 | 默认值 |
| --- | ---: |
| train / validation 最少样本 | 200 / 100 |
| validation 最大 RMSE，bps | 5.0 |
| 相对零预测的最少 RMSE 改善率 | 0.0 |
| validation 最少方向准确率 | 0.5 |
| 模拟最少 events / fills | 1,000 / 10 |
| 模拟最低净 PnL，USD × 10,000 | 0 |
| 最大模拟回撤，USD × 10,000 | 1,000,000 |
| 成本压力最小倍数 | 2.0 |
| 成本压力最低净 PnL，USD × 10,000 | -50,000 |
| 最少独立 holdout sessions | 1 |
| paper 模型最大年龄 | 604,800 秒 |

训练门检查按日期隔离的 train/validation/test、标签 horizon purge、不可跨 session 标签、真实样本数和锁定的 test 身份。模拟门只接受完整 test 分区，不能选择其中获利的日期；日期必须晚于用于选择模型的 validation。schema 2 报告保留每个模型、基线、成本和延迟情景的全部原始 C++ 会话账本及其内容摘要，注册表从这些账本重新构造汇总并核对现金、FIFO 已实现 PnL、成交数量、库存与成本恒等式。仅修改汇总收益或删去亏损 session 不能通过。WAIT 基线必须无挂单、无成交、无费用、无风险敞口及零 PnL。

共享 `ml.valuation.evaluation_contract()` 固定研究假设为单笔 1 股、下单上限 5/s、每日亏损上限 100 USD、下单与撤单延迟各 1 ms、报价寿命 1 s、tick 0.01 USD、两倍成本与五倍延迟情景。它们是**模拟假设**，不是已测得的 IBKR 性能或实盘授权额度。引擎实际配置必须与合同和准入 policy 相符，报告自报一套更宽松的参数仍会被拒绝。成本和延迟压力都检查预声明的压力净 PnL 下限、回撤限额、无 kill、无剩余敞口；压力情景允许在既定下限内亏损。合成数据与零成交仍不能证明可交易或盈利。

新的验收和 approved model 加载还要求研究源码摘要与当前安装的 `backtest.py`、`features.py`、`runtime.py`、`model.py`、`data.py`、`valuation.py` 一致。研究逻辑或合同改变后，应在新测试区间重新形成证据；旧报告保留只读，不能删除原 holdout 锁来重跑调参。schema 1 缺少完整账本，继续支持历史展示，但以 `LEGACY_REPORT_REQUIRES_REVALIDATION` 拒绝新晋级与自动加载。摘要只验证内容/版本一致性，不是实际执行的独立签名；能够改写全部文件的同 UID 操作员仍在信任边界之外。

报价新鲜度与账户估值有效性分别记录：收盘后无有效报价，但库存和未完成订单均为零、现金与已实现 PnL 一致时，`reconciled_flat_ledger` 可提供确定的零未实现 PnL 与净 PnL。持仓期间存在未观测估值区间时，最终平仓不能补回回撤路径，`drawdown_complete=false`、回撤为 `null`。无有效估值时不能用零填充。没有成交的 all-WAIT 回放以 `NO_FILLS` 拒绝晋级；若训练阶段尚未通过，则先以 `TRAINING_NOT_ACCEPTED` 拒绝模拟准入。多会话最大回撤是每个独立平仓起始会话回撤的最大值，不等于连续账户回撤；`open_exposure` 按各会话独立检查，不能用正负库存相抵。

确定性重放比较策略决策、成交、会计与风险结果。原始引擎报告也保留实际 wall-clock 运行耗时，因此完整报告字节、测量耗时及其摘要不要求在多次执行间相等；不会为制造字节相同而删掉真实性能测量。

## 操作接口

所有命令在项目根运行；以下变量由操作员设置为真实路径和已生成身份，不包含真实密钥。

```sh
python -m ml.registry --store "$MODEL_STORE" install-policy --file "$ADMISSION_FILE"
python -m ml.train --manifest "$DATA_MANIFEST" --output "$TRAINING_RUN" --exporter build/trading-ml-replay
python -m ml.registry --store "$MODEL_STORE" register --model "$TRAINING_RUN/model.json" --manifest "$DATA_MANIFEST" --admission-sha256 "$ADMISSION_SHA256"
python -m ml.registry --store "$MODEL_STORE" evaluate --version "$MODEL_VERSION" --report "$TRAINING_RUN/report.json"
# 先按 AI_PIPELINE.md 运行真实 C++ 模拟生成报告，再提交证据。
python -m ml.registry --store "$MODEL_STORE" validate-simulation --version "$MODEL_VERSION" --report "$SIMULATION_REPORT"
python -m ml.registry --store "$MODEL_STORE" status
```

研究模型通过门槛后可显式执行 `activate --version "$MODEL_VERSION" --mode simulation`。这些命令不会连接券商。失败应保留原始证据并调查，不能编造通过报告。

paper 需要操作员拥有的私有 `0600` 批准 JSON，包含：`schema_version=1`、`action="approve_paper"`、`approved=true`、`version`、`model_sha256`、`policy_sha256`、`gates_sha256`、`data_manifest_sha256`、`validation_report_sha256`、`simulation_report_sha256`、`operator_id`、`issued_at_ns`、`data_authorization_confirmed=true`。身份、报告摘要必须与已验证证据完全相同，批准时间不得未来或超过一天。

```sh
python -m ml.registry --store "$MODEL_STORE" approve-paper --version "$MODEL_VERSION" --operator-file "$APPROVAL_FILE" --manifest "$DATA_MANIFEST"
python -m ml.registry --store "$MODEL_STORE" activate --version "$MODEL_VERSION" --mode paper --operator-file "$APPROVAL_FILE"
```

批准和激活分开。只有非合成、有授权声明且所有验证通过的产物能够走此流程；批准不代替 IBKR runner 的独立 paper、账户、行情和风控门禁。`load_approved_model(store, mode="paper")` 返回 `ApprovedModel(path, version, sha256, manifest)`，启动时重验全链并冻结该版本，模型本身没有重置风控或启动交易的权限。

回滚使用 `rollback --version TARGET --mode simulation|paper --operator-file FILE`。批准 `action="rollback"`，还必须绑定当前 `from_version`，目标仍需满足对应门槛，包括 paper 的年龄限制；不会删除旧模型或清除 ledger。生产系统应先停止接单并处理未完成订单，再由受控进程重启加载，注册表不会热替换运行中的策略。

## 漂移与定期再训练

`python -m ml.retrain` 是一次有界 tick，适合 cron/systemd timer。默认仅保存意图；只有 `--execute` 才启动真实 `ml.train` 子进程。单个 store 写锁串行化离线训练，推理和只读状态不取得此锁。

```sh
python -m ml.retrain --store "$MODEL_STORE" --base-version "$MODEL_VERSION" \
  --manifest "$NEW_DATA_MANIFEST" --window "$FEATURE_WINDOW" \
  --exporter build/trading-ml-replay --interval-seconds 86400
# 审核配置后，同样参数加 --execute 执行；合成研究数据需另加 --allow-synthetic。
```

窗口格式为 `schema_version=1, model_version, feature_schema_version="causal_book_v1", feature_names, observed_at_ns, samples`。names 顺序取 `ml.features.FEATURE_NAMES`，每个 samples 行是相同顺序的有限数值。最多 10,000 行，默认至少 200 行、时间不超过一小时；样本不足时 `drift_detected=null`，不会推断“稳定”。窗口采集接入需要由部署方提供，未配置窗口时只有定时检查，不能声称已实时监控线上漂移。

漂移使用仅训练集拟合的参考分布：标准化均值偏移默认 2.0，PSI 默认 0.2，满足任一个即产生意图。PSI 保留上下溢出桶并使用 0.5 平滑。漂移不证明模型失效，也不授权风险放行。没有新授权数据时返回 skipped/blocked；synthetic 必须显式允许且只能生成研究 candidate。

新数据必须同一标的、至少十个 session、未消费 test 源或 test 日期，新的 test 时间晚于已登记模型的 test。每个实际训练数据集和 test 身份在启动子进程前写入 attempts ledger；即便子进程失败，也不自动重复尝试。默认 dry-run 不消费尝试。模型训练的全部冻结配置均继承，包括 alpha 网格、seed、features、horizon、样本限制；输出配置若变化则拒绝登记。不得利用这个定时器反复调参测试同一 holdout。

真实运行保存 invocation、stdout/stderr、成功产物或 failure.json。默认超时 1,800 秒，最多 7,200 秒。成功仅创建新 candidate，并更新调度时间；不自动 evaluate、不自动 promote、不改变 active 指针。定时器应记录退出码，CLI 对 failed/blocked 返回 2；缺数据等跳过状态可返回 0，运维必须同时查看状态，不能把退出 0 等同于完成训练。

早期 `attempts.schema_version=1` 需要一次显式升级：`python -m ml.retrain --store "$MODEL_STORE" --migrate-attempts`。此命令不能与训练参数同时使用；它核对每个历史 attempt 的真实训练文件与已登记不可变 candidate 的文件 SHA、dataset/admission/manifest 和完整 split，重建 test 来源/日期键。成功时保存只读 `attempts.v1.<sha256>.json` 原始字节备份并原子写入 v2；重复运行幂等。存在失败、未知或未完整登记的历史尝试时拒绝迁移并保持原文件，必须调查并恢复证据，禁止删除 ledger 绕过 test 锁。

## 文件系统与部署边界

采用本地单机可信操作员 store，默认目录 `0700`、可变控制文件 `0600`、不可变证据 `0444`。读取禁止路径穿越、各级 symlink、FIFO/设备、重复 JSON key、NaN 和超限文档；registry root/内部关键目录及文件要求当前 UID 或 root 拥有且不可 group/world 写。父目录必须由部署方保证不可被非可信用户重命名替换；系统 `/tmp` 的 sticky bit 并非 store 的访问隔离替代品。

只读 Web 应挂载经过授权的 store 副本，使用 root 拥有且不可写的目录/文件或匹配服务 UID，**不可把模型目录改成 0777**。`read_status` 最多读取最近 50 模型、256 文件、合计 16 MiB，输出白名单摘要，不输出源绝对路径、系数、操作员批准文件或私有日志。存储过大或损坏返回 invalid，需运维处理；不是静默截断为全部通过。

SHA256 和本地只读权限用于发现损坏与约束流程，不抵御同 UID 恶意操作员，也不是签名证书或外部独立验证。生产需受控目录所有权、备份、独立批准身份及变更审计；多主机并发写入/NFS/分布式一致性未实现。备份必须包含 policies、证据、所有 model versions、active/history、holdout 和再训练 ledger，恢复后先只读校验，不能只复制 active model 而丢失来源与批准链。

## 验证

```sh
cmake -S engine -B build && cmake --build build
python -m unittest ml.tests.test_registry ml.tests.test_retrain -v
```

注册表单测使用明确的单元测试证据 fixtures 验证正负向门禁；licensed 类型 fixture 不构成实际授权行情或券商验收。再训练测试使用实际 C++ 导出器、合成 ITCH 和真实训练子进程，验证新 candidate 与“不自动激活”，并验证失败记录、重复数据阻止、漂移与不足样本。外部 paper 与 live 未由这些测试证明，当前 release 的实际证据以 `docs/VERIFICATION.md` 和 release artifacts 为准。
