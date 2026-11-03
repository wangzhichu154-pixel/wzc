# 候选镜像安全扫描结果

最新 rc.4（2026-11-03）证据见 [security-gate.json](evidence/rc4/security-gate.json)：最终镜像 `sha256:c51392a1508b9a3b4c278336a835b005d8dc7dd7f5341c12c8f7f62a4ede696c` 的全量 Trivy 0.76.1 扫描为 0 Critical、45 High、77 Medium、62 Low、7 Unknown。已知严重度命中涉及 9 个唯一 CVE，仍无扫描器提供的修复版本；独立 SQLite 条件另产生一条同 CVE 阻断记录。未知严重度明细独立保留且拒绝发布，不吞掉已知 High 结果。下文旧版本记录保留用于比较。

rc.4 门禁加入实际 SQLite 版本、编译选项及 FTS5 能力探针、同镜像 ID、OS/Python 包扫描覆盖、证据时效和已核验上游规则。Alpine 比较镜像虽被扫描器报告零 High/Critical，SQLite 3.53.4 + FTS5 仍命中 [上游受影响条件](https://security-tracker.debian.org/tracker/CVE-2026-119373)；没有改用它规避扫描结果。能力探针仅创建空临时表，不执行漏洞利用。版本条件不是完整可达性或厂商补丁评估。

固定 Python 运行与训练依赖本轮 pip-audit 均未报告已知漏洞；这不消除系统包、原生捆绑库或未知漏洞风险。完整实测范围见 [VERIFICATION_RC4.md](VERIFICATION_RC4.md)。

2026-11-01，使用 Trivy **0.76.1** 和本次下载的漏洞数据库扫描最终本地应用镜像。仅启用漏洞扫描，筛选 HIGH/CRITICAL；没有使用 ignore-unfixed 或忽略文件。

**发布安全门槛尚未通过。** 最终应用镜像：`sha256:12cb0c1e7cae768ddf3533fb09e2e6484cf6347b260308722892be2647822a94`。机器可读摘要：[image-security-summary.json](evidence/image-security-summary.json)。

| 对象 | Critical 包/CVE命中 | High 包/CVE命中 | 唯一 CVE |
|---|---:|---:|---:|
| 初始 Debian bookworm 应用镜像 | 2 | 54 | 17 |
| 升级后 Debian trixie 13.7 应用镜像 | 0 | 46 | 10 |

一项 CVE 可能在同源多个二进制包上重复命中，不能将 46 个命中解释成 46 个不同漏洞。最终报告未给出这些 High 项的 FixedVersion；这不构成忽略依据。

## 已执行的修复

- 构建与运行阶段统一到 Debian trixie，安装发行版当前可用更新。
- 删除不需要的系统 libpq5/LDAP 依赖和 Python wheel 阶段的 gcc/libpq-dev；依赖限定下载 binary wheels，缺失 wheel 时构建失败。
- 实测 `psycopg.pq.__impl__ == binary`、捆绑 libpq 版本 `180006`、SQLite 运行库 `3.46.1`；C++ 共享库成功加载，真实 PostgreSQL/Redis/Celery→C++ 业务链路复测通过。
- 保留非 root、只读根、cap_drop ALL、no-new-privileges、私有文件目录和同步风控。以上降低攻击前提，不能代替漏洞修复。

## 未关闭项

| CVE 组 | 涉及功能与处理要求 |
|---|---|
| CVE-2026-76642 / 78408 / 78409 / 78410 | util-linux 同源工具与库；涉及多包，需发行版修复或经过证据审查的组件级可达性结论。不得删除 dpkg 元数据消除报告。 |
| CVE-2026-54369 | libacl；应用本身不提供任意 ACL 操作入口，仍需更新或正式适用性评估。 |
| CVE-2025-69720 | ncurses/tinfo 工具及库；没有用户终端数据库执行入口，仍保留报告。 |
| CVE-2026-119373 / 119586 | SQLite；paper 私有账本确实使用 SQLite。采用固定 SQL/绑定参数，无用户 SQL 或 SQLite 文件上传；未使用 FTS5/恢复 CLI。不能因此直接删除运行库或宣称已修复。 |
| CVE-2026-16742 | systemd/udev 相关组件；容器不是完整 systemd 主机，但依赖级报告尚未关闭。 |
| CVE-2026-9538 | Perl 相关组件，发行版状态 fix_deferred；无应用侧执行用户 Perl 或解包其归档的流程，仍保留门槛。 |

表中的攻击面判断是代码与部署初审，不是渗透测试、厂商 VEX 或最终风险豁免。没有添加 CI 忽略规则；当前 HIGH/CRITICAL 零容忍检查会阻止候选发布。

后续应优先采用发行版修复或经过维护的精简基础镜像，重新跑运行、数据库与恢复回归。不能混装不同 Debian suites、临时覆盖系统共享库或仅为扫描通过剥离必要依赖。

Python 运行依赖 pip-audit 未报告已知漏洞，但不能覆盖 wheel 内嵌全部原生代码。必须继续跟踪 psycopg-binary 中的 libpq/OpenSSL 等依赖。

## 范围限制

该镜像报告覆盖应用镜像；PostgreSQL、Redis、Nginx、Prometheus 与 exporters 是其他镜像，尚未逐镜像完成安全发布评估。GitHub CI 文件已配置镜像检查，但本工作区没有远程仓库运行记录。生产环境还需漏洞处理、TLS、真实邮件、密钥与访问控制验证；本地检查不能替代这些发布条件。
# rc.3 复扫补充（2026-11-02）

本次最终候选镜像 `sha256:46270b1df71e4b4936e9d1246309fd8f6df70483713e5a39161e62145d62cf86` 经 Trivy 0.76.1 实扫：0 Critical、46 High 命中、10 个唯一 CVE，扫描器未给出修复版本。既有风险未关闭，未增加忽略规则，发布门槛保持阻断。新增 `requirements-ml.txt` 完整固定依赖清单扫描未发现已知漏洞；训练库没有进入 Web 运行镜像。详见 [安全证据](evidence/rc3/security-summary.json)。

