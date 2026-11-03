# v1.0.0-rc.4：可恢复研究流水线与评估验收

验收日期：2026-11-03（Asia/Shanghai；机器日志保留 UTC / epoch 时间）。本轮实际修改代码、训练模型、回测、备份恢复、浏览器验证和本地容器部署均已执行。**数据是合成 ITCH，未连接 IBKR、未发送券商订单、未验证真实市场盈利，正式发布仍被安全及外部接入条件阻断。**

## 本轮交付

- `ml.pipeline`：一条命令执行政策预声明、训练、登记、预测门槛、C++ 回测和模拟门槛；冻结请求与源码/依赖/数据身份，保留每阶段日志、退出码和收据。
- `ml.reservations`：Pipeline 与 Retrain 在同一注册库锁下预留测试区间，按行情 SHA 和股票/会话日期双重排重。失败保留记录，恢复不自动重新训练。
- schema 2 模拟报告：覆盖整个测试集，验证模型、数据、源码、引擎和固定研究协议，从各会话的原始账本重建模型/基线/成本压力/延迟压力结果。WAIT 基线必须没有订单、成交、费用和敞口。旧 schema 1 只能保留历史展示，不绕过新的晋级或加载检查。
- 账户估值：收盘后已平仓、无挂单且现金对平的账户可计算终值；报价仍无效。持仓期间有估值缺口时回撤仍不完整，最终平仓不能消除该缺口。
- `ml.store_backup`：有界归档、同一文件描述符校验与解析、新目录原子恢复、活动指针隔离、完整消费账本、只读快照与耐久性失败处理。
- 运营界面展示真实拒绝原因、账户净值与行情/回撤状态；模型操作继续只读。
- 安全门禁核对同一镜像的完整扫描与实际运行组件，并独立检查上游公告条件。CI 传递已验证镜像，发布阶段不重新构建不同产物。

代码审计计划见 [RC4_PLAN.md](RC4_PLAN.md)，操作说明见 [ML_PIPELINE_OPS.md](ML_PIPELINE_OPS.md)、[MODEL_BACKUP.md](MODEL_BACKUP.md)。

## 实际训练与独立测试

模型：`ridge-d008e93b6e608c2753c8bff9`。模型文件 SHA-256：`ff956a91f00087762a3c1c136a40e62f9ea248755fe35c35cb8efba56ff9a0ed`。

使用 20 个合成会话（2026-10-01 至 2026-10-28，生成 seed 46），每个会话只有短时间窗口，不是全天真实市场样本。训练 seed 42，六个因果盘口特征；目标为 500 ms 后中间价收益（bps），最大标签延迟 250 ms。按完整会话划分，禁止跨重置/会话标签；750 ms 的 purge/embargo 条件实际检查，因会话间隔足够，本次移除 0 行。标准化只拟合训练数据，使用三个时间滚动验证折，按验证集从预先声明的 α 候选选择 10.0。独立测试集没有参与调参。

| 划分 | 会话数 | 样本数 |
|---|---:|---:|
| 训练 | 12 | 229,560 |
| 验证 | 4 | 76,520 |
| 独立测试 | 4 | 76,520 |

| 独立测试指标 | Ridge | 零预测基线 | Microprice 基线 |
|---|---:|---:|---:|
| RMSE（bps） | 1.437069 | 1.437678 | 1.725101 |
| MAE（bps） | 1.091279 | 1.070634 | 1.368554 |

Ridge 测试集方向准确率为 52.1058%，分母是 54,326 个非零收益标签；这不是校准后的成交/盈利概率，重叠标签也不是独立交易样本。Ridge 的 RMSE 略优于零预测，但 MAE 更差，不能将单项改善解释为交易优势。标准库加载推理与训练库预测最大绝对差为 `2.7755575615628914e-17` bps。

训练日志、配置、模型、数据身份、滚动验证结果均实际保存：[model.json](evidence/rc4/model.json)、[training-report.json](evidence/rc4/training-report.json)、[train.log](evidence/rc4/train.log)。

## 实际交易模拟结果

固定研究费用没有因结果改变：每股费用 0.0035 USD、每单最低费用 1 USD、每股滑点缓冲 0.002 USD；这些是研究示例值，不是已授权的真实账户参数或 IBKR 费用承诺。C++ 同步风控保持 long-only、单次一股、独立库存限额，模型只能过滤方向。

- 四个独立测试会话产生 76,824 条结构化状态，其中 76,680 次因预计收益不足以覆盖成本而 WAIT，144 次为预热/重置。
- 模型、成本感知双边基线、等待基线、双倍成本和五倍延迟情景均为 **0 成交、0 成交额、0 费用、0 库存、0 挂单、净 PnL 0**。
- 收盘后 `quote_mark_valid=false`；平仓账本核对后 `net_pnl_valid=true`、`drawdown_complete=true`，本次无交易所以最大会话内回撤为 0。没有闭合持仓周期，平均持仓时间为 null；没有提供资本基数，收益率及换手率比例为 null。
- 预测评估通过；模拟准入拒绝，错误码 **`NO_FILLS`**。流水线 `completed` 代表处理完成，`admission_passed=false`、`automatic_promotion=false`、`execution_authorized=false`；没有任何活动模型指针。

成交模型保留可见 FIFO 排队、延迟后激活、撤单延迟、部分成交与敞口，触价不等于成交。由于无成交，本次不能校准真实排队、实际滑点、持仓时长或经济成本敏感性。按会话独立平仓起始的结果不是连续账户权益曲线；真实牛熊/高波动市场阶段验证未执行。

