from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def require_roles(*roles):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            user = request.user
            if not user.email_verified or not user.is_active:
                raise PermissionDenied("请先完成邮箱验证。")
            privileged = {"admin", "superadmin"}
            allowed = user.is_superuser or (user.role in roles and (
                user.role not in privileged or user.is_staff
            ))
            if not allowed:
                raise PermissionDenied("没有执行此操作的权限。")
            return view(request, *args, **kwargs)
        return wrapped
    return decorator
