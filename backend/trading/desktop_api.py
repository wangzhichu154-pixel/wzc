"""Session-authenticated native-client endpoints for existing research workflows.

The desktop uses the same model forms, ownership locks, CSRF middleware and
audit service as the browser interface. No endpoint grants execution authority.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Count

from .forms import RiskProfileForm
from .models import Dataset, PlatformSafetyState, ReplayJob, RiskProfile, StrategyAutomation
from .services import DomainError, lock_active_user, update_risk_profile
from .views import api_endpoint, body_json, envelope, job_data


RISK_FIELDS = tuple(RiskProfileForm.Meta.fields)


def _risk_values(profile):
    return {
        field: str(value) if isinstance(value := getattr(profile, field), Decimal) else value
        for field in RISK_FIELDS
    }


def _risk_data(profile):
    return {
        **_risk_values(profile),
        "persisted": profile.pk is not None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
        "scope": "research",
        "live_trading": False,
    }


@api_endpoint("GET", "POST", "PATCH")
def api_risk(request):
    if request.method == "GET":
        # Reading defaults must not create a configuration or affect risk state.
        profile = RiskProfile.objects.filter(owner=request.user).first() or RiskProfile(owner=request.user)
        return envelope(True, data=_risk_data(profile))

    values = body_json(request)
    if not values or set(values) - set(RISK_FIELDS):
        raise DomainError("VALIDATION_ERROR", "仅支持六项研究风险参数，且至少提供一项。")
    if any(isinstance(value, (bool, list, dict)) for value in values.values()):
        raise DomainError("VALIDATION_ERROR", "研究风险参数必须为数值或数值字符串。")

    with transaction.atomic():
        # Serialize partial read/modify/write with the shared ownership lock.
        # This also rechecks verification and account deletion immediately before
        # mutation, instead of trusting the session's earlier user snapshot.
        current = lock_active_user(request.user, verified=True)
        profile = RiskProfile.objects.filter(owner=current).first() or RiskProfile(owner=current)
        submitted = {**_risk_values(profile), **values} if request.method == "PATCH" else values
        form = RiskProfileForm(submitted, instance=profile)
        if not form.is_valid():
            return envelope(False, "VALIDATION_ERROR", "研究风险参数无效。",
                            data=form.errors.get_json_data(), status=400)
        saved = update_risk_profile(current, form.cleaned_data, request)
    return envelope(True, message="研究参数已保存，仅影响此后创建的回放。", data=_risk_data(saved))


@api_endpoint("GET")
def api_dashboard(request):
    datasets = Dataset.objects.filter(owner=request.user, deleted_at__isnull=True)
    jobs = ReplayJob.objects.filter(owner=request.user)
    counts = {row["status"]: row["count"] for row in jobs.values("status").annotate(count=Count("pk"))}
    automations = StrategyAutomation.objects.filter(owner=request.user, deleted_at__isnull=True)
    safety = PlatformSafetyState.objects.filter(pk=1).first()
    return envelope(True, data={
        "dataset_count": datasets.count(),
        "job_count": sum(counts.values()),
        "running_count": counts.get(ReplayJob.Status.RUNNING, 0),
        "completed_count": counts.get(ReplayJob.Status.SUCCEEDED, 0),
        "automation_running": automations.filter(state=StrategyAutomation.State.RUNNING).count(),
        "automation_attention": automations.filter(state__in=[
            StrategyAutomation.State.PAUSED, StrategyAutomation.State.FAULTED,
        ]).count(),
        "research_paused": bool(safety and safety.halted),
        "recent_jobs": [job_data(job) for job in jobs[:5]],
        "scope": "research",
        "live_trading": False,
    })
