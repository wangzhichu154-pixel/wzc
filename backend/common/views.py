import hmac
import os
from pathlib import Path
import time
from functools import lru_cache

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.cache import cache
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest, multiprocess


def envelope(code, message, status, data=None):
    return JsonResponse({"success": status < 400, "code": code, "message": message, "data": data}, status=status)


@require_GET
def live(request):
    return envelope("OK", "", 200, {"status": "alive", "version": settings.APP_VERSION})


@lru_cache(maxsize=1)
def required_migrations():
    return set(MigrationLoader(None, ignore_no_migrations=True).graph.leaf_nodes())


@require_GET
def ready(request):
    healthy = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        cache.set("health:probe", True, timeout=10)
        healthy = cache.get("health:probe") is True
        healthy = healthy and Path(settings.ENGINE_BINARY).is_file() and os.access(settings.ENGINE_BINARY, os.X_OK)
        healthy = healthy and required_migrations().issubset(MigrationRecorder(connection).applied_migrations())
        if not getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            last = cache.get("worker:heartbeat", 0)
            healthy = healthy and 0 <= time.time() - last < 120
    except Exception:
        healthy = False
    # Dependency details are deliberately not exposed to anonymous callers.
    return envelope("OK" if healthy else "NOT_READY", "" if healthy else "服务依赖尚未就绪。", 200 if healthy else 503, {"ready": healthy})


@require_GET
def metrics(request):
    supplied = request.headers.get("Authorization", "")
    if not settings.METRICS_TOKEN or not hmac.compare_digest(supplied, f"Bearer {settings.METRICS_TOKEN}"):
        return envelope("FORBIDDEN", "禁止访问。", 403)
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        payload = generate_latest(registry)
    else:
        payload = generate_latest()
    return HttpResponse(payload, content_type=CONTENT_TYPE_LATEST)


@require_GET
def policy(request, page):
    return render(request, f"pages/{page}.html")


def error_response(request, status, code, message):
    if request.path.startswith("/api/"):
        return envelope(code, message, status, {"request_id": getattr(request, "request_id", "")})
    return render(request, f"{status}.html", {"request_id": getattr(request, "request_id", ""), "error_message": message}, status=status)


def csrf_failure(request, reason=""):
    return error_response(request, 403, "CSRF_FAILED", "会话校验失败，请刷新页面后重试。")


def bad_request(request, exception=None):
    return error_response(request, 400, "BAD_REQUEST", "请求格式不正确。")


def forbidden(request, exception=None):
    return error_response(request, 403, "FORBIDDEN", "您没有执行此操作的权限。")


def not_found(request, exception=None):
    return error_response(request, 404, "NOT_FOUND", "页面或资源不存在。")


def server_error(request):
    return error_response(request, 500, "INTERNAL_ERROR", "服务遇到异常，请稍后重试并保留请求编号。")


@login_required
@require_GET
def execution_console(request):
    if not request.user.is_operator:
        raise PermissionDenied
    from .execution_status import get_paper_status
    return render(request, "pages/execution.html", {"paper": get_paper_status()})


@require_GET
def execution_api(request):
    if not request.user.is_authenticated:
        return envelope("AUTHENTICATION_REQUIRED", "请先登录。", 401)
    if not request.user.is_operator:
        return envelope("FORBIDDEN", "需要运营权限。", 403)
    from .execution_status import get_paper_status
    return envelope("OK", "", 200, get_paper_status())


def _ml_operator(request):
    """A role name alone never grants access to private model evidence."""
    return bool(request.user.is_authenticated and request.user.is_operator and request.user.deleted_at is None)


@login_required
@require_GET
def ml_console(request):
    if not _ml_operator(request):
        raise PermissionDenied
    from .ml_status import get_ml_status, ml_display_context
    response = render(request, "pages/ml.html", {"ml": ml_display_context(get_ml_status())})
    response["Cache-Control"] = "private, no-store"
    return response


def ml_api(request):
    if not request.user.is_authenticated or not request.user.is_active:
        response = envelope("AUTHENTICATION_REQUIRED", "请先登录。", 401, {"execution_authorized": False})
    elif not _ml_operator(request):
        response = envelope("FORBIDDEN", "需要已验证的运营账户。", 403, {"execution_authorized": False})
    elif request.method != "GET":
        response = envelope("METHOD_NOT_ALLOWED", "此接口只读，仅支持 GET。", 405, {"execution_authorized": False})
        response["Allow"] = "GET"
    else:
        from .ml_status import get_ml_status
        state = get_ml_status()
        invalid = state["state"] in {"invalid", "unavailable"}
        response = envelope("ML_STATUS_UNAVAILABLE" if invalid else "OK", "模型登记状态不可用。" if invalid else "", 503 if invalid else 200, state)
    response["Cache-Control"] = "private, no-store"
    return response
