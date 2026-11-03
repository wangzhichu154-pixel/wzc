import uuid

from django.apps import apps
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .audit import record_event
from .forms import BoundedPasswordChangeForm, BoundedSetPasswordForm, EmailForm, LoginForm, ReauthenticateForm, RegisterForm
from .models import AccountToken, DeviceSession, EmailJob, LoginRecord, User
from .ratelimit import allow_request
from .services import enqueue_email, lock_reauthenticated_user, revoke_sessions, token_digest


def _limited(request, scope, *, email=None, limit=10, seconds=60):
    ip = getattr(request, "client_ip", request.META.get("REMOTE_ADDR", ""))
    if not allow_request(scope + ":ip", ip, limit=limit, seconds=seconds):
        return True
    return email is not None and not allow_request(
        scope + ":email", email.strip().lower(), limit=max(3, limit // 2), seconds=seconds
    )


def _rate_response(request):
    response = render(request, "accounts/notice.html", {
        "title": "请求过于频繁", "notice": "请稍后重试。若问题持续，请联系平台管理员。",
    }, status=429)
    response["Retry-After"] = "60"
    return response


def _register_user(request, values):
    # The response is intentionally identical for an existing email address.
    with transaction.atomic():
        user = User.objects.filter(email=values["email"]).first()
        if user is None:
            try:
                with transaction.atomic():
                    user = User.objects.create_user(email=values["email"], password=values["password1"])
                    record_event(request, "account.register", user=user, metadata={"terms_accepted": True})
            except IntegrityError:
                user = User.objects.get(email=values["email"])
        else:
            # Keep the dominant hashing cost comparable for duplicate addresses.
            User().set_password(values["password1"])
        if user.is_active and not user.email_verified:
            enqueue_email(user, EmailJob.Kind.VERIFY)


@sensitive_post_parameters("password1", "password2")
@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("accounts:profile")
    if request.method == "POST" and _limited(request, "register", email=request.POST.get("email", ""), limit=10, seconds=300):
        return _rate_response(request)
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _register_user(request, form.cleaned_data)
        return render(request, "accounts/notice.html", {
            "title": "请检查邮箱", "notice": "如果该邮箱可用于注册，验证邮件将进入发送队列。链接会过期，请使用最新邮件。若已有账户，请登录或找回密码。",
        })
    return render(request, "accounts/register.html", {"form": form})


@sensitive_post_parameters("password")
@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("accounts:profile")
    if request.method == "POST" and _limited(request, "login", email=request.POST.get("email", ""), limit=20, seconds=300):
        return _rate_response(request)
    form = LoginForm(request, request.POST or None)
    next_url = request.POST.get("next", request.GET.get("next", "/"))
    if request.method == "POST" and form.is_valid():
        login(request, form.user)
        if not url_has_allowed_host_and_scheme(next_url, {request.get_host()}, require_https=request.is_secure()):
            next_url = "/"
        return redirect(next_url)
    return render(request, "accounts/login.html", {"form": form, "next": next_url})


@require_POST
@login_required
def logout_view(request):
    logout(request)
    return redirect("accounts:login")


@login_required
@require_GET
def profile(request):
    return render(request, "accounts/profile.html", {
        "login_records": LoginRecord.objects.filter(user=request.user).order_by("-created_at")[:20],
        "active_sessions": DeviceSession.objects.filter(user=request.user, revoked_at__isnull=True, expires_at__gt=timezone.now()).count(),
    })


@require_http_methods(["GET", "POST"])
def verify(request, token):
    with transaction.atomic():
        entry = AccountToken.objects.select_for_update().select_related("user").filter(
            digest=token_digest(token), kind=AccountToken.Kind.VERIFY,
        ).first()
        if entry is None or not entry.usable:
            return render(request, "accounts/notice.html", {"title": "链接不可用", "notice": "链接无效、已使用或已过期。请登录账户重新申请验证邮件。"}, status=400)
        if request.method == "POST":
            entry.user.email_verified = True
            entry.user.save(update_fields=["email_verified", "updated_at"])
            AccountToken.objects.filter(user=entry.user, kind=entry.kind, used_at__isnull=True).update(used_at=timezone.now())
            record_event(request, "account.email_verified", user=entry.user)
            return render(request, "accounts/notice.html", {"title": "邮箱验证成功", "notice": "现在可以登录并使用已授权的研究功能。实盘交易仍需独立授权与验收。"})
    return render(request, "accounts/verify.html")


@login_required
@require_POST
def resend_verification(request):
    if _limited(request, "verify-resend", email=request.user.email, limit=6, seconds=300):
        return _rate_response(request)
    if not request.user.email_verified:
        enqueue_email(request.user, EmailJob.Kind.VERIFY)
    messages.info(request, "如果账户需要验证，邮件请求已进入发送队列。")
    return redirect("accounts:profile")


@require_http_methods(["GET", "POST"])
def password_reset(request):
    if request.method == "POST" and _limited(request, "password-reset", email=request.POST.get("email", ""), limit=10, seconds=300):
        return _rate_response(request)
    form = EmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = User.objects.filter(email=form.cleaned_data["email"].strip().lower(), is_active=True, deleted_at__isnull=True).first()
        if user:
            enqueue_email(user, EmailJob.Kind.RESET)
        return render(request, "accounts/notice.html", {
            "title": "找回密码", "notice": "如果该邮箱关联有效账户，密码重置邮件将进入发送队列。请检查邮件或稍后重试。",
        })
    return render(request, "accounts/password_reset.html", {"form": form})


@sensitive_post_parameters("new_password1", "new_password2")
@require_http_methods(["GET", "POST"])
def password_reset_confirm(request, token):
    if request.method == "POST" and _limited(request, "reset-confirm", limit=20, seconds=300):
        return _rate_response(request)
    with transaction.atomic():
        entry = AccountToken.objects.select_for_update().select_related("user").filter(
            digest=token_digest(token), kind=AccountToken.Kind.RESET,
        ).first()
        if entry is None or not entry.usable:
            return render(request, "accounts/notice.html", {"title": "链接不可用", "notice": "密码重置链接无效、已使用或已过期，请重新申请。"}, status=400)
        form = BoundedSetPasswordForm(entry.user, request.POST or None)
        if request.method == "POST" and form.is_valid():
            user = form.save(commit=False)
            user.security_version += 1
            user.save(update_fields=["password", "security_version", "updated_at"])
            AccountToken.objects.filter(user=user, kind=entry.kind, used_at__isnull=True).update(used_at=timezone.now())
            revoke_sessions(user)
            record_event(request, "account.password_reset", user=user)
            messages.success(request, "密码已更新，所有原有登录会话已失效，请重新登录。")
            return redirect("accounts:login")
    return render(request, "accounts/password_reset_confirm.html", {"form": form})


@login_required
@sensitive_post_parameters("old_password", "new_password1", "new_password2")
@require_http_methods(["GET", "POST"])
def password_change(request):
    if request.method == "POST" and _limited(request, "password-change", limit=10, seconds=300):
        return _rate_response(request)
    form = BoundedPasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=request.user.pk)
            # Re-check under the account lock in case another session changed it.
            if not user.check_password(form.cleaned_data["old_password"]):
                form.add_error("old_password", "密码已发生变更，请重新登录。")
            else:
                user.set_password(form.cleaned_data["new_password1"])
                user.security_version += 1
                user.save(update_fields=["password", "security_version", "updated_at"])
                AccountToken.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())
                revoke_sessions(user)
                record_event(request, "account.password_changed", user=user)
                logout(request)
                messages.success(request, "密码已更新，所有设备会话已撤销。请重新登录。")
                return redirect("accounts:login")
    return render(request, "accounts/password_change.html", {"form": form})


