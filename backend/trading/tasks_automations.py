import logging

from celery import shared_task
from django.conf import settings
from django.db.models import F

from .automations import tick_one
from .models import StrategyAutomation

log = logging.getLogger(__name__)


@shared_task(acks_late=True, reject_on_worker_lost=True)
def tick_automations():
    """Bounded fair scan; row locks make repeated/concurrent delivery harmless."""
    size = min(max(int(getattr(settings, "MAX_AUTOMATIONS_PER_TICK", 100)), 1), 1000)
    ids = list(StrategyAutomation.objects.filter(state=StrategyAutomation.State.RUNNING, deleted_at__isnull=True)
               .order_by(F("last_checked_at").asc(nulls_first=True), "created_at").values_list("pk", flat=True)[:size])
    results = {}
    for automation_id in ids:
        try:
            result = tick_one(automation_id)
        except Exception:
            # A DB/network outage must not make the scheduler invent a terminal
            # order state. Persisted intent and a future bounded scan can recover.
            log.exception("Automation scheduler failed", extra={"automation_id": str(automation_id)})
            result = "error"
        results[result] = results.get(result, 0) + 1
    return results
