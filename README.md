# Vector · 矢量交易

基于美股做市系统方案实现的 C++20 行情研究引擎与 Django 运营平台。

当前 rc.4 完善 AI 离线训练流水线、逐会话评估校验和模型库备份恢复。真实数据、真实 IBKR Paper 与实盘仍未验收。入口见 [AI 升级审计](docs/AI_UPGRADE_AUDIT.md)、[流水线手册](docs/ML_PIPELINE_OPS.md)、[模型备份恢复](docs/MODEL_BACKUP.md)及[本轮验收记录](docs/VERIFICATION_RC4.md)。

**交付状态：1.0.0 候选工程，尚未通过实盘生产发布验收。** 已存在可运行代码、真实持久化、真实 ITCH 文件处理与自动化测试；不是静态页面或伪后端。它也不是已获盈利验证的自动实盘交易系统。外部接入、低延迟目标、托管部署与运营配置有独立门槛，见 [发布清单](docs/RELEASE_CHECKLIST.md)。

## 已实现的业务

| 能力 | 实际行为与边界 |
|---|---|
| 用户与权限 | 邮箱注册/验证、Argon2 密码、登录/退出、密码恢复、设备撤销、会话期限；会员身份与管理员权限分离，所有数据在服务端检查归属 |
| 私有行情数据 | 真实 ITCH 文件上传，流式大小/格式校验、SHA-256、配额、鉴权下载和异步删除 |
| 行情研究回放 | 持久任务经 Celery 调用真实 C++20 解析器和订单簿；没有账户成交时不伪造 PnL |
| 单次做市仿真 | 明确选择 simulation，执行自动报价、延迟、撤单、模拟成交、库存、费用/回扣和净 PnL 计算；估值无效时隐藏无效收益 |
| 成本感知报价 | 将每股费用、最低佣金、滑点缓冲与目标净边际纳入报价距离；最低佣金按每单累计成交量增量计费，回扣不缩窄报价 |
| 收益评估 | 对同模型/参数的真实模拟输出保存不可变评估，拒绝重复行情累计、核验净盈亏与最低费用，并显示无返佣和额外成本压力；无成交、敞口与样本不足单独分类 |
| 自动运行 | `StrategyAutomation` 持久化周期历史仿真：创建后停止，密码复核启动/停止，次数限制、无重叠调度、幂等命令、故障与风险锁存 |
| 运营与隐私 | 管理后台、反馈工单、稳定用户分组 Feature Flag、研究暂停、账户导出/注销及操作审计 |
| 独立 Paper 执行代码 | `execution/auto_paper.py` 调用真实官方 SDK 下单/撤单接口并使用 C++ 共享核心；默认不连接，满足显式纸账户、live feed、对账等门槛才允许 paper 发送 |
| 执行状态只读展示 | 运营用户的 `/execution/` 读取独立 paper 进程输出；未配置、陈旧或非法状态显示不可用，网页没有交易启动按钮 |
| AI 训练与验证 | C++ 导出并重验原始行情；因果特征、Ridge、滚动验证、测试集锁、JSON 模型与可复现日志；合成模型不可进入券商 Paper |
| AI 模拟与运营 | `/ai/` 只读展示真实训练/模拟证据；方向过滤、成本门槛、模型登记、漂移与再训练流程；发布失败保持原模型/停止 |
| 交付设施 | PostgreSQL migrations/角色分权、Redis、Docker、Nginx、监控、备份/隔离恢复工具和 CI |

**历史仿真、IBKR paper 与 live 是不同模式。** 自动运行对同一历史输入每轮建立独立模拟账户，不把重复运行的结果累加成真实收益。Paper 发送代码存在不等于真实 SDK、Gateway、账户与行情联调已经验收；本次未连接真实 broker、未提交真实账户查询或订单。Live 始终拒绝，普通用户注册或会员身份不产生交易授权，任何模式都不保证盈利。

## 快速启动

需要 Linux、Docker Engine 与 Compose v2、Python 3.12（用于生成本地配置）。

```sh
python3 scripts/init_env.py
docker compose up -d --build
docker compose exec backend python manage.py createsuperuser
```

浏览器访问 `http://localhost:8080`。本地入口只绑定 loopback；开发 HTTP 不代表正式 HTTPS 验收。首次启动执行专用 migration 服务，应用运行角色没有建表权限。

`init_env.py` 生成随机密钥和数据库凭据，不覆盖已有配置；`.env` 与 `.secrets/` 不进入 Git。生产需设置 SMTP、合法主体、支持邮箱、域名、TLS、备份加密与部署目标，参见 [部署手册](docs/DEPLOYMENT.md)。不要将本地配置直接用于真实用户环境。

开发邮箱默认写入私有 `email_data` 卷的 `/app/emails`，由本地运维通过容器访问，不将验证或重置链接写入共享 stdout。生产配置验证拒绝 console/file 邮件后端；真实邮件送达和反垃圾配置需要用实际域名验证。

