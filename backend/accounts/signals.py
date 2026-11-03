from datetime import timedelta

from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver
from django.utils import timezone

from .audit import fingerprint, record_event, request_ip_hash
from .models import DeviceSession, EmailJob, LoginRecord, User
from .services import enqueue_email


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    if request is None:
        return
    if not request.session.session_key:
        request.session.save()
    now = timezone.now()
    ip = request_ip_hash(request)
    agent = request.META.get("HTTP_USER_AGENT", "")[:200]
    previous = DeviceSession.objects.filter(user=user)
    new_device = previous.exists() and not previous.filter(ip_hash=ip, user_agent=agent).exists()
    request.session["security_version"] = user.security_version
    request.session["authenticated_at"] = now.isoformat()
    request.session.set_expiry(min(
        getattr(settings, "SESSION_COOKIE_AGE", 1800),
        getattr(settings, "ACCOUNT_SESSION_MAX_AGE", 604800),
    ))
    DeviceSession.objects.update_or_create(session_key=request.session.session_key, defaults={
        "user": user, "user_agent": agent, "ip_hash": ip,
        "last_seen_at": now, "expires_at": now + timedelta(seconds=getattr(settings, "ACCOUNT_SESSION_MAX_AGE", 604800)),
        "revoked_at": None,
    })
    LoginRecord.objects.create(user=user, email_hash=fingerprint(user.email), ip_hash=ip, success=True, new_device=new_device)
    record_event(request, "account.login", user=user, metadata={"new_device": new_device})
    if new_device and user.email_verified:
        enqueue_email(user, EmailJob.Kind.SECURITY)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if request is not None:
        DeviceSession.objects.filter(session_key=request.session.session_key).update(revoked_at=timezone.now())
        if user:
            record_event(request, "account.logout", user=user)


@receiver(user_login_failed)
def on_failed_login(sender, credentials, request, **kwargs):
    email = str(credentials.get("email", credentials.get("username", ""))).strip().lower()
    user = User.objects.filter(email=email, deleted_at__isnull=True).first()
    LoginRecord.objects.create(user=user, email_hash=fingerprint(email), ip_hash=request_ip_hash(request), success=False)
