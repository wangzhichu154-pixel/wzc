"""Immutable, owner-scoped evidence from completed simulation scenarios.

This is fixed-fill accounting stress, not a fresh execution simulation, an
out-of-sample test, a continuous portfolio, or authorization to place orders.
"""
import hashlib
import json
import re
import uuid

from django.conf import settings
from django.db import transaction

from accounts.audit import record_event
from .automations import validate_risk_snapshot
from .models import ReplayJob, ResearchAssessment
from .services import DomainError, SIMULATION_DEFAULTS, lock_active_user, normalize_simulation
from .tasks import EngineFailure, validate_result

POLICY_VERSION = "fixed_fill_no_rebate_stress_v1"
LEGACY_MODEL = "displayed_fifo_conservative_v1"
COST_MODEL = "displayed_fifo_conservative_v2_cost_aware"
COST_POLICY = "conservative_one_share_v2"
NEW_COST_KEYS = {"min_fee_per_order_units", "slippage_buffer_units", "min_net_edge_units"}
STATUS_LABELS = {
    "invalid_valuation": "估值无效，无法评估净收益",
    "risk_stopped": "样本触发风险熔断",
    "no_fills": "没有模拟成交，无法评估收益",
    "open_exposure": "存在未平仓库存或未结束订单",
    "losing_sample": "样本净收益为负",
    "cost_sensitive_sample": "成本压力下不再为正",
    "flat_sample": "样本净收益为零",
    "insufficient_sample": "研究样本数量不足",
    "positive_research_sample": "正收益研究样本，仅可继续 paper 研究",
}
LIMITATIONS = [
    "这是历史行情模拟，不是实盘成交或 IBKR 账户收益；执行授权始终关闭。",
    "不同文件不等于独立交易日；SHA256 仅排除字节完全相同的输入，无法证明时间窗口不重叠。",
    "各运行从独立初始库存开始；金额之和仅为重置场景之和，不是连续账户或投资组合表现。",
    "压力情景保持原有模拟成交不变，移除全部假设返佣并增加每成交股成本；未重新模拟延迟、队列或成交概率。",
    "样本数量门槛是可配置的研究筛选规则，不构成统计显著性、样本外验证或未来盈利证明。",
    "单次最大回撤逐项列出，未计算也未声称具有合并账户回撤。",
]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def invalid(message):
    raise DomainError("INVALID_ASSESSMENT_INPUT", message, 409)


def _integer(data, key, minimum=None):
    value = data.get(key)
    if type(value) is not int or abs(value) > 2**63 - 1 or (minimum is not None and value < minimum):
        invalid(f"模拟结果缺少有效整数账本字段：{key}。")
    return value


