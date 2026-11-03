# 模型注册库备份与恢复

`python -m ml.store_backup` 是本地运维命令，负责完整注册库的可校验归档、隔离恢复和只读展示快照。它不连接券商、不启动策略、不授予执行权限。数据库与媒体恢复仍按 [BACKUP_RESTORE.md](BACKUP_RESTORE.md) 执行，两者不能相互替代。

## 备份范围

归档保留注册库内的全部普通文件和目录，包括索引中已不可见的旧模型、每个模型的 JSON 权重与 manifest、预先登记的准入规则、评估证据、不可变事件与失败尝试、激活历史、holdout 消费账本、再训练请求与运行日志、共享研究预留账本。归档不只复制 `index.json` 指向的最新模型。

外部 ITCH/CSV 数据集、其源目录中的 holdout 锁、`ml.pipeline --run-dir` 位于注册库外的训练/回测产物与 stdout/stderr/receipt、券商账户状态、环境密钥与数据库 **不在本归档范围内**。这些内容需要独立备份和关联版本记录。manifest 的 `includes_registry_run_logs=true` 只承诺注册库内的运行日志，`external_pipeline_runs_included=false` 明确排除外部流水线目录。恢复注册库后，不得因外部数据锁缺失而把已经使用的测试区间重新当作独立测试集。

归档含私有模型及研究记录，默认权限为 `0600`，本工具不提供加密或离机存储。离机前使用已配置的加密备份设施，密钥与备份分离存储；不要上传到公共目录。SHA-256 是完整性校验，不是来源签名；期望 hash 应从独立可信记录获取。

## 创建一致备份

输出父目录须预先创建且属于当前运维用户；输出文件必须不存在。

```sh
mkdir -m 700 -p backups/models
python -m ml.store_backup backup \
  --store artifacts/ai-rc4/model-store \
  --output backups/models/rc4-model-store.tar.gz
```

成功时输出 JSON，包含 `state=backed_up`、`archive_sha256`、文件数、总字节数和实际验证结果。将该 JSON 与独立保存的 checksum 纳入变更记录。命令不会覆盖现有归档；计划任务应使用每次唯一的输出名称。

备份持有注册库相同的 writer lock，验证所有模型/事件/证据绑定，以及已接受评估与 holdout 账本的对应关系。归档前后再次核对源文件清单与内容，拒绝未经正常写入协议的并发变化。锁只约束遵循该协议的写入者，不能替代主机最小权限与文件系统保护。

## 恢复到全新目录

先在隔离环境恢复，目标目录必须不存在。以下 hash 占位符必须替换为备份时独立记录的实际值：

```sh
python -m ml.store_backup restore \
  --archive backups/models/rc4-model-store.tar.gz \
  --destination artifacts/recovered-rc4-model-store \
  --expected-sha256 实际归档SHA256
```

恢复流程在私有临时目录内检查归档，再验证完整注册库；每个文件写入后执行 fsync，发布前从最深层目录逐级 fsync。最终使用 Linux `renameat2(RENAME_NOREPLACE)` 原子发布到新目录并 fsync 目标父目录。拒绝覆盖和原地恢复；运行平台/文件系统必须支持所需语义。主机断电恢复仍须在实际存储上演练，工具测试不能替代实测 RPO/RTO。

恢复前后的归档哈希检查与解压使用同一打开的文件描述符，并核对路径 inode 未被替换。成员采用有界 USTAR 子集：拒绝路径穿越、符号/硬链接、FIFO、稀疏文件、PAX/GNU 扩展、重复/遗漏成员、未知文件、大小或 hash 不匹配。默认上限：4,096 个文件、单文件 16 MiB、总明文 256 MiB、压缩归档 300 MiB。超限必须先审查容量需求，不应关闭校验。

所有 `active-simulation.json` / `active-paper.json` 指针在发布前移入 `recovery-quarantine/`，其原始字节和历史保留。恢复目标根目录没有有效执行指针，`recovery-audit.json` 明确记录 `execution_authorized=false`。源注册库保持不变。恢复不延续旧的执行授权；恢复后仍需核对数据权限、模型时效、当前准入规则、账户/订单/持仓并执行已授权的审批流程。历史 schema 1 证据可以保留供审计，不能借备份恢复绕过当前 schema 2 的重新验证要求。

## Web 只读快照

```sh
python -m ml.store_backup snapshot \
  --store artifacts/ai-rc4/model-store \
  --destination artifacts/rc4-web-snapshot \
  --expected-reader-uid "$(id -u)"
```

快照经同一备份/恢复校验，文件权限 `0400`、目录 `0500`，执行指针隔离。`--expected-reader-uid` 必须与当前进程 UID 一致；命令不执行 sudo/chown。容器 Web 用户需要由部署流程显式配置同一只读身份/副本权限，并以只读 bind mount 挂载；不要放宽原始研究库权限来解决容器读取问题。也可在 `restore` 命令中使用 `--read-only --expected-reader-uid`。

## 演练与失败处理

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  python -m pytest ml/tests/test_store_backup.py -q
```

测试使用临时数据、实际数值训练和 C++ 导出器，同时覆盖归档攻击、账本遗漏、模型/证据篡改、发布失败和执行指针隔离。构建 `build/trading-ml-replay` 后再运行，避免因缺少二进制而跳过测试。测试中的合约模型/成交仅验证恢复语义，不是获利或券商可交易证据。

退出码 `0` 表示本命令完成；退出码 `2` 表示参数、完整性或文件系统操作失败。发布前失败不会产生目标目录，并清理本次未发布的临时恢复目录。若原子发布已完成、随后目标父目录 fsync 失败，命令明确报告 `published inactive`：目标已存在、没有执行指针，但掉电耐久性未确认。此时保留目标与原归档、检查存储并重新校验，不能自动覆盖或把失败返回当作可以删除目标的依据。备份归档同样可能在发布后父目录 fsync 失败时已存在，应保留并校验。不得删除 holdout 消费或研究预留记录来强制恢复通过。

每次正式演练记录归档 hash、恢复审计、模型文件 hash 对照、全部账本保留情况、无有效激活指针、只读页面可读性及耗时。仅归档创建成功不等于恢复已验证；本手册中的命令示例也不表示已在目标生产主机执行。
