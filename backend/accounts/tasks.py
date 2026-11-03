import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.contrib.sessions.models import Session
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import AccountToken, AuditEvent, DeviceSession, EmailJob, LoginRecord
from .services import issue_token

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def deliver_email(job_id):
    now = timezone.now()
    with transaction.atomic():
        job = EmailJob.objects.select_for_update().select_related("user").filter(pk=job_id).first()
        if (not job or job.status in {"sent", "failed", "canceled"}
                or job.next_attempt_at > now
                or (job.lease_until and job.lease_until > now)):
            return
        if not job.user.is_active or job.user.deleted_at:
            job.status = "canceled"
            job.save(update_fields=["status", "updated_at"])
            return
        job.status = "sending"
        job.attempts += 1
        job.lease_until = now + timedelta(minutes=5)
        job.save(update_fields=["status", "attempts", "lease_until", "updated_at"])

    try:
        user = job.user
        if job.kind == EmailJob.Kind.SECURITY:
            subject = "交易研究平台：新的登录设备"
            body = "您的账户刚刚从一个新设备或网络登录。如果不是您本人，请立即登录账户、修改密码并撤销其他会话。"
        else:
            kind = AccountToken.Kind.RESET if job.kind == EmailJob.Kind.RESET else AccountToken.Kind.VERIFY
            _, raw = issue_token(user, kind)
            route = "accounts:password_reset_confirm" if kind == AccountToken.Kind.RESET else "accounts:verify"
            link = settings.PUBLIC_BASE_URL.rstrip("/") + reverse(route, kwargs={"token": raw})
            subject = "交易研究平台：重置密码" if kind == AccountToken.Kind.RESET else "交易研究平台：验证邮箱"
            body = f"请通过以下链接完成操作：\n\n{link}\n\n链接仅限本人使用、一次有效且会过期。如果您未发起操作，请忽略此邮件。"
        count = send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False)
        if count != 1:
            raise RuntimeError("email_backend_did_not_accept")
    except Exception as exc:
        code = type(exc).__name__[:64]
        EmailJob.objects.filter(pk=job_id).update(
            status="failed" if job.attempts >= 5 else "pending",
            lease_until=None, next_attempt_at=timezone.now() + timedelta(seconds=min(3600, 60 * 2 ** job.attempts)),
            last_error_code=code,
        )
        logger.error("email_delivery_failed", extra={"email_job_id": job_id, "error_code": code})
        return
    EmailJob.objects.filter(pk=job_id).update(
        status="sent", sent_at=timezone.now(), lease_until=None, last_error_code="",
    )


@shared_task(ignore_result=True)
def drain_email_outbox():
    now = timezone.now()
    jobs = EmailJob.objects.filter(
        status__in=["pending", "sending"], next_attempt_at__lte=now,
    ).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now)).order_by("created_at")[:100]
    for job in jobs:
        deliver_email(job.pk)


@shared_task(ignore_result=True)
def prune_account_history():
    """Bounded retention work. No active session or pending mail is silently discarded."""
    now = timezone.now()
    data_cutoff = now - timedelta(days=getattr(settings, "DATA_RETENTION_DAYS", 30))
    audit_cutoff = now - timedelta(days=getattr(settings, "AUDIT_RETENTION_DAYS", 365))
    selections = {
        "tokens": AccountToken.objects.filter(expires_at__lt=data_cutoff),
        "sessions": DeviceSession.objects.filter(expires_at__lt=data_cutoff),
        "session_store": Session.objects.filter(expire_date__lt=now),
        "mail": EmailJob.objects.filter(status__in=["sent", "failed", "canceled"], created_at__lt=data_cutoff),
        "login_records": LoginRecord.objects.filter(created_at__lt=audit_cutoff),
        "audit_events": AuditEvent.objects.filter(created_at__lt=audit_cutoff),
    }
    counts = {}
    for name, selection in selections.items():
        ids = list(selection.order_by("pk").values_list("pk", flat=True)[:1000])
        counts[name] = selection.model.objects.filter(pk__in=ids).delete()[0] if ids else 0
    if any(counts.values()):
        logger.info("account_retention_pruned", extra={"counts": counts})
