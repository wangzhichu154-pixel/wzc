# v1.0.0-rc.1 实际验证记录

验证日期：2026-11-01（UTC）。环境：本地 Linux 托管工作区，3 个可见逻辑 CPU；并非交易所机房或正式用户环境。本文记录运行结果，不替代 Production 发布批准。

## 已完成的产品链路

已交付可运行的账户/权限系统、私有 ITCH 上传、真实 C++ 回放、做市仿真、自动历史仿真调度、风险配置、模拟成交与费用后损益、管理后台、反馈、审计、账户安全和隐私操作。各轮历史仿真是独立实验，不累计重复回放为真实盈利。

另外交付独立 IBKR paper 自动执行代码：真实 SDK 调用接口连接 C++ C ABI 同步风控，包含订单预留、成交/佣金记账、故障撤单和状态投影。实际 SDK/账户/行情联调尚未完成。Web 只读观察该进程；不能通过会员身份或页面开关取得实盘发单权限。Live 始终拒绝。

## 测试结果

| 验证 | 实际结果 | 范围与边界 |
|---|---|---|
| Python 应用测试 | 168 passed，另 21 subtests passed | 隔离 SQLite；包括真正执行 C++ 子进程的测试，不等于 PostgreSQL 并发验证 |
| PostgreSQL / Redis 全套回归 | 168 passed，另 21 subtests passed，39.68 秒 | 本地真实 PostgreSQL 随机命名隔离测试库、Redis 独立 DB15；不使用业务库，测试库自动清理 |
| 应用覆盖率 | 81%，2,569 条可执行语句，479 未覆盖 | 已排除 tests、migrations；子进程不计入 Python coverage，不声称全部路径覆盖 |
| C++ Release | CTest 4 suites 全通过；内部 27 core + 16 ABI + 1 C header + 16 CLI = 60 checks | 协议、簿、风险、晚到成交、C ABI、确定性 CLI |
| C++ sanitizers | AddressSanitizer / UndefinedBehaviorSanitizer 通过 | LeakSanitizer 受环境 ptrace 限制，未声称通过 |
| Execution | 38 tests passed，0 skipped | 真实当前共享库 + 仅测试使用的 SDK 边界替身；没有连接券商或下单 |
| 运维脚本 | 7 tests passed | 私有配置、拒绝覆盖、损坏备份、路径穿越、危险 tar 成员、Compose tmpfs |
| Lint / migrations | Ruff 通过；makemigrations --check --dry-run 无漂移 | 镜像构建再次执行 migration drift 检查 |
| Chromium E2E | 9 类流程通过，browser_errors = [] | 注册→文件邮件验证→登录→上传→真实回放/仿真→自动启停→权限拒绝→退出 |
| 响应式 | 7 种分辨率 × 8 页面，无 document overflow | 1920×1080、1440×900、1366×768、1024×768、768×1024、390×844、375×667；不是全浏览器无障碍认证 |

浏览器使用临时数据库、真正的本地文件邮件和真实 C++ 引擎。合成 ITCH 仅为明确标注的协议测试输入，未伪装成商业行情。E2E 不连接 SMTP 服务、不验证 broker 成交。

## 实际部署与恢复

- Docker 多阶段构建成功，运行非 root、只读根文件系统；构建含 C++ 测试与迁移一致性检查。
- 在实际 PostgreSQL 16、带认证 Redis 7.4、独立 Celery Worker 中完成上传→任务队列→C++→持久结果联调，8 条协议消息结果正确。数据库运行角色执行 DDL 被拒绝。
- Nginx 升级验收发现旧后端地址缓存导致持续 502；已改用 Docker DNS 动态解析。故障证据与修复后的检查分开记录，未把 502 计算成成功请求。
- Prometheus 实际抓取 application、node、PostgreSQL、Redis 四个目标，全部 `up`。内部网络导致本机监控端口未发布的问题已修复。尚未接通外部告警接收人。
- 对真实本地数据库运行 pg_dump，并恢复到新建 `restored_*` 测试库；26 条迁移历史一致，pending migrations = 0，测试库随后删除。一次恢复耗时约 3.97 秒，数据量很小，不能据此外推生产 RTO。
- 停止 backend/worker/scheduler 写入后备份私有媒体，在新建隔离 volume 恢复 6 个目录/文件成员、206 字节，并删除测试卷；原媒体卷未覆盖。
- 恢复脚本测试不等于基于完整生产数据的灾备演练，也不包括 broker 持仓/订单恢复。开发联调账户和文件已按明确测试标记清理。

