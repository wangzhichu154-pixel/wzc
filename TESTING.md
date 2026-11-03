# Testing

最新验收为 [rc.4](docs/VERIFICATION_RC4.md)：408 pytest + 172 subtests、34 运维/安全 unittest、81 执行器 tests、C++ 5 suites，及真实模型的七尺寸浏览器测试。完整首次失败与最终回归证据均保存。各节旧数字仅代表对应历史版本；不表示真实券商或正式生产已通过。

新增重点回归包括训练前跨入口测试区间预留、子进程中断恢复、旧 gate attempt 拒绝、完整测试集与原始账本重算、成本/延迟压力门槛、归档攻击与无激活恢复、扫描证据身份/覆盖/时效/未知严重度。API 固定窗口限流边界使用受控时钟验证，仍运行真实缓存与密码哈希，不依赖测试恰好落在某一分钟内。

## 单元与集成

```sh
.venv/bin/cmake -S engine -B build -DCMAKE_BUILD_TYPE=Release
.venv/bin/cmake --build build -j2
.venv/bin/ctest --test-dir build --output-on-failure
.venv/bin/python -m pytest -q
.venv/bin/python -m unittest discover -s scripts/tests -v
.venv/bin/python -m unittest discover -s execution/tests -p 'test_*.py' -v
.venv/bin/cmake -S execution -B build-execution
.venv/bin/cmake --build build-execution
.venv/bin/ctest --test-dir build-execution --output-on-failure
.venv/bin/ruff check .
.venv/bin/python backend/manage.py makemigrations --check --dry-run --settings=config.testing
.venv/bin/python -m pip check
```

源码中的合成 ITCH、测试账户和测试替身只用于隔离测试/故障注入，不是生产 UI 的数据来源。真实引擎测试要求 binary 存在；跳过该测试的结果不能算闭环通过。

引擎构建还生成真实 `libvector_core.so`。CTest 检查核心、C ABI 和可由 C 编译器使用的头文件；存在 Python 解释器时也执行 CLI 测试。`execution/tests` 使用真实共享库和仅位于测试目录的 SDK 边界替身，检查风险预留、发送、部分成交、费用、更正/未知状态、撤单及失效保护；这不构成官方 SDK 或真实 broker 联调。

Docker 构建以非 root 身份执行 `makemigrations --check --dry-run --settings=config.testing`，防止模型文件与 migrations 漂移的镜像进入运行阶段。该检查不能替代已有数据升级、数据库约束和恢复验证。

## 自动运行生命周期

必须覆盖以下可观察行为：

- 创建配置保持 stopped；未验证、无权限、密码错误或 feature gate 不满足不能启动。
- 参数在创建时快照；任务以真实 `mode=simulation` 执行，不把历史公共成交当成本账户成交。
- 每个配置只有一个 queued/running 任务，调度锁和数据库唯一约束共同防止重叠。
- 队列投递失败保留任务 identity，重试同一命令不增加无关运行次数。
- 停止增加 generation，旧授权任务在 worker 准入时被拒绝；已执行任务的取消必须观察到终态。
- 已停止后重放旧 start 命令不得重新启动。
- 模拟风险 Kill、无效估值和连续失败锁存；恢复需要明确确认且不重置运行预算。
- 用户/数据集删除、平台暂停与功能开关变化不能留下继续执行的未授权任务。
- 每轮是独立历史模拟账户，不累加重复输入为真实利润。

## PostgreSQL / Redis

通过专用测试数据库连接设置 DATABASE_URL，Redis 测试数据库设置 REDIS_URL 与 TEST_USE_REDIS=1；CI 的数据库只能用于测试，不允许指向生产。SQLite 不验证 PostgreSQL row-lock 行为。

## 浏览器 E2E

```sh
PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/ms-playwright" .venv/bin/python -m playwright install chromium
PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/ms-playwright" .venv/bin/python tests/e2e/run.py
```

`tests/e2e/run.py` 启动 loopback-only Django 测试服务器，创建独立临时 SQLite、测试邮件和上传目录，应用真实 migration；不会采用生产 DATABASE_URL。Celery eager 模式执行真实 Worker 和 C++ 子进程；这不验证真实 Redis/worker 并发或 SMTP 投递。

流程包含注册、读取本地测试邮件、显式验证、登录、上传二进制、真实 replay、真实 simulation、创建/启动自动配置、执行调度、查询关联任务、停止和退出。测试使用真实页面和 API，不 mock 页面成功状态。

响应式检查 1920×1080、1440×900、1366×768、1024×768、768×1024、390×844、375×667，共八类页面：dashboard、dataset、replay、simulation、account、risk、automation、about。检查主标题、水平溢出、移动导航和浏览器错误。产物保存在忽略的 `artifacts/e2e/`；不保存带令牌的浏览器 trace。脚本存在不等于执行通过。

当前已报告结果：168 项 pytest 测试和 21 个子测试通过；C++ 60 项检查通过；E2E 真实 C++ 与自动运行流程通过，7 个尺寸 × 8 个页面，浏览器错误 0。具体版本、命令及后续环境验证以 [验证记录](docs/VERIFICATION.md) 为准。

## 性能

`engine/benchmark/` 测完整文件回放。报告包含工作负载、二进制SHA256、CPU、样本数、P50/P90/P99/P99.9/Max和失败数。它不测网络交易链路。API压力测试须先确定目标并发/数据量，报告错误率、连接池、Worker积压和尾延迟；不接受只报告Average。

## 安全及恢复

```sh
.venv/bin/python -m pip_audit -r requirements.txt --strict
python3 scripts/backup.py --help
python3 scripts/restore_verify.py --help
```

验证缺口、重复回报、失联、超时、Queue故障、哈希不匹配、越权、CSRF、重置令牌重放、会话撤销、注销及备份恢复。安全扫描网络不可用时记录“未完成”，不得记为无漏洞。

运维脚本的安全测试涵盖凭据权限/随机性/拒绝覆盖、符号链接、备份校验、tar traversal，以及 Compose 将 tmpfs 选项错误解析成独立挂载路径的回归。它们不代替真实 `pg_dump → pg_restore`、业务数据核对或媒体恢复。

只读执行状态应测试普通用户拒绝、允许字段过滤、错误 schema、符号链接、未来时间、陈旧文件、无效 PnL 与未配置状态。状态 API 返回成功不等于 broker 已连接或持仓完成对账。

真实部署还需独立验证 Docker 服务健康、最小权限数据库角色、Celery/Redis、监控抓取/告警通知、备份恢复、SMTP、TLS、Staging 与目标负载。IBKR 还需要批准的 SDK、真实 Gateway、纸账户、获授权 live feed 与受控接入验收；本地替身不充当这些证据。

实际命令结果与未完成项写入 `docs/VERIFICATION.md`，不将计划测试当作执行证据。
