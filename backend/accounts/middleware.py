from datetime import timedelta

from django.conf import settings
from django.contrib.auth import logout
from django.shortcuts import render
from django.utils import timezone

from .models import DeviceSession
from .ratelimit import allow_request


class SessionSecurityMiddleware:
    """Enforces revocation and absolute expiry for both portal and Django admin."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and request.path.rstrip("/") == "/admin/login":
            identity = getattr(request, "client_ip", request.META.get("REMOTE_ADDR", ""))
            email = str(request.POST.get("username", "")).strip().lower()
            if (not allow_request("admin-login-ip", identity, limit=20, seconds=300)
                    or not allow_request("admin-login-email", email, limit=10, seconds=300)):
                response = render(request, "accounts/notice.html", {
                    "title": "请求过于频繁", "notice": "登录请求过于频繁，请稍后重试。",
                }, status=429)
                response["Retry-After"] = "300"
                return response
        if request.user.is_authenticated:
            now = timezone.now()
            session = DeviceSession.objects.filter(
                session_key=request.session.session_key, user=request.user,
                revoked_at__isnull=True, expires_at__gt=now,
            ).first()
            if (session is None or not request.user.is_active or request.user.deleted_at is not None
                    or request.session.get("security_version") != request.user.security_version):
                logout(request)
            elif now - session.last_seen_at >= timedelta(minutes=5):
                session.last_seen_at = now
                session.save(update_fields=["last_seen_at"])
                remaining = int((session.expires_at - now).total_seconds())
                idle = getattr(settings, "SESSION_COOKIE_AGE", 1800)
                request.session.set_expiry(min(remaining, idle))
        # Admin credentials may also have been established through the public
        # login endpoint. Checking only AdminAuthenticationForm is insufficient.
        if (request.path.startswith("/admin/") and request.user.is_authenticated
                and not request.user.is_operator):
            return render(request, "403.html", {
                "request_id": getattr(request, "request_id", ""),
                "error_message": "该账户未获管理后台访问权限。",
            }, status=403)
        return self.get_response(request)
