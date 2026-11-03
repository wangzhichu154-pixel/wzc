import json
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditEvent
from .automations import command_automation, create_automation, tick_one
from .models import AutomationCommand, FeatureFlag, PlatformSafetyState, ReplayJob, RiskProfile, StrategyAutomation
from .services import DomainError, create_replay, delete_dataset, export_user_data, prepare_account_deletion, upload_dataset
from .tasks import run_replay
from .tasks_automations import tick_automations
from .tests import fixture_bytes


@override_settings(RATE_LIMIT_ENABLED=False, EMAIL_DELIVERY_EAGER=False)
class AutomationTests(TestCase):
    password = "Automation#TestingPass123"

    def setUp(self):
        cache.clear()
        self.temp = tempfile.TemporaryDirectory()
        override = override_settings(MEDIA_ROOT=Path(self.temp.name))
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(self.temp.cleanup)
        self.user = get_user_model().objects.create_user(email="automation@example.test", password=self.password, email_verified=True)
        self.other = get_user_model().objects.create_user(email="other-automation@example.test", password=self.password, email_verified=True)
        self.dataset = upload_dataset(self.user, "Test-only market data", SimpleUploadedFile("input.itch", fixture_bytes()))
        self.client.force_login(self.user)
        self.dispatch = patch("trading.automations.dispatch_job", return_value=True).start()
        self.addCleanup(patch.stopall)

    def create(self, **kwargs):
        values = {"name": "Periodic research", "dataset": self.dataset, "interval_seconds": 60, "max_runs": 10, "key": uuid.uuid4().hex}
        values.update(kwargs)
        return create_automation(self.user, **values)[0]

    def command(self, item, action="start", **kwargs):
        values = {"action": action, "password": self.password, "key": uuid.uuid4().hex}
        values.update(kwargs)
        return command_automation(self.user, item, **values)

    def queued(self, **kwargs):
        item = self.create(**kwargs)
        item, _, _ = self.command(item)
        self.assertEqual(tick_one(item.pk), "created")
        item.refresh_from_db()
        return item, item.last_job

    def finish(self, job, *, status="succeeded", killed=False, valid=True):
        ReplayJob.objects.filter(pk=job.pk).update(status=status, finished_at=timezone.now(),
            result={"summary": {"simulation": {"killed": killed, "pnl_mark_valid": valid}}} if status == "succeeded" else {})

    def test_creation_does_not_start_or_execute(self):
        item = self.create()
        self.assertEqual(item.state, "stopped")
        self.assertIsNone(item.next_run_at)
        self.assertEqual(tick_one(item.pk), "inactive")
        self.assertFalse(ReplayJob.objects.exists())
        self.dispatch.assert_not_called()

    def test_creation_idempotency_matches_payload(self):
        key = uuid.uuid4().hex
        first = self.create(key=key)
        second = self.create(key=key)
        self.assertEqual(first.pk, second.pk)
        with self.assertRaises(DomainError) as error:
            self.create(key=key, max_runs=20)
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_bad_intervals_and_budgets_rejected(self):
        for kwargs in ({"interval_seconds": 59}, {"interval_seconds": True}, {"max_runs": 0}, {"max_runs": 1001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(DomainError):
                self.create(**kwargs)

    @override_settings(MAX_AUTOMATIONS_PER_USER=1)
    def test_creation_quota(self):
        self.create()
        with self.assertRaises(DomainError) as error:
            self.create()
        self.assertEqual(error.exception.code, "AUTOMATION_QUOTA")

    def test_unverified_creation_is_rejected(self):
        get_user_model().objects.filter(pk=self.user.pk).update(email_verified=False)
        with self.assertRaises(DomainError):
            self.create()

    def test_start_requires_correct_password(self):
        item = self.create()
        with self.assertRaises(DomainError) as error:
            self.command(item, password="wrong")
        self.assertEqual(error.exception.code, "REAUTHENTICATION_FAILED")
        item.refresh_from_db()
        self.assertEqual(item.state, "stopped")

    def test_snapshot_survives_personal_profile_change(self):
        RiskProfile.objects.create(owner=self.user, max_order_size=33, tick_size=50)
        item = self.create()
        RiskProfile.objects.filter(owner=self.user).update(max_order_size=44, tick_size=100)
        self.command(item)
        tick_one(item.pk)
        item.refresh_from_db()
        self.assertEqual(item.last_job.risk_parameters["max_order_size"], 33)
        self.assertEqual(item.last_job.risk_parameters["tick_size"], 50)
        self.assertEqual(item.last_job.mode, "simulation")

    def test_only_one_active_run_across_repeated_ticks(self):
        item, job = self.queued()
        for hours in (1, 4, 48):
            self.assertEqual(tick_one(item.pk, now=timezone.now() + timedelta(hours=hours)), "active")
        self.assertEqual(item.jobs.count(), 1)
        self.assertEqual(job.automation_generation, item.generation)

    def test_database_enforces_one_active_run(self):
        item, _ = self.queued()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReplayJob.objects.create(owner=self.user, dataset=self.dataset, automation=item, mode="simulation")

    def test_missed_intervals_are_not_replayed(self):
        item, job = self.queued()
        self.finish(job)
        later = timezone.now() + timedelta(days=3)
        self.assertEqual(tick_one(item.pk, now=later), "created")
        item.refresh_from_db()
        self.assertEqual(item.runs_created, 2)
        self.assertEqual(item.next_run_at, later + timedelta(seconds=60))
        self.assertEqual(item.jobs.count(), 2)

    def test_run_limit_stops_without_reset(self):
        item, job = self.queued(max_runs=1)
        self.finish(job)
        tick_one(item.pk)
        item.refresh_from_db()
        self.assertEqual(item.state, "stopped")
        self.assertEqual(item.last_reason_code, "RUN_LIMIT_REACHED")
        with self.assertRaises(DomainError) as error:
            self.command(item)
        self.assertEqual(error.exception.code, "RUN_LIMIT_REACHED")

    def test_three_consecutive_failures_latch(self):
        item, job = self.queued()
        now = timezone.now()
        for number in range(3):
            self.finish(job, status="failed")
            now += timedelta(minutes=2)
            tick_one(item.pk, now=now)
            item.refresh_from_db()
            job = item.last_job
        self.assertEqual(item.state, "faulted")
        self.assertEqual(item.consecutive_failures, 3)
        self.assertTrue(item.risk_latched)
        self.assertEqual(item.runs_created, 3)

    def test_success_resets_consecutive_failure_count(self):
        item, job = self.queued()
        self.finish(job, status="failed")
        tick_one(item.pk, now=timezone.now() + timedelta(minutes=2))
        item.refresh_from_db()
        self.assertEqual(item.consecutive_failures, 1)
        self.finish(item.last_job)
        tick_one(item.pk, now=timezone.now() + timedelta(minutes=4))
        item.refresh_from_db()
        self.assertEqual(item.consecutive_failures, 0)

    def test_simulated_loss_kill_latches_without_auto_restart(self):
        item, job = self.queued()
        self.finish(job, killed=True)
        tick_one(item.pk)
        item.refresh_from_db()
        self.assertEqual(item.state, "paused")
        self.assertTrue(item.risk_latched)
        self.assertEqual(item.last_reason_code, "SIMULATION_RISK_KILL")
        self.assertEqual(tick_one(item.pk, now=timezone.now() + timedelta(days=10)), "inactive")
        self.assertEqual(item.jobs.count(), 1)

    def test_manual_start_cannot_bypass_unaccounted_kill(self):
        item, job = self.queued()
        self.finish(job, killed=True)
        with self.assertRaises(DomainError) as error:
            self.command(item)
        self.assertEqual(error.exception.code, "AUTOMATION_LATCHED")
        item.refresh_from_db()
        self.assertTrue(item.risk_latched)
        self.assertEqual(item.state, "paused")

    def test_explicit_authorized_latch_acknowledgment(self):
        item, job = self.queued()
        self.finish(job, killed=True)
        tick_one(item.pk)
        item, _, _ = self.command(item, acknowledge_latch=True)
        self.assertEqual(item.state, "running")
        self.assertFalse(item.risk_latched)
        self.assertTrue(AuditEvent.objects.filter(action="automation.latch_acknowledged", actor=self.user).exists())

    def test_invalid_mark_latches(self):
        item, job = self.queued()
        self.finish(job, valid=False)
        tick_one(item.pk)
        item.refresh_from_db()
        self.assertTrue(item.risk_latched)
        self.assertEqual(item.last_reason_code, "INVALID_SIMULATION_MARK")

    def test_stop_cancels_queued_job(self):
        item, job = self.queued()
        item, _, _ = self.command(item, "stop")
        job.refresh_from_db()
        self.assertEqual(item.state, "stopped")
        self.assertEqual(job.status, "canceled")
        self.assertIsNone(item.next_run_at)

    def test_stop_requests_running_cancellation_and_blocks_restart(self):
        item, job = self.queued()
        ReplayJob.objects.filter(pk=job.pk).update(status="running")
        self.command(item, "stop")
        job.refresh_from_db()
        self.assertEqual(job.status, "running")
        self.assertTrue(job.cancellation_requested)
        with self.assertRaises(DomainError) as error:
            self.command(item)
        self.assertEqual(error.exception.code, "ACTIVE_JOB")

    def test_old_start_retry_after_stop_does_not_restart(self):
        item = self.create()
        key = uuid.uuid4().hex
        _, original, _ = self.command(item, key=key)
        stopped, _, _ = self.command(item, "stop")
        current, receipt, replayed = self.command(item, key=key)
        self.assertTrue(replayed)
        self.assertEqual(receipt.pk, original.pk)
        self.assertEqual(current.state, "stopped")
        self.assertEqual(current.generation, stopped.generation)

    def test_stop_command_idempotency(self):
        item, _ = self.queued()
        key = uuid.uuid4().hex
        first, _, _ = self.command(item, "stop", key=key)
        second, _, replayed = self.command(item, "stop", key=key)
        self.assertTrue(replayed)
        self.assertEqual(first.generation, second.generation)
        self.assertEqual(AutomationCommand.objects.filter(owner=self.user, key=key).count(), 1)

    def test_dataset_deletion_stops_future_automation(self):
        item = self.create()
        self.command(item)
        delete_dataset(self.user, self.dataset)
        item.refresh_from_db()
        self.assertEqual(item.state, "faulted")
        self.assertEqual(item.last_reason_code, "DATASET_DELETED")

    def test_account_deletion_stops_and_redacts_automation(self):
        item = self.create()
        self.command(item)
        prepare_account_deletion(self.user)
        item.refresh_from_db()
        self.assertEqual(item.state, "stopped")
        self.assertIsNotNone(item.deleted_at)
        self.assertEqual(item.risk_snapshot, {})
        self.assertEqual(item.simulation_parameters, {})

    def test_account_authorization_loss_pauses_and_cancels(self):
        item, job = self.queued()
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        tick_one(item.pk)
        item.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(item.state, "paused")
        self.assertEqual(job.status, "canceled")

    def test_feature_flag_pause_requires_manual_restart(self):
        item, job = self.queued()
        flag = FeatureFlag.objects.create(name="automations", enabled=False)
        tick_one(item.pk)
        item.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(item.state, "paused")
        self.assertEqual(job.status, "canceled")
        flag.enabled, flag.rollout_percent = True, 100
        flag.save()
        self.assertEqual(tick_one(item.pk), "inactive")

    def test_worker_checks_generation_before_execution(self):
        item, job = self.queued()
        StrategyAutomation.objects.filter(pk=item.pk).update(generation=item.generation + 1)
        with patch("trading.tasks._execute") as execute:
            run_replay(str(job.pk))
        execute.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "canceled")

    def test_worker_checks_feature_flag_before_execution(self):
        _, job = self.queued()
        FeatureFlag.objects.create(name="automations", enabled=False)
        with patch("trading.tasks._execute") as execute:
            run_replay(str(job.pk))
        execute.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "canceled")

    def test_platform_pause_cancels_pending_run(self):
        item, job = self.queued()
        PlatformSafetyState.objects.filter(pk=1).update(halted=True)
        tick_one(item.pk)
        item.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(item.state, "paused")
        self.assertEqual(job.status, "canceled")

    @override_settings(MAX_ACTIVE_REPLAYS_PER_USER=1)
    def test_job_quota_defers_without_consuming_run_count(self):
        create_replay(self.user, self.dataset)
        item = self.create()
        self.command(item)
        now = timezone.now()
        self.assertEqual(tick_one(item.pk, now=now), "quota")
        item.refresh_from_db()
        self.assertEqual(item.runs_created, 0)
        self.assertEqual(item.state, "running")
        self.assertGreater(item.next_run_at, now)

    def test_scheduler_task_registration_and_bounded_run(self):
        first, second = self.create(), self.create()
        self.command(first)
        self.command(second)
        with override_settings(MAX_AUTOMATIONS_PER_TICK=1):
            self.assertEqual(tick_automations().get("created"), 1)
            self.assertEqual(tick_automations().get("created"), 1)
        self.assertEqual(ReplayJob.objects.count(), 2)

    def test_api_rejects_cross_user_and_missing_key(self):
        item = self.create()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("trading:api_automation", args=[item.pk])).status_code, 404)
        self.client.force_login(self.user)
        response = self.client.post(reverse("trading:api_automations"), json.dumps({"name": "API", "dataset_id": str(self.dataset.pk)}), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "IDEMPOTENCY_KEY_REQUIRED")

    def test_api_create_start_stop_and_no_secret_receipts(self):
        response = self.client.post(reverse("trading:api_automations"), json.dumps({"name": "API automation", "dataset_id": str(self.dataset.pk)}), content_type="application/json", HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex)
        self.assertEqual(response.status_code, 201)
        identity = response.json()["data"]["automation"]["id"]
        self.assertEqual(response.json()["data"]["automation"]["state"], "stopped")
        for action, state in (("start", "running"), ("stop", "stopped")):
            response = self.client.post(reverse(f"trading:api_automation_{action}", args=[identity]), json.dumps({"password": self.password}), content_type="application/json", HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["automation"]["state"], state)
            self.assertNotIn(self.password, response.content.decode())
        self.assertNotIn(self.password, json.dumps(list(AutomationCommand.objects.values("outcome"))))

    def test_api_rejects_live_mode(self):
        response = self.client.post(reverse("trading:api_automations"), json.dumps({"name": "Live", "dataset_id": str(self.dataset.pk), "mode": "live"}), content_type="application/json", HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_MODE")

    def test_csrf_required_for_automation_start(self):
        item = self.create()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(reverse("trading:api_automation_start", args=[item.pk]), json.dumps({"password": self.password}), content_type="application/json", HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex)
        self.assertEqual(response.status_code, 403)

    def test_automation_ui_and_export(self):
        item = self.create()
        self.assertEqual(self.client.get(reverse("trading:automations")).status_code, 200)
        response = self.client.get(reverse("trading:automation_detail", args=[item.pk]))
        self.assertContains(response, "不累计重复模拟的盈亏", status_code=200)
        self.assertEqual(len(export_user_data(self.user)["automations"]), 1)

    @skipUnless(Path(settings.ENGINE_BINARY).is_file(), "Build C++ engine first")
    def test_real_engine_automatic_cycle(self):
        item, job = self.queued(max_runs=1)
        run_replay(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, "succeeded", job.error_message)
        self.assertEqual(job.result["summary"]["mode"], "simulation")
        tick_one(item.pk)
        item.refresh_from_db()
        self.assertEqual(item.runs_created, 1)
        self.assertEqual(item.state, "stopped")
        self.assertEqual(item.last_accounted_job_id, job.pk)

    def test_legacy_automation_cannot_silently_start_new_economics(self):
        item = self.create()
        old = {k: v for k, v in item.simulation_parameters.items() if k not in {"min_fee_per_order_units", "slippage_buffer_units", "min_net_edge_units"}}
        item.simulation_parameters = old
        item.save(update_fields=["simulation_parameters"])
        with self.assertRaises(DomainError) as error:
            self.command(item)
        self.assertEqual(error.exception.code, "SIMULATION_CONTRACT_CHANGED")
        item.refresh_from_db()
        self.assertEqual(item.state, "stopped")
        self.assertEqual(item.simulation_parameters, old)

    def test_legacy_running_automation_faults_before_dispatch(self):
        item = self.create()
        self.command(item)
        item.refresh_from_db()
        old = {k: v for k, v in item.simulation_parameters.items() if k not in {"min_fee_per_order_units", "slippage_buffer_units", "min_net_edge_units"}}
        item.simulation_parameters = old
        item.save(update_fields=["simulation_parameters"])
        self.assertEqual(tick_one(item.pk), "faulted")
        item.refresh_from_db()
        self.assertTrue(item.risk_latched)
        self.assertEqual(item.last_reason_code, "SIMULATION_CONTRACT_CHANGED")
        self.assertFalse(item.jobs.exists())
        self.dispatch.assert_not_called()
