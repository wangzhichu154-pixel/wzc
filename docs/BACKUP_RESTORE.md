# 备份与恢复运行手册

数据库、私有媒体、配置密钥和审计保留是不同恢复对象。Redis 不是业务事实的唯一持久来源；数据库 outbox 和任务账本应允许重新调度。不要只备份 Redis 就声称可恢复系统。

## 先选择正确部署

所有脚本通过项目目录中的 Docker Compose 操作服务，默认选择该目录 `.env`。生产使用独立 `.env.production` 时，**必须先显式选择生产环境和同一个 project name**，否则可能备份到开发实例：

```sh
export COMPOSE_ENV_FILES=.env.production
export COMPOSE_FILE=docker-compose.yml:infrastructure/deployment/compose.production.yml
export COMPOSE_PROJECT_NAME=实际生产项目名
docker compose ps
```

以上变量只指定配置位置和项目身份，不包含密钥。先核实容器和数据库实例，再执行后续备份、停止写入与恢复操作；脚本继承这些 Compose 变量。systemd unit 需配置同样的 `Environment=COMPOSE_ENV_FILES=...`、`Environment=COMPOSE_FILE=...` 和项目名，不应直接把本地示例安装到生产。

## 数据库备份

```sh
python scripts/backup.py --output backups/daily --keep-days 14
python scripts/backup.py --output backups/weekly --keep-days 84
```

`pg_dump` 生成一致性快照，custom archive 内部压缩，先写 0600 临时文件，再完成 SHA-256、大小和 manifest。工具不读取/打印 `.env`；凭据由数据库容器提供。manifest 明确 `restore_verified=false`，TOC 校验不等于恢复成功。

可安装 `age` 并指定真实公钥进行离机前加密：

```sh
python scripts/backup.py --output backups/daily --keep-days 14 --age-recipient '实际AGE公钥'
```

加密私钥存于独立受控位置，定期演练在主机丢失时仍能恢复。公钥不是密钥。没有启用加密的备份必须放在受控加密存储，禁止上传到公共对象存储。

`infrastructure/deployment/trading-backup-*.service/timer` 提供每日和每周完整备份调度。运维方创建 `trading-ops` 用户、限定目录、Docker 访问，并按实际安装路径修订 unit 后启用。每日与每周都是完整逻辑快照；保留周期分别 14/84 天。需要更小 RPO 时配置 PostgreSQL WAL 归档/PITR；当前逻辑备份不能提供任意时间点恢复。

## 独立数据库恢复验证

```sh
python scripts/restore_verify.py backups/daily/实际备份.json
python scripts/restore_verify.py backups/daily/实际加密备份.json --identity /受控位置/age-identity
```

工具先校验文件名、大小和 checksum，再创建随机 `restored_*` 数据库，恢复 archive，检查迁移历史和当前应用可读性。成功生成 `.restore-report.json`，默认删除临时数据库。失败也尝试清理本次创建的数据库。

**脚本从不覆盖 `trading`，不会接受任意目标名称。** 需要保留恢复结果时：

```sh
python scripts/restore_verify.py backups/daily/实际备份.json --target-db restored_incident_001 --keep
```

保留结果后，先对恢复库运行业务只读核对和迁移兼容检查，再在维护窗口切换运行/迁移 DSN。切换前停止所有旧写入者。应用账本、数据集引用、用户权限及任务记录均需核对。脚本验证数据库恢复，不伪称业务 E2E 或媒体恢复通过。

## 一致的数据库 + 私有媒体快照

当前本地 volume 方案以受控维护窗口保证一致性。没有实现跨数据库与文件系统的在线原子快照。

```sh
docker compose stop backend worker scheduler
python scripts/backup.py --output backups/consistent --keep-days 14
python scripts/backup_media.py --output backups/consistent
docker compose up -d backend worker scheduler
```

将两份 manifest 关联到同一个变更/备份记录。出现备份错误时先保存故障证据，再恢复服务；不要让自动脚本忽略错误后把不完整快照标记完成。`backup_media.py` 会拒绝在已知写入服务仍运行时执行；若存在独立写入者，运维方也必须停写。备份媒体归档是私有数据，离机传输前加密；媒体清理需与隐私保留策略一起配置。

媒体恢复不碰现有卷：

```sh
python scripts/restore_media.py backups/consistent/实际媒体备份.json
python scripts/restore_media.py backups/consistent/实际媒体备份.json --keep
```

工具检查 archive checksum、成员路径、链接/特殊文件和总解压大小，在新建的 `restored_media_*` volume 内恢复，默认验证后删除。保留时，由运维方在维护窗口显式配置该卷作为应用媒体卷，再核对数据库引用的文件及内容 hash；不能把空目录绑定替换为已恢复成功。

## 生产要求与限制

- 本地备份不能防主机损坏；需受控离机复制、访问审计和恢复凭据。
- 保留策略覆盖普通备份、周备份、手工事故快照和云端版本；注销数据可能在到期前留于受限备份，必须在隐私政策说明，并防止恢复后重新激活已删除账户。
- 每周至少进行自动恢复演练，定期人工执行业务核对；记录备份年龄、恢复耗时与最大可接受数据损失。
- RPO/RTO 是实测指标，不因脚本存在而自动达标。
- 恢复时先隔离网络和外部写入，尤其不得因恢复任务/订单记录而重放真实外部交易。
- 恢复数据库不包含 broker 实际订单状态；金融交易恢复必须额外完成执行渠道对账，保持 DISARMED。
- 当前脚本的执行结果与测试记录必须单独保存；本文件不声明已在目标生产环境完成演练。
