import json
import uuid
from functools import wraps
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count
from django.http import FileResponse, Http404, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.debug import sensitive_post_parameters

from accounts.audit import record_event
from accounts.ratelimit import allow_request
from .forms import DatasetUploadForm, FeedbackForm, ReplayRequestForm, RiskProfileForm, SafetyForm
from .models import Dataset, Feedback, PlatformSafetyState, ReplayJob, RiskProfile, StrategyAutomation
from .services import (ACTIVE, DomainError, cancel_job, create_feedback, create_replay,
                       delete_dataset, dispatch_job, lock_active_user, update_risk_profile, upload_dataset)


def _dataset(user, pk):
    return get_object_or_404(Dataset, pk=pk, owner=user, deleted_at__isnull=True)


def _job(user, pk):
    return get_object_or_404(ReplayJob.objects.select_related("dataset"), pk=pk, owner=user)


def _verified(user):
    if not user.is_active or not user.email_verified:
        raise PermissionDenied("请先验证邮箱。")


@login_required
@require_GET
def dashboard(request):
    datasets = Dataset.objects.filter(owner=request.user, deleted_at__isnull=True)
    jobs = ReplayJob.objects.filter(owner=request.user)
    counts = {x["status"]: x["count"] for x in jobs.values("status").annotate(count=Count("pk"))}
    safety = PlatformSafetyState.objects.filter(pk=1).first()
    automations = StrategyAutomation.objects.filter(owner=request.user, deleted_at__isnull=True)
    return render(request, "trading/dashboard.html", {"dataset_count": datasets.count(), "job_count": jobs.count(), "running_count": counts.get("running", 0), "completed_count": counts.get("succeeded", 0), "recent_jobs": jobs.select_related("dataset")[:5], "safety": safety,
        "automation_running": automations.filter(state="running").count(), "automation_attention": automations.filter(state__in=["paused", "faulted"]).count()})


