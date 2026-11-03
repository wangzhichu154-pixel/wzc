"""Authorized periodic historical simulations, with bounded work and latches.

Lock order: User -> Dataset -> PlatformSafetyState -> StrategyAutomation -> Job.
The scheduler never assumes permission to trade real money or carries simulated
profits from repeated historical input into a fictitious cumulative account.
"""
import logging
import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.audit import record_event
from .models import (AutomationCommand, Dataset, FeatureFlag, PlatformSafetyState,
                     ReplayJob, RiskProfile, StrategyAutomation)
from .services import (ACTIVE, DomainError, create_replay, dispatch_job, lock_active_user,
                       normalize_simulation, snapshot_risk, validate_simulation_snapshot)

log = logging.getLogger(__name__)


def validate_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", value):
        raise DomainError("IDEMPOTENCY_KEY_REQUIRED", "请提供 8–128 个字母、数字或 _.:- 组成的幂等键。")
    return value


def validate_risk_snapshot(owner, value):
    keys = {"max_order_size", "tick_size", "soft_inventory_limit", "hard_inventory_limit", "daily_loss_limit_cents", "orders_per_second"}
    if not isinstance(value, dict) or set(value) != keys or any(type(x) is not int or x <= 0 for x in value.values()):
        raise DomainError("INVALID_RISK_SNAPSHOT", "自动运行的风险参数快照无效。", 409)
    profile = RiskProfile(owner=owner, **{k: v for k, v in value.items() if k != "daily_loss_limit_cents"}, daily_loss_limit=Decimal(value["daily_loss_limit_cents"]) / 100)
    try:
        profile.clean_fields(exclude=["owner"])
        profile.clean()
    except ValidationError:
        raise DomainError("INVALID_RISK_SNAPSHOT", "自动运行的风险参数快照不符合限额。", 409) from None


def _feature_allowed(user):
    return all(flag.allows(user) for flag in FeatureFlag.objects.filter(name__in=["replay", "simulation", "automations"]))


def create_automation(user, *, name, dataset, interval_seconds=300, max_runs=10,
                      simulation_parameters=None, key, request=None):
    key = validate_key(key)
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise DomainError("VALIDATION_ERROR", "名称必须为 1–120 个字符。")
    if type(interval_seconds) is not int or not 60 <= interval_seconds <= 86400:
        raise DomainError("VALIDATION_ERROR", "运行间隔必须为 60–86400 秒。")
    if type(max_runs) is not int or not 1 <= max_runs <= 1000:
        raise DomainError("VALIDATION_ERROR", "最大运行次数必须为 1–1000。")
    parameters = normalize_simulation("simulation", simulation_parameters)
    with transaction.atomic():
        owner = lock_active_user(user, verified=True)
        dataset = Dataset.objects.select_for_update().get(pk=dataset.pk)
        if dataset.owner_id != owner.pk or dataset.deleted_at:
            raise DomainError("NOT_FOUND", "数据集不存在。", 404)
        existing = StrategyAutomation.objects.filter(owner=owner, creation_key=key).first()
        if existing:
            if (existing.name != name.strip() or existing.dataset_id != dataset.pk
                    or existing.interval_seconds != interval_seconds or existing.max_runs != max_runs
                    or existing.simulation_parameters != parameters or existing.deleted_at):
                raise DomainError("IDEMPOTENCY_CONFLICT", "幂等键已用于不同自动运行配置。", 409)
            return existing, True
        if StrategyAutomation.objects.filter(owner=owner, deleted_at__isnull=True).count() >= getattr(settings, "MAX_AUTOMATIONS_PER_USER", 20):
            raise DomainError("AUTOMATION_QUOTA", "自动运行配置数量已达上限。", 409)
        if not _feature_allowed(owner):
            raise DomainError("FEATURE_DISABLED", "当前账户暂未开放自动模拟功能。", 403)
        risk = snapshot_risk(owner)
        validate_risk_snapshot(owner, risk)
        automation = StrategyAutomation.objects.create(owner=owner, name=name.strip(), dataset=dataset,
            interval_seconds=interval_seconds, max_runs=max_runs, risk_snapshot=risk,
            simulation_parameters=parameters, creation_key=key)
        record_event(request, "automation.created", user=owner, target=str(automation.pk), metadata={"dataset": str(dataset.pk), "interval_seconds": interval_seconds, "max_runs": max_runs, "state": "stopped"})
        return automation, False


