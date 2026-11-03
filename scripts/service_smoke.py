"""Run inside the DEVELOPMENT backend container with a real DB, Redis and worker.

Usage: docker compose exec -T backend python - < scripts/service_smoke.py
Creates an explicitly named temporary acceptance user and cleans up its records.
Never run against a production/staging database.
"""
import json
import os
import secrets
import struct
import time

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402
django.setup()
from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.core.cache import cache  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.db import DatabaseError, connection, transaction  # noqa: E402
from trading.models import Dataset, ReplayJob, ResearchAssessment, RiskProfile  # noqa: E402
from trading.services import create_replay, dispatch_job, upload_dataset  # noqa: E402
from trading.assessments import create_assessment  # noqa: E402

if settings.APP_ENV not in {"development", "testing"}:
    raise SystemExit("Refusing acceptance writes outside isolated development/testing")
if "postgresql" not in settings.DATABASES["default"]["ENGINE"]:
    raise SystemExit("This test requires real PostgreSQL")
if not settings.REDIS_URL or getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
    raise SystemExit("This test requires real Redis/Celery dispatch")

ddl_denied = False
try:
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE public.vector_acceptance_ddl_probe (id integer)")
        transaction.set_rollback(True)
except DatabaseError:
    ddl_denied = True
if not ddl_denied:
    raise SystemExit("Runtime database role unexpectedly has DDL capability")

key = "acceptance:" + secrets.token_hex(12)
cache.set(key, "ok", timeout=30)
if cache.get(key) != "ok":
    raise SystemExit("Shared cache check failed")
cache.delete(key)


def header(kind, locate=1, timestamp=0):
    return kind.encode() + struct.pack(">HH", locate, 0) + timestamp.to_bytes(6, "big")


def completed(job):
    if not dispatch_job(job):
        raise RuntimeError("Real task broker did not accept dispatch")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        job.refresh_from_db()
        if job.status in {"succeeded", "failed", "canceled"}:
            break
        time.sleep(.25)
    if job.status != "succeeded":
        raise RuntimeError(f"Real Worker task did not succeed: {job.status}/{job.error_code}")
    return job


def simulation_capture():
    # Explicit protocol fixture with visible queue ahead and two later partial
    # fills. This tests accounting mechanics, never a profitable market claim.
    frames = [header("S", 0) + b"O", header("S", 0) + b"Q", header("H") + b"TEST    T     "]
    def add(reference, side, when):
        return header("A", timestamp=when) + struct.pack(">QcI8sI", reference, side, 100, b"TEST    ", 1000000 if side == b"B" else 1000200)
    def execute(reference, quantity, match, when):
        return header("E", timestamp=when) + struct.pack(">QIQ", reference, quantity, match)
    frames += [add(1, b"B", 1000000), add(2, b"S", 1000000),
        header("P", timestamp=3000000) + struct.pack(">QcI8sIQ", 900, b"B", 1, b"TEST    ", 1000100, 1000),
        add(3, b"B", 4000000), execute(1, 100, 2001, 5000000), execute(3, 30, 2002, 6000000),
        add(4, b"S", 8000000), execute(2, 100, 2003, 9000000), execute(4, 30, 2004, 10000000)]
    return b"".join(struct.pack(">H", len(message)) + message for message in frames)


messages = [header("S", 0) + b"O", header("S", 0) + b"Q",
    header("H") + b"TEST    T     ",
    header("A") + struct.pack(">QcI8sI", 1, b"B", 100, b"TEST    ", 1000000),
    header("A") + struct.pack(">QcI8sI", 2, b"S", 100, b"TEST    ", 1000200),
    header("E") + struct.pack(">QIQ", 1, 30, 1),
    header("D") + struct.pack(">Q", 1), header("D") + struct.pack(">Q", 2)]
capture = b"".join(struct.pack(">H", len(message)) + message for message in messages)
user = get_user_model().objects.create_user(email=f"service-acceptance-{secrets.token_hex(8)}@example.test",
    password=secrets.token_urlsafe(32), email_verified=True)
dataset = None
try:
    dataset = upload_dataset(user, "Temporary service acceptance fixture",
        SimpleUploadedFile("acceptance.itch", capture))
    job = completed(create_replay(user, dataset))
    if job.result.get("messages") != 8 or job.result.get("book_valid") is not True:
        raise RuntimeError("Unexpected real engine result")
    simulation_data = upload_dataset(user, "Temporary service acceptance simulation",
        SimpleUploadedFile("accounting.itch", simulation_capture()))
    simulation = completed(create_replay(user, simulation_data, mode="simulation",
        simulation_parameters={"fee_per_share_units": 0, "min_fee_per_order_units": 100}))
    ledger = simulation.result["summary"]["simulation"]
    if ledger["filled_quantity"] != 60 or ledger["fees_units"] != 200:
        raise RuntimeError("Per-order minimum commission fixture did not reconcile")
    assessment, _ = create_assessment(user, job_ids=[str(simulation.pk)], extra_cost_per_share_units=10)
    if assessment.result["execution_authorized"] is not False or assessment.result["status"] != "open_exposure":
        raise RuntimeError("Research assessment must preserve open-order exposure and deny authorization")
    print(json.dumps({"postgres": True, "runtime_ddl_denied": ddl_denied, "redis": True,
        "real_celery_worker": True, "real_cpp_engine": True, "replay_messages": 8,
        "simulation_messages": 12, "synthetic_filled_quantity": 60, "minimum_fees_units": 200,
        "persistent_assessment": True, "assessment_status": assessment.result["status"],
        "execution_authorized": False, "scope": "isolated local development; synthetic accounting fixture only"}))
finally:
    ResearchAssessment.objects.filter(owner=user).delete()
    ReplayJob.objects.filter(owner=user).delete()
    for item in Dataset.objects.filter(owner=user):
        item.file.delete(save=False)
    Dataset.objects.filter(owner=user).delete()
    RiskProfile.objects.filter(owner=user).delete()
    user.delete()
