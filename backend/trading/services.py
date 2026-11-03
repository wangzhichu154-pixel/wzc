"""Business operations, ownership checks, and durable task intents.

No HTTP endpoint can place a live broker order. Research settings are snapshotted
on each replay so changes do not silently alter in-flight or historical jobs.
"""
import hashlib
import logging
import re
from decimal import Decimal
from pathlib import Path
from tempfile import SpooledTemporaryFile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from accounts.audit import record_event
from .models import Dataset, FeatureFlag, Feedback, PlatformSafetyState, ReplayJob, RiskProfile

log = logging.getLogger(__name__)
ACTIVE = (ReplayJob.Status.QUEUED, ReplayJob.Status.RUNNING)
SIMULATION_DEFAULTS = {
    "latency_ns": 1000000,
    "cancel_latency_ns": 1000000,
    "fee_per_share_units": 30,
    "min_fee_per_order_units": 0,
    "slippage_buffer_units": 0,
    "min_net_edge_units": 0,
    "rebate_per_share_units": 0,
    "simulation_window_ns": 1000000000,
}
SIMULATION_BOUNDS = {
    "latency_ns": (1000, 10000000000),
    "cancel_latency_ns": (1000, 10000000000),
    "fee_per_share_units": (0, 1000000000),
    "rebate_per_share_units": (0, 1000000),
    "min_fee_per_order_units": (0, 1000000000),
    "slippage_buffer_units": (0, 1000000000),
    "min_net_edge_units": (0, 1000000000),
    "simulation_window_ns": (1000000, 60000000000),
}


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def lock_active_user(user, *, verified=False):
    """Call inside atomic(): same ownership lock as account deletion."""
    current = get_user_model().objects.select_for_update().get(pk=user.pk)
    if not current.is_active or current.deleted_at is not None:
        raise DomainError("ACCOUNT_UNAVAILABLE", "账户当前不可用。", 403)
    if verified and not current.email_verified:
        raise DomainError("EMAIL_NOT_VERIFIED", "请先验证邮箱。", 403)
    return current


def normalize_simulation(mode, parameters):
    if mode not in ReplayJob.Mode.values:
        raise DomainError("INVALID_MODE", "仅支持行情回放或自动做市模拟。")
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict) or set(parameters) - set(SIMULATION_DEFAULTS):
        raise DomainError("INVALID_SIMULATION_PARAMETERS", "模拟参数包含不支持的字段。")
    if mode == ReplayJob.Mode.REPLAY:
        if parameters:
            raise DomainError("INVALID_SIMULATION_PARAMETERS", "行情回放模式不接受模拟成交参数。")
        return {}
    normalized = {**SIMULATION_DEFAULTS, **parameters}
    for key, value in normalized.items():
        low, high = SIMULATION_BOUNDS[key]
        if type(value) is not int or not low <= value <= high:
            raise DomainError("INVALID_SIMULATION_PARAMETERS", f"{key} 必须为 {low}–{high} 范围的整数。")
    return normalized


def validate_simulation_snapshot(parameters):
    """Never upgrade persisted execution intent implicitly across engine contracts."""
    if not isinstance(parameters, dict) or set(parameters) != set(SIMULATION_DEFAULTS):
        raise DomainError("SIMULATION_CONTRACT_CHANGED", "此任务使用旧模拟参数契约；请检查成本参数并创建新任务或自动配置。", 409)
    return normalize_simulation(ReplayJob.Mode.SIMULATION, parameters)


def validate_envelope(stream):
    """Validate every length prefix; semantic ITCH validation belongs to C++."""
    stream.seek(0)
    count = 0
    while True:
        prefix = stream.read(2)
        if not prefix:
            break
        if len(prefix) != 2:
            raise DomainError("INVALID_ITCH", "文件末尾的消息长度前缀不完整。")
        length = int.from_bytes(prefix, "big")
        if not 11 <= length <= 4096:
            raise DomainError("INVALID_ITCH", "消息长度不符合 ITCH 文件格式。")
        payload = stream.read(length)
        if len(payload) != length:
            raise DomainError("INVALID_ITCH", "ITCH 消息被截断。")
        count += 1
    stream.seek(0)
    if not count:
        raise DomainError("EMPTY_DATASET", "不能上传空行情文件。")
    return count


