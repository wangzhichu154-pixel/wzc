import hashlib
import hmac

from django.conf import settings

from .models import AuditEvent


def fingerprint(value):
    key = getattr(settings, "ACCOUNT_IP_HASH_KEY", settings.SECRET_KEY)
    return hmac.new(key.encode(), str(value).encode(), hashlib.sha256).hexdigest()


def request_ip_hash(request):
    if request is None:
        return ""
    return fingerprint(getattr(request, "client_ip", request.META.get("REMOTE_ADDR", "")))


def _safe_metadata(metadata, depth=0):
    blocked = {"password", "password1", "password2", "token", "secret", "authorization", "cookie", "session_key", "email", "ip"}
    if depth >= 3:
        return "[truncated]"
    cleaned = {}
    for key, value in list((metadata or {}).items())[:30]:
        name = str(key).lower()
        if name in blocked or any(s in name for s in ("password", "secret", "token")):
            continue
        if isinstance(value, dict):
            value = _safe_metadata(value, depth + 1)
        elif isinstance(value, str):
            value = value[:300]
        elif not isinstance(value, (bool, int, float)) and value is not None:
            value = "[unsupported]"
        cleaned[str(key)[:64]] = value
    return cleaned


def record_event(request, action, *, target="", metadata=None, user=None):
    actor = user or getattr(request, "user", None)
    if actor is not None and not getattr(actor, "is_authenticated", False):
        actor = None
    return AuditEvent.objects.create(
        actor=actor,
        action=action[:80],
        target=str(target)[:200],
        request_id=str(getattr(request, "request_id", ""))[:64],
        ip_hash=request_ip_hash(request),
        metadata=_safe_metadata(metadata),
    )
