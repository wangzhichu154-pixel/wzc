# 部署与升级

本仓库提供可运行的本地栈与生产部署配置。配置文件存在不代表 Staging 或 Production 已部署，也不代表实盘交易已获准。实际验证记录以 RELEASE_CHECKLIST 和测试产物为准。

## 前置条件

- Linux、Docker Engine、Compose v2；生产覆盖文件使用 `!override`，需要 Compose >= 2.24.4。
- Python >= 3.11 用于运维脚本；建议与应用一致使用 3.12。
- 生产主机容量、域名、TLS 证书、真实 SMTP、镜像仓库访问及外部备份位置由部署方配置。
- 应用镜像非 root；数据库与 Redis 无宿主机公开端口。只允许 Nginx 连接应用端口。
- `.env`、`.secrets/`、数据目录和备份不得提交 Git。不要把 `docker compose config` 的完整输出发布到工单，它包含展开后的凭据。

## 本地开发 / 验证

```sh
python scripts/init_env.py
docker compose build
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8080/health/ready/
docker compose exec backend python manage.py createsuperuser
```

`init_env.py` 使用安全随机数生成独立密码，拒绝覆盖已有配置，文件权限为 0600。初始化不是生产配置向导。默认邮件保存到私有 `email_data` 卷的 `/app/emails`，仅限受控开发环境通过容器运维访问；不将验证/重置 token 写入共享 stdout。生产启动检查要求真实 SMTP。

访问 `http://localhost:8080`。默认仅绑定 loopback；公网部署必须使用下面的 TLS 配置。`migrate` 使用 DDL 角色执行迁移；backend/worker 使用只能进行业务 DML 的角色。初始化脚本只对首次创建的 PostgreSQL 数据卷生效，修改 `.env` 不会自动轮换已有数据库角色密码。

运行中的服务：database、redis、migrate（成功后退出）、backend、worker、scheduler、nginx。scheduler 运行 Celery beat，负责 outbox 重试及工作进程心跳。不要在多个副本上无协调地启动重复 beat。

## 受管理云环境构建

遵循环境提供的网络与 CA 配置，不关闭 TLS 验证，不复制会话 CA 到镜像：

```sh
BUILD_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
  docker compose -f docker-compose.yml \
  -f infrastructure/deployment/compose.managed-build.yml build
```

也可直接 `docker build --secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt -t trading-system:local .`。构建 secret 只在网络依赖安装步骤挂载。普通环境无需该覆盖文件。

## 四套环境

| 环境 | 数据与依赖 | 发布规则 |
|---|---|---|
| Development | 本地独立数据、私有文件邮件后端 | 仅 loopback |
| Testing | 临时 PostgreSQL/Redis、测试邮箱、编译后的真实 C++ 引擎 | 每次 CI 重建，不接生产凭据 |
| Staging | 独立数据库/Redis/SMTP/域名，尽量同生产规格 | 完整迁移、E2E、恢复、负载及安全验收 |
| Production | 独立账号、密钥、备份和审计 | 仅晋级通过 Staging 的不可变镜像摘要 |

每个部署使用独立目录、`--env-file` 和 Compose project name；不得复用生产卷。`.env.production` 等真实文件同样禁止入 Git。

## HTTPS 部署

在生产环境文件配置：

- `APP_ENV=production`、`DJANGO_DEBUG=false`。
- 精确的 `DJANGO_ALLOWED_HOSTS`（业务域名，并保留内部健康检查需要的 `127.0.0.1` 与监控使用的 `backend`）。
- `PUBLIC_BASE_URL=https://真实域名`，匹配的 `DJANGO_CSRF_TRUSTED_ORIGINS`。
- `DJANGO_SECURE_SSL_REDIRECT=true`；Compose 在私网应用入口设置 `TRUST_PROXY=true`，Nginx 会覆盖而非追加客户端提供的 forwarded headers。将 `TRUSTED_PROXY_CIDRS` 从开发用私有地址段收紧到实际 Nginx 网络，不接受任意外部代理。
- `EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend`，真实 SMTP 和通知地址；TLS 与 SSL 模式只能启用其一；`EMAIL_DELIVERY_EAGER=false`。
- `LEGAL_ENTITY_NAME`、`SUPPORT_EMAIL` 及真实隐私政策所需信息。
- `APP_IMAGE=仓库/镜像@sha256:已验证摘要`。
- `TLS_CERT_DIR` 为已有证书目录，含 `fullchain.pem` 和 `privkey.pem`；私钥仅授予 TLS 容器所需读取权限，禁止为省事设置全局可读。