完整证据：[pipeline-result.json](evidence/rc4/pipeline-result.json)、[backtest-report.json](evidence/rc4/backtest-report.json)、[evaluation-plan.json](evidence/rc4/evaluation-plan.json)。

## 恢复、备份与界面

实际执行原请求的 `--resume`：88 个不可变文件哈希全部保持一致，没有重复训练；改变 seed 后恢复返回 `REQUEST_CHANGED`。见 [恢复验证](evidence/rc4/resume-verification.json)。

rc.3 原始模型库实际备份并恢复到新目录：2 个模型、4 份证据；rc.4 新模型库实际备份、恢复并制作只读快照：1 个模型、2 份证据。共享测试预留逐字节一致，均为 `restored_inactive`，没有活动交易指针。原始注册库保留。容器使用独立快照，将该副本所有权明确设置为 UID 10001；不是修改原始研究库。

恢复工具不包含注册库外的 Pipeline run-dir、行情文件及数据目录 holdout 锁，这些需要独立备份。本次交付研究证据包包含对应合成数据与完整 run-dir；它不代表加密离机灾备或生产 RPO/RTO 已验收。

真实模型驱动的浏览器测试覆盖 7 个屏幕尺寸、运营/普通/匿名权限、只读 API、零成交拒绝原因与估值区分，刷新不修改注册库。桌面和移动端截图见 [desktop](evidence/rc4/ai-desktop.png)、[mobile](evidence/rc4/ai-mobile.png)。

## 工程验证结果

| 检查 | 实际结果 |
|---|---|
| 全量 pytest 最终回归 | **408 passed，172 subtests passed** |
| 应用 coverage | **84%**，统计范围为 backend，非全部 C++/研究代码 |
| C++ Release | 5 个 CTest suites 通过 |
| C++ ASan/UBSan | 5 个 suites 通过；LSan 因环境限制关闭，未记为通过 |
| 独立执行器 | 81 tests 通过；无券商网络，测试边界使用替身 |
| 运维与安全工具 | 34 unittest tests 通过 |
| 核心浏览器流程 | 7 尺寸 × 10 页面；真实 C++ 回放/仿真，0 浏览器错误 |
| AI 浏览器最终回归 | 7 尺寸，实际本轮模型，0 浏览器错误 |
| Ruff / migration drift | 通过，无新增数据库 schema 变更 |
| 本地服务联调 | 真实 PostgreSQL、认证 Redis、Celery Worker、C++ 通过；应用角色 DDL 被拒绝，临时验收数据已清理 |
| 容器运行 | UID 10001、只读模型快照、未安装 sklearn；研究源码 SHA 与训练冻结请求一致 |
| HTTP | live/ready 均 200，版本 rc.4，匿名 AI API 401 |
| CI 产物传递 | YAML 检查及本地镜像导出/加载 ID 一致；远程 GitHub Actions 未执行 |

首次完整回归发现一项限流测试跨分钟边界。现有 API 限流是固定窗口，不是任意滚动 60 秒上限；首次请求落入旧窗口，因此测试原本的第六次请求假设不成立。修正测试时钟控制并新增窗口切换、身份/scope 隔离、缓存故障拒绝测试，没有改变生产限额。首次失败及修复后的结果均保留。

## 实测时延与安全门槛

本轮离线 Python 特征计算与策略政策执行共 76,824 次：P50 **26.030 μs**、P90 **44.558 μs**、P99 **227.253 μs**、P99.9 **10.524 ms**、Max **53.284 ms**。测量时并行运行了构建与回归，有资源争用；这是该次运行的原始观测，不是隔离 CPU 的性能上限，更不是 NIC→券商→交易所 ACK 时延。网络、IBKR 和交易所延迟未测。

最终本地候选镜像：`sha256:c51392a1508b9a3b4c278336a835b005d8dc7dd7f5341c12c8f7f62a4ede696c`。Trivy 0.76.1 全量扫描记录 **0 Critical、45 High、77 Medium、62 Low、7 Unknown**；High 是包/CVE 命中数。独立 SQLite 规则增加一条阻断记录，共 46 条已知严重阻断记录、9 个唯一 CVE；7 条未知严重度记录另行保留并阻断，不当作已安全。固定 Python 运行及训练依赖的 pip-audit 均未发现已知漏洞。

Alpine 比较镜像没有部署：扫描器报告零 High/Critical，但实际 SQLite 3.53.4 启用 FTS5，匹配 [CVE-2026-119373 的上游受影响条件](https://security-tracker.debian.org/tracker/CVE-2026-119373)，独立门禁仍拒绝。版本/能力匹配不是可利用性证明，也没有把厂商未收录视为漏洞已修复。最终门禁见 [security-gate.json](evidence/rc4/security-gate.json)。

## 未执行或仍受限制

真实授权行情训练、真实市场样本外表现、真实 IBKR SDK/Gateway/Paper 验收、实盘资金/标的/风险额度授权、实盘恢复对账，均未执行。Live 继续在代码中拒绝。

正式 Staging/Production 目标、域名 HTTPS、真实 SMTP、全组件镜像安全评估、完整渗透与生产灾备演练未完成；当前服务是本地 Development 验证，不能称为正式上线。镜像安全门禁未通过，未推送/发布生产镜像。

所有精选证据及 SHA 清单见 [EVIDENCE_MANIFEST.json](evidence/rc4/EVIDENCE_MANIFEST.json)。部署与交易批准必须基于真实条件另行验收，模型不能扩大风险限额或批准自己。