## 完整研究流程

1. 注册、完成邮箱验证并登录；上传有权使用的 ITCH 文件。
2. 在风险设置页配置单笔尺寸、tick、库存、订单频率和模拟损失限制。
3. 创建 replay 或 simulation 任务，查看真实任务状态、有效参数、账本结果与错误原因。
4. 在 `/automations/` 保存数据集、间隔、运行次数及模拟假设；配置初始状态为停止。
5. 输入当前密码明确启动。调度器创建真实仿真任务，同一配置同时最多一个未完成任务；任务使用不可变参数快照。
6. 在详情页检查每轮独立结果。停止会阻止后续调度并请求取消在途任务；是否结束以任务终态为准。
7. 模拟损失熔断、无效估值或连续失败会暂停/锁存；只有检查原因、重新认证并明确确认后才能恢复，不由后台自动重置。

在 `/assessments/` 选择已完成的模拟任务，检查扣费后的样本结果和额外成本压力。正收益研究样本不会授予执行权限，也不能代替样本外验证；详见[自动交易与收益验证](docs/AUTOMATED_TRADING.md)。

研究暂停或自动仿真停止不等于 broker Cancel All。独立 paper 工具的接入方式、身份验证、退出与对账限制见 [IBKR 文档](docs/IBKR.md)；不要把历史文件作为 paper 的实时行情来源。

## 本地开发与验证

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -r requirements-ml.txt
.venv/bin/cmake -S engine -B build -DCMAKE_BUILD_TYPE=Release
.venv/bin/cmake --build build -j2
.venv/bin/ctest --test-dir build --output-on-failure
.venv/bin/python -m pytest -q
.venv/bin/python -m unittest discover -s scripts/tests -v
.venv/bin/ruff check .
```

测试设置自动创建隔离密钥，默认使用隔离 SQLite；PostgreSQL、共享 Redis、真实 Worker 和浏览器验收单独运行，不能用 SQLite 测试冒充生产并发验证。

命令行处理自己有权使用的行情记录：

```sh
build/trading-replay --input /path/to/capture.itch
```

输入为每条 ITCH 消息前有两字节大端长度的未压缩记录，不是 PCAP、ZIP 或 IBKR 行情。详见 [引擎协议与限制](engine/README.md)。缺少会话起点或非法 reference 时不会编造完整盘口。

## 工程结构

```text
backend/config/       环境配置、URL、WSGI、Celery
backend/accounts/     用户、会话、验证、权限、隐私与审计
backend/trading/      数据集、回放/仿真任务、运营控制与 API
                      StrategyAutomation、持久命令与周期调度
backend/common/       安全头、代理信任、指标、日志与错误
backend/templates/    服务端渲染页面
backend/static/       自托管设计系统与渐进增强 JS
engine/               C++20 解析、订单簿、策略、风险、仿真、CLI、C ABI
execution/            只读 IBKR 探针、显式 Paper runner、账本与有界行情接收
infrastructure/       Docker、Nginx、PostgreSQL 角色、监控、环境覆盖
scripts/              配置生成、备份、隔离恢复及安全测试
tests/                浏览器与跨模块验收
docs/                 审计、路线图、发布与实际验证记录
```

## 关键边界

1. Web、数据库和任务队列不在低延迟交易热路径中。
2. 研究暂停不等于 broker Cancel All；网关未确认撤单时不能宣称无开放订单。
3. 文件处理耗时不等于 NIC Tick-to-Send，更不等于 IBKR 或交易所 ACK。
4. 参考订单簿尚有动态内存分配，不能声称已满足零分配 HFT 架构。
5. 当前不提供付费订阅，没有伪支付、假收入或预设盈利曲线。
6. Paper runner 只支持受限单标的场景，没有日中快照引导、自动缺口恢复、同日重启接续或 live 下单；完整限制以执行模块文档为准。
7. 真实接入与生产发布只能依据可观测验收结果判断，配置文件存在不等于通过。

## 验证记录

各版本实测结果分别保存在 [rc.2](docs/VERIFICATION_RC2.md)、[rc.3](docs/VERIFICATION_RC3.md) 和 [rc.4](docs/VERIFICATION_RC4.md) 验收记录中。测试数、模型版本、数据来源及未执行项目按对应记录核对，不把旧证据自动视为新版本已通过。这些结果不证明真实 IBKR、Production、低延迟或盈利成立。

Docker 服务、数据库分权、监控和恢复演练的实际结果统一记录在 [验证记录](docs/VERIFICATION.md)，不要用构建成功替代运行验收。

文档入口：[架构](ARCHITECTURE.md) · [API](API.md) · [安全](SECURITY.md) · [测试](TESTING.md) · [实际验收](docs/VERIFICATION.md) · [镜像安全阻塞](docs/SECURITY_FINDINGS.md) · [部署](DEPLOYMENT.md) · [变更](CHANGELOG.md)。