@login_required
@require_GET
def devices(request):
    return render(request, "accounts/devices.html", {
        "devices": DeviceSession.objects.filter(user=request.user).order_by("-last_seen_at")[:100],
        "current_session_key": request.session.session_key,
        "now": timezone.now(),
    })


@login_required
@require_POST
def revoke_device(request, device_id):
    session = get_object_or_404(DeviceSession, pk=device_id, user=request.user)
    with transaction.atomic():
        session.revoked_at = timezone.now()
        session.save(update_fields=["revoked_at"])
        Session.objects.filter(session_key=session.session_key).delete()
        record_event(request, "account.session_revoked", target=str(session.pk))
    if session.session_key == request.session.session_key:
        logout(request)
        return redirect("accounts:login")
    messages.success(request, "该设备会话已撤销。")
    return redirect("accounts:devices")


@login_required
@require_POST
def revoke_other_devices(request):
    sessions = DeviceSession.objects.filter(user=request.user, revoked_at__isnull=True).exclude(session_key=request.session.session_key)
    with transaction.atomic():
        keys = list(sessions.values_list("session_key", flat=True))
        Session.objects.filter(session_key__in=keys).delete()
        sessions.update(revoked_at=timezone.now())
        record_event(request, "account.other_sessions_revoked", metadata={"count": len(keys)})
    messages.success(request, "其他设备的登录会话已撤销。")
    return redirect("accounts:devices")


