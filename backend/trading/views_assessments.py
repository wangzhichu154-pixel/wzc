import json
from decimal import Decimal
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET

from accounts.ratelimit import allow_request
from .assessments import create_assessment
from .forms_assessments import AssessmentForm
from .models import ResearchAssessment
from .services import DomainError
from .views import api_endpoint, body_json, envelope


def _owned(request, pk):
    return get_object_or_404(ResearchAssessment, pk=pk, owner=request.user, deleted_at__isnull=True)


def _limit(request):
    ip = getattr(request, "client_ip", request.META.get("REMOTE_ADDR", ""))
    if not (allow_request("assessment-user", str(request.user.pk), limit=10, seconds=60)
            and allow_request("assessment-ip", ip, limit=30, seconds=60)):
        raise DomainError("RATE_LIMITED", "评估请求过于频繁，请一分钟后重试。", 429)


def assessment_data(item, *, detail=True):
    value = {"id": str(item.pk), "policy_version": item.policy_version, "result": item.result,
             "snapshot_sha256": item.snapshot_sha256, "created_at": item.created_at.isoformat(),
             "execution_authorized": False}
    if detail:
        value["snapshot"] = item.snapshot
    return value


def assessment_api(*methods):
    """Keep authorization visibly false on success and application error paths."""
    def decorate(view):
        endpoint = api_endpoint(*methods, verified=True)(view)
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            response = endpoint(request, *args, **kwargs)
            value = json.loads(response.content)
            if value["data"] is None:
                value["data"] = {"execution_authorized": False}
            response.content = json.dumps(value, ensure_ascii=False)
            response["Cache-Control"] = "private, no-store"
            if response.status_code == 429:
                response["Retry-After"] = "60"
            return response
        return wrapped
    return decorate


def _money(value):
    return f"{Decimal(value) / Decimal(10000):,.4f}" if type(value) is int else "不可用"


def detail_context(item):
    result = item.result
    totals = result.get("totals", {})
    money = {name: _money(result.get(name)) for name in ("net_pnl_units", "stressed_net_pnl_units", "additional_cost_units", "removed_rebates_units")}
    money.update({name: _money(totals.get(name)) for name in ("realized_pnl_units", "unrealized_pnl_units", "fees_units", "rebates_units")})
    entries = []
    for entry in item.snapshot.get("inputs", []):
        row = dict(entry)
        row["net_usd"] = _money(row["net_pnl_units"]) if row["pnl_mark_valid"] else "不可用"
        row["drawdown_usd"] = _money(row["drawdown_units"]) if row["pnl_mark_valid"] else "不可用"
        entries.append(row)
    return {"assessment": item, "result": result, "money": money, "entries": entries,
            "snapshot_json": json.dumps(item.snapshot, ensure_ascii=False, indent=2),
            "result_json": json.dumps(result, ensure_ascii=False, indent=2)}


@login_required
def assessments(request):
    if request.method not in ("GET", "POST"):
        return envelope(False, "METHOD_NOT_ALLOWED", "不支持此方法。", {"execution_authorized": False}, status=405)
    form = AssessmentForm(request.POST if request.method == "POST" else None, user=request.user)
    status = 200
    if request.method == "POST":
        try:
            _limit(request)
            if form.is_valid():
                item, _ = create_assessment(request.user, job_ids=form.cleaned_data["job_ids"],
                    extra_cost_per_share_units=form.cleaned_data["extra_cost_per_share_units"],
                    idempotency_key=form.cleaned_data.get("idempotency_key") or None, request=request)
                return redirect("trading:assessment_detail", pk=item.pk)
            status = 400
        except DomainError as exc:
            form.add_error(None, exc.message)
            status = exc.status
    items = ResearchAssessment.objects.filter(owner=request.user, deleted_at__isnull=True)
    response = render(request, "trading/assessments.html", {"form": form, "page_obj": Paginator(items, 20).get_page(request.GET.get("page"))}, status=status)
    response["Cache-Control"] = "private, no-store"
    if status == 429:
        response["Retry-After"] = "60"
    return response


@login_required
@require_GET
def assessment_detail(request, pk):
    response = render(request, "trading/assessment_detail.html", detail_context(_owned(request, pk)))
    response["Cache-Control"] = "private, no-store"
    return response


@assessment_api("GET", "POST")
def api_assessments(request):
    if request.method == "GET":
        page = Paginator(ResearchAssessment.objects.filter(owner=request.user, deleted_at__isnull=True), 20).get_page(request.GET.get("page"))
        return envelope(True, data={"items": [assessment_data(item, detail=False) for item in page], "page": page.number, "pages": page.paginator.num_pages, "total": page.paginator.count, "execution_authorized": False})
    _limit(request)
    data = body_json(request)
    if set(data) - {"job_ids", "extra_cost_per_share_units"}:
        raise DomainError("VALIDATION_ERROR", "请求包含不支持的字段。")
    item, replayed = create_assessment(request.user, job_ids=data.get("job_ids"),
        extra_cost_per_share_units=data.get("extra_cost_per_share_units", 10),
        idempotency_key=request.headers.get("Idempotency-Key"), request=request)
    response = envelope(True, data={**assessment_data(item), "idempotency_replayed": replayed}, status=200 if replayed else 201)
    response["Location"] = f"/api/v1/assessments/{item.pk}/"
    return response


@assessment_api("GET")
def api_assessment(request, pk):
    return envelope(True, data=assessment_data(_owned(request, pk)))
