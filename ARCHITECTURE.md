# Architecture

## 三条独立的运行路径

控制面使用 Django 5.2 LTS、PostgreSQL、Redis、Celery 和服务端 HTML。单体模块化设计减少跨服务一致性成本，auth、业务服务、ORM、队列执行分层，文件私有存储不经 Nginx 公开。

计算核心以 C++20 实现 ITCH 解码、订单簿、报价、单标的风险及确定性研究，提供 `trading-replay` CLI 和同一核心的 `libvector_core.so` C ABI。CLI 的 replay 模式只研究公共行情，simulation 模式明确创建模拟订单/成交和账本。现有内部容器会动态分配内存，属于正确性基线，未达到零分配 HFT 实现。

第三条路径是独立、显式授权的 `execution/auto_paper.py`：通过 C ABI 调用核心，使用官方 IBKR SDK 的真实接入/下单/撤单方法。它不由 Web 或 Celery 自动启动；默认预检不连接网络，live 始终拒绝。代码与本地测试替身完成不等于官方 SDK、broker 或实时行情联调通过。

```text
Browser → Nginx → Django → PostgreSQL (users / datasets / jobs / automations)
                           ↑                        ↓
                     Celery Beat → durable job → Redis → Worker
                           ↑                         ↓
                     outcome / audit ← result ← C++ replay / simulation

Authorized live ITCH multicast → Mold sequencing → C++ shared core
                                                      ↓
                                      quote → synchronous risk reservation
                                                      ↓
Independent Paper Runner → one-time send permit → Official IBKR SDK
        ↑                                             ↓
        └──────── normalized callbacks ← TWS / IB Gateway paper account
        |
        └→ private atomic status file → Django read-only operator page
```

## 一致性与故障恢复

- 任务先持久化，队列投递失败仍保留 job identity。用户用同一 Idempotency-Key 重试。
- Worker 通过数据库行锁准入；重复投递不能重复启动同一已执行任务。
- 文件 SHA-256 与大小在执行前再次校验。
- 运行状态具备 heartbeat、超时、取消、失联检查和有限重试。
- 注册邮箱采用持久 outbox，临时 broker/SMTP 失败可重试；数据库只存一次性令牌摘要。
- 数据库 schema 通过 migration 升级，runtime DML 角色与 migration DDL 角色分离。
- 删除先标记不可用，再由可重试任务清理文件；注销撤销会话、脱敏用户并清理关联数据。

研究 Worker 执行前验证文件大小/hash、用户/平台权限和任务状态，以参数数组启动无 shell 的真实引擎子进程。子进程使用受限环境、CPU/内存/文件大小上限及父进程死亡保护；这些不是完整安全沙箱，仍需容器和宿主机隔离。任务 JSON 必须通过结果契约校验后保存。

## StrategyAutomation 生命周期

`StrategyAutomation` 记录 owner、私有 dataset、不可变风险/仿真假设、周期、最大次数、generation、锁存状态和最近任务；`AutomationCommand` 保存不含密码的持久幂等回执。初始状态始终 `stopped`。

```text
create → stopped
           |
           | verified owner + password + unique command key
           v
        running → schedule one durable simulation job → account outcome
           |                       |
           | stop / max_runs        | feature/account/data gate
           v                       v
        stopped                  paused
                                   |
                        repeated failure / invalid config
                                   v
                                faulted
```

- 周期为 60–86400 秒，最大运行次数 1–1000；默认 300 秒和 10 次。次数按创建的尝试计算，包括失败。
- Celery beat 每 15 秒触发有界调度扫描。owner → dataset → platform → automation → job 使用一致锁顺序，数据库部分唯一约束禁止同一配置有两个 queued/running 任务。
- 不补发错过的历史周期，不并行重叠；队列故障保留同一 durable job，不反复消耗运行次数。
- Worker 再次检查 state、generation 和功能开关，旧授权任务不能在停止后继续准入。
- stop 增加 generation，取消尚未执行的任务并请求停止在途子进程；收到请求不等于任务已经结束。
- 模拟风险 Kill、无效估值、连续三轮失败/取消等触发锁存。恢复需人工检查、密码复核和显式确认，不恢复已消耗的运行预算。
- 重放旧 start 命令只返回原回执和当前状态，不会把已停止的配置重新启动。
- 每轮历史仿真建立独立账本，不延续上一轮持仓，也不累加成真实利润。

