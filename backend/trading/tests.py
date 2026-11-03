"""Protocol fixtures are explicitly synthetic test inputs, never product data."""
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import sys
import time
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditEvent
from .forms import RiskProfileForm
from .models import Dataset, FeatureFlag, PlatformSafetyState, ReplayJob, RiskProfile
from .services import (DomainError, cancel_job, create_feedback, create_replay, delete_dataset, dispatch_job,
                       export_user_data, prepare_account_deletion, update_risk_profile, upload_dataset, validate_envelope)
from .tasks import EngineFailure, _execute, purge_deleted_datasets, reconcile_replays, run_replay, validate_result


def fixture_bytes():
    def header(kind, locate=1, timestamp=0):
        return kind.encode() + struct.pack(">HH", locate, 0) + timestamp.to_bytes(6, "big")
    messages = [
        header("S", 0) + b"O",
        header("S", 0) + b"Q",
        header("H") + b"TEST    " + b"T" + b" " + b"    ",
        header("A", timestamp=1_000_000) + struct.pack(">QcI8sI", 1, b"B", 100, b"TEST    ", 1_000_000),
        header("A", timestamp=1_000_000) + struct.pack(">QcI8sI", 2, b"S", 100, b"TEST    ", 1_000_200),
        header("E", timestamp=2_000_000) + struct.pack(">QIQ", 1, 30, 1),
    ]
    return b"".join(struct.pack(">H", len(item)) + item for item in messages)