@login_required
def datasets(request):
    if request.method not in ("GET", "POST"):
        return envelope(False, "METHOD_NOT_ALLOWED", "不支持此方法。", status=405)
    form = DatasetUploadForm(request.POST if request.method == "POST" else None, request.FILES if request.method == "POST" else None)
    if request.method == "POST":
        _verified(request.user)
        if form.is_valid():
            try:
                dataset = upload_dataset(request.user, form.cleaned_data["name"], form.cleaned_data["file"], request)
            except DomainError as exc:
                form.add_error(None, exc.message)
            else:
                messages.success(request, "文件已安全保存，长度前缀已验证。完整消息语义将在回放时校验。")
                return redirect("trading:dataset_detail", pk=dataset.pk)
    page = Paginator(Dataset.objects.filter(owner=request.user, deleted_at__isnull=True), 20).get_page(request.GET.get("page"))
    return render(request, "trading/datasets.html", {"form": form, "page_obj": page, "max_size_mb": getattr(settings, "MAX_DATASET_BYTES", 268435456) // 1048576})


@login_required
@require_GET
def dataset_detail(request, pk):
    dataset = _dataset(request.user, pk)
    return render(request, "trading/dataset_detail.html", {"dataset": dataset, "jobs": dataset.jobs.filter(owner=request.user)[:20], "replay_form": ReplayRequestForm()})


@login_required
@require_GET
def dataset_download(request, pk):
    dataset = _dataset(request.user, pk)
    try:
        file = dataset.file.open("rb")
    except OSError:
        raise Http404("数据集文件不可用。") from None
    record_event(request, "dataset.download", target=str(dataset.pk))
    response = FileResponse(file, as_attachment=True, filename=f"{dataset.pk}.itch", content_type="application/octet-stream")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def dataset_delete(request, pk):
    try:
        delete_dataset(request.user, _dataset(request.user, pk), request)
        messages.success(request, "数据集已停止访问，文件已进入删除队列。历史审计记录按保留策略处理。")
    except DomainError as exc:
        messages.error(request, exc.message)
        return redirect("trading:dataset_detail", pk=pk)
    return redirect("trading:datasets")


@login_required
@require_POST
def replay_create(request, pk):
    _verified(request.user)
    dataset = _dataset(request.user, pk)
    form = ReplayRequestForm({"mode": "replay", **request.POST.dict()})
    if not form.is_valid():
        return render(request, "trading/dataset_detail.html", {"dataset": dataset, "jobs": dataset.jobs.filter(owner=request.user)[:20], "replay_form": form}, status=400)
    try:
        job = create_replay(request.user, dataset, request, mode=form.cleaned_data["mode"], simulation_parameters=form.simulation_parameters())
        if not dispatch_job(job):
            messages.warning(request, "任务已持久保存，但队列暂不可用；尚未开始执行。")
        return redirect("trading:job_detail", pk=job.pk)
    except DomainError as exc:
        messages.error(request, exc.message)
        return redirect("trading:dataset_detail", pk=pk)


@login_required
@require_GET
def jobs(request):
    page = Paginator(ReplayJob.objects.filter(owner=request.user).select_related("dataset"), 20).get_page(request.GET.get("page"))
    return render(request, "trading/jobs.html", {"page_obj": page})


@login_required
@require_GET
def job_detail(request, pk):
    job = _job(request.user, pk)
    simulation = job.result.get("summary", {}).get("simulation", {}) if job.status == "succeeded" and job.mode == "simulation" else {}
    simulation_money = {key.removesuffix("_units"): f"{Decimal(value) / 10000:.4f}" for key, value in simulation.items() if key.endswith("_units") and type(value) is int}
    pricing = simulation.get("pricing_economics", {})
    pricing_money = {key.removesuffix("_units"): f"{Decimal(value) / 10000:.4f}" for key, value in pricing.items() if key.endswith("_units") and type(value) is int} if isinstance(pricing, dict) else {}
    return render(request, "trading/job_detail.html", {"job": job, "active": job.status in ACTIVE, "retryable": job.status in ("failed", "canceled") and job.attempt < getattr(settings, "MAX_REPLAY_ATTEMPTS", 3), "result_json": json.dumps(job.result, indent=2, ensure_ascii=False), "parameters_json": json.dumps(job.risk_parameters, indent=2, ensure_ascii=False), "simulation_parameters_json": json.dumps(job.simulation_parameters, indent=2, ensure_ascii=False), "simulation": simulation, "simulation_money": simulation_money, "pricing_money": pricing_money})


@login_required
@require_POST
def replay_cancel(request, pk):
    try:
        cancel_job(request.user, _job(request.user, pk), request)
        messages.success(request, "取消请求已记录。正在运行的任务将在检查点终止。")
    except DomainError as exc:
        messages.error(request, exc.message)
    return redirect("trading:job_detail", pk=pk)


@login_required
@require_POST
def replay_retry(request, pk):
    original = _job(request.user, pk)
    try:
        job = create_replay(request.user, _dataset(request.user, original.dataset_id), request, parent=original, mode=original.mode, simulation_parameters=original.simulation_parameters)
        if not dispatch_job(job):
            messages.warning(request, "重试任务已保存，等待队列恢复。")
        return redirect("trading:job_detail", pk=job.pk)
    except DomainError as exc:
        messages.error(request, exc.message)
        return redirect("trading:job_detail", pk=pk)


@login_required
def risk_profile(request):
    if request.method not in ("GET", "POST"):
        return envelope(False, "METHOD_NOT_ALLOWED", "不支持此方法。", status=405)
    profile = RiskProfile.objects.filter(owner=request.user).first() or RiskProfile(owner=request.user)
    form = RiskProfileForm(request.POST if request.method == "POST" else None, instance=profile)
    if request.method == "POST":
        _verified(request.user)
        if form.is_valid():
            try:
                update_risk_profile(request.user, form.cleaned_data, request)
            except DomainError as exc:
                form.add_error(None, exc.message)
                return render(request, "trading/risk.html", {"form": form}, status=exc.status)
            messages.success(request, "研究参数已保存，仅影响此后创建的回放。")
            return redirect("trading:risk_profile")
    return render(request, "trading/risk.html", {"form": form})


@login_required
def feedback(request):
    if request.method not in ("GET", "POST"):
        return envelope(False, "METHOD_NOT_ALLOWED", "不支持此方法。", status=405)
    form = FeedbackForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            create_feedback(request.user, form.cleaned_data, request)
        except DomainError as exc:
            form.add_error(None, exc.message)
            return render(request, "trading/feedback.html", {"form": form, "page_obj": []}, status=exc.status)
        messages.success(request, "反馈已提交，可以在这里查看处理状态。")
        return redirect("trading:feedback")
    page = Paginator(Feedback.objects.filter(owner=request.user), 20).get_page(request.GET.get("page"))
    return render(request, "trading/feedback.html", {"form": form, "page_obj": page})


@sensitive_post_parameters("password")
@login_required
def operations(request):
    if not request.user.is_active or not request.user.email_verified or not request.user.is_staff or not request.user.has_perm("trading.change_platformsafetystate"):
        raise PermissionDenied
    if request.method not in ("GET", "POST"):
        return envelope(False, "METHOD_NOT_ALLOWED", "不支持此方法。", status=405)
    form = SafetyForm(request.POST if request.method == "POST" else None)
    if request.method == "POST":
        ip = getattr(request, "client_ip", request.META.get("REMOTE_ADDR", ""))
        if not (allow_request("operations-user", str(request.user.pk), limit=5, seconds=60)
                and allow_request("operations-ip", ip, limit=20, seconds=60)):
            form.add_error(None, "操作过于频繁，请稍后再试。")
            response = render(request, "trading/operations.html", {"form": form, "safety": PlatformSafetyState.objects.filter(pk=1).first(), "counts": [], "open_feedback": 0}, status=429)
            response["Retry-After"] = "60"
            return response
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                current = lock_active_user(request.user, verified=True)
                if not current.is_staff or not current.has_perm("trading.change_platformsafetystate"):
                    raise PermissionDenied
                if not current.check_password(form.cleaned_data["password"]):
                    raise DomainError("INVALID_PASSWORD", "密码不正确。", 403)
                PlatformSafetyState.objects.get_or_create(pk=1)
                safety = PlatformSafetyState.objects.select_for_update().get(pk=1)
                safety.halted = form.cleaned_data["action"] == "pause"
                safety.reason = form.cleaned_data["reason"]
                safety.changed_by = current
                safety.generation += 1
                safety.save()
                record_event(request, "research.pause" if safety.halted else "research.resume", target="platform", metadata={"generation": safety.generation, "reason": safety.reason})
        except DomainError as exc:
            form.add_error("password" if exc.code == "INVALID_PASSWORD" else None, exc.message)
        else:
            messages.success(request, "研究任务入口状态已更新。此操作不控制任何经纪商订单。")
            return redirect("trading:operations")
    counts = ReplayJob.objects.values("status").annotate(count=Count("pk")).order_by("status")
    return render(request, "trading/operations.html", {"form": form, "safety": PlatformSafetyState.objects.filter(pk=1).first(), "counts": counts, "open_feedback": Feedback.objects.filter(status="open").count(),
        "automation_counts": StrategyAutomation.objects.filter(deleted_at__isnull=True).values("state").annotate(count=Count("pk")).order_by("state")})


def envelope(success, code="OK", message="", data=None, status=200):
    return JsonResponse({"success": success, "code": code, "message": message, "data": data}, status=status)


def api_endpoint(*methods, verified=False):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                response = envelope(False, "METHOD_NOT_ALLOWED", "不支持此方法。", status=405)
                response["Allow"] = ", ".join(methods)
                return response
            if not request.user.is_authenticated or not request.user.is_active:
                return envelope(False, "AUTHENTICATION_REQUIRED", "请先登录。", status=401)
            if verified and not request.user.email_verified:
                return envelope(False, "EMAIL_NOT_VERIFIED", "请先验证邮箱。", status=403)
            try:
                return view(request, *args, **kwargs)
            except DomainError as exc:
                return envelope(False, exc.code, exc.message, status=exc.status)
            except Http404:
                return envelope(False, "NOT_FOUND", "资源不存在。", status=404)
        return wrapped
    return decorate


def body_json(request):
    if request.content_type != "application/json":
        raise DomainError("UNSUPPORTED_CONTENT_TYPE", "请使用 application/json。", 415)
    if len(request.body) > 16384:
        raise DomainError("REQUEST_TOO_LARGE", "请求内容过大。", 413)
    try:
        value = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise DomainError("INVALID_JSON", "JSON 内容无效。") from None
    if not isinstance(value, dict):
        raise DomainError("INVALID_JSON", "JSON 内容必须是对象。")
    return value


def dataset_data(obj):
    return {"id": str(obj.pk), "name": obj.name, "bytes": obj.byte_size, "sha256": obj.sha256, "messages": obj.message_count, "created_at": obj.created_at.isoformat()}


def job_data(obj):
    return {"id": str(obj.pk), "dataset_id": str(obj.dataset_id), "status": obj.status, "mode": obj.mode, "attempt": obj.attempt, "risk_parameters": obj.risk_parameters, "simulation_parameters": obj.simulation_parameters, "result": obj.result if obj.status == "succeeded" else None, "error_code": obj.error_code, "error_message": obj.error_message, "cancellation_requested": obj.cancellation_requested, "created_at": obj.created_at.isoformat(), "finished_at": obj.finished_at.isoformat() if obj.finished_at else None}


@ensure_csrf_cookie
@api_endpoint("GET")
def api_session(request):
    return envelope(True, data={"authenticated": True, "email_verified": request.user.email_verified, "role": request.user.role, "live_trading": False, "csrf_token": get_token(request)})


@api_endpoint("GET", "POST")
def api_datasets(request):
    if request.method == "GET":
        page = Paginator(Dataset.objects.filter(owner=request.user, deleted_at__isnull=True), 50).get_page(request.GET.get("page"))
        return envelope(True, data={"items": [dataset_data(x) for x in page], "page": page.number, "pages": page.paginator.num_pages, "total": page.paginator.count})
    if not request.user.email_verified:
        raise DomainError("EMAIL_NOT_VERIFIED", "请先验证邮箱。", 403)
    form = DatasetUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return envelope(False, "VALIDATION_ERROR", "上传参数无效。", data=form.errors.get_json_data(), status=400)
    dataset = upload_dataset(request.user, form.cleaned_data["name"], form.cleaned_data["file"], request)
    return envelope(True, "CREATED", data=dataset_data(dataset), status=201)


@api_endpoint("GET", "DELETE")
def api_dataset(request, pk):
    dataset = _dataset(request.user, pk)
    if request.method == "DELETE":
        delete_dataset(request.user, dataset, request)
        return envelope(True, "DELETED")
    return envelope(True, data=dataset_data(dataset))


@api_endpoint("GET", "POST")
def api_jobs(request):
    if request.method == "GET":
        page = Paginator(ReplayJob.objects.filter(owner=request.user), 50).get_page(request.GET.get("page"))
        return envelope(True, data={"items": [job_data(x) for x in page], "page": page.number, "pages": page.paginator.num_pages, "total": page.paginator.count})
    value = body_json(request)
    try:
        dataset_id = uuid.UUID(str(value.get("dataset_id", "")))
    except ValueError:
        raise DomainError("VALIDATION_ERROR", "dataset_id 必须为有效 UUID。") from None
    job = create_replay(request.user, _dataset(request.user, dataset_id), request, idempotency_key=request.headers.get("Idempotency-Key"), mode=value.get("mode", "replay"), simulation_parameters=value.get("simulation_parameters"))
    delivered = dispatch_job(job)
    job.refresh_from_db()
    return envelope(delivered, "ACCEPTED" if delivered else "QUEUE_UNAVAILABLE", "" if delivered else "任务已保存但尚未投递，请勿重复创建。", data=job_data(job), status=202 if delivered else 503)


@api_endpoint("GET")
def api_job(request, pk):
    return envelope(True, data=job_data(_job(request.user, pk)))


@api_endpoint("POST")
def api_cancel(request, pk):
    job = cancel_job(request.user, _job(request.user, pk), request)
    return envelope(True, "CANCEL_REQUESTED", data=job_data(job), status=202)


@api_endpoint("POST", verified=True)
def api_retry(request, pk):
    parent = _job(request.user, pk)
    job = create_replay(request.user, _dataset(request.user, parent.dataset_id), request, parent=parent, idempotency_key=request.headers.get("Idempotency-Key"), mode=parent.mode, simulation_parameters=parent.simulation_parameters)
    delivered = dispatch_job(job)
    job.refresh_from_db()
    return envelope(delivered, "ACCEPTED" if delivered else "QUEUE_UNAVAILABLE", data=job_data(job), status=202 if delivered else 503)