此处的研究调度锁存与实际执行器的 Kill 是不同对象。允许用户重新授权一轮历史仿真，不构成清除 paper C++ 核心 Kill 的权限。

## 交易不变量

风险参考模型在新单前检查并预留最坏方向库存；PendingCancel/Unknown 不释放额度；成交转移风险而非凭空减少风险；Kill 不允许策略自行复位。公共行情 B/Broken Trade 不直接修正用户持仓，真实持仓要由执行通道确认。

simulation 使用显式成交/排队/延迟假设，计入费用和回扣，保留期末未平库存及未完成订单，不虚构最后一笔平仓。估值无效时净/未实现 PnL 和 drawdown 不作为有效数值呈现。费用后结果不保证盈利。

Paper runner 目前仅支持单只 USD 股票、整数股、ISLAND 路由与正常连续交易的 DAY 限价单。账户必须独占、flat、无开放订单且满足当日初始损益确认；行情必须来自授权 live multicast，从实际 session 序号 1 与 S/O 连续构建。它不支持日中快照引导、缺口恢复、同日状态续接或自动市价平仓。启动条件、错误及退出语义见 [IBKR 文档](docs/IBKR.md) 与 [C ABI](engine/C_ABI.md)。

真实 SDK 回调只进入有界队列，单 owner 线程维护 C++ 状态与适配层账本。发送前必须预留风险并消费一次性 permit；发送结果未知保留预留。适配层 SQLite journal 持久化身份与审计，会增加 Architecture A 完整链路延迟，不能声称端到端亚毫秒。任何无法对账的情况均禁止新增风险；断开 socket 不代表撤单成功。

## 只读执行状态

Paper runner 可原子写入 0600 状态文件。Web 用 `PAPER_STATUS_PATH` 只读接入，校验 schema/kind、大小、类型、时间及允许字段；默认 5 秒超龄显示 stale。账户号、执行明细和未知字段不向页面传播。`/execution/` 与 `/api/v1/execution/status/` 仅供已验证、有效的运营角色查看；没有启动、停止或下单 API，`live_enabled` 始终为 false。该集成需要单独配置受控只读挂载，默认未接入外部状态。

## 配置和升级

APP_ENV 分为 development/testing/staging/production。部署环境强制 PostgreSQL、共享 Redis、HTTPS、异步 SMTP 和必要运营配置。FeatureFlag 对已实现功能做稳定用户百分比分流，不能替代权限或交易风控。

采用 expand → migrate/backfill → deploy → contract 的迁移方式。已有数据兼容性未验证时不执行破坏性 rollback；保留旧镜像、备份和前滚路径。多实例发布前还需校验共享会话、共享限流和 Worker 版本兼容。

任务投递携带 `contract_version=4`，旧 worker 应拒绝无法理解的授权/自动化契约。先部署 worker 支持，再开放新功能；回退前排空或隔离不兼容任务。Docker 构建额外检查模型与 migrations 无漂移；构建、迁移通过仍不能替代恢复和数据正确性验收。

未来原生 HFT hot path 必须采用独立连续事件循环和可测量协议入口，不能调用 Web、SQL、Redis 或 Python Worker。本仓库没有把批量回放耗时或本地函数耗时标为 IBKR／交易所 ACK 延迟。

## 离线研究和模型恢复（rc.4）

`ml.pipeline` 串联政策预声明、训练、登记、预测评估、C++ 回测和模拟评估。运行目录记录冻结请求与分阶段收据；本地注册库写锁保护 `research/reservations.json`，与周期再训练共享测试数据预留。完整输出可以复核恢复，不完整训练不会自动重跑，流水线不修改活动模型指针。

schema 2 模拟报告保留每个会话、每种情景的原始引擎账本及摘要。准入端核对整个锁定测试集、模型/数据/源码/引擎/参数身份，再重算汇总与压力结果。最终账户估值和实时行情有效性分离，缺失回撤路径持续保持无效。

`ml.store_backup` 在同一写锁下生成一致、受大小限制的归档。恢复到新目录并完成校验，随后隔离活动指针，再原子发布目录；Web 可以挂载只读快照。外部行情原始文件和其本地测试锁需单独备份。数据恢复和交易授权是独立操作。