def inspect_job(job):
    if job.mode != ReplayJob.Mode.SIMULATION or job.status != ReplayJob.Status.SUCCEEDED or job.finished_at is None:
        invalid("只能评估已经成功完成的模拟任务。")
    if job.dataset.deleted_at or job.dataset.owner_id != job.owner_id:
        invalid("任务的数据集已不可用。")
    if not re.fullmatch(r"[0-9a-f]{64}", job.dataset.sha256):
        invalid("任务的数据集完整性标识无效。")
    try:
        validate_result(job.result, ReplayJob.Mode.SIMULATION)
        validate_risk_snapshot(job.owner, job.risk_parameters)
    except (EngineFailure, DomainError):
        invalid("任务结果或风险参数快照不完整，不能纳入评估。")
    value, summary = job.result, job.result["summary"]
    sim = summary["simulation"]
    if type(value["schema_version"]) is not int or summary.get("orders_sent") != 0 or type(summary.get("orders_sent")) is not int:
        invalid("结果未能证明使用受支持的研究执行模式。")
    if sim.get("currency") != "USD" or type(sim.get("price_units_per_dollar")) is not int or sim["price_units_per_dollar"] != 10000:
        invalid("不支持此结果的币种或金额单位。")
    model = sim.get("model_version")
    if model not in (LEGACY_MODEL, COST_MODEL):
        invalid("模拟成交模型版本不受支持。")
    raw = job.simulation_parameters
    expected_keys = set(SIMULATION_DEFAULTS) - NEW_COST_KEYS if model == LEGACY_MODEL else set(SIMULATION_DEFAULTS)
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        invalid("执行参数快照与模拟模型版本不一致。")
    try:
        normalized = normalize_simulation("simulation", raw)
    except DomainError:
        invalid("模拟参数快照无效。")
    if summary.get("effective_risk_parameters") != job.risk_parameters:
        invalid("引擎实际风险参数与任务快照不一致。")
    config = sim.get("config")
    if not isinstance(config, dict):
        invalid("缺少执行时的成本与延迟参数。")
    for name in expected_keys:
        result_key = "quote_lifetime_ns" if name == "simulation_window_ns" else name
        if type(config.get(result_key)) is not int or config[result_key] != normalized[name]:
            invalid(f"引擎实际参数与任务快照不一致：{name}。")
    loss_units = job.risk_parameters["daily_loss_limit_cents"] * 100
    for name in ("daily_loss_limit_units", "daily_drawdown_limit_units"):
        if type(config.get(name)) is not int or config[name] != loss_units:
            invalid("模拟亏损限额与风险快照不一致。")
    cost_policy = "legacy_no_cost_aware_quote_floor"
    if model == COST_MODEL:
        economics = sim.get("pricing_economics")
        if config.get("cost_policy_version") != COST_POLICY or not isinstance(economics, dict) or economics.get("policy_version") != COST_POLICY:
            invalid("报价成本策略版本不一致。")
        for key in ("fee_per_share_units", *sorted(NEW_COST_KEYS)):
            if type(economics.get(key)) is not int or economics[key] != normalized[key]:
                invalid("报价成本字段与执行快照不一致。")
        floor = sum(normalized[k] for k in ("fee_per_share_units", *NEW_COST_KEYS))
        if (type(economics.get("cost_half_spread_floor_units")) is not int or economics["cost_half_spread_floor_units"] != floor
                or type(economics.get("minimum_commission_allocation_shares")) is not int
                or economics["minimum_commission_allocation_shares"] != 1
                or economics.get("rebates_reduce_quote_floor") is not False
                or economics.get("reference") != "inventory_skewed_reservation_price"):
            invalid("报价成本门槛无法与策略合同对应。")
        for key in job.risk_parameters:
            if key == "daily_loss_limit_cents":
                continue
            if type(config.get(key)) is not int or config[key] != job.risk_parameters[key]:
                invalid("模型配置风险字段与任务快照不一致。")
        cost_policy = COST_POLICY
    counts = {key: _integer(sim, key, 0) for key in (
        "orders_submitted", "orders_canceled", "fill_count", "filled_quantity", "open_orders_at_end")}
    ledger = {key: _integer(sim, key, 0 if key in ("fees_units", "rebates_units", "drawdown_units") else None)
              for key in ("realized_pnl_units", "unrealized_pnl_units", "fees_units", "rebates_units", "net_pnl_units", "drawdown_units", "inventory", "cash_before_fees_units")}
    if ledger["net_pnl_units"] != ledger["realized_pnl_units"] + ledger["unrealized_pnl_units"] - ledger["fees_units"] + ledger["rebates_units"]:
        invalid("净收益恒等式校验失败。")
    qty, fills, orders = counts["filled_quantity"], counts["fill_count"], counts["orders_submitted"]
    if ((qty == 0) != (fills == 0) or qty < fills or (qty > 0 and orders == 0)
            or counts["open_orders_at_end"] > orders or counts["orders_canceled"] > orders
            or abs(ledger["inventory"]) > qty):
        invalid("成交数量、订单或库存统计不一致。")
    if (qty + ledger["inventory"]) % 2:
        invalid("买卖成交股数与期末库存奇偶关系不一致。")
    if not qty and any(ledger[k] for k in ledger):
        invalid("没有成交的样本不应出现账户收益、费用或库存。")
    if ledger["inventory"] == 0 and (ledger["unrealized_pnl_units"] != 0 or ledger["cash_before_fees_units"] != ledger["realized_pnl_units"]):
        invalid("已平仓样本的现金与已实现收益不一致。")
    if ledger["rebates_units"] != qty * normalized["rebate_per_share_units"]:
        invalid("假设返佣与模拟成交股数不一致。")
    base_fees = qty * normalized["fee_per_share_units"]
    charged_orders = _integer(sim["pricing_economics"], "fee_charged_orders", 0) if model == COST_MODEL else 0
    maximum_order = job.risk_parameters["max_order_size"]
    buy_quantity = (qty + ledger["inventory"]) // 2
    sell_quantity = (qty - ledger["inventory"]) // 2
    minimum_orders = ((buy_quantity + maximum_order - 1) // maximum_order
                      + (sell_quantity + maximum_order - 1) // maximum_order)
    if minimum_orders > min(fills, orders):
        invalid("买卖成交量超过订单数量与单笔限额允许范围。")
    if model == COST_MODEL and (not minimum_orders <= charged_orders <= min(fills, orders) or (qty == 0 and charged_orders != 0)):
        invalid("最低佣金计费订单数量与成交统计不一致。")
    min_fee = normalized["min_fee_per_order_units"]
    lower_fees = max(base_fees, charged_orders * min_fee)
    upper_fees = base_fees + charged_orders * min_fee
    if not lower_fees <= ledger["fees_units"] <= upper_fees:
        invalid("费用账本与执行成本参数不一致。")
    identity = {"schema_version": value["schema_version"], "model_version": model,
                "cost_policy_version": cost_policy, "risk_parameters": job.risk_parameters,
                "simulation_parameters": normalized, "currency": "USD", "price_units_per_dollar": 10000}
    entry = {"job_id": str(job.pk), "dataset_id": str(job.dataset_id), "dataset_sha256": job.dataset.sha256,
             "result_sha256": digest(value), "engine_checksum": value["checksum"],
             "finished_at": job.finished_at.isoformat(), "raw_simulation_parameters": raw,
             "execution_identity": identity, "execution_config": config,
             "pnl_mark_valid": sim["pnl_mark_valid"], "killed": sim["killed"], **counts, **ledger}
    return identity, entry


def build_result(entries, *, extra_cost, min_captures, min_quantity):
    count, quantity = len(entries), sum(e["filled_quantity"] for e in entries)
    marks_valid = all(e["pnl_mark_valid"] for e in entries)
    killed = any(e["killed"] for e in entries)
    open_exposure = any(e["inventory"] != 0 or e["open_orders_at_end"] > 0 for e in entries)
    totals = {key: sum(e[key] for e in entries) for key in ("realized_pnl_units", "fees_units", "rebates_units", "fill_count", "orders_submitted")}
    totals["unrealized_pnl_units"] = sum(e["unrealized_pnl_units"] for e in entries) if marks_valid else None
    baseline = sum(e["net_pnl_units"] for e in entries) if marks_valid else None
    increment = quantity * extra_cost
    stressed = baseline - totals["rebates_units"] - increment if marks_valid else None
    enough = count >= min_captures and quantity >= min_quantity
    if not marks_valid:
        status = "invalid_valuation"
    elif killed:
        status = "risk_stopped"
    elif quantity == 0:
        status = "no_fills"
    elif open_exposure:
        status = "open_exposure"
    elif baseline < 0:
        status = "losing_sample"
    elif baseline > 0 and stressed <= 0:
        status = "cost_sensitive_sample"
    elif baseline == 0:
        status = "flat_sample"
    elif not enough:
        status = "insufficient_sample"
    else:
        status = "positive_research_sample"
    return {"status": status, "status_label": STATUS_LABELS[status], "execution_authorized": False,
            "profitability_proven": False, "out_of_sample_verified": False,
            "capture_count": count, "filled_quantity": quantity, "all_marks_valid": marks_valid,
            "risk_killed": killed, "open_exposure": open_exposure, "sample_threshold_met": enough,
            "legacy_pricing_model": entries[0]["execution_identity"]["model_version"] == LEGACY_MODEL,
            "net_pnl_units": baseline, "stressed_net_pnl_units": stressed,
            "additional_cost_units": increment, "removed_rebates_units": totals["rebates_units"],
            "extra_cost_per_share_units": extra_cost, "currency": "USD", "price_units_per_dollar": 10000,
            "scenario_sum_only": True, "overlapping_windows_not_excluded": True,
            "thresholds": {"min_captures": min_captures, "min_filled_quantity": min_quantity},
            "totals": totals, "limitations": LIMITATIONS}


def create_assessment(user, *, job_ids, extra_cost_per_share_units=10, idempotency_key=None, request=None):
    if not isinstance(job_ids, list) or not 1 <= len(job_ids) <= 50:
        raise DomainError("VALIDATION_ERROR", "请选择 1–50 个已完成的模拟任务。")
    try:
        identifiers = [uuid.UUID(str(value)) for value in job_ids]
    except (ValueError, TypeError, AttributeError):
        raise DomainError("VALIDATION_ERROR", "任务 ID 格式无效。") from None
    if len(set(identifiers)) != len(identifiers):
        raise DomainError("DUPLICATE_JOB", "不能重复选择同一任务。")
    if type(extra_cost_per_share_units) is not int or not 0 <= extra_cost_per_share_units <= 10000:
        raise DomainError("VALIDATION_ERROR", "每成交股额外成本必须为 0–10000 单位的整数。")
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", idempotency_key)):
        raise DomainError("INVALID_IDEMPOTENCY_KEY", "幂等键必须为 8–128 个字母、数字或 _.:- 字符。")
    request_sha = digest({"job_ids": sorted(map(str, identifiers)), "extra_cost_per_share_units": extra_cost_per_share_units})
    with transaction.atomic():
        owner = lock_active_user(user, verified=True)
        if idempotency_key:
            existing = ResearchAssessment.objects.filter(owner=owner, idempotency_key=idempotency_key, deleted_at__isnull=True).first()
            if existing:
                if existing.request_sha256 != request_sha:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "此幂等键已用于不同评估请求。", 409)
                return existing, True
        if ResearchAssessment.objects.filter(owner=owner, deleted_at__isnull=True).count() >= getattr(settings, "MAX_ASSESSMENTS_PER_USER", 1000):
            raise DomainError("ASSESSMENT_QUOTA", "评估数量已达账户上限。", 409)
        # User lock serializes privacy erasure and dataset deletion; completed
        # jobs have no normal writer. Scope ownership before exposing validity.
        jobs = list(ReplayJob.objects.filter(owner=owner, pk__in=identifiers).select_related("dataset", "owner").order_by("pk"))
        if len(jobs) != len(identifiers):
            raise DomainError("NOT_FOUND", "选择的任务不存在。", 404)
        entries, identity, hashes = [], None, set()
        for job in jobs:
            current, entry = inspect_job(job)
            if entry["dataset_sha256"] in hashes:
                raise DomainError("DUPLICATE_CAPTURE", "相同 SHA256 行情不能重复累计；请选择唯一文件对应的一个任务。", 409)
            if identity is not None and current != identity:
                raise DomainError("INCOMPATIBLE_PARAMETERS", "不能合并不同风险、成本、延迟、模型或报价策略版本的运行。", 409)
            hashes.add(entry["dataset_sha256"])
            identity = current
            entries.append(entry)
        min_captures = getattr(settings, "ASSESSMENT_MIN_CAPTURES", 5)
        min_quantity = getattr(settings, "ASSESSMENT_MIN_FILLED_QUANTITY", 100)
        if type(min_captures) is not int or not 1 <= min_captures <= 50 or type(min_quantity) is not int or not 1 <= min_quantity <= 10**12:
            raise DomainError("ASSESSMENT_POLICY_UNAVAILABLE", "研究评估阈值配置无效。", 503)
        result = build_result(entries, extra_cost=extra_cost_per_share_units, min_captures=min_captures, min_quantity=min_quantity)
        snapshot = {"policy_version": POLICY_VERSION, "inputs": entries, "execution_identity": identity,
                    "thresholds": result["thresholds"], "extra_cost_per_share_units": extra_cost_per_share_units,
                    "stress_policy": "net_minus_all_rebates_minus_filled_quantity_times_incremental_cost",
                    "execution_authorized": False}
        assessment = ResearchAssessment.objects.create(owner=owner, policy_version=POLICY_VERSION, snapshot=snapshot,
            result=result, snapshot_sha256=digest(snapshot), request_sha256=request_sha, idempotency_key=idempotency_key)
        record_event(request, "assessment.created", user=owner, target=str(assessment.pk),
                     metadata={"capture_count": len(entries), "status": result["status"]})
        return assessment, False