def upload_dataset(user, name, upload, request=None):
    if not user.email_verified:
        raise DomainError("EMAIL_NOT_VERIFIED", "请先验证邮箱。", 403)
    original = Path(upload.name.replace("\\", "/")).name
    if Path(original).suffix.lower() != ".itch":
        raise DomainError("UNSUPPORTED_FORMAT", "仅支持 .itch 长度前缀文件。")
    max_size = getattr(settings, "MAX_DATASET_BYTES", 268435456)
    if upload.size is not None and upload.size > max_size:
        raise DomainError("FILE_TOO_LARGE", "文件超过上传限制。", 413)
    name = name.strip()
    if not name or len(name) > 120:
        raise DomainError("INVALID_NAME", "名称必须为 1–120 个字符。")
    digest, size = hashlib.sha256(), 0
    with SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as stream:
        for chunk in upload.chunks():
            size += len(chunk)
            if size > max_size:
                raise DomainError("FILE_TOO_LARGE", "文件超过上传限制。", 413)
            digest.update(chunk)
            stream.write(chunk)
        count = validate_envelope(stream)
        dataset = Dataset(owner=user, name=name, original_name=original[:200], byte_size=size,
                          sha256=digest.hexdigest(), message_count=count)
        stored_name = None
        try:
            with transaction.atomic():
                # Serialize quota checks for this user, not for the whole service.
                lock_active_user(user, verified=True)
                existing = Dataset.objects.filter(owner=user, purged_at__isnull=True)
                used = existing.aggregate(total=Sum("byte_size"))["total"] or 0
                if used + size > getattr(settings, "MAX_USER_DATASET_BYTES", 1073741824):
                    raise DomainError("STORAGE_QUOTA", "账户存储配额不足。", 409)
                if existing.count() >= getattr(settings, "MAX_USER_DATASETS", 100):
                    raise DomainError("DATASET_QUOTA", "数据集数量已达配额。", 409)
                dataset.file.save(f"{digest.hexdigest()}.itch", File(stream), save=False)
                stored_name = dataset.file.name
                dataset.save()
                record_event(request, "dataset.upload", user=user, target=str(dataset.pk),
                             metadata={"bytes": size, "messages": count})
        except Exception:
            if stored_name:
                dataset.file.storage.delete(stored_name)
            raise
    return dataset


def snapshot_risk(user):
    profile, _ = RiskProfile.objects.get_or_create(owner=user)
    return {
        "max_order_size": profile.max_order_size,
        "tick_size": profile.tick_size,
        "soft_inventory_limit": profile.soft_inventory_limit,
        "hard_inventory_limit": profile.hard_inventory_limit,
        "daily_loss_limit_cents": int(Decimal(profile.daily_loss_limit) * 100),
        "orders_per_second": profile.orders_per_second,
    }


