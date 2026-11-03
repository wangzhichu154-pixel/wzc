"""Session-authenticated JSON endpoints; CSRF protection applies to all mutations."""
import json
from functools import wraps

from django.contrib.auth import login, logout
from django.contrib.sessions.models import Session
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie

from .audit import record_event
from .forms import EmailForm, LoginForm, RegisterForm
from .models import DeviceSession, EmailJob, User
from .services import enqueue_email
from .views import _limited, _register_user


def response(data=None, *, code="OK", message="", status=200):
    result = JsonResponse({"success": status < 400, "code": code, "message": message, "data": data}, status=status)
    result["Cache-Control"] = "no-store"
    return result


def methods(*allowed):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method not in allowed:
                result = response(code="METHOD_NOT_ALLOWED", message="不支持此请求方法。", status=405)
                result["Allow"] = ", ".join(allowed)
                return result
            return view(request, *args, **kwargs)
        return wrapped
    return decorator


def payload(request):
    if request.content_type != "application/json":
        raise ValueError("Content-Type must be application/json")
    if len(request.body) > 16384:
        raise ValueError("Request is too large")
    data = json.loads(request.body)
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    return data


def _read(request):
    if request.content_type != "application/json":
        return None, response(code="UNSUPPORTED_CONTENT_TYPE", message="请使用 application/json。", status=415)
    if len(request.body) > 16384:
        return None, response(code="REQUEST_TOO_LARGE", message="请求内容过大。", status=413)
    try:
        return payload(request), None
    except (ValueError, UnicodeError):
        return None, response(code="INVALID_REQUEST", message="请求必须是有效的 JSON 对象。", status=400)


def user_data(user):
    return {"id": user.pk, "email": user.email, "email_verified": user.email_verified,
            "role": user.role, "is_operator": user.is_operator}


@ensure_csrf_cookie
@methods("GET")
def csrf(request):
    return response({"csrf_token": get_token(request)})


@methods("POST")
def register(request):
    data, error = _read(request)
    if error is not None:
        return error
    email = data.get("email", "")
    if not isinstance(email, str):
        return response(code="VALIDATION_ERROR", message="邮箱格式不正确。", status=400)
    if _limited(request, "register", email=email, limit=10, seconds=300):
        return response(code="RATE_LIMITED", message="请求过于频繁，请稍后重试。", status=429)
    form = RegisterForm(data)
    if not form.is_valid():
        return response({"fields": form.errors.get_json_data()}, code="VALIDATION_ERROR", message="请检查输入。", status=400)
    _register_user(request, form.cleaned_data)
    return response(code="REGISTRATION_RECEIVED", message="如果邮箱可用于注册，验证邮件将进入发送队列。", status=202)


@methods("POST")
def login_view(request):
    data, error = _read(request)
    if error is not None:
        return error
    email = data.get("email", "")
    if not isinstance(email, str):
        return response(code="INVALID_CREDENTIALS", message="邮箱或密码无效，或账户不可用。", status=401)
    if _limited(request, "login", email=email, limit=20, seconds=300):
        return response(code="RATE_LIMITED", message="请求过于频繁，请稍后重试。", status=429)
    form = LoginForm(request, data)
    if not form.is_valid():
        return response(code="INVALID_CREDENTIALS", message="邮箱或密码无效，或账户不可用。", status=401)
    login(request, form.user)
    return response(user_data(form.user))


@methods("POST")
def logout_view(request):
    logout(request)
    return response()


@methods("GET")
def me(request):
    if not request.user.is_authenticated:
        return response(code="AUTHENTICATION_REQUIRED", message="请先登录。", status=401)
    return response(user_data(request.user))


@methods("GET")
def devices(request):
    if not request.user.is_authenticated:
        return response(code="AUTHENTICATION_REQUIRED", message="请先登录。", status=401)
    values = []
    for session in DeviceSession.objects.filter(user=request.user).order_by("-last_seen_at")[:100]:
        values.append({"id": str(session.pk), "user_agent": session.user_agent,
                       "created_at": session.created_at, "last_seen_at": session.last_seen_at,
                       "expires_at": session.expires_at, "revoked_at": session.revoked_at,
                       "is_current": session.session_key == request.session.session_key})
    return response({"devices": values})


@methods("POST")
def revoke_device(request, device_id):
    if not request.user.is_authenticated:
        return response(code="AUTHENTICATION_REQUIRED", message="请先登录。", status=401)
    with transaction.atomic():
        session = DeviceSession.objects.select_for_update().filter(pk=device_id, user=request.user).first()
        if session is None:
            return response(code="NOT_FOUND", message="会话不存在。", status=404)
        session.revoked_at = timezone.now()
        session.save(update_fields=["revoked_at"])
        Session.objects.filter(session_key=session.session_key).delete()
        record_event(request, "account.session_revoked", target=str(session.pk))
    if session.session_key == request.session.session_key:
        logout(request)
    return response()


@methods("POST")
def password_reset(request):
    data, error = _read(request)
    if error is not None:
        return error
    email = data.get("email", "")
    if not isinstance(email, str):
        return response(code="VALIDATION_ERROR", message="邮箱格式不正确。", status=400)
    if _limited(request, "password-reset", email=email, limit=10, seconds=300):
        return response(code="RATE_LIMITED", message="请求过于频繁，请稍后重试。", status=429)
    form = EmailForm(data)
    if not form.is_valid():
        return response(code="VALIDATION_ERROR", message="邮箱格式不正确。", status=400)
    user = User.objects.filter(email=form.cleaned_data["email"].lower(), is_active=True, deleted_at__isnull=True).first()
    if user:
        enqueue_email(user, EmailJob.Kind.RESET)
    return response(code="RESET_RECEIVED", message="如果该邮箱关联有效账户，重置邮件将进入发送队列。", status=202)
