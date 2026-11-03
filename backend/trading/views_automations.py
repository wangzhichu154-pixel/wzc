import json
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from accounts.ratelimit import allow_request
from .automations import command_automation, create_automation
from .forms_automations import AutomationCommandForm, AutomationCreateForm
from .models import Dataset, StrategyAutomation
from .services import DomainError
from .views import api_endpoint, body_json, envelope


def _owned(request, pk):
    return get_object_or_404(StrategyAutomation.objects.select_related("dataset", "last_job"), pk=pk, owner=request.user, deleted_at__isnull=True)


def _limit_control(request):
    ip = getattr(request, "client_ip", request.META.get("REMOTE_ADDR", ""))
    if not (allow_request("automation-control-user", str(request.user.pk), limit=10, seconds=60)
            and allow_request("automation-control-ip", ip, limit=30, seconds=60)):
        raise DomainError("RATE_LIMITED", "控制请求过于频繁，请一分钟后重试。", 429)


def automation_data(item):
    return {"id": str(item.pk), "name": item.name, "dataset_id": str(item.dataset_id), "mode": "simulation",
            "state": item.state, "generation": item.generation, "runs_created": item.runs_created,
            "max_runs": item.max_runs, "interval_seconds": item.interval_seconds,
            "consecutive_failures": item.consecutive_failures, "risk_latched": item.risk_latched,
            "risk_snapshot": item.risk_snapshot, "simulation_parameters": item.simulation_parameters,
            "next_run_at": item.next_run_at.isoformat() if item.next_run_at else None,
            "last_checked_at": item.last_checked_at.isoformat() if item.last_checked_at else None,
            "last_job_id": str(item.last_job_id) if item.last_job_id else None,
            "last_reason_code": item.last_reason_code, "last_reason": item.last_reason,
            "created_at": item.created_at.isoformat(), "live_trading": False}


def detail_context(automation, *, form=None, action=None):
    return {"automation": automation, "jobs": automation.jobs.order_by("-created_at")[:30],
            "start_form": form if action == "start" else AutomationCommandForm(action="start"),
            "stop_form": form if action == "stop" else AutomationCommandForm(action="stop"),
            "risk_json": json.dumps(automation.risk_snapshot, ensure_ascii=False, indent=2),
            "simulation_json": json.dumps(automation.simulation_parameters, ensure_ascii=False, indent=2)}


@login_required
def automations(request):
    if request.method not in ("GET", "POST"):
        return envelope(False, "METHOD_NOT_ALLOWED", "不支持此方法。", status=405)
    form = AutomationCreateForm(request.POST if request.method == "POST" else None, user=request.user)
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            automation, _ = create_automation(request.user, name=form.cleaned_data["name"], dataset=form.cleaned_data["dataset"],
                interval_seconds=form.cleaned_data["interval_seconds"], max_runs=form.cleaned_data["max_runs"],
                simulation_parameters=form.simulation_parameters(), key=form.cleaned_data["key"], request=request)
        except DomainError as exc:
            form.add_error(None, exc.message)
            status = exc.status
        else:
            messages.success(request, "自动运行配置已保存，目前为停止状态。检查参数并验证密码后才能启动。")
            return redirect("trading:automation_detail", pk=automation.pk)
    items = StrategyAutomation.objects.filter(owner=request.user, deleted_at__isnull=True).select_related("dataset")
    page = Paginator(items, 20).get_page(request.GET.get("page"))
    return render(request, "trading/automations.html", {"form": form, "page_obj": page}, status=status)


@login_required
@require_GET
def automation_detail(request, pk):
    return render(request, "trading/automation_detail.html", detail_context(_owned(request, pk)))


@sensitive_post_parameters("password", "start-password", "stop-password")
@login_required
@require_POST
def automation_command(request, pk, action):
    automation = _owned(request, pk)
    form = AutomationCommandForm(request.POST, action=action)
    status = 400
    try:
        _limit_control(request)
        if form.is_valid():
            automation, _, replayed = command_automation(request.user, automation, action=action,
                password=form.cleaned_data["password"], key=form.cleaned_data["key"],
                acknowledge_latch=form.cleaned_data.get("acknowledge_latch", False), request=request)
            messages.success(request, "重复命令已识别，未重复改变状态。" if replayed else ("已授权自动模拟。后台调度器将按配置运行，直到停止、次数耗尽或触发保护。" if action == "start" else "已停止后续自动运行，并请求取消尚未结束的关联任务。"))
            return redirect("trading:automation_detail", pk=automation.pk)
    except DomainError as exc:
        form.add_error(None, exc.message)
        status = exc.status
    automation.refresh_from_db()
    response = render(request, "trading/automation_detail.html", detail_context(automation, form=form, action=action), status=status)
    if status == 429:
        response["Retry-After"] = "60"
    return response


@api_endpoint("GET", "POST")
def api_automations(request):
    if request.method == "GET":
        page = Paginator(StrategyAutomation.objects.filter(owner=request.user, deleted_at__isnull=True), 50).get_page(request.GET.get("page"))
        return envelope(True, data={"items": [automation_data(item) for item in page], "page": page.number, "pages": page.paginator.num_pages, "total": page.paginator.count})
    body = body_json(request)
    if body.get("mode", "simulation") != "simulation":
        raise DomainError("INVALID_MODE", "自动运行仅支持历史模拟，不提供实盘模式。")
    try:
        dataset_id = uuid.UUID(str(body.get("dataset_id", "")))
    except ValueError:
        raise DomainError("VALIDATION_ERROR", "dataset_id 必须为 UUID。") from None
    dataset = get_object_or_404(Dataset, pk=dataset_id, owner=request.user, deleted_at__isnull=True)
    automation, replayed = create_automation(request.user, name=body.get("name"), dataset=dataset,
        interval_seconds=body.get("interval_seconds", 300), max_runs=body.get("max_runs", 10),
        simulation_parameters=body.get("simulation_parameters"), key=request.headers.get("Idempotency-Key"), request=request)
    return envelope(True, "OK" if replayed else "CREATED", data={"automation": automation_data(automation), "replayed": replayed}, status=200 if replayed else 201)


@api_endpoint("GET")
def api_automation(request, pk):
    return envelope(True, data=automation_data(_owned(request, pk)))


@sensitive_post_parameters("password")
@api_endpoint("POST", verified=True)
def api_automation_command(request, pk, action):
    _limit_control(request)
    automation = _owned(request, pk)
    body = body_json(request)
    automation, receipt, replayed = command_automation(request.user, automation, action=action,
        password=body.get("password"), key=request.headers.get("Idempotency-Key"),
        acknowledge_latch=body.get("acknowledge_latch", False), request=request)
    return envelope(True, data={"automation": automation_data(automation), "command": {"id": str(receipt.pk),
        "action": receipt.action, "original_outcome": receipt.outcome, "replayed": replayed}})