def _cancel_inflight(automation, reason):
    now = timezone.now()
    for job in ReplayJob.objects.select_for_update().filter(automation=automation, status__in=ACTIVE):
        if job.status == ReplayJob.Status.QUEUED:
            job.status = ReplayJob.Status.CANCELED
            job.finished_at = now
            job.error_code, job.error_message = "AUTOMATION_STOPPED", reason
        else:
            job.cancellation_requested = True
        job.save(update_fields=["status", "finished_at", "error_code", "error_message", "cancellation_requested"])
        record_event(None, "automation.job_cancel_requested", user=automation.owner, target=str(job.pk), metadata={"automation": str(automation.pk)})


def _pause(automation, code, message, *, fault=False, latch=False, cancel=True):
    automation.state = StrategyAutomation.State.FAULTED if fault else StrategyAutomation.State.PAUSED
    automation.next_run_at = None
    automation.last_reason_code, automation.last_reason = code, message
    automation.risk_latched = automation.risk_latched or latch
    automation.generation += 1
    if cancel:
        _cancel_inflight(automation, message)
    record_event(None, "automation.paused", user=automation.owner, target=str(automation.pk), metadata={"code": code, "risk_latched": automation.risk_latched, "generation": automation.generation})


def _account_terminal(automation):
    if not automation.last_job_id or automation.last_accounted_job_id == automation.last_job_id:
        return
    job = ReplayJob.objects.select_for_update().get(pk=automation.last_job_id)
    if job.owner_id != automation.owner_id or job.automation_id != automation.pk:
        _pause(automation, "AUTOMATION_INTEGRITY", "自动任务关联不一致，需管理员检查。", fault=True, latch=True)
        return
    if job.status in ACTIVE:
        return
    automation.last_accounted_job = job
    if job.status == ReplayJob.Status.SUCCEEDED:
        simulation = job.result.get("summary", {}).get("simulation", {})
        if simulation.get("killed") is True:
            _pause(automation, "SIMULATION_RISK_KILL", "上一轮模拟触发风险熔断，自动运行已锁存暂停。", latch=True)
        elif simulation.get("pnl_mark_valid") is not True:
            _pause(automation, "INVALID_SIMULATION_MARK", "上一轮模拟估值无效，需检查行情后手动恢复。", latch=True)
        else:
            automation.consecutive_failures = 0
    elif job.status == ReplayJob.Status.FAILED or (job.status == ReplayJob.Status.CANCELED and automation.state == StrategyAutomation.State.RUNNING):
        automation.consecutive_failures += 1
        if automation.consecutive_failures >= 3:
            _pause(automation, "CONSECUTIVE_FAILURES", "连续三轮失败或被取消，自动运行已故障锁存。", fault=True, latch=True)
    record_event(None, "automation.job_accounted", user=automation.owner, target=str(automation.pk), metadata={"job": str(job.pk), "status": job.status, "consecutive_failures": automation.consecutive_failures})


