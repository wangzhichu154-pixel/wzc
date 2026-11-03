import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import AccountToken, DeviceSession, EmailJob, credential_digest

logger = logging.getLogger(__name__)


def lock_reauthenticated_user(user, password):
    """Caller holds transaction.atomic(); serialize privacy actions with mutations.

    Validating the form's user object alone can use an outdated password/account
    state after a concurrent password reset or closure. Re-read and authenticate
    the locked account before exporting or clearing private research snapshots.
    """
    current = get_user_model().objects.select_for_update().get(pk=user.pk)
    if (not current.is_active or current.deleted_at is not None
            or not current.check_password(password)):
        raise ValidationError("账户或密码已发生变更，请重新登录后操作。")
    return current


def token_digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_token(user, kind):
    raw = secrets.token_urlsafe(32)
    lifetime = 1800 if kind == AccountToken.Kind.RESET else getattr(settings, "ACCOUNT_VERIFICATION_TTL", 86400)
    token = AccountToken.objects.create(
        user=user, kind=kind, digest=token_digest(raw),
        credential_digest=credential_digest(user),
        expires_at=timezone.now() + timedelta(seconds=lifetime),
    )
    return token, raw


def revoke_sessions(user):
    now = timezone.now()
    sessions = DeviceSession.objects.filter(user=user, revoked_at__isnull=True)
    keys = list(sessions.values_list("session_key", flat=True))
    Session.objects.filter(session_key__in=keys).delete()
    sessions.update(revoked_at=now)


def enqueue_email(user, kind):
    with transaction.atomic():
        # Serialize per-user outbox creation, limiting duplicate email requests.
        type(user).objects.select_for_update().get(pk=user.pk)
        recent = EmailJob.objects.filter(
            user=user, kind=kind, created_at__gte=timezone.now() - timedelta(seconds=60)
        ).exclude(status="failed").first()
        if recent:
            return recent
        job = EmailJob.objects.create(user=user, kind=kind)
        transaction.on_commit(lambda: dispatch_email(job.pk))
        return job


def dispatch_email(job_id):
    from .tasks import deliver_email
    if getattr(settings, "EMAIL_DELIVERY_EAGER", False):
        deliver_email(job_id)
        return
    try:
        deliver_email.delay(job_id)
    except Exception:
        # Durable outbox remains pending. Celery beat/management command will retry.
        logger.error("email_dispatch_unavailable", extra={"email_job_id": job_id})