原始本地结果保存在 `artifacts/`，该目录可能包含私有测试备份，默认不进入源码交付包。筛选后的无凭据证据位于 `docs/evidence/`。

## 性能实测

1. Web 健康检查：本地 Nginx→Django→PostgreSQL/Redis readiness，10 requests/s，共 300 请求，300 个 HTTP 200，拒绝准入 0。完成时间相对于计划发起时间：P50 11.721 ms、P90 16.054 ms、P99 31.941 ms、P99.9/Max 66.757 ms。仅代表这次轻量负载，不能解释为业务容量或交易延迟。
2. C++ 合成输入：50,003 条消息、1,510,055 字节，100 次独立完整文件回放，输出 checksum 一致、失败 0；进程总耗时 P50 17.274 ms / P99 24.954 ms，内部文件处理 P50 14.282 ms / P99 21.006 ms，吞吐中位数约 350 万消息/秒。期间有其他本地服务运行；100 个样本无法稳定估计 P99.9。
3. 完整二进制/输入 SHA-256、平台与分位数见 `engine/benchmark/baseline-synthetic.json`。这些是整文件回放结果，**不是每 tick 的 NIC→Decision、Tick-to-Send 或 Tick-to-ACK**。IBKR、互联网和交易所延迟没有测量。

## 安全扫描与发布门槛

运行时 Python 依赖 closure 的 pip-audit 检查未报告已知漏洞。这不涵盖 OS 软件包、未知漏洞、业务逻辑或真实攻击面测试。

首次 bookworm 镜像 Trivy 扫描报告 56 个包/CVE 命中（17 个唯一 CVE，含 2 个 Critical 命中）。因此升级到 Debian trixie 基础镜像、安装当前可用安全更新，并移除 Web 不需要的系统 libpq/LDAP 依赖；psycopg 使用固定 binary wheel。最终镜像扫描结论单列于 `SECURITY_FINDINGS.md`。未增加忽略规则来使 CI 强行通过。

最终 Trivy 0.76.1 复扫：0 Critical、46 High 包/CVE命中，10 个唯一 CVE，发行版尚无 FixedVersion；发布门槛保持未通过。最终镜像已重新部署，本地 readiness 为 200、四类监控目标正常、真实任务链路再次成功。

以下条件未完成，**不能将本候选标记为正式 Production v1.0.0 或实盘上线成功**：

1. 最终镜像漏洞门槛关闭、部署目标的安全测试与正式责任人审查。
2. 实际 Staging/Production 域名、可信 TLS、真实 SMTP 投递、主体信息与用户政策。
3. 不可变镜像发布、生产权限/凭据、离机加密备份、目标数据量恢复、通知渠道和灰度/回退演练。
4. 官方 IBKR SDK 与具体 Gateway 版本兼容性、真实 paper 账户、交易权限、contract route、market rule 和授权实时 ITCH 源。
5. paper 场景的市场时段、网络断连、未知订单、持仓/成交对账和延迟实测；当前不支持非 flat 启动、同日自动重启或 gap 自动恢复。
6. 实盘执行仍未实现并始终拒绝。零分配 HFT 数据结构、完整 multicast 恢复、多标的策略组合、实际机房延迟与可盈利性均未得到验证。

不提供付费订阅，支付和收入不适用。外部服务缺失不会返回伪成功；没有收益承诺或默认打开的真实资金自动交易。
