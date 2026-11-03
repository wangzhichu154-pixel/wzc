"""Read-only, strictly allowlisted UI projection of the local model registry.

The registry owns its fixed-file JSON protocol and bounded O_NOFOLLOW reader.
This module never loads executable model formats or accepts request paths.
"""
import math
from pathlib import Path
import re
import stat
from decimal import Decimal
from datetime import UTC, datetime

from django.conf import settings

MAX_MODELS = 50
VERSION = re.compile(r"ridge-[0-9a-f]{24}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
STAGES = {
    "candidate": "研究候选，尚未完成晋级验证",
    "trained": "已登记训练产物", "registered": "已登记模型", "evaluated": "已记录预测评估",
    "simulation_validated": "已记录模拟验证", "operator_paper_approved": "已记录运营纸账户审批",
}
PROVENANCE = {"synthetic": "合成测试数据", "licensed_market": "授权行情（登记声明）"}
SPLITS = {"train": "训练集", "validation": "验证集", "test": "测试集"}
ESTIMATORS = {"ridge": "Ridge 模型", "zero": "零预测基线", "microprice": "Microprice 基线"}
RETRAIN_STATES = {
    "skipped": "本次再训练已跳过", "insufficient_data": "再训练数据不足",
    "idle": "没有待处理训练请求", "not_requested": "未请求再训练", "requested": "再训练已请求",
    "pending": "等待再训练", "running": "再训练进行中", "completed": "再训练已完成",
    "succeeded": "再训练已完成", "failed": "再训练失败", "blocked": "再训练受阻",
    "drift_detected": "检测到漂移", "not_configured": "尚未配置再训练", "none": "暂无再训练记录",
}

RETRAIN_REASONS = {
    "NOT_DUE": "尚未到再训练时间", "PERIODIC": "定期再训练", "FEATURE_DRIFT": "检测到特征分布漂移",
    "INSUFFICIENT_WINDOW": "数据窗口不足", "NO_NEW_AUTHORIZED_DATA": "没有新增授权数据",
    "DATA_ALREADY_ATTEMPTED": "当前数据已尝试训练", "TRAINING_FAILED": "训练执行失败",
    "CREATED_CANDIDATE": "已生成新的研究候选", "DRY_RUN": "仅执行检查，未训练",
    "NO_MODEL": "没有基准模型", "DATA_INVALID": "数据校验失败",
    "OPERATOR_REVIEW_REQUIRED": "需要运营检查触发条件",
}

VALUATION_BASES = {
    "fresh_bbo": "基于有效盘口估值",
    "reconciled_flat_ledger": "已平仓且无挂单，以核对后的账本计算",
    "mixed_valid": "各会话分别使用有效盘口或已核对平仓账本",
    "unavailable": "账户终值无法完整核验",
}

# Only fixed codes mapped to local text may reach the API or page. Never echo a
# registry exception, operator comment, filename or unrecognized code verbatim.
FAILURE_LABELS = {
    "NO_FILLS": "没有模拟成交，不能验证交易效果",
    "INSUFFICIENT_FILLS": "模拟成交样本不足",
    "INSUFFICIENT_EVENTS": "模拟事件数量不足",
    "INSUFFICIENT_SESSIONS": "独立留出会话不足",
    "INCOMPLETE_VALUATION": "账户估值不完整",
    "INCOMPLETE_DRAWDOWN": "持仓期间存在估值缺口，回撤路径不完整",
    "OPEN_EXPOSURE": "仍有库存或未终结订单",
    "RISK_KILLED": "模拟期间触发风险熔断",
    "LOSS_LIMIT": "成本后收益未达到预声明门槛",
    "DRAWDOWN_LIMIT": "回撤超过预声明上限",
    "COST_STRESS_FAILED": "成本压力情景未通过",
    "MISSING_STRESS_SCENARIO": "缺少要求的压力情景",
    "IDENTITY_MISMATCH": "模型、数据或政策身份不匹配",
    "INVALID_EVIDENCE": "评估证据未通过完整性检查",
    "TRAINING_NOT_ACCEPTED": "预测评估尚未通过",
    "TRAINING_GATE_REJECTED": "预测指标未达到预声明门槛",
    "LEGACY_REPORT_REQUIRES_REVALIDATION": "旧估值报告需要重新验证，不能自动升级准入",
    "LEGACY_FAILURE_UNCLASSIFIED": "旧失败记录未保存结构化原因",
    "HOLDOUT_MISMATCH": "留出数据身份或时序不匹配",
    "AGGREGATE_MISMATCH": "会话账本与汇总指标不一致",
    "NET_PNL_BELOW_LIMIT": "成本后收益未达到预声明门槛",
    "STRESS_NET_PNL_BELOW_LIMIT": "成本压力收益低于预声明下限",
    "MISSING_COST_STRESS": "缺少要求的成本压力情景",
    "UNRECOGNIZED_REVIEW_REQUIRED": "需要运营检查未识别的准入原因",
}


def _failure_codes(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 64 or any(not isinstance(code, str) or len(code) > 256 for code in value):
        raise ValueError("Invalid failure code list")
    result = []
    for code in value:
        safe = code if code in FAILURE_LABELS else "UNRECOGNIZED_REVIEW_REQUIRED"
        if safe not in result:
            result.append(safe)
    return result


def _empty(state):
    return {"schema_version": 1, "available": False, "state": state, "models": [],
            "active": {"simulation": None, "paper": None}, "retrain": None,
            "live_enabled": False, "execution_authorized": False}


def _root(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("Invalid registry root")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Registry root must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Registry ancestors must be directories, not links")
    return path


def _read_registry(root):
    # No model inference or numerical training dependencies run in the web app.
    from ml.registry import read_status
    return read_status(root)


def _version(value, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or VERSION.fullmatch(value) is None:
        raise ValueError("Invalid model identity")
    return value


def _hash(value):
    if not isinstance(value, str) or HASH.fullmatch(value) is None:
        raise ValueError("Invalid artifact digest")
    return value


def _count(value, *, maximum=10**15):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("Invalid count")
    return value


def _number(value, *, low=None, high=1e30, nullable=False):
    if value is None and nullable:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > high or (low is not None and value < low):
        raise ValueError("Invalid finite metric")
    return value


def _training(value):
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("split"), dict) or not isinstance(value.get("metrics"), dict):
        raise ValueError("Invalid training report projection")
    splits, metrics = {}, {}
    for name in SPLITS:
        split = value["split"].get(name)
        estimators = value["metrics"].get(name)
        if not isinstance(split, dict) or not isinstance(estimators, dict):
            raise ValueError("Incomplete split metrics")
        sessions = split.get("sessions")
        # Producer may retain session IDs; only their count can cross this edge.
        if isinstance(sessions, list):
            if len(sessions) > 10000 or any(not isinstance(item, (str, dict)) for item in sessions):
                raise ValueError("Invalid session count")
            sessions = len(sessions)
        splits[name] = {"sessions": _count(sessions), "samples": _count(split.get("samples"))}
        metrics[name] = {}
        for estimator in ESTIMATORS:
            metric = estimators.get(estimator)
            if not isinstance(metric, dict) or not {"mse_bps2", "mae_bps", "rmse_bps", "correlation", "direction_accuracy", "samples"}.issubset(metric):
                raise ValueError("Missing estimator metrics")
            clean = {key: _number(metric.get(key), low=0, nullable=True) for key in ("mse_bps2", "mae_bps", "rmse_bps")}
            clean["correlation"] = _number(metric.get("correlation"), low=-1, high=1, nullable=True)
            clean["direction_accuracy"] = _number(metric.get("direction_accuracy"), low=0, high=1, nullable=True)
            clean["samples"] = _count(metric.get("samples"))
            metrics[name][estimator] = clean
    return {"split": splits, "metrics": metrics, "selected_alpha": _number(value.get("selected_alpha"), low=0)}


def _simulation(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Invalid simulation evidence")
    # Final producer contract is deliberately narrow; no raw report or paths
    # can escape via this dictionary, even if the registry adds more fields.
    if value.get("currency", "USD") != "USD" or value.get("currency_units", "1/10000 USD") != "1/10000 USD":
        raise ValueError("Unsupported simulation currency")
    if type(value.get("price_units_per_dollar", 10000)) is not int or value.get("price_units_per_dollar", 10000) != 10000:
        raise ValueError("Unsupported simulation price units")
    result = {}
    account_valuation = "valuation" in value
    for key in ("capture_count", "sessions", "filled_quantity", "fill_count", "open_orders_at_end", "events", "fills"):
        if key in value:
            result[key] = _count(value[key])
    for key in ("realized_pnl_units", "unrealized_pnl_units", "fees_units", "rebates_units", "net_pnl_units", "drawdown_units", "max_drawdown_units", "inventory", "cash_before_fees_units"):
        if key in value:
            if value[key] is None and key in {"net_pnl_units", "unrealized_pnl_units", "drawdown_units", "max_drawdown_units"} and (account_valuation or value.get("pnl_mark_valid") is False):
                result[key] = None
                continue
            if type(value[key]) is not int or abs(value[key]) > 2**63 - 1:
                raise ValueError("Invalid simulation money")
            if key in {"fees_units", "rebates_units", "drawdown_units", "max_drawdown_units"} and value[key] < 0:
                raise ValueError("Negative simulation cost or drawdown")
            result[key] = value[key]
    for key in ("pnl_mark_valid", "killed", "open_exposure", "valid"):
        if key in value:
            if type(value[key]) is not bool:
                raise ValueError("Invalid simulation risk flag")
            result[key] = value[key]
    if not result:
        raise ValueError("Unsupported simulation report projection")
    ledger_keys = {"realized_pnl_units", "unrealized_pnl_units", "fees_units", "rebates_units", "net_pnl_units"}
    result["ledger_complete"] = ledger_keys.issubset(result) and all(type(result[key]) is int for key in ledger_keys)
    if result["ledger_complete"] and result["net_pnl_units"] != result["realized_pnl_units"] + result["unrealized_pnl_units"] - result["fees_units"] + result["rebates_units"]:
        raise ValueError("Simulation ledger identity mismatch")
    if "max_drawdown_units" in result:
        if "drawdown_units" in result and result["drawdown_units"] != result["max_drawdown_units"]:
            raise ValueError("Inconsistent simulation drawdown")
        result["drawdown_units"] = result["max_drawdown_units"]
        result["drawdown_scope"] = "max_single_scenario"
    else:
        result["drawdown_scope"] = "recorded_scope_unspecified"
    if account_valuation:
        result["valuation"] = _account_valuation(value["valuation"], value, result)
        result["net_pnl_valid"] = result["valuation"]["net_pnl_valid"]
        result["quote_mark_valid"] = result["valuation"]["quote_mark_valid"]
        result["drawdown_complete"] = result["valuation"]["drawdown_complete"]
        result["valuation_label"] = VALUATION_BASES[result["valuation"]["basis"]]
        result["valuation_contract"] = "account_valuation_v1"
    else:
        # Older evidence has no independent account-valuation proof. A closed or
        # stale market never silently upgrades an old null into a settled value.
        result["valuation"] = None
        result["valuation_contract"] = "legacy_quote_mark"
        result["quote_mark_valid"] = result.get("pnl_mark_valid")
        result["net_pnl_valid"] = result.get("pnl_mark_valid") is True
        result["drawdown_complete"] = None
        result["valuation_label"] = "旧报告按行情标记判断，未记录独立账户估值证据"
    if not account_valuation and result.get("pnl_mark_valid") is not True:
        for key in ("net_pnl_units", "unrealized_pnl_units", "drawdown_units", "max_drawdown_units"):
            result[key] = None
    for key in ("capture_count", "filled_quantity", "fill_count", "open_orders_at_end", "inventory", "pnl_mark_valid", "killed", "open_exposure"):
        result.setdefault(key, None)
    result["currency"] = "USD"
    result["price_units_per_dollar"] = 10000
    return result


def _account_valuation(value, raw, ledger):
    required = {"schema_version", "basis", "quote_mark_valid", "net_pnl_valid", "drawdown_complete",
                "unrealized_pnl_units", "net_pnl_units", "max_drawdown_units"}
    if not isinstance(value, dict) or not required.issubset(value) or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Unsupported account valuation evidence")
    if value["basis"] not in VALUATION_BASES:
        raise ValueError("Unknown account valuation basis")
    for key in ("quote_mark_valid", "net_pnl_valid", "drawdown_complete"):
        if type(value[key]) is not bool:
            raise ValueError("Invalid account valuation flag")
    ledger_required = {"realized_pnl_units", "unrealized_pnl_units", "fees_units", "rebates_units", "net_pnl_units",
                       "max_drawdown_units", "cash_before_fees_units", "inventory", "open_orders_at_end",
                       "open_exposure", "pnl_mark_valid", "killed", "filled_quantity", "fill_count"}
    if not ledger_required.issubset(ledger):
        raise ValueError("Account valuation requires a complete accounting and exposure report")
    if ledger["pnl_mark_valid"] != value["quote_mark_valid"]:
        raise ValueError("Quote freshness and account valuation flags differ")
    for key in ("unrealized_pnl_units", "net_pnl_units", "max_drawdown_units"):
        if value[key] != ledger[key] or type(value[key]) is not type(ledger[key]):
            raise ValueError("Account valuation and ledger metrics differ")
    if value["net_pnl_valid"]:
        if not ledger["ledger_complete"] or value["basis"] == "unavailable":
            raise ValueError("Valid account valuation requires a reconciled ledger")
    elif value["basis"] != "unavailable" or ledger["net_pnl_units"] is not None or ledger["unrealized_pnl_units"] is not None:
        raise ValueError("Invalid account terminal value must stay unavailable")
    if value["drawdown_complete"]:
        if not value["net_pnl_valid"] or type(ledger["max_drawdown_units"]) is not int:
            raise ValueError("Complete drawdown requires a valid terminal value and numeric drawdown")
    elif ledger["max_drawdown_units"] is not None:
        raise ValueError("Incomplete valuation path cannot publish a numeric drawdown")
    positions = raw.get("residual_inventory_by_session")
    if not isinstance(positions, dict) or not 1 <= len(positions) <= 2500:
        raise ValueError("Per-session exposure evidence is required")
    if any(not isinstance(key, str) or not 1 <= len(key) <= 128 or type(quantity) is not int or abs(quantity) > 2**63 - 1
           for key, quantity in positions.items()):
        raise ValueError("Invalid session exposure evidence")
    if sum(positions.values()) != ledger["inventory"]:
        raise ValueError("Session inventory total does not reconcile")
    exposure = any(quantity != 0 for quantity in positions.values()) or ledger["open_orders_at_end"] != 0
    if ledger["open_exposure"] != exposure:
        raise ValueError("Net inventory must not hide offsetting session exposure")
    if not any(positions.values()) and ledger["cash_before_fees_units"] != ledger["realized_pnl_units"]:
        raise ValueError("Flat-session cash must equal realized PnL before fees")
    if value["basis"] == "reconciled_flat_ledger":
        if (not value["net_pnl_valid"] or value["quote_mark_valid"] or exposure or ledger["inventory"] != 0
                or ledger["unrealized_pnl_units"] != 0 or ledger["cash_before_fees_units"] != ledger["realized_pnl_units"]):
            raise ValueError("Flat account valuation requires settled cash and no residual exposure")
    elif value["basis"] == "fresh_bbo":
        if not value["net_pnl_valid"] or value["quote_mark_valid"] is not True:
            raise ValueError("Fresh-book valuation requires a valid market mark")
    elif value["basis"] == "mixed_valid":
        if not value["net_pnl_valid"] or len(positions) < 2:
            raise ValueError("Mixed valuation requires multiple individually valid sessions")
    elif value["quote_mark_valid"] or value["drawdown_complete"]:
        raise ValueError("Unavailable valuation cannot claim a valid quote or complete path")
    if ledger["fill_count"] > ledger["filled_quantity"] or ("fills" in ledger and ledger["fills"] != ledger["fill_count"]):
        raise ValueError("Fill counts do not reconcile")
    if ledger["fill_count"] == 0:
        if ledger["filled_quantity"] != 0 or any(ledger[key] != 0 for key in ("fees_units", "rebates_units", "realized_pnl_units", "cash_before_fees_units", "inventory")):
            raise ValueError("No-fill simulation cannot contain fabricated account changes")
    return {key: value[key] for key in required}


def _project(value):
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("Unsupported registry status schema")
    if type(value.get("available")) is not bool or value.get("live_enabled") is not False:
        raise ValueError("Invalid registry status capability")
    state = value.get("state")
    if state not in {"ready", "empty", "not_configured", "invalid", "unavailable"}:
        raise ValueError("Invalid registry status")
    if not value["available"]:
        return _empty(state if state != "ready" else "invalid")
    if state not in {"ready", "empty"} or not isinstance(value.get("models"), list) or len(value["models"]) > MAX_MODELS:
        raise ValueError("Invalid model list")
    active = value.get("active")
    if not isinstance(active, dict) or not {"simulation", "paper"}.issubset(active):
        raise ValueError("Missing model pointers")
    answer = {**_empty("ready"), "available": True,
              "active": {key: _version(active.get(key), nullable=True) for key in ("simulation", "paper")}}
    seen = set()
    for model in value["models"]:
        if not isinstance(model, dict) or model.get("stage") not in STAGES or model.get("provenance_kind") not in PROVENANCE:
            raise ValueError("Invalid registered model")
        version = _version(model.get("version"))
        if version in seen:
            raise ValueError("Duplicate model version")
        seen.add(version)
        item = {"version": version, "stage": model["stage"], "stage_label": STAGES[model["stage"]],
                "provenance_kind": model["provenance_kind"], "provenance_label": PROVENANCE[model["provenance_kind"]],
                "registered_at_ns": _count(model.get("registered_at_ns"), maximum=2**63 - 1),
                "model_sha256": _hash(model.get("model_sha256")), "policy_sha256": _hash(model.get("policy_sha256")),
                "training_summary": _training(model.get("training_summary")),
                "simulation_summary": _simulation(model.get("simulation_summary")),
                "active_simulation": answer["active"]["simulation"] == version,
                "active_paper": answer["active"]["paper"] == version,
                "execution_authorized": False}
        for outcome in ("evaluation_passed", "simulation_passed"):
            status = model.get(outcome)
            if status is not None and type(status) is not bool:
                raise ValueError("Invalid validation outcome")
            item[outcome] = status
            code_key = outcome.removesuffix("_passed") + "_failure_codes"
            codes = _failure_codes(model.get(code_key))
            if status is True and codes:
                raise ValueError("Accepted evidence cannot also contain admission failures")
            item[code_key] = codes
            item[outcome.removesuffix("_passed") + "_failures"] = [
                {"code": code, "label": FAILURE_LABELS[code]} for code in codes]
        answer["models"].append(item)
    retrain = value.get("retrain")
    if retrain is not None:
        if not isinstance(retrain, dict) or retrain.get("state") not in RETRAIN_STATES:
            raise ValueError("Invalid retraining evidence")
        answer["retrain"] = {"state": retrain["state"], "state_label": RETRAIN_STATES[retrain["state"]],
                             "updated_at_ns": _count(retrain.get("updated_at_ns"), maximum=2**63 - 1)}
        reason = retrain.get("reason")
        if not isinstance(reason, str) or reason not in RETRAIN_REASONS:
            reason = "OPERATOR_REVIEW_REQUIRED"
        answer["retrain"]["trigger_label"] = RETRAIN_REASONS[reason]
        # Only locally mapped labels cross the boundary, never raw operator text.
    if not answer["models"]:
        answer["state"] = "empty"
    return answer


def get_ml_status():
    root = getattr(settings, "ML_MODEL_STORE_DIR", "")
    if not root:
        return _empty("not_configured")
    try:
        return _project(_read_registry(_root(root)))
    except (ImportError, OSError):
        return _empty("unavailable")
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, AttributeError):
        return _empty("invalid")


def ml_display_context(state):
    """Human formatting is separate from the machine-readable registry view."""
    models = []
    for model in state["models"]:
        item = dict(model)
        item["registered_at"] = datetime.fromtimestamp(item["registered_at_ns"] // 1000000000, UTC)
        item["metric_rows"] = []
        training = item["training_summary"]
        if training:
            for split, label in SPLITS.items():
                for estimator, estimator_label in ESTIMATORS.items():
                    row = {**training["metrics"][split][estimator], "split": label, "estimator": estimator_label,
                           "sessions": training["split"][split]["sessions"]}
                    item["metric_rows"].append(row)
        simulation = item["simulation_summary"]
        if simulation:
            item["simulation_money"] = {key: f"{Decimal(simulation[key]) / Decimal(10000):,.4f}" if type(simulation.get(key)) is int else "不可用"
                                        for key in ("realized_pnl_units", "unrealized_pnl_units", "fees_units", "rebates_units", "net_pnl_units", "drawdown_units")}
        models.append(item)
    retrain = state["retrain"]
    if retrain:
        retrain = {**retrain, "updated_at": datetime.fromtimestamp(retrain["updated_at_ns"] // 1000000000, UTC)}
    return {**state, "models": models, "retrain": retrain}
