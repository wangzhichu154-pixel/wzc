# 可恢复的离线研究流水线

`ml.pipeline` 把已有真实组件组合成一个可记录、可恢复的研究任务：

```text
冻结请求及预声明门槛
  → 安装 policy（早于训练开始）
  → 真正调用 ml.train
  → 登记模型 candidate
  → 预测评估
  → 真正调用 ml.backtest / C++ FIFO
  → 保存模拟准入结果
```

预测门槛被拒绝时，仍执行回测以收集真实证据。`completed` 只表示研究流程完成，须另读 `admission_passed` 和两个 gate 的 `state`。零成交、亏损、不完整估值或其他门槛拒绝不是软件崩溃；子进程失败、超时、证据不匹配是独立的失败状态。

流水线从不调用 `activate`、`approve-paper`、broker 或 Live，不设置活动模型指针。即使所有研究门槛通过，`automatic_promotion` 和 `execution_authorized` 仍是 false。合成模型的 Paper 禁用规则继续由 Registry 独立执行。

## 一次完整运行

安装项目依赖及 `requirements-ml.txt`，编译 `build/trading-ml-replay`。先按 [AI_PIPELINE.md](AI_PIPELINE.md) 准备有权使用的单标的 ITCH 文件，并用 `ml.prepare` 得到核对 SHA 的 `manifest.json`。训练至少需要十个独立会话。不得把缺少授权的数据重命名为已授权来源。

`ml/pipeline.example.json` 是完整、显式的训练和特征配置；`ml/admission.example.json` 是研究示例费用与门槛，不能当作真实账户授权。操作员应在运行前确定配置，不能读完测试结果后调整参数再复用同一测试区间。

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m ml.pipeline \
  --manifest /absolute/data/manifest.json \
  --run-dir /absolute/research/new-run \
  --store /absolute/research/model-store \
  --admission ml/admission.example.json \
  --config ml/pipeline.example.json \
  --binary build/trading-ml-replay
```

新运行要求不存在的 `--run-dir`。Pipeline 以非阻塞 `flock` 限制同一个运行目录只有一个 writer；重复调度不会堆积等待。模型库自身另有 writer 锁。它们是本机文件系统机制，不支持 NFS 或分布式多主节点一致性。

任何子进程启动前，Pipeline 在模型库 writer 锁内检查已注册候选（即使尚未评估）、`holdouts.json`、周期再训练的 `retrain/attempts.json` 和共享 `research/reservations.json`。按测试数据 SHA 与 `symbol + session date` 双重排重，并要求该标的测试区间晚于已消费/预留区间。随后原子写入共享请求预留；周期/漂移再训练使用同一机制。因此改运行目录、修改描述、改变种子、由 Pipeline 换成 Retrain，都不能重新使用已预留测试集。训练失败也保留预留。

共享账本 schema 1 的 `requests` 以 `pipeline:<request sha>` 或 `retrain:<request sha>` 为键，绑定数据/manifest SHA、完整测试会话、SHA/date keys 和首次预留时间。只有原冻结请求的 `--resume` 可识别自有预留；这不授予重跑已有训练阶段的权限。完整模型库备份必须保留此账本。外置的 `--run-dir`（日志、模型、报告、receipt）还须按独立研究产物备份，模型库备份不能替代它。

结果目录：

```text
new-run/
  request.json       # 数据/manifest、policy、参数、引擎、源码、依赖和路径身份
  admission.json     # 本次冻结的门槛副本
  reservation.json   # 模型库共享训练前预留的精确副本
  status.json        # 原子更新；摘要不能代替真实产物
  stages/
    policy/
    training/
    registration/
    prediction_gate/
    backtest/
    simulation_gate/
      invocation.json
      gate-before.json # 仅 gate：调用前 latest attempt SHA，防复用旧结果
      process.json   # 实际 PID、Linux start ticks 与 boot identity
      stdout.log
      stderr.log
      exit.json      # 实际退出/超时；不会伪造缺失退出码
      receipt.json   # 输出及日志逐文件 SHA、核验结果
  training/          # model/report/config/data-manifest/train.log
  backtest/          # 原始 C++ 输出、方向文件、输入绑定和最终报告
```

正常完成 CLI 返回 0，包括候选被业务门槛拒绝的情况；依赖/数据/产物/运行失败返回 2；同一运行正在被另一进程持有返回 4。调度器应按 JSON 的 state、stage state、admission_passed 和 failure_codes 监控，不能把 shell exit 0 当作可交易批准。

## 恢复与幂等

```sh
# 参数、路径和源数据必须与原运行一致；这里只额外增加 --resume。
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m ml.pipeline \
  --manifest /absolute/data/manifest.json \
  --run-dir /absolute/research/new-run \
  --store /absolute/research/model-store \
  --admission ml/admission.example.json \
  --config ml/pipeline.example.json \
  --binary build/trading-ml-replay \
  --resume
```

- 每次恢复重新核对当前数据、policy、配置、依赖、C++ 可执行文件及相关 Python 源码身份。已消费测试集不能因恢复操作而换模型、换门槛或换实现。
- 有 receipt 的阶段重新核对所有产物、日志与 Registry 证据，不再启动子进程；改写 `status.json` 为成功不能绕过真实核验。
- 崩溃发生在完整训练输出之后、receipt 写入之前时，必须核对模型自身摘要、原始数据、完整 config/report 和末尾 `training_complete` 日志，才能接纳已有产物；不会重新训练。
- 没有记录 exit.json 的完整产物恢复会显式标记 `recovered_unknown_exit=true`。这不是对原子进程退出成功的推断。
- Gate 在调用前先保存 latest attempt 身份快照，随后仅接受身份已变化、且本次 invocation 开始后、已记录 exit 之前生成的 Registry attempt，并核对对应不可变 attempt 文件和精确报告 SHA。较早的同报告拒绝记录不能冒充当前 gate 完成；记录的退出码必须精确为通过 `0` 或正常门槛拒绝 `2`。其他退出、超时或证据缺失都视为程序失败。
- 若相同 PID/start ticks/boot identity 的子进程仍在运行，恢复被阻止，避免封存仍在写入的产物。
- 输出不完整、子进程失败或超时会保留文件并停止。不要删除目录、测试消费锁或 Registry holdout 台账来绕过限制。先检查日志，恢复可信完整证据；否则取得新的独立测试数据并创建新实验。
- 每个阶段最多运行配置的 timeout；超时终止整个子进程组。目录大小和文件数量有边界，任务仍应运行在受控磁盘配额、CPU/内存限制与备份策略下。

Pipeline 保存本地路径与训练 stdout/stderr，属于操作员资料，不通过 Web 状态 API直接暴露。目录必须由操作员/root 拥有且不可组/其他用户写入；拒绝符号链接、特殊文件和不安全控制文件。SHA 与权限不防御恶意同 UID 操作员，生产应配合独立身份与变更审计。

## 调度建议

可让受控 scheduler/systemd 每次选取新的、已准备好的数据 manifest 和唯一 run-dir，再执行上述命令。固定一个已完成 run 的 `--resume` 只做核验，不代表读取到了新行情。参数与路径应由可信配置生成，不拼接用户提供的 shell 文本。

服务应设置专用用户、`UMask=0077`、工作目录、资源上限和读写目录白名单；训练密钥不应出现在命令行。当前文档没有启用任何永久 scheduler，也没有暗中部署自动交易。已有 `ml.retrain` 仍提供周期/漂移触发和新 candidate 生成，使用方法见 [ML_LIFECYCLE.md](ML_LIFECYCLE.md)。