```sh
docker compose --env-file .env.production \
  -f docker-compose.yml -f infrastructure/deployment/compose.production.yml config --quiet
docker compose --env-file .env.production \
  -f docker-compose.yml -f infrastructure/deployment/compose.production.yml pull
docker compose --env-file .env.production \
  -f docker-compose.yml -f infrastructure/deployment/compose.production.yml up -d --no-build
```

不要用自签名假证书宣称 HTTPS 验收通过。证书自动签发/续期由主机或入口层承担，并配置到期告警。该配置支持 TLS 1.2/1.3，HTTP 301 跳转 HTTPS。应用、Redis、数据库及 metrics 不直接公开；静态资源公开，上传行情文件通过应用鉴权读取。Nginx 普通请求体上限为 64 KiB，仅 `/datasets/` 和 `/api/v1/datasets/` 上传入口允许 257 MiB，为 256 MiB 文件预留 multipart 开销；调整时同时对齐 `MAX_DATASET_BYTES`。敏感验证/重置 token 路径禁用 Nginx 原始 URI 错误日志，应用使用规范路由审计。

Staging 使用独立环境文件及 `compose.staging.yml`，域名、证书、数据库卷与生产隔离。

上传入口关闭 Nginx 整体请求缓冲，避免 64 MiB 代理临时盘限制合法大文件。应用 `/tmp` 上限为 768 MiB（按实际用量占 RAM），为默认两个同步 web worker 的上传临时文件留空间；增加 worker 数或上传上限前重新计算内存预算，必要时改用独立受限磁盘临时目录并建立清理任务。该容量配置不是并发性能验证结果。

## 监控

```sh
docker compose --profile monitoring up -d
```

Prometheus 默认只绑定 `127.0.0.1:9090`，可通过 SSH 隧道访问。它以专用 Bearer token 访问应用的私网 `/metrics/`；Nginx 明确阻止该路径。`.secrets/metrics_token` 必须与环境变量 `METRICS_TOKEN` 同步轮换。Compose file-backed secrets 不执行 uid/gid 重映射：生成器将 `.secrets/` 设为 0700，内部 token 文件设为 0444，使仅获得该文件挂载的非 root Prometheus 可读；宿主机其他用户不能遍历父目录。非秘密绑定配置文件使用 0644，启动脚本 0755。

Node exporter、PostgreSQL exporter、Redis exporter 均无公开端口。Node exporter 只读挂载主机统计目录与文件系统视图；在受限环境中若不允许这些挂载，部署主机侧 exporter 并调整 scrape target，不使用 privileged 作为绕过方式。PostgreSQL exporter 使用单独的 `pg_monitor` 角色。

已提供基础可用性、磁盘、内存、数据库、Redis、API 错误率和 P99 告警规则。**告警规则不等于通知已经送达**：上线前配置 Alertmanager 或等价通知集成，并实际演练收件人收到告警；对任务心跳/队列积压、备份时效、证书到期补充目标部署对应告警。应用日志与审计日志分开保留，采集系统不得记录密码、cookie、reset token、完整 DSN。

## CI、发布与回退

GitHub Actions 执行 lint、C++/CLI 测试、Django migration 检查、Python 测试、浏览器依赖安装、依赖审计、Compose 校验、镜像构建和镜像漏洞扫描。main 上通过后推送 commit 对应候选镜像，产生 SBOM/provenance；不会凭空向未配置的生产主机部署。

生产过程必须先完成：

1. 在 Staging 使用同一摘要完成清单，保存测试、性能和恢复证据。
2. 完整备份；明确本次 migration 是 expand、backfill 还是 contract。
3. 兼容性变更先增加新字段/接口，保持前后两个应用版本可读；数据回填可重试。
4. 晋级已测试镜像，执行 migration，然后启动 web/worker/scheduler；观察 readiness、错误率和任务状态。
5. 按用户群 Feature Flag 灰度；前后版本并行或蓝绿切流需目标负载均衡器支持并单独演练。
6. 异常时停止扩大灰度，关闭功能开关；仅在数据库兼容时回退旧镜像。不可逆迁移走验证过的前滚修复或恢复流程，不盲目反向 migration。

当前单机 Compose 提供可重复部署，**不承诺零停机、高可用或自动滚动回退**。缺少真实 Staging/Production 目标和外部配置时，发布状态应保持 Blocked，不能以 `docker compose config` 通过替代部署成功。

运维用户的 Docker socket 访问等同于主机高权限，应使用受控账号，禁止把 Docker socket 挂入应用容器。
