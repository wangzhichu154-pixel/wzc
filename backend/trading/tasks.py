import json
import hashlib
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.audit import record_event
from .models import Dataset, PlatformSafetyState, ReplayJob, StrategyAutomation

log = logging.getLogger(__name__)
OUTPUT_LIMIT = 2 * 1024 * 1024


class EngineFailure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def validate_result(value, mode=ReplayJob.Mode.REPLAY):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise EngineFailure("INVALID_ENGINE_RESULT", "引擎返回了不支持的结果格式。")
    for key in ("messages", "symbols", "decisions", "trades", "last_sequence", "elapsed_ns"):
        if type(value.get(key)) is not int or value[key] < 0:
            raise EngineFailure("INVALID_ENGINE_RESULT", "引擎结果字段校验失败。")
    if value.get("book_valid") is not True:
        raise EngineFailure("INVALID_ORDER_BOOK", "订单簿完整性验证未通过。")
    if not isinstance(value.get("checksum"), str) or not 1 <= len(value["checksum"]) <= 128:
        raise EngineFailure("INVALID_ENGINE_RESULT", "引擎缺少确定性校验值。")
    if not isinstance(value.get("summary"), dict):
        raise EngineFailure("INVALID_ENGINE_RESULT", "引擎缺少摘要。")
    if mode == ReplayJob.Mode.SIMULATION:
        simulation = value["summary"].get("simulation")
        if value["summary"].get("mode") != "simulation" or not isinstance(simulation, dict):
            raise EngineFailure("INVALID_ENGINE_RESULT", "引擎没有返回所请求的模拟结果。")
        for key in ("orders_submitted", "orders_canceled", "fill_count", "filled_quantity", "open_orders_at_end"):
            if type(simulation.get(key)) is not int or simulation[key] < 0:
                raise EngineFailure("INVALID_ENGINE_RESULT", "模拟执行统计无效。")
        for key in ("realized_pnl_units", "unrealized_pnl_units", "fees_units", "rebates_units", "net_pnl_units", "drawdown_units", "inventory"):
            if type(simulation.get(key)) is not int:
                raise EngineFailure("INVALID_ENGINE_RESULT", "模拟账本字段无效。")
        if type(simulation.get("killed")) is not bool or type(simulation.get("pnl_mark_valid")) is not bool:
            raise EngineFailure("INVALID_ENGINE_RESULT", "模拟风控状态无效。")
    return value


def _command(job, source):
    binary = Path(settings.ENGINE_BINARY).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise EngineFailure("ENGINE_UNAVAILABLE", "C++ 回放引擎未安装或不可执行。")
    timeout = int(getattr(settings, "REPLAY_TIMEOUT_SECONDS", 120))
    memory = int(getattr(settings, "REPLAY_MEMORY_BYTES", 1073741824))
    args = [sys.executable, "-m", "trading.engine_runner", str(memory), str(timeout + 5), str(OUTPUT_LIMIT), str(os.getpid()), str(binary), "--input", str(source)]
    for name in ("max_order_size", "tick_size", "soft_inventory_limit", "hard_inventory_limit", "daily_loss_limit_cents", "orders_per_second"):
        value = job.risk_parameters.get(name)
        if type(value) is not int or value <= 0:
            raise EngineFailure("INVALID_RISK_CONFIGURATION", "任务风险参数不完整，请创建新任务。")
        args += ["--" + name.replace("_", "-"), str(value)]
    if job.mode not in ReplayJob.Mode.values:
        raise EngineFailure("INVALID_MODE", "任务模式无效。")
    if job.mode == ReplayJob.Mode.SIMULATION:
        from .services import DomainError, validate_simulation_snapshot
        try:
            parameters = validate_simulation_snapshot(job.simulation_parameters)
        except DomainError as exc:
            raise EngineFailure(exc.code, exc.message) from None
        args += ["--simulate"]
        for name, value in parameters.items():
            args += ["--" + name.replace("_", "-"), str(value)]
    return args, timeout


