# API v1

Base: `/api/v1/`。浏览器同源 session 认证；所有修改请求必须携带 CSRF token。没有长期保存在 localStorage 的 bearer token，也不伪造 Refresh Token。服务端会话提供续期、绝对期限、设备撤销和安全版本失效。

```json
{"success":true,"code":"OK","message":"","data":{}}
```

应用层错误用相同结构、相应 HTTP 状态及稳定 code，响应带 `X-Request-ID`。400 参数错误，401 未登录，403 无权限/CSRF，404 不存在或不归当前用户，409 状态冲突，413 过大，415 类型错误，429 限流，503 依赖未就绪。代理层拒绝可能直接返回 HTTP 错误，客户端应先检查状态和 Content-Type，不假设所有网络失败都带 JSON。

| Method | Path | 功能 |
|---|---|---|
| GET | `/auth/csrf/` | 获取 masked CSRF token |
| POST | `/auth/register/` | 邮箱注册；接收不等于邮件已送达 |
| POST | `/auth/login/` | 建立服务器会话 |
| POST | `/auth/logout/` | 退出 |
| GET | `/auth/me/` | 当前用户身份 |
| GET | `/auth/devices/` | 本人会话列表 |
| POST | `/auth/devices/<uuid>/revoke/` | 撤销本人设备 |
| POST | `/auth/password/reset/` | 请求重置，防账户枚举 |
| GET | `/session/` | 研究能力与 session 信息 |
| GET/POST | `/datasets/` | 本人数据集列表/文件上传 |
| GET/DELETE | `/datasets/<uuid>/` | 本人元数据/删除意图 |
| GET/POST | `/jobs/` | 本人任务列表/创建 |
| GET | `/jobs/<uuid>/` | 状态、风险快照及结果 |
| POST | `/jobs/<uuid>/cancel/` | 取消请求 |
| POST | `/jobs/<uuid>/retry/` | 创建有限次数的新尝试 |
| GET/POST | `/automations/` | 本人的自动运行列表 / 创建停止状态配置 |
| GET | `/automations/<uuid>/` | 当前状态、参数快照、调度和最近任务 |
| POST | `/automations/<uuid>/start/` | 密码复核后授权周期历史仿真 |
| POST | `/automations/<uuid>/stop/` | 密码复核，停止调度并请求取消关联任务 |
| GET | `/execution/status/` | 运营用户读取独立 Paper 状态；不提供控制操作 |

## 研究任务与仿真

提交研究任务包含 `dataset_id`，`mode` 只能是 `replay` 或 `simulation`，默认 replay。客户端必须读取服务端能力和校验错误，不能发送任意命令或路径。提交时建议带 `Idempotency-Key`（8–128 ASCII 字母、数字或 `_.:-`）；队列故障返回 503 时 data 仍可能含已持久化 job ID，应查询原任务，不盲目创建第二笔。

`simulation_parameters` 只接受受限整数：

| 字段 | 单位 / 合法范围 | 默认 |
|---|---|---|
| `latency_ns` | 1,000–10,000,000,000 ns | 1,000,000 |
| `cancel_latency_ns` | 1,000–10,000,000,000 ns | 1,000,000 |
| `fee_per_share_units` | 每股 1/10000 USD，0–10,000 | 30，即 $0.003 |
| `rebate_per_share_units` | 每股 1/10000 USD，0–10,000 | 0 |
| `simulation_window_ns` | 报价最大寿命，1,000,000–60,000,000,000 ns | 1,000,000,000 |

这些是用户的仿真假设，不是实际 broker 费率或延迟测量。风险参数从本人的 RiskProfile 快照，tick 单位是 1/10000 USD。simulation 返回模拟账本；replay 的公共成交不等于账户成交，不产生虚构 PnL。`pnl_mark_valid=false` 的结果不得由客户端改成零或显示为有效收益。

## 自动运行

创建请求示意（UUID 替换为本人真实数据集）：

```json
{
  "name": "历史做市评估",
  "dataset_id": "<dataset-uuid>",
  "mode": "simulation",
  "interval_seconds": 300,
  "max_runs": 10,
  "simulation_parameters": {"latency_ns": 1000000, "fee_per_share_units": 30}
}
```

创建以及 start/stop 的 POST **必须**提供 `Idempotency-Key`。创建返回 201，幂等重复返回 200；初始状态固定为 `stopped`，不会自动发起任务。周期 60–86400 秒、次数 1–1000，每轮是同一历史数据的独立 simulation。传入 paper/live mode 会被拒绝。

启动或停止请求：

```json
{"password":"<当前密码>","acknowledge_latch":false}
```

密码只用于本次重新认证，不保存到幂等回执或审计。只有确实处理了历史仿真锁存原因时，才在 start 中发送 `acknowledge_latch:true`；该字段不能解除 broker 执行器 Kill。普通控制有用户/IP 频率限制，限流返回 429。

返回包含 `automation` 和 `command`。`command.original_outcome` 是原命令执行结果，`automation.state` 是当前状态：停止后再次重放旧 start 的 key 不会重新启动，即使原回执记录过 running。

自动配置状态为 `stopped/running/paused/faulted`。停止只承诺禁止后续调度及发出取消意图；需查询 `last_job_id` 或任务列表确认正在运行的子进程已经到达终态。`runs_created` 包括失败尝试，锁存确认不重置次数。配置响应恒为 `mode=simulation`、`live_trading=false`。

## 研究收益评估

`GET/POST /api/v1/assessments/` 和 `GET /api/v1/assessments/<uuid>/` 要求已验证的当前账户，始终按所有权查询。POST 示例：

```json
{"job_ids":["<已完成模拟任务 UUID>"],"extra_cost_per_share_units":10}
```