@override_settings(RATE_LIMIT_ENABLED=False, EMAIL_DELIVERY_EAGER=False)
class TradingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=Path(self.temp.name))
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.temp.cleanup)
        self.user = get_user_model().objects.create_user(email="owner@example.test", password="Long#UniqueSecret123", email_verified=True)
        self.other = get_user_model().objects.create_user(email="other@example.test", password="Long#OtherSecret123", email_verified=True)
        self.client.force_login(self.user)

    def upload(self, user=None, payload=None):
        return upload_dataset(user or self.user, "Test market capture", SimpleUploadedFile("capture.itch", fixture_bytes() if payload is None else payload))

    def test_upload_stores_private_random_path_and_hash(self):
        dataset = self.upload()
        self.assertNotIn("capture", dataset.file.name)
        self.assertEqual(dataset.sha256, hashlib.sha256(fixture_bytes()).hexdigest())
        self.assertEqual(dataset.message_count, 6)
        self.assertEqual(Path(dataset.file.path).read_bytes(), fixture_bytes())
        self.assertTrue(AuditEvent.objects.filter(action="dataset.upload", actor=self.user).exists())

    def test_truncated_prefix_rejected(self):
        with self.assertRaises(DomainError):
            validate_envelope(io.BytesIO(b"\x00"))

    def test_truncated_message_rejected(self):
        with self.assertRaises(DomainError):
            self.upload(payload=fixture_bytes()[:-1])
        self.assertFalse(Dataset.objects.exists())

    def test_empty_file_rejected(self):
        with self.assertRaises(DomainError):
            self.upload(payload=b"")

    def test_extension_rejected(self):
        with self.assertRaises(DomainError):
            upload_dataset(self.user, "Invalid", SimpleUploadedFile("script.html", fixture_bytes()))

    @override_settings(MAX_DATASET_BYTES=10)
    def test_upload_size_limit(self):
        with self.assertRaises(DomainError) as error:
            self.upload()
        self.assertEqual(error.exception.status, 413)

    @override_settings(MAX_USER_DATASETS=1)
    def test_dataset_quota(self):
        self.upload()
        with self.assertRaises(DomainError) as error:
            self.upload()
        self.assertEqual(error.exception.code, "DATASET_QUOTA")

    def test_unverified_cannot_upload(self):
        self.user.email_verified = False
        self.user.save()
        with self.assertRaises(DomainError):
            self.upload()

    def test_private_dataset_and_download(self):
        dataset = self.upload(self.other)
        self.assertEqual(self.client.get(reverse("trading:dataset_detail", args=[dataset.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("trading:dataset_download", args=[dataset.pk])).status_code, 404)
        response = self.client.get(reverse("trading:api_dataset", args=[dataset.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])

    def test_authenticated_download_not_public_url(self):
        dataset = self.upload()
        response = self.client.get(reverse("trading:dataset_download", args=[dataset.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), fixture_bytes())
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.client.get("/private-media/" + dataset.file.name).status_code, 404)

    def test_api_requires_login_json(self):
        self.client.logout()
        response = self.client.get(reverse("trading:api_jobs"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "AUTHENTICATION_REQUIRED")

    def test_ownership_applies_to_replay_creation(self):
        dataset = self.upload(self.other)
        with self.assertRaises(DomainError) as error:
            create_replay(self.user, dataset)
        self.assertEqual(error.exception.code, "NOT_FOUND")

    def test_replay_parameters_are_snapshotted(self):
        dataset = self.upload()
        profile = RiskProfile.objects.create(owner=self.user, max_order_size=23, tick_size=50)
        job = create_replay(self.user, dataset)
        profile.max_order_size = 47
        profile.tick_size = 100
        profile.save()
        job.refresh_from_db()
        self.assertEqual(job.risk_parameters["max_order_size"], 23)
        self.assertEqual(job.risk_parameters["tick_size"], 50)

    @override_settings(MAX_ACTIVE_REPLAYS_PER_USER=1)
    def test_active_job_quota(self):
        dataset = self.upload()
        create_replay(self.user, dataset)
        with self.assertRaises(DomainError) as error:
            create_replay(self.user, dataset)
        self.assertEqual(error.exception.code, "JOB_QUOTA")

    def test_idempotency_reuses_job(self):
        dataset = self.upload()
        first = create_replay(self.user, dataset, idempotency_key="client-request-123")
        second = create_replay(self.user, dataset, idempotency_key="client-request-123")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ReplayJob.objects.count(), 1)

    def test_idempotency_conflicting_payload_rejected(self):
        first, second = self.upload(), self.upload()
        create_replay(self.user, first, idempotency_key="client-request-123")
        with self.assertRaises(DomainError) as error:
            create_replay(self.user, second, idempotency_key="client-request-123")
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_pause_blocks_task_intake(self):
        dataset = self.upload()
        PlatformSafetyState.objects.create(pk=1, halted=True, reason="maintenance")
        with self.assertRaises(DomainError) as error:
            create_replay(self.user, dataset)
        self.assertEqual(error.exception.code, "RESEARCH_PAUSED")

    def test_pause_after_enqueue_prevents_execution(self):
        job = create_replay(self.user, self.upload())
        PlatformSafetyState.objects.filter(pk=1).update(halted=True)
        with patch("trading.tasks._execute") as execute:
            run_replay(str(job.pk))
        execute.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "canceled")

    def test_disabled_feature_blocks_replay(self):
        FeatureFlag.objects.create(name="replay", enabled=False)
        with self.assertRaises(DomainError) as error:
            create_replay(self.user, self.upload())
        self.assertEqual(error.exception.code, "FEATURE_DISABLED")

    def test_feature_rollout_stable_and_range(self):
        flag = FeatureFlag(name="replay", enabled=True, rollout_percent=50)
        self.assertEqual(flag.allows(self.user), flag.allows(self.user))
        flag.rollout_percent = 0
        self.assertFalse(flag.allows(self.user))
        flag.rollout_percent = 100
        self.assertTrue(flag.allows(self.user))
        flag.staff_only = True
        self.assertFalse(flag.allows(self.user))

    def test_cancel_queued_job_terminal(self):
        job = cancel_job(self.user, create_replay(self.user, self.upload()))
        self.assertEqual(job.status, "canceled")
        self.assertIsNotNone(job.finished_at)

    def test_cancel_running_requires_confirmation(self):
        job = create_replay(self.user, self.upload())
        ReplayJob.objects.filter(pk=job.pk).update(status="running")
        job = cancel_job(self.user, job)
        self.assertEqual(job.status, "running")
        self.assertTrue(job.cancellation_requested)

    def test_retry_limit_and_duplicate_parent(self):
        dataset = self.upload()
        job = create_replay(self.user, dataset)
        ReplayJob.objects.filter(pk=job.pk).update(status="failed")
        retry = create_replay(self.user, dataset, parent=job)
        self.assertEqual(retry.attempt, 2)
        with self.assertRaises(DomainError):
            create_replay(self.user, dataset, parent=job)
        ReplayJob.objects.filter(pk=retry.pk).update(status="failed", attempt=3)
        with self.assertRaises(DomainError) as error:
            create_replay(self.user, dataset, parent=retry)
        self.assertEqual(error.exception.code, "RETRY_LIMIT")

    def test_dispatch_failure_retains_durable_intent(self):
        job = create_replay(self.user, self.upload())
        with patch("trading.tasks.run_replay.apply_async", side_effect=ConnectionError("unavailable")):
            self.assertFalse(dispatch_job(job))
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.error_code, "QUEUE_UNAVAILABLE")

    def test_stale_worker_becomes_failed(self):
        job = create_replay(self.user, self.upload())
        ReplayJob.objects.filter(pk=job.pk).update(status="running", heartbeat_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(reconcile_replays()["orphaned"], 1)
        job.refresh_from_db()
        self.assertEqual(job.error_code, "WORKER_LOST")

    def test_invalid_result_rejected(self):
        for result in ({}, {"schema_version": 2}, {"schema_version": 1, "messages": True}):
            with self.assertRaises(EngineFailure):
                validate_result(result)

    def test_duplicate_delivery_does_not_reexecute(self):
        job = create_replay(self.user, self.upload())
        ReplayJob.objects.filter(pk=job.pk).update(status="succeeded")
        with patch("trading.tasks._execute") as execute:
            run_replay(str(job.pk))
        execute.assert_not_called()

    @override_settings(ENGINE_BINARY="/does/not/exist")
    def test_missing_engine_is_failure_not_fake_result(self):
        job = create_replay(self.user, self.upload())
        run_replay(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "ENGINE_UNAVAILABLE")
        self.assertEqual(job.result, {})

    def test_tampered_file_fails_integrity(self):
        dataset = self.upload()
        Path(dataset.file.path).write_bytes(b"tampered")
        job = create_replay(self.user, dataset)
        run_replay(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.error_code, "DATASET_INTEGRITY")

    @skipUnless(Path(settings.ENGINE_BINARY).is_file(), "Build C++ engine before running real-process integration test")
    def test_real_cpp_engine_roundtrip(self):
        job = create_replay(self.user, self.upload())
        run_replay(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, "succeeded", job.error_message)
        self.assertEqual(job.result["messages"], 6)
        self.assertEqual(job.result["summary"]["orders_sent"], 0)
        self.assertIsNone(job.result["summary"]["pnl"])
        self.assertEqual(job.result["summary"]["effective_risk_parameters"]["max_order_size"], 100)

    @skipUnless(Path(settings.ENGINE_BINARY).is_file(), "Build C++ engine before running determinism integration test")
    def test_real_replay_deterministic_checksum(self):
        dataset = self.upload()
        one = create_replay(self.user, dataset)
        run_replay(str(one.pk))
        two = create_replay(self.user, dataset)
        run_replay(str(two.pk))
        one.refresh_from_db()
        two.refresh_from_db()
        self.assertEqual(one.status, "succeeded", one.error_message)
        self.assertEqual(two.status, "succeeded", two.error_message)
        self.assertEqual(one.result["checksum"], two.result["checksum"])

    def test_file_purge_removes_content_and_preserves_audit(self):
        dataset = self.upload()
        path = Path(dataset.file.path)
        delete_dataset(self.user, dataset)
        self.assertEqual(self.client.get(reverse("trading:dataset_download", args=[dataset.pk])).status_code, 404)
        self.assertEqual(purge_deleted_datasets(), 1)
        self.assertFalse(path.exists())
        dataset.refresh_from_db()
        self.assertIsNotNone(dataset.purged_at)
        self.assertTrue(AuditEvent.objects.filter(action="dataset.delete").exists())

    def test_active_dataset_cannot_be_deleted(self):
        dataset = self.upload()
        create_replay(self.user, dataset)
        with self.assertRaises(DomainError):
            delete_dataset(self.user, dataset)

    def test_account_deletion_rejects_running_work(self):
        job = create_replay(self.user, self.upload())
        ReplayJob.objects.filter(pk=job.pk).update(status="running")
        with self.assertRaises(ValidationError):
            prepare_account_deletion(self.user)

    def test_account_deletion_cancels_queued_and_redacts(self):
        job = create_replay(self.user, self.upload())
        prepare_account_deletion(self.user)
        job.refresh_from_db()
        self.assertEqual(job.status, "canceled")
        self.assertEqual(job.risk_parameters, {})
        self.assertFalse(RiskProfile.objects.filter(owner=self.user).exists())
        self.assertTrue(Dataset.objects.filter(owner=self.user, deleted_at__isnull=False).exists())

    def test_export_excludes_other_user(self):
        self.upload()
        self.upload(self.other)
        result = export_user_data(self.user)
        self.assertEqual(len(result["datasets"]), 1)
        self.assertNotIn("file", result["datasets"][0])

    def test_risk_profile_rejects_inverted_limits(self):
        form = RiskProfileForm(data={"max_order_size": 100, "soft_inventory_limit": 1000, "hard_inventory_limit": 500, "daily_loss_limit": "1000.00", "orders_per_second": 5})
        self.assertFalse(form.is_valid())

    def test_non_admin_cannot_pause(self):
        self.assertEqual(self.client.get(reverse("trading:operations")).status_code, 403)
        response = self.client.post(reverse("trading:operations"), {"action": "pause", "reason": "test pause", "password": "Long#UniqueSecret123"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PlatformSafetyState.objects.filter(halted=True).exists())

    def test_csrf_required_for_job_mutation(self):
        dataset = self.upload()
        secure_client = Client(enforce_csrf_checks=True)
        secure_client.force_login(self.user)
        response = secure_client.post(reverse("trading:api_jobs"), data=json.dumps({"dataset_id": str(dataset.pk)}), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ReplayJob.objects.exists())

    def test_dashboard_and_product_pages(self):
        for name in ("dashboard", "datasets", "jobs", "risk_profile", "feedback"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse("trading:" + name)).status_code, 200)

    def test_api_rejects_malformed_json(self):
        response = self.client.post(reverse("trading:api_jobs"), data="{", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_JSON")

    def test_api_rejects_foreign_dataset(self):
        dataset = self.upload(self.other)
        response = self.client.post(reverse("trading:api_jobs"), data=json.dumps({"dataset_id": str(dataset.pk)}), content_type="application/json")
        self.assertEqual(response.status_code, 404)

    def test_api_jobs_list_is_scoped(self):
        create_replay(self.user, self.upload())
        create_replay(self.other, self.upload(self.other))
        response = self.client.get(reverse("trading:api_jobs"))
        self.assertEqual(response.json()["data"]["total"], 1)

    def test_api_upload_uses_real_storage(self):
        response = self.client.post(reverse("trading:api_datasets"), {"name": "Via API", "file": SimpleUploadedFile("input.itch", fixture_bytes())})
        self.assertEqual(response.status_code, 201)
        dataset = Dataset.objects.get(pk=response.json()["data"]["id"])
        self.assertEqual(Path(dataset.file.path).read_bytes(), fixture_bytes())

    def test_api_queue_failure_reports_persisted_job(self):
        dataset = self.upload()
        with patch("trading.tasks.run_replay.apply_async", side_effect=ConnectionError("unavailable")):
            response = self.client.post(reverse("trading:api_jobs"), data=json.dumps({"dataset_id": str(dataset.pk)}), content_type="application/json", HTTP_IDEMPOTENCY_KEY="replay-request-abc")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["data"]["status"], "queued")
        self.assertEqual(body["data"]["error_code"], "QUEUE_UNAVAILABLE")
        self.assertEqual(ReplayJob.objects.count(), 1)

    def test_empty_feedback_form_shows_validation(self):
        response = self.client.post(reverse("trading:feedback"), {})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)

    def slow_test_executable(self):
        executable = Path(self.temp.name) / "slow-test-engine"
        executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(10)\n", encoding="utf-8")
        executable.chmod(0o700)
        return str(executable)

    def test_actual_subprocess_timeout(self):
        job = create_replay(self.user, self.upload())
        with override_settings(ENGINE_BINARY=self.slow_test_executable(), REPLAY_TIMEOUT_SECONDS=1):
            run_replay(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "ENGINE_TIMEOUT")

    def test_actual_subprocess_cancellation(self):
        job = create_replay(self.user, self.upload())
        ReplayJob.objects.filter(pk=job.pk).update(status="running", cancellation_requested=True)
        with override_settings(ENGINE_BINARY=self.slow_test_executable()):
            with self.assertRaises(EngineFailure) as error:
                _execute(job)
        self.assertEqual(error.exception.code, "CANCELED")

    def test_risk_get_does_not_create_personal_record(self):
        response = self.client.get(reverse("trading:risk_profile"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(RiskProfile.objects.filter(owner=self.user).exists())

    def test_stale_user_cannot_recreate_deleted_risk_profile(self):
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False, deleted_at=timezone.now())
        with self.assertRaises(DomainError):
            update_risk_profile(self.user, {"max_order_size": 100, "soft_inventory_limit": 500, "hard_inventory_limit": 1000, "daily_loss_limit": "1000.00", "orders_per_second": 5})
        self.assertFalse(RiskProfile.objects.filter(owner=self.user).exists())

    def test_stale_user_cannot_recreate_deleted_feedback(self):
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False, deleted_at=timezone.now())
        with self.assertRaises(DomainError):
            create_feedback(self.user, {"subject": "race", "message": "should not save"})
        self.assertFalse(self.user.feedback_items.exists())

    @patch("accounts.ratelimit.time")
    def test_operations_password_attempts_are_rate_limited(self, limiter_clock):
        # Keep all attempts in one fixed window. Password hashing may otherwise
        # carry the first request across a real wall-clock minute boundary.
        limiter_clock.time.return_value = 60_030.0
        operator = get_user_model().objects.create_superuser(email="operator@example.test", password="Operator#ActualPass123")
        self.client.force_login(operator)
        for _ in range(5):
            response = self.client.post(reverse("trading:operations"), {"action": "pause", "reason": "maintenance checks", "password": "incorrect-password"})
            self.assertEqual(response.status_code, 200)
        response = self.client.post(reverse("trading:operations"), {"action": "pause", "reason": "maintenance checks", "password": "Operator#ActualPass123"})
        self.assertEqual(response.status_code, 429)
        self.assertFalse(PlatformSafetyState.objects.filter(halted=True).exists())

    def test_unknown_execution_mode_rejected(self):
        with self.assertRaises(DomainError) as error:
            create_replay(self.user, self.upload(), mode="live")
        self.assertEqual(error.exception.code, "INVALID_MODE")

    def test_simulation_parameters_snapshotted(self):
        job = create_replay(self.user, self.upload(), mode="simulation", simulation_parameters={"fee_per_share_units": 50})
        self.assertEqual(job.mode, "simulation")
        self.assertEqual(job.simulation_parameters["fee_per_share_units"], 50)
        self.assertEqual(job.simulation_parameters["latency_ns"], 1000000)

    def test_simulation_parameters_must_be_bounded_integer(self):
        dataset = self.upload()
        for parameters in ({"fee_per_share_units": -1}, {"latency_ns": True}, {"unknown": 1}, {"simulation_window_ns": 10**20}):
            with self.subTest(parameters=parameters), self.assertRaises(DomainError):
                create_replay(self.user, dataset, mode="simulation", simulation_parameters=parameters)

    def test_idempotency_mode_conflict_rejected(self):
        dataset = self.upload()
        create_replay(self.user, dataset, idempotency_key="request-mode-123")
        with self.assertRaises(DomainError) as error:
            create_replay(self.user, dataset, idempotency_key="request-mode-123", mode="simulation")
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_api_rejects_live_mode(self):
        dataset = self.upload()
        response = self.client.post(reverse("trading:api_jobs"), data=json.dumps({"dataset_id": str(dataset.pk), "mode": "live"}), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_MODE")

    def test_simulation_model_rejects_plain_replay_result(self):
        value = {"schema_version": 1, "messages": 1, "symbols": 1, "decisions": 0, "trades": 0, "last_sequence": 1, "elapsed_ns": 1, "book_valid": True, "checksum": "abc", "summary": {}}
        with self.assertRaises(EngineFailure):
            validate_result(value, "simulation")

    def simulation_dataset(self):
        fixture = Path(self.temp.name) / "synthetic-simulation-test.itch"
        generator = Path(settings.BASE_DIR).parent / "engine" / "tests" / "make_simulation_fixture.py"
        subprocess.run([sys.executable, str(generator), str(fixture)], check=True, capture_output=True, timeout=10)
        return self.upload(payload=fixture.read_bytes())

    @skipUnless(Path(settings.ENGINE_BINARY).is_file(), "Build simulation-enabled engine first")
    def test_real_simulation_roundtrip_and_ledger(self):
        job = create_replay(self.user, self.simulation_dataset(), mode="simulation")
        run_replay(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, "succeeded", job.error_message)
        result = job.result["summary"]["simulation"]
        self.assertEqual(result["fill_count"], 2)
        self.assertEqual(result["filled_quantity"], 60)
        self.assertEqual(result["net_pnl_units"], result["realized_pnl_units"] + result["unrealized_pnl_units"] - result["fees_units"] + result["rebates_units"])
        self.assertTrue(result["pnl_mark_valid"])
        response = self.client.get(reverse("trading:job_detail", args=[job.pk]))
        self.assertContains(response, "模拟交易结果")
        self.assertContains(response, "不是真实交易")

    @skipUnless(Path(settings.ENGINE_BINARY).is_file(), "Build simulation-enabled engine first")
    def test_real_simulation_high_cost_prevents_uneconomic_fills(self):
        # Cost-aware quotes widen before execution; high fees must not force
        # the old model's fictitious fill at an unchanged unprofitable quote.
        RiskProfile.objects.create(owner=self.user, daily_loss_limit="1.00")
        job = create_replay(self.user, self.simulation_dataset(), mode="simulation", simulation_parameters={"fee_per_share_units": 1000})
        run_replay(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, "succeeded", job.error_message)
        result = job.result["summary"]["simulation"]
        self.assertFalse(result["killed"])
        self.assertEqual(result["fill_count"], 0)
        self.assertEqual(result["net_pnl_units"], 0)
        self.assertEqual(result["pricing_economics"]["cost_half_spread_floor_units"], 1000)

    @skipUnless(Path(settings.ENGINE_BINARY).is_file(), "Build simulation-enabled engine first")
    def test_simulation_invalid_mark_does_not_show_valid_net_pnl(self):
        job = create_replay(self.user, self.simulation_dataset(), mode="simulation")
        run_replay(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, "succeeded", job.error_message)
        job.result["summary"]["simulation"]["pnl_mark_valid"] = False
        job.save(update_fields=["result"])
        response = self.client.get(reverse("trading:job_detail", args=[job.pk]))
        self.assertContains(response, "估值不可用")
        self.assertNotContains(response, "<dd>0.4200</dd>")

    def test_simulation_flag_can_gate_new_mode_independently(self):
        FeatureFlag.objects.create(name="simulation", enabled=False)
        dataset = self.upload()
        with self.assertRaises(DomainError) as error:
            create_replay(self.user, dataset, mode="simulation")
        self.assertEqual(error.exception.code, "FEATURE_DISABLED")
        self.assertEqual(create_replay(self.user, dataset).mode, "replay")

    def test_old_task_contract_cannot_execute_new_work(self):
        job = create_replay(self.user, self.upload())
        with self.assertRaises(ValueError):
            run_replay(str(job.pk), contract_version=1)
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")

    def test_admin_cannot_restore_deleted_feedback(self):
        from django.contrib import admin
        from django.test import RequestFactory
        from .admin import FeedbackAdmin
        from .models import Feedback
        from django.core.exceptions import PermissionDenied
        item = create_feedback(self.user, {"subject": "private subject", "message": "private message"})
        stale = Feedback.objects.get(pk=item.pk)
        prepare_account_deletion(self.user)
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False, deleted_at=timezone.now())
        stale.response = "would resurrect data"
        request = RequestFactory().post("/admin/trading/feedback/")
        request.user = self.other
        with self.assertRaises(PermissionDenied):
            FeedbackAdmin(Feedback, admin.site).save_model(request, stale, None, True)
        item.refresh_from_db()
        self.assertEqual(item.message, "")
        self.assertEqual(item.response, "")

    def test_tick_size_model_and_form_constraints(self):
        for tick in (0, 100001):
            profile = RiskProfile(owner=self.user, tick_size=tick)
            with self.subTest(tick=tick), self.assertRaises(ValidationError):
                profile.full_clean()
        form = RiskProfileForm(data={"max_order_size": 100, "tick_size": 50, "soft_inventory_limit": 500, "hard_inventory_limit": 1000, "daily_loss_limit": "1000.00", "orders_per_second": 5})
        self.assertTrue(form.is_valid(), form.errors)

    @skipUnless(Path(settings.ENGINE_BINARY).is_file(), "Build C++ engine before testing tick configuration")
    def test_real_engine_receives_nondefault_tick(self):
        RiskProfile.objects.create(owner=self.user, tick_size=50)
        job = create_replay(self.user, self.upload())
        run_replay(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, "succeeded", job.error_message)
        self.assertEqual(job.result["summary"]["effective_risk_parameters"]["tick_size"], 50)

    def test_tick_migration_does_not_restore_deleted_snapshots(self):
        import importlib
        from django.apps import apps
        from django.db import connection
        module = importlib.import_module("trading.migrations.0005_riskprofile_tick_size_and_more")
        first = create_replay(self.user, self.upload())
        second = create_replay(self.user, self.upload())
        old = dict(first.risk_parameters)
        old.pop("tick_size")
        ReplayJob.objects.filter(pk=first.pk).update(risk_parameters=old)
        ReplayJob.objects.filter(pk=second.pk).update(risk_parameters={})
        from types import SimpleNamespace
        module.record_historical_tick_default(apps, SimpleNamespace(connection=connection))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.risk_parameters["tick_size"], 100)
        self.assertEqual(second.risk_parameters, {})

    def test_engine_wrapper_rejects_wrong_parent(self):
        completed = subprocess.run([
            sys.executable, "-m", "trading.engine_runner", "1073741824", "5", "2097152",
            str(os.getpid() + 1000000), self.slow_test_executable(), "--input", "unused",
        ], cwd=settings.BASE_DIR, capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 70)

    def test_engine_dies_when_spawning_worker_dies(self):
        pidfile = Path(self.temp.name) / "engine-pid"
        executable = Path(self.temp.name) / "test-orphan-engine"
        executable.write_text(f"#!{sys.executable}\nimport os, time\nfrom pathlib import Path\nPath({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(20)\n", encoding="utf-8")
        executable.chmod(0o700)
        worker_code = (
            "import os, subprocess, sys; "
            "p = subprocess.Popen([sys.executable, '-m', 'trading.engine_runner', "
            "'1073741824', '10', '2097152', str(os.getpid()), "
            f"{str(executable)!r}, '--input', 'unused']); p.wait()"
        )
        worker = subprocess.Popen([sys.executable, "-c", worker_code], cwd=settings.BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child_pid = None
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pidfile.exists(), "Engine did not start under parent-death protection")
            child_pid = int(pidfile.read_text())
            worker.kill()
            worker.wait(timeout=5)
            deadline = time.monotonic() + 3
            state = "running"
            while time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{child_pid}/stat").read_text().split(") ", 1)[1][0]
                except FileNotFoundError:
                    state = "gone"
                if state in ("Z", "gone"):
                    break
                time.sleep(0.02)
            self.assertIn(state, ("Z", "gone"), "Engine survived worker death")
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
            if child_pid is not None:
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass

    def test_cost_parameters_are_preserved_and_task_contract_bumped(self):
        costs = {"min_fee_per_order_units": 10, "slippage_buffer_units": 20, "min_net_edge_units": 30}
        job = create_replay(self.user, self.upload(), mode="simulation", simulation_parameters=costs)
        for key, value in costs.items():
            self.assertEqual(job.simulation_parameters[key], value)
        with patch("trading.tasks.run_replay.apply_async") as send:
            dispatch_job(job)
        self.assertEqual(send.call_args.kwargs["kwargs"], {"contract_version": 4})

    def test_legacy_queued_job_is_not_silently_upgraded(self):
        job = create_replay(self.user, self.upload(), mode="simulation")
        old = {k: v for k, v in job.simulation_parameters.items() if k not in {"min_fee_per_order_units", "slippage_buffer_units", "min_net_edge_units"}}
        job.simulation_parameters = old
        job.save(update_fields=["simulation_parameters"])
        run_replay(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "SIMULATION_CONTRACT_CHANGED")
        self.assertEqual(job.simulation_parameters, old)

    def test_cost_parameters_reject_out_of_range_and_bool(self):
        dataset = self.upload()
        for key in ("min_fee_per_order_units", "slippage_buffer_units", "min_net_edge_units"):
            for value in (-1, True, 1000000001):
                with self.subTest(key=key, value=value), self.assertRaises(DomainError):
                    create_replay(self.user, dataset, mode="simulation", simulation_parameters={key: value})

    @skipUnless(Path(settings.ENGINE_BINARY).is_file(), "Build cost-aware engine first")
    def test_actual_engine_receives_cost_snapshot(self):
        costs = {"min_fee_per_order_units": 10, "slippage_buffer_units": 20, "min_net_edge_units": 30}
        job = create_replay(self.user, self.simulation_dataset(), mode="simulation", simulation_parameters=costs)
        run_replay(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, "succeeded", job.error_message)
        simulation = job.result["summary"]["simulation"]
        for key, value in costs.items():
            self.assertEqual(simulation["config"][key], value)
            self.assertEqual(simulation["pricing_economics"][key], value)
        self.assertEqual(simulation["pricing_economics"]["cost_half_spread_floor_units"], 90)