def _user_export(user):
    payload = {
        "schema_version": 1, "exported_at": timezone.now().isoformat(),
        "account": {"id": user.pk, "email": user.email, "role": user.role,
                    "email_verified": user.email_verified, "created_at": user.date_joined.isoformat()},
        "login_records": list(LoginRecord.objects.filter(user=user).values("success", "new_device", "created_at")),
        "audit_events": list(user.auditevent_set.values("action", "target", "metadata", "created_at")),
    }
    if apps.is_installed("trading"):
        from trading import services as trading_services
        exporter = getattr(trading_services, "export_user_data", None)
        if exporter:
            payload["research"] = exporter(user)
    return payload


@login_required
@sensitive_post_parameters("password")
@require_http_methods(["GET", "POST"])
def export_data(request):
    if request.method == "POST" and _limited(request, "data-export", limit=5, seconds=300):
        return _rate_response(request)
    form = ReauthenticateForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                user = lock_reauthenticated_user(request.user, form.cleaned_data["password"])
                record_event(request, "account.data_export", user=user)
                payload = _user_export(user)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            response = JsonResponse(payload, json_dumps_params={"ensure_ascii": False})
            response["Content-Disposition"] = 'attachment; filename="account-export.json"'
            response["Cache-Control"] = "no-store"
            return response
    return render(request, "accounts/export.html", {"form": form})


@login_required
@sensitive_post_parameters("password")
@require_http_methods(["GET", "POST"])
def delete_account(request):
    if request.method == "POST" and _limited(request, "account-delete", limit=5, seconds=300):
        return _rate_response(request)
    form = ReauthenticateForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        if request.POST.get("confirm") != "DELETE":
            form.add_error(None, "请输入 DELETE 确认注销。")
        elif request.user.is_staff or request.user.is_superuser:
            form.add_error(None, "管理员账户需先由另一名超级管理员撤销管理权限后，再执行注销。")
        else:
            try:
                with transaction.atomic():
                    user = lock_reauthenticated_user(request.user, form.cleaned_data["password"])
                    if user.is_staff or user.is_superuser:
                        raise ValidationError("管理员账户需先撤销管理权限后，再执行注销。")
                    if apps.is_installed("trading"):
                        from trading import services as trading_services
                        prepare = getattr(trading_services, "prepare_account_deletion", None)
                        if prepare is None:
                            raise ValidationError("研究数据删除服务尚不可用，请联系管理员。")
                        prepare(user)
                    record_event(request, "account.deleted", user=user)
                    revoke_sessions(user)
                    AccountToken.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())
                    EmailJob.objects.filter(user=user).exclude(status="sent").update(status="canceled")
                    user.email = f"deleted-{uuid.uuid4().hex}@invalid.local"
                    user.username = uuid.uuid4().hex
                    user.first_name = user.last_name = ""
                    user.is_active = False
                    user.email_verified = False
                    user.deleted_at = timezone.now()
                    user.security_version += 1
                    user.set_unusable_password()
                    user.save()
                logout(request)
                return render(request, "accounts/notice.html", {"title": "账户已注销", "notice": "账户已停用并匿名化，登录会话已撤销。必要安全审计依保留政策保存；备份中的历史数据按备份生命周期到期清除。"})
            except ValidationError as exc:
                form.add_error(None, exc)
    return render(request, "accounts/delete.html", {"form": form})