def create_replay(user, dataset, request=None, parent=None, idempotency_key=None,
                  mode=ReplayJob.Mode.REPLAY, simulation_parameters=None):
    if not user.email_verified:
        raise DomainError("EMAIL_NOT_VERIFIED", "请先验证邮箱。", 403)
    if idempotency_key is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", idempotency_key):
        raise DomainError("INVALID_IDEMPOTENCY_KEY", "Idempotency-Key 必须为 8–128 个字母、数字或 _.:- 字符。")
    simulation_parameters = normalize_simulation(mode, simulation_parameters)
    with transaction.atomic():
        lock_active_user(user, verified=True)
        if idempotency_key:
            existing = ReplayJob.objects.filter(owner=user, idempotency_key=idempotency_key).first()
            if existing is not None:
                if (existing.dataset_id != dataset.pk or existing.parent_id != (parent.pk if parent else None)
                        or existing.mode != mode or existing.simulation_parameters != simulation_parameters):
                    raise DomainError("IDEMPOTENCY_CONFLICT", "此幂等键已用于不同请求。", 409)
                return existing
        dataset = Dataset.objects.select_for_update().get(pk=dataset.pk)
        if dataset.owner_id != user.pk or dataset.deleted_at:
            raise DomainError("NOT_FOUND", "数据集不存在。", 404)
        PlatformSafetyState.objects.get_or_create(pk=1)
        safety = PlatformSafetyState.objects.select_for_update().get(pk=1)
        if safety.halted:
            raise DomainError("RESEARCH_PAUSED", "管理员已暂停新研究任务。", 503)
        names = ["replay", "simulation"] if mode == ReplayJob.Mode.SIMULATION else ["replay"]
        for flag in FeatureFlag.objects.filter(name__in=names):
            if not flag.allows(user):
                raise DomainError("FEATURE_DISABLED", "当前账户暂未开放所选研究功能。", 403)
        if ReplayJob.objects.filter(owner=user, status__in=ACTIVE).count() >= getattr(settings, "MAX_ACTIVE_REPLAYS_PER_USER", 2):
            raise DomainError("JOB_QUOTA", "请等待现有任务完成后再提交。", 409)
        attempt = 1
        if parent is not None:
            parent = ReplayJob.objects.select_for_update().get(pk=parent.pk)
            if parent.owner_id != user.pk or parent.status not in (ReplayJob.Status.FAILED, ReplayJob.Status.CANCELED):
                raise DomainError("INVALID_RETRY", "只能重试自己的失败或取消任务。", 409)
            if ReplayJob.objects.filter(parent=parent).exists():
                raise DomainError("ALREADY_RETRIED", "此任务已创建重试，请查看后续任务。", 409)
            attempt = parent.attempt + 1
            if attempt > getattr(settings, "MAX_REPLAY_ATTEMPTS", 3):
                raise DomainError("RETRY_LIMIT", "已达到重试上限，请检查文件或联系管理员。", 409)
        job = ReplayJob.objects.create(owner=user, dataset=dataset, attempt=attempt, parent=parent,
                                       risk_parameters=snapshot_risk(user), idempotency_key=idempotency_key,
                                       mode=mode, simulation_parameters=simulation_parameters)
        record_event(request, "replay.created", user=user, target=str(job.pk), metadata={"dataset": str(dataset.pk), "attempt": attempt, "mode": mode})
    return job


def dispatch_job(job):
    from .tasks import run_replay
    if job.status != ReplayJob.Status.QUEUED:
        return True
    try:
        # The explicit keyword makes pre-simulation workers reject the task
        # instead of silently executing a simulation request as plain replay.
        run_replay.apply_async(args=[str(job.pk)], kwargs={"contract_version": 4}, task_id=f"replay-{job.pk}")
        ReplayJob.objects.filter(pk=job.pk, status=ReplayJob.Status.QUEUED).update(dispatched_at=timezone.now(), error_code="", error_message="")
        return True
    except Exception:
        # The DB row is a durable intent. A periodic dispatcher can retry it;
        # returning False means it was not confirmed delivered to the broker.
        log.exception("Replay dispatch failed", extra={"job_id": str(job.pk)})
        ReplayJob.objects.filter(pk=job.pk, status=ReplayJob.Status.QUEUED).update(error_code="QUEUE_UNAVAILABLE", error_message="任务已保存，等待任务队列恢复。")
        return False


def cancel_job(user, job, request=None):
    with transaction.atomic():
        lock_active_user(user)
        job = ReplayJob.objects.select_for_update().get(pk=job.pk, owner=user)
        if job.status == ReplayJob.Status.QUEUED:
            job.status, job.finished_at = ReplayJob.Status.CANCELED, timezone.now()
        elif job.status == ReplayJob.Status.RUNNING:
            job.cancellation_requested = True
        else:
            raise DomainError("JOB_FINISHED", "任务已经结束。", 409)
        job.save(update_fields=["status", "finished_at", "cancellation_requested"])
        record_event(request, "replay.cancel_requested", user=user, target=str(job.pk))
    return job


def delete_dataset(user, dataset, request=None):
    with transaction.atomic():
        lock_active_user(user)
        dataset = Dataset.objects.select_for_update().get(pk=dataset.pk, owner=user, deleted_at__isnull=True)
        if dataset.jobs.filter(status__in=ACTIVE).exists():
            raise DomainError("DATASET_IN_USE", "请先取消或等待该数据集的运行任务。", 409)
        dataset.deleted_at = timezone.now()
        dataset.save(update_fields=["deleted_at"])
        from .automations import pause_dataset_automations
        pause_dataset_automations(user, dataset)
        record_event(request, "dataset.delete", user=user, target=str(dataset.pk))
        transaction.on_commit(lambda: _request_purge())


