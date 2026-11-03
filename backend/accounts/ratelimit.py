import logging
import time

from django.core.cache import cache

from .audit import fingerprint

logger = logging.getLogger(__name__)


def allow_request(scope, identity, *, limit, seconds):
    """Atomic increment on shared Redis; fail closed if the limiter is unavailable."""
    key = f"accounts:rate:{scope}:{fingerprint(identity)}:{int(time.time()) // seconds}"
    try:
        if cache.add(key, 1, timeout=seconds + 1):
            return True
        return cache.incr(key) <= limit
    except Exception:
        logger.error("account_rate_limit_unavailable", extra={"scope": scope})
        return False
