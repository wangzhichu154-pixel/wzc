import time
from celery import shared_task
from django.core.cache import cache


@shared_task(ignore_result=True)
def worker_heartbeat():
    cache.set("worker:heartbeat", time.time(), timeout=120)