def _request_purge():
    from .tasks import purge_deleted_datasets
    try:
        purge_deleted_datasets.delay()
    except Exception:
        log.exception("Dataset purge dispatch failed; periodic maintenance will retry")


def prepare_account_deletion(user):
    """Called within the account deletion transaction; no broker side effects."""
    if ReplayJob.objects.filter(owner=user, status=ReplayJob.Status.RUNNING).exists():
        raise ValidationError("请先取消正在运行的回放，等待任务结束后注销。")
    now = timezone.now()
    from .models import ResearchAssessment, StrategyAutomation
    from django.db.models import F
    StrategyAutomation.objects.filter(owner=user).update(state="stopped", next_run_at=None, deleted_at=now,
        name="Deleted automation", risk_snapshot={}, simulation_parameters={}, generation=F("generation") + 1,
        last_reason_code="ACCOUNT_DELETED", last_reason="")
    ReplayJob.objects.filter(owner=user, status=ReplayJob.Status.QUEUED).update(status=ReplayJob.Status.CANCELED, finished_at=now)
    ReplayJob.objects.filter(owner=user).update(result={}, risk_parameters={}, simulation_parameters={}, error_message="")
    ResearchAssessment.objects.filter(owner=user).update(snapshot={}, result={}, deleted_at=now, snapshot_sha256="", request_sha256="", idempotency_key=None)
    RiskProfile.objects.filter(owner=user).delete()
    Dataset.objects.filter(owner=user).update(deleted_at=now, name="Deleted dataset", original_name="")
    Feedback.objects.filter(owner=user).update(subject="Deleted account feedback", message="", response="")
    transaction.on_commit(_request_purge)


def export_user_data(user):
    from .models import ResearchAssessment, StrategyAutomation
    return {
        "assessments": list(ResearchAssessment.objects.filter(owner=user, deleted_at__isnull=True).values("id", "policy_version", "snapshot", "result", "snapshot_sha256", "created_at")),
        "datasets": list(Dataset.objects.filter(owner=user).values("id", "name", "byte_size", "sha256", "message_count", "created_at", "deleted_at")),
        "replays": list(ReplayJob.objects.filter(owner=user).values("id", "dataset_id", "status", "mode", "risk_parameters", "simulation_parameters", "result", "created_at", "finished_at")),
        "risk_profile": list(RiskProfile.objects.filter(owner=user).values("max_order_size", "tick_size", "soft_inventory_limit", "hard_inventory_limit", "daily_loss_limit", "orders_per_second")),
        "feedback": list(Feedback.objects.filter(owner=user).values("id", "subject", "message", "status", "response", "created_at")),
        "automations": list(StrategyAutomation.objects.filter(owner=user).values("id", "name", "dataset_id", "state", "risk_snapshot", "simulation_parameters", "interval_seconds", "max_runs", "runs_created", "risk_latched", "created_at", "deleted_at")),
    }


def update_risk_profile(user, values, request=None):
    with transaction.atomic():
        current = lock_active_user(user, verified=True)
        profile, _ = RiskProfile.objects.get_or_create(owner=current)
        for name in ("max_order_size", "soft_inventory_limit", "hard_inventory_limit", "daily_loss_limit", "orders_per_second"):
            setattr(profile, name, values[name])
        profile.tick_size = values.get("tick_size", profile.tick_size)
        profile.full_clean()
        profile.save()
        record_event(request, "risk.research_profile_changed", user=current, target=str(profile.pk), metadata={k: str(v) for k, v in values.items()})
        return profile


def create_feedback(user, values, request=None):
    with transaction.atomic():
        current = lock_active_user(user)
        item = Feedback(owner=current, subject=values["subject"], message=values["message"])
        item.full_clean()
        item.save()
        record_event(request, "feedback.created", user=current, target=str(item.pk))
        return item