def command_automation(user, automation, *, action, password, key,
                       acknowledge_latch=False, request=None):
    key = validate_key(key)
    if action not in ("start", "stop") or type(acknowledge_latch) is not bool:
        raise DomainError("VALIDATION_ERROR", "操作或风险确认参数无效。")
    if not isinstance(password, str) or not password or len(password) > 1024:
        raise DomainError("REAUTHENTICATION_REQUIRED", "请输入当前密码。", 403)
    pending_error = None
    with transaction.atomic():
        owner = lock_active_user(user, verified=True)
        if not owner.check_password(password):
            raise DomainError("REAUTHENTICATION_FAILED", "密码不正确。", 403)
        # Retain the same row ordering as scheduler and task-claim paths.
        dataset = Dataset.objects.select_for_update().get(pk=automation.dataset_id)
        PlatformSafetyState.objects.get_or_create(pk=1)
        safety = PlatformSafetyState.objects.select_for_update().get(pk=1)
        automation = StrategyAutomation.objects.select_for_update().get(pk=automation.pk)
        if automation.owner_id != owner.pk or automation.deleted_at:
            raise DomainError("NOT_FOUND", "自动运行不存在。", 404)
        receipt = AutomationCommand.objects.filter(owner=owner, key=key).first()
        if receipt:
            if receipt.automation_id != automation.pk or receipt.action != action or receipt.acknowledged_latch != acknowledge_latch:
                raise DomainError("IDEMPOTENCY_CONFLICT", "此命令幂等键已用于其他操作。", 409)
            return automation, receipt, True
        _account_terminal(automation)
        try:
            if action == "start":
                if dataset.deleted_at or dataset.owner_id != owner.pk:
                    raise DomainError("DATASET_UNAVAILABLE", "数据集已不可用。", 409)
                if safety.halted or not _feature_allowed(owner):
                    raise DomainError("EXECUTION_DISABLED", "平台或功能开关禁止自动运行。", 409)
                if automation.runs_created >= automation.max_runs:
                    raise DomainError("RUN_LIMIT_REACHED", "运行次数已达到上限；如需继续，请明确创建新配置。", 409)
                if automation.risk_latched and not acknowledge_latch:
                    raise DomainError("AUTOMATION_LATCHED", "此配置存在风险或故障锁存，请检查原因并明确确认后再恢复。", 409)
                validate_risk_snapshot(owner, automation.risk_snapshot)
                validate_simulation_snapshot(automation.simulation_parameters)
                if automation.state != StrategyAutomation.State.RUNNING:
                    if automation.jobs.filter(status__in=ACTIVE).exists():
                        raise DomainError("ACTIVE_JOB", "请等待上一轮取消或结束后再启动。", 409)
                    running = StrategyAutomation.objects.filter(owner=owner, state=StrategyAutomation.State.RUNNING, deleted_at__isnull=True).count()
                    if running >= getattr(settings, "MAX_RUNNING_AUTOMATIONS_PER_USER", 3):
                        raise DomainError("AUTOMATION_ACTIVE_QUOTA", "同时运行的自动配置已达上限。", 409)
                    if automation.risk_latched:
                        record_event(request, "automation.latch_acknowledged", user=owner, target=str(automation.pk), metadata={"previous_reason": automation.last_reason_code})
                    automation.risk_latched = False
                    automation.consecutive_failures = 0
                    automation.generation += 1
                    automation.state = StrategyAutomation.State.RUNNING
                    automation.next_run_at = timezone.now()
                    automation.last_reason_code = automation.last_reason = ""
            else:
                automation.state = StrategyAutomation.State.STOPPED
                automation.next_run_at = None
                automation.generation += 1
                if not automation.risk_latched:
                    automation.last_reason_code, automation.last_reason = "USER_STOP", "用户已停止自动运行。"
                _cancel_inflight(automation, "用户停止自动运行。")
        except DomainError as exc:
            # Persist newly discovered risk latches even when a start is denied.
            pending_error = exc
        automation.save()
        if pending_error is None:
            receipt = AutomationCommand.objects.create(owner=owner, automation=automation, key=key, action=action,
                acknowledged_latch=acknowledge_latch, outcome={"state": automation.state, "generation": automation.generation})
            record_event(request, f"automation.{action}", user=owner, target=str(automation.pk), metadata={"generation": automation.generation, "command": str(receipt.pk)})
    if pending_error is not None:
        raise pending_error
    return automation, receipt, False