最多 50 个任务，额外每成交股成本范围 0–10000（单位 1/10000 USD），默认 10。可提供 `Idempotency-Key`；重复同请求返回原记录，冲突返回 409。创建返回 201；报告不可通过 API 修改。列表分页，详情包含 `result`、`snapshot`、`snapshot_sha256` 和策略版本。

服务端拒绝跨用户、非模拟/未完成任务、相同 SHA256 行情重复累计、不同参数/模型混合以及与账本或费用下限不一致的结果。压力净盈亏 = 原净盈亏 − 全部假设回扣 − 成交股数 × 额外每股成本；它保持原模拟成交不变，不能被解释成重新执行的成交仿真。

结果区分 `invalid_valuation`、`risk_stopped`、`no_fills`、`open_exposure`、`losing_sample`、`cost_sensitive_sample`、`flat_sample`、`insufficient_sample`、`positive_research_sample`。`execution_authorized`、`profitability_proven`、`out_of_sample_verified` 恒为 false。无效估值的净盈亏和压力净盈亏为 null；独立场景之和不是连续账户收益。

研究门槛通过环境配置 `ASSESSMENT_MIN_CAPTURES`、`ASSESSMENT_MIN_FILLED_QUANTITY`，默认 5 个不同文件和 100 股成交；阈值不代表统计显著性。POST 有用户/IP 限流，账户注销清除私有快照。

新 simulation 参数增加 `min_fee_per_order_units`、`slippage_buffer_units`、`min_net_edge_units`，默认均 0，范围 0–1000000000。每股费用范围同为 0–1000000000，回扣范围 0–1000000。任务契约升级为 4；旧排队模拟或旧自动配置缺少完整成本字段时明确失败/暂停并要求新建，历史已完成结果不改写。

## 只读 Paper 状态

`GET /api/v1/execution/status/` 对匿名用户返回 401，对无运营权限用户返回 403。运营权限要求有效且已验证账户，并满足 superuser 或 staff + 管理角色条件。

未接入状态文件时返回 `available=false,state=not_configured,live_enabled=false`；文件非法、不可读、陈旧时显示对应 unavailable/invalid/stale，而不是伪造 connected 状态。有效状态仅包含允许的连接、armed、库存、开放订单、估值有效性、PnL 和 Kill 字段。账户号、密钥和未知字段不会转发。无效估值下 `net_pnl_units=null`；有值时单位为 1/10000 USD。

此接口读的是独立 Paper 进程的受控状态投影，不连接 broker、不启动自动交易，也不能确认所有真实订单已经撤销。读取状态成功不代表 SDK 或执行链路完成验收。

## 上传及安全边界

上传用 multipart，大小和用户配额均由服务器限制。代理默认只允许 64 KiB 普通请求体；只有 `/datasets/` 和 `/api/v1/datasets/` 放行较大上传。所有文件使用随机内部路径，禁止 URL 抓取、任意 filesystem path、压缩文件自动解压或命令参数透传。详细参数和源码契约见 [trading README](backend/trading/README.md) 和 `backend/accounts/api_urls.py`。

`/health/live/` 是进程存活，`/health/ready/` 检查依赖，不代表真实 broker 已可交易。`/metrics/` 仅供私网监控并要求 Bearer metrics credential，不是用户 API。

没有 `/orders/live/` 这样的网页直接实盘下单端点。API v2 将使用独立路径；v1 已有结果记录必须保留 schema_version 兼容解析。
# AI 运营状态接口（rc.4，兼容旧报告）

`GET /api/v1/ai/status/`：仅已验证邮箱且处于有效状态的运营管理员可读。使用现有 Session、统一 API envelope 与服务端权限校验；不接受客户端传入模型路径。普通用户返回 403，未登录返回 401；POST 等写方法拒绝。

`data` 包含 `available`、`state`、`models`、`retrain` 与恒为 false 的 `execution_authorized`。模型记录只投影版本、provenance、训练/模拟指标、评估结果和激活模式；不返回账户标识、文件路径、原始元数据或密钥。未配置目录、空注册库、非法文件与缺失指标显式区分，缺失数据不填零。对应页面为 `/ai/`。

训练、模型审批、策略启用和券商操作均没有 Web 写接口。CLI 与独立执行进程的使用见 `docs/AI_PIPELINE.md`。

模型的 `simulation_summary.valuation` 区分 `fresh_bbo`、`reconciled_flat_ledger`、`mixed_valid` 和 `unavailable`。`quote_mark_valid` 仅代表报价标记；`net_pnl_valid` 表示是否可以展示账户终值；`drawdown_complete` 表示风险路径是否完整。平仓结算允许显示净值，不恢复行情/交易权限。缺失字段保持 null，不将旧报告隐式升级。

`evaluation_failure_codes` / `simulation_failure_codes` 提供受限错误码，例如 `NO_FILLS`、`OPEN_EXPOSURE`、`HOLDOUT_MISMATCH`、`AGGREGATE_MISMATCH`、`INCOMPLETE_DRAWDOWN` 和 `LEGACY_REPORT_REQUIRES_REVALIDATION`。这些字段用于解释证据为何未通过，不是重新启用策略的指令。

# Native desktop additions (rc.5)

`GET /api/v1/dashboard/` 返回当前用户的数据集/任务/自动仿真统计及最多五条近期任务，不构造账户 PnL。
`GET /api/v1/risk/` 返回六项研究参数、persisted/updated_at、scope=research、live_trading=false。
`PATCH /api/v1/risk/` 部分更新；`POST` 完整更新。复用现有表单、用户锁、CSRF、API 限流及审计，修改仅影响新任务。
风险修改要求已验证邮箱，不接受 owner、实盘开关、熔断复位等附加字段。