def _execute(job):
    root = Path(settings.MEDIA_ROOT).resolve()
    source = Path(job.dataset.file.path).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise EngineFailure("DATASET_UNAVAILABLE", "数据集文件不可用。")
    if source.stat().st_size != job.dataset.byte_size:
        raise EngineFailure("DATASET_INTEGRITY", "数据集大小与上传记录不一致。")
    with source.open("rb") as input_file:
        if hashlib.file_digest(input_file, "sha256").hexdigest() != job.dataset.sha256:
            raise EngineFailure("DATASET_INTEGRITY", "数据集校验和与上传记录不一致。")
    args, timeout = _command(job, source)
    backend_root = str(Path(__file__).resolve().parent.parent)
    with tempfile.TemporaryFile(mode="w+b") as out, tempfile.TemporaryFile(mode="w+b") as err:
        process = subprocess.Popen(args, cwd=backend_root, env={"PATH": os.defpath, "LANG": "C.UTF-8"}, stdout=out, stderr=err, stdin=subprocess.DEVNULL, close_fds=True)
        started, next_heartbeat = time.monotonic(), 0.0
        try:
            while process.poll() is None:
                elapsed = time.monotonic() - started
                if elapsed > timeout:
                    raise EngineFailure("ENGINE_TIMEOUT", "回放超出运行时间限制。")
                if os.fstat(out.fileno()).st_size > OUTPUT_LIMIT or os.fstat(err.fileno()).st_size > OUTPUT_LIMIT:
                    raise EngineFailure("ENGINE_OUTPUT_LIMIT", "引擎输出超出安全限制。")
                if elapsed >= next_heartbeat:
                    current = ReplayJob.objects.filter(pk=job.pk).values("status", "cancellation_requested").first()
                    if not current or current["status"] != ReplayJob.Status.RUNNING or current["cancellation_requested"]:
                        raise EngineFailure("CANCELED", "任务已取消。")
                    ReplayJob.objects.filter(pk=job.pk, status=ReplayJob.Status.RUNNING).update(heartbeat_at=timezone.now())
                    next_heartbeat = elapsed + 1
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
            out.seek(0)
            err.seek(0)
            if process.returncode != 0:
                detail = err.read(1000).decode("utf-8", errors="replace").strip()
                log.warning("Replay engine rejected input", extra={"job_id": str(job.pk), "return_code": process.returncode})
                raise EngineFailure("ENGINE_REJECTED_INPUT", detail or "引擎未能完成回放，可能达到资源限制。")
            payload = out.read(OUTPUT_LIMIT + 1)
            if len(payload) > OUTPUT_LIMIT:
                raise EngineFailure("ENGINE_OUTPUT_LIMIT", "引擎输出超出安全限制。")
            try:
                value = json.loads(payload, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            except (ValueError, UnicodeDecodeError):
                raise EngineFailure("INVALID_ENGINE_RESULT", "引擎返回的 JSON 无效。") from None
            return validate_result(value, job.mode)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


@shared_task(acks_late=True, reject_on_worker_lost=True)
def run_replay(job_id, contract_version=4):
    if contract_version != 4:
        raise ValueError("Unsupported replay task contract; deploy compatible workers")
    identity = ReplayJob.objects.filter(pk=job_id).values("owner_id", "dataset_id", "automation_id").first()
    if identity is None:
        return
    with transaction.atomic():
        # Match create_replay lock order to avoid cross-request deadlocks.
        owner = get_user_model().objects.select_for_update().get(pk=identity["owner_id"])
        dataset = Dataset.objects.select_for_update().get(pk=identity["dataset_id"])
        PlatformSafetyState.objects.get_or_create(pk=1)
        safety = PlatformSafetyState.objects.select_for_update().get(pk=1)
        automation = StrategyAutomation.objects.select_for_update().filter(pk=identity["automation_id"]).first() if identity["automation_id"] else None
        job = ReplayJob.objects.select_for_update().filter(pk=job_id).first()
        if not job or job.status != ReplayJob.Status.QUEUED:
            return
        automation_invalid = identity["automation_id"] and (automation is None or automation.deleted_at
            or automation.state != StrategyAutomation.State.RUNNING or automation.risk_latched
            or job.automation_generation != automation.generation)
        if automation is not None:
            from .automations import _feature_allowed
            automation_invalid = automation_invalid or not _feature_allowed(owner)
        if safety.halted or dataset.deleted_at or not owner.is_active or not owner.email_verified or automation_invalid:
            job.status = ReplayJob.Status.CANCELED
            job.error_code = "EXECUTION_DISABLED"
            job.error_message = "任务执行条件已失效。"
            job.finished_at = timezone.now()
            job.save()
            return
        job.status = ReplayJob.Status.RUNNING
        job.owner, job.dataset = owner, dataset
        job.started_at = job.heartbeat_at = timezone.now()
        job.error_code = job.error_message = ""
        job.save()
        record_event(None, "replay.started", user=job.owner, target=str(job.pk))
    result, code, message = {}, "", ""
    try:
        result = _execute(job)
    except EngineFailure as exc:
        code, message = exc.code, exc.message
    except Exception:
        log.exception("Replay execution failed", extra={"job_id": str(job.pk)})
        code, message = "WORKER_ERROR", "任务执行失败，请记录任务 ID 并联系管理员。"
    with transaction.atomic():
        current = ReplayJob.objects.select_for_update().get(pk=job.pk)
        if current.status != ReplayJob.Status.RUNNING:
            return
        if current.cancellation_requested or code == "CANCELED":
            current.status, code, message = ReplayJob.Status.CANCELED, "CANCELED", "任务已取消。"
            result = {}
        else:
            current.status = ReplayJob.Status.FAILED if code else ReplayJob.Status.SUCCEEDED
        current.result, current.error_code, current.error_message = result, code, message[:1000]
        current.finished_at = current.heartbeat_at = timezone.now()
        current.save()
        record_event(None, "replay.finished", user=job.owner, target=str(job.pk), metadata={"status": current.status, "error_code": code})


@shared_task
def reconcile_replays():
    """Periodic outbox retry and orphan detection; duplicate delivery is safe."""
    from .services import dispatch_job
    now = timezone.now()
    stale = now - timedelta(seconds=int(getattr(settings, "REPLAY_TIMEOUT_SECONDS", 120)) + 60)
    count = ReplayJob.objects.filter(status=ReplayJob.Status.RUNNING).filter(Q(heartbeat_at__lt=stale) | Q(heartbeat_at__isnull=True, started_at__lt=stale)).update(status=ReplayJob.Status.FAILED, finished_at=now, error_code="WORKER_LOST", error_message="执行器心跳失效；任务已终止，请检查后重试。")
    queued = ReplayJob.objects.filter(status=ReplayJob.Status.QUEUED).filter(Q(dispatched_at__isnull=True) | Q(dispatched_at__lt=now - timedelta(minutes=5)))[:100]
    dispatched = 0
    for job in queued:
        dispatched += int(dispatch_job(job))
    return {"orphaned": count, "dispatched": dispatched}


@shared_task
def purge_deleted_datasets():
    count = 0
    for dataset in Dataset.objects.filter(deleted_at__isnull=False, purged_at__isnull=True)[:100]:
        if dataset.jobs.filter(status__in=[ReplayJob.Status.QUEUED, ReplayJob.Status.RUNNING]).exists():
            continue
        try:
            dataset.file.storage.delete(dataset.file.name)
        except OSError:
            log.exception("Dataset purge failed", extra={"dataset_id": str(dataset.pk)})
            continue
        Dataset.objects.filter(pk=dataset.pk, purged_at__isnull=True).update(purged_at=timezone.now(), file="", sha256="")
        count += 1
    return count