def tick_one(automation_id, *, now=None):
    """Create at most one new run; never replay missed scheduler intervals."""
    now = now or timezone.now()
    identity = StrategyAutomation.objects.filter(pk=automation_id).values("owner_id", "dataset_id").first()
    if identity is None:
        return "missing"
    new_job = None
    outcome = "idle"
    with transaction.atomic():
        owner = get_user_model().objects.select_for_update().get(pk=identity["owner_id"])
        dataset = Dataset.objects.select_for_update().get(pk=identity["dataset_id"])
        PlatformSafetyState.objects.get_or_create(pk=1)
        safety = PlatformSafetyState.objects.select_for_update().get(pk=1)
        automation = StrategyAutomation.objects.select_for_update().get(pk=automation_id)
        if automation.deleted_at or automation.state != StrategyAutomation.State.RUNNING:
            return "inactive"
        automation.owner, automation.dataset = owner, dataset
        automation.last_checked_at = now
        if automation.risk_latched:
            _pause(automation, "LATCHED_STATE", "存在未解除的风险锁存，不能自动恢复。", latch=True)
        elif not owner.is_active or owner.deleted_at or not owner.email_verified:
            _pause(automation, "ACCOUNT_UNAVAILABLE", "账户授权失效，自动运行已暂停。")
        elif dataset.deleted_at or dataset.owner_id != owner.pk:
            _pause(automation, "DATASET_UNAVAILABLE", "数据集不可用，自动运行已暂停。", fault=True)
        elif safety.halted or not _feature_allowed(owner):
            _pause(automation, "EXECUTION_DISABLED", "平台或功能开关已关闭，恢复后仍需手动启动。")
        else:
            _account_terminal(automation)
            if automation.state == StrategyAutomation.State.RUNNING:
                if automation.jobs.filter(status__in=ACTIVE).exists():
                    outcome = "active"
                elif automation.runs_created >= automation.max_runs:
                    automation.state, automation.next_run_at = StrategyAutomation.State.STOPPED, None
                    automation.last_reason_code, automation.last_reason = "RUN_LIMIT_REACHED", "已完成配置允许的最大运行次数。"
                    record_event(None, "automation.run_limit_reached", user=owner, target=str(automation.pk), metadata={"runs_created": automation.runs_created})
                    outcome = "limit"
                elif automation.next_run_at is not None and automation.next_run_at <= now:
                    try:
                        validate_risk_snapshot(owner, automation.risk_snapshot)
                        validate_simulation_snapshot(automation.simulation_parameters)
                        key = f"auto:{automation.pk}:{automation.generation}:{automation.runs_created + 1}"
                        if ReplayJob.objects.filter(owner=owner, idempotency_key=key).exists():
                            raise DomainError("AUTOMATION_ID_COLLISION", "自动任务身份冲突，需管理员检查。", 409)
                        new_job = create_replay(owner, dataset, mode="simulation", simulation_parameters=automation.simulation_parameters, idempotency_key=key)
                        new_job.automation = automation
                        new_job.automation_generation = automation.generation
                        new_job.risk_parameters = dict(automation.risk_snapshot)
                        new_job.save(update_fields=["automation", "automation_generation", "risk_parameters"])
                        automation.last_job = new_job
                        automation.runs_created += 1
                        automation.last_reason_code = automation.last_reason = ""
                        record_event(None, "automation.run_created", user=owner, target=str(automation.pk), metadata={"job": str(new_job.pk), "run_number": automation.runs_created, "generation": automation.generation})
                        outcome = "created"
                    except DomainError as exc:
                        if exc.code == "JOB_QUOTA":
                            automation.last_reason_code, automation.last_reason = exc.code, "等待账户执行配额；不会补发错过的运行。"
                            outcome = "quota"
                        else:
                            _pause(automation, exc.code, exc.message, fault=True, latch=True)
                            outcome = "faulted"
                    if automation.state == StrategyAutomation.State.RUNNING:
                        automation.next_run_at = now + timedelta(seconds=automation.interval_seconds)
        automation.save()
    if new_job is not None:
        dispatch_job(new_job)
    return outcome


def pause_dataset_automations(user, dataset):
    """Caller holds owner and dataset locks and has rejected active jobs."""
    for automation in StrategyAutomation.objects.select_for_update().filter(owner=user, dataset=dataset, deleted_at__isnull=True):
        _pause(automation, "DATASET_DELETED", "关联数据集已删除，自动运行不可继续。", fault=True)
        automation.save()
