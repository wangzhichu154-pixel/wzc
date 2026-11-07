"""Native-client API checks with isolated users and explicitly synthetic rows."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from accounts.models import AuditEvent
from .models import Dataset, PlatformSafetyState, ReplayJob, RiskProfile, StrategyAutomation


@override_settings(RATE_LIMIT_ENABLED=False, EMAIL_DELIVERY_EAGER=False)
class DesktopApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            email="desktop-owner@example.test", password="Isolated-Test-Password-42!", email_verified=True,
        )
        self.other = get_user_model().objects.create_user(
            email="desktop-other@example.test", password="Other-Isolated-Test-Password-42!", email_verified=True,
        )
        self.client.force_login(self.user)

    def patch_risk(self, values, client=None):
        return (client or self.client).patch("/api/v1/risk/", json.dumps(values), content_type="application/json")

    def dataset(self, owner=None, **overrides):
        return Dataset.objects.create(owner=owner or self.user, name="Synthetic API fixture",
                                      original_name="fixture.itch", file="tests/unopened.itch",
                                      byte_size=12, sha256="0" * 64, message_count=1, **overrides)

    def test_anonymous_reads_require_login(self):
        self.client.logout()
        for url in ("/api/v1/risk/", "/api/v1/dashboard/"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "AUTHENTICATION_REQUIRED")

    def test_get_default_profile_does_not_write(self):
        response = self.client.get("/api/v1/risk/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["daily_loss_limit"], "1000.00")
        self.assertEqual(data["max_order_size"], 100)
        self.assertEqual(data["scope"], "research")
        self.assertFalse(data["persisted"])
        self.assertFalse(data["live_trading"])
        self.assertIsNone(data["updated_at"])
        self.assertFalse(RiskProfile.objects.exists())
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_partial_update_preserves_other_values_and_ownership(self):
        RiskProfile.objects.create(owner=self.user, tick_size=50, daily_loss_limit="321.09")
        other = RiskProfile.objects.create(owner=self.other, max_order_size=99)
        response = self.patch_risk({"max_order_size": 23})
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["max_order_size"], 23)
        self.assertEqual(data["tick_size"], 50)
        self.assertEqual(data["daily_loss_limit"], "321.09")
        self.assertTrue(data["persisted"])
        other.refresh_from_db()
        self.assertEqual(other.max_order_size, 99)
        self.assertTrue(AuditEvent.objects.filter(actor=self.user, action="risk.research_profile_changed").exists())

    def test_cannot_reassign_owner_or_write_control_fields(self):
        for value in ({"owner": self.other.pk}, {"live_trading": True}, {"risk_latched": False}, {}):
            with self.subTest(value=value):
                response = self.patch_risk(value)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
        self.assertFalse(RiskProfile.objects.exists())

    def test_update_reuses_model_form_cross_field_validation(self):
        for values in (
            {"soft_inventory_limit": 1000}, {"hard_inventory_limit": 50},
            {"max_order_size": 10001}, {"tick_size": 0}, {"orders_per_second": 51},
            {"daily_loss_limit": "0"}, {"daily_loss_limit": "1.001"},
            {"max_order_size": True}, {"orders_per_second": [1]},
        ):
            with self.subTest(values=values):
                self.assertEqual(self.patch_risk(values).status_code, 400)
        self.assertFalse(RiskProfile.objects.exists())

    def test_post_requires_complete_profile(self):
        response = self.client.post("/api/v1/risk/", json.dumps({"max_order_size": 50}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        values = {"max_order_size": 50, "tick_size": 100, "soft_inventory_limit": 200,
                  "hard_inventory_limit": 400, "daily_loss_limit": "125.25", "orders_per_second": 3}
        response = self.client.post("/api/v1/risk/", json.dumps(values), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["daily_loss_limit"], "125.25")

    def test_unverified_can_read_but_cannot_update(self):
        self.user.email_verified = False
        self.user.save(update_fields=["email_verified"])
        self.assertEqual(self.client.get("/api/v1/risk/").status_code, 200)
        response = self.patch_risk({"max_order_size": 25})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "EMAIL_NOT_VERIFIED")
        self.assertFalse(RiskProfile.objects.exists())

    def test_inactive_account_cannot_read_or_mutate(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(self.client.get("/api/v1/dashboard/").status_code, 401)
        self.assertEqual(self.patch_risk({"max_order_size": 25}).status_code, 401)

    def test_csrf_is_enforced_for_mutations(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = self.patch_risk({"max_order_size": 25}, client)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(RiskProfile.objects.exists())
        token = client.get("/api/v1/auth/csrf/").json()["data"]["csrf_token"]
        response = client.patch("/api/v1/risk/", json.dumps({"max_order_size": 25}),
                                content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)

    def test_json_and_method_contract(self):
        self.assertEqual(self.client.patch("/api/v1/risk/", "[", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.patch("/api/v1/risk/", "[]", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.patch("/api/v1/risk/", "x", content_type="text/plain").status_code, 415)
        self.assertEqual(self.client.patch("/api/v1/risk/", '"' + "x" * 17000 + '"', content_type="application/json").status_code, 413)
        response = self.client.delete("/api/v1/risk/")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "GET, POST, PATCH")
        self.assertEqual(self.client.post("/api/v1/dashboard/").status_code, 405)

    @override_settings(RATE_LIMIT_ENABLED=True)
    def test_global_api_rate_limit_applies(self):
        with patch("accounts.ratelimit.allow_request", return_value=False):
            response = self.patch_risk({"max_order_size": 25})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "60")
        self.assertFalse(RiskProfile.objects.exists())

    def test_risk_update_leaves_existing_snapshot_and_latch_unchanged(self):
        dataset = self.dataset()
        job = ReplayJob.objects.create(owner=self.user, dataset=dataset, risk_parameters={"max_order_size": 100})
        safety = PlatformSafetyState.objects.create(halted=True, reason="Test research latch")
        automation = StrategyAutomation.objects.create(owner=self.user, dataset=dataset, name="Test paused simulation",
                                                       state="faulted", risk_latched=True)
        self.assertEqual(self.patch_risk({"max_order_size": 25}).status_code, 200)
        job.refresh_from_db()
        safety.refresh_from_db()
        automation.refresh_from_db()
        self.assertEqual(job.risk_parameters["max_order_size"], 100)
        self.assertTrue(safety.halted)
        self.assertTrue(automation.risk_latched)
        self.assertEqual(automation.state, "faulted")

    def test_dashboard_filters_owner_and_deleted_objects(self):
        own = self.dataset()
        other = self.dataset(self.other)
        self.dataset(deleted_at=timezone.now())
        jobs = [ReplayJob.objects.create(owner=self.user, dataset=own, status=status)
                for status in ("running", "succeeded", "queued")]
        ReplayJob.objects.create(owner=self.other, dataset=other, status="running")
        StrategyAutomation.objects.create(owner=self.user, dataset=own, name="Own running", state="running")
        StrategyAutomation.objects.create(owner=self.user, dataset=own, name="Own faulted", state="faulted")
        StrategyAutomation.objects.create(owner=self.user, dataset=own, name="Deleted", state="running", deleted_at=timezone.now())
        StrategyAutomation.objects.create(owner=self.other, dataset=other, name="Other running", state="running")
        PlatformSafetyState.objects.create(halted=True, reason="Operator-only reason")
        response = self.client.get("/api/v1/dashboard/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["dataset_count"], 1)
        self.assertEqual(data["job_count"], 3)
        self.assertEqual(data["running_count"], 1)
        self.assertEqual(data["completed_count"], 1)
        self.assertEqual(data["automation_running"], 1)
        self.assertEqual(data["automation_attention"], 1)
        self.assertEqual({row["id"] for row in data["recent_jobs"]}, {str(job.pk) for job in jobs})
        self.assertTrue(data["research_paused"])
        self.assertFalse(data["live_trading"])
        self.assertNotIn("Operator-only reason", response.content.decode())
        self.assertNotIn("pnl", data)

    def test_dashboard_limits_recent_jobs_without_writing(self):
        dataset = self.dataset()
        for _ in range(7):
            ReplayJob.objects.create(owner=self.user, dataset=dataset)
        data = self.client.get("/api/v1/dashboard/").json()["data"]
        self.assertEqual(data["job_count"], 7)
        self.assertEqual(len(data["recent_jobs"]), 5)
        self.assertFalse(data["research_paused"])
        self.assertFalse(PlatformSafetyState.objects.exists())
