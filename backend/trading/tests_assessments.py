"""Research assessment tests; all ledger fixtures below are explicitly synthetic.

These values test accounting, provenance and authorization invariants. They are
never seeds, user-visible demonstration results or evidence of profitability.
"""
import copy
import hashlib
import json
import tempfile
import uuid
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditEvent
from .assessments import create_assessment
from .models import Dataset, ReplayJob, ResearchAssessment
from .services import (DomainError, create_replay, export_user_data, normalize_simulation,
                       prepare_account_deletion, snapshot_risk, upload_dataset)
from .tasks import run_replay
from .tests import fixture_bytes


@override_settings(RATE_LIMIT_ENABLED=False, EMAIL_DELIVERY_EAGER=False)
class AssessmentTests(TestCase):
    """Persisted terminal jobs exercise the service, database and HTTP contract."""

    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(email="assessment@example.test", password="Assessment#123Strong", email_verified=True)
        self.other = get_user_model().objects.create_user(email="other-assessment@example.test", password="Assessment#456Strong", email_verified=True)
        self.client.force_login(self.user)
        self.risk = snapshot_risk(self.user)
        self.parameters = normalize_simulation("simulation", {})

    def ledger(self, *, parameters=None, risk=None):
        p = copy.deepcopy(parameters or self.parameters)
        r = copy.deepcopy(risk or self.risk)
        pricing = {
            "policy_version": "conservative_one_share_v2", "reference": "inventory_skewed_reservation_price",
            "fee_per_share_units": p["fee_per_share_units"], "min_fee_per_order_units": p["min_fee_per_order_units"],
            "slippage_buffer_units": p["slippage_buffer_units"], "min_net_edge_units": p["min_net_edge_units"],
            "cost_half_spread_floor_units": sum(p[name] for name in ("fee_per_share_units", "min_fee_per_order_units", "slippage_buffer_units", "min_net_edge_units")),
            "minimum_commission_allocation_shares": 1, "rebates_reduce_quote_floor": False,
            "commission_model": "max(min_order_fee, cumulative_filled_quantity * fee_per_share)",
        }
        quantity = 60
        fees = quantity * p["fee_per_share_units"]
        rebates = quantity * p["rebate_per_share_units"]
        simulation = {
            "model_version": "displayed_fifo_conservative_v2_cost_aware", "currency": "USD", "price_units_per_dollar": 10000,
            "orders_submitted": 4, "orders_activated": 4, "orders_canceled": 4, "open_orders_at_end": 0,
            "risk_rejections": 0, "fill_count": 2, "filled_quantity": quantity, "queue_consumed": 200,
            "cancel_requests": 4, "fills_while_cancel_pending": 0, "post_only_rejections": 0,
            "realized_pnl_units": 6000, "unrealized_pnl_units": 0, "fees_units": fees, "rebates_units": rebates,
            "net_pnl_units": 6000 - fees + rebates, "drawdown_units": 900, "cash_before_fees_units": 6000,
            "inventory": 0, "worst_long_inventory": 100, "worst_short_inventory": -70,
            "killed": False, "pnl_mark_valid": True,
            "pricing_economics": {**pricing, "zero_fill_outcome": False, "cost_widened_decisions": 0, "no_valid_quote_decisions": 0, "fee_charged_orders": 2},
            "config": {
                "cost_policy_version": "conservative_one_share_v2", "latency_ns": p["latency_ns"],
                "cancel_latency_ns": p["cancel_latency_ns"], "quote_lifetime_ns": p["simulation_window_ns"],
                "fee_per_share_units": p["fee_per_share_units"], "rebate_per_share_units": p["rebate_per_share_units"],
                "min_fee_per_order_units": p["min_fee_per_order_units"], "slippage_buffer_units": p["slippage_buffer_units"], "min_net_edge_units": p["min_net_edge_units"],
                **{k: r[k] for k in ("max_order_size", "soft_inventory_limit", "hard_inventory_limit", "orders_per_second", "tick_size")},
                "daily_loss_limit_units": r["daily_loss_limit_cents"] * 100,
                "daily_drawdown_limit_units": r["daily_loss_limit_cents"] * 100,
            }, "assumptions": ["Synthetic test fixture; not empirical performance."],
        }
        return {
            "schema_version": 1, "messages": 12, "symbols": 1, "decisions": 8, "trades": 5,
            "book_valid": True, "last_sequence": 12, "elapsed_ns": 12345, "checksum": "53677e327e2909c4",
            "summary": {"mode": "simulation", "orders_sent": 0, "fills_simulated": 2, "pnl": None,
                "decision_checksum": "b760adc176e0d62d", "effective_risk_parameters": r,
                "simulation": simulation, "pricing_economics": pricing,
                "limitations": ["Synthetic test fixture; does not authorize execution."]},
        }

    def job(self, *, owner=None, capture=None, parameters=None, risk=None, result=None, **kwargs):
        owner = owner or self.user
        identity = capture or uuid.uuid4().hex
        dataset = Dataset.objects.create(owner=owner, name="Synthetic assessment test input", original_name="test.itch",
            file=f"datasets/{owner.pk}/{uuid.uuid4().hex}.itch", byte_size=385, message_count=12,
            sha256=hashlib.sha256(identity.encode()).hexdigest())
        p = copy.deepcopy(parameters or self.parameters)
        r = copy.deepcopy(risk or self.risk)
        values = {"owner": owner, "dataset": dataset, "mode": "simulation", "status": "succeeded",
                  "simulation_parameters": p, "risk_parameters": r, "finished_at": timezone.now(),
                  "result": copy.deepcopy(self.ledger(parameters=p, risk=r) if result is None else result)}
        values.update(kwargs)
        return ReplayJob.objects.create(**values)

    def assess(self, *jobs, **kwargs):
        return create_assessment(self.user, job_ids=[str(job.pk) for job in jobs], **kwargs)[0]

    def post(self, jobs, **kwargs):
        payload = {"job_ids": [str(job.pk) for job in jobs]}
        payload.update(kwargs)
        return self.client.post(reverse("trading:api_assessments"), json.dumps(payload), content_type="application/json")

    def test_owned_completed_simulation_creates_immutable_snapshot(self):
        job = self.job()
        item = self.assess(job)
        self.assertEqual(item.owner, self.user)
        self.assertEqual(ResearchAssessment.objects.count(), 1)
        self.assertFalse(item.result["execution_authorized"])
        text = json.dumps(item.snapshot)
        self.assertIn(str(job.pk), text)
        self.assertIn(job.dataset.sha256, text)
        self.assertIn("displayed_fifo_conservative_v2_cost_aware", text)
        self.assertIn("conservative_one_share_v2", text)
        self.assertTrue(AuditEvent.objects.filter(actor=self.user, target=str(item.pk)).exists())

    def test_source_result_change_does_not_rewrite_existing_snapshot(self):
        job = self.job()
        item = self.assess(job)
        original = copy.deepcopy(item.snapshot)
        ReplayJob.objects.filter(pk=job.pk).update(result={})
        item.refresh_from_db()
        self.assertEqual(item.snapshot, original)

    def test_foreign_job_is_not_accepted(self):
        own, foreign = self.job(), self.job(owner=self.other)
        with self.assertRaises(DomainError) as error:
            self.assess(own, foreign)
        self.assertEqual(error.exception.status, 404)
        self.assertFalse(ResearchAssessment.objects.exists())

    def test_missing_job_is_not_disclosed(self):
        with self.assertRaises(DomainError) as error:
            create_assessment(self.user, job_ids=[str(uuid.uuid4())])
        self.assertEqual(error.exception.status, 404)

    def test_invalid_selection_sizes_and_identifiers_rejected(self):
        for ids in ([], [str(uuid.uuid4())] * 51, "not-a-list", ["not-a-uuid"], [True]):
            with self.subTest(ids=ids), self.assertRaises(DomainError):
                create_assessment(self.user, job_ids=ids)

    def test_duplicate_job_ids_do_not_accumulate_twice(self):
        job = self.job()
        with self.assertRaises(DomainError):
            create_assessment(self.user, job_ids=[str(job.pk), str(job.pk)])

    def test_duplicate_capture_hashes_rejected(self):
        first, second = self.job(capture="same-bytes"), self.job(capture="same-bytes")
        with self.assertRaises(DomainError):
            self.assess(first, second)
        self.assertFalse(ResearchAssessment.objects.exists())

    def test_conflicting_duplicate_capture_not_selected_by_profit(self):
        first, second = self.job(capture="same-bytes"), self.job(capture="same-bytes")
        simulation = second.result["summary"]["simulation"]
        for key in ("realized_pnl_units", "cash_before_fees_units", "net_pnl_units"):
            simulation[key] += 10000
        second.save(update_fields=["result"])
        with self.assertRaises(DomainError):
            self.assess(first, second)

    def test_nonterminal_failed_and_replay_results_rejected(self):
        for field, value in (("status", "failed"), ("status", "running"), ("status", "queued"), ("status", "canceled"), ("mode", "replay")):
            with self.subTest(field=field, value=value), self.assertRaises(DomainError):
                self.assess(self.job(**{field: value}))

    def test_missing_or_malformed_ledger_rejected_without_zero_defaults(self):
        variants = [None, {}, {"summary": {}}, self.ledger(), self.ledger(), self.ledger()]
        del variants[3]["summary"]["simulation"]["fees_units"]
        variants[4]["summary"]["simulation"]["net_pnl_units"] = True
        variants[5]["summary"]["simulation"]["filled_quantity"] = "60"
        for value in variants:
            with self.subTest(value=value), self.assertRaises(DomainError):
                item = self.job(result={} if value is None else value)
                self.assess(item)

    def test_inconsistent_pnl_arithmetic_rejected(self):
        value = self.ledger()
        value["summary"]["simulation"]["net_pnl_units"] += 1
        with self.assertRaises(DomainError):
            self.assess(self.job(result=value))

    def test_cost_config_must_match_executed_snapshot(self):
        value = self.ledger()
        value["summary"]["simulation"]["config"]["fee_per_share_units"] += 1
        with self.assertRaises(DomainError):
            self.assess(self.job(result=value))

    def test_effective_risk_must_match_executed_snapshot(self):
        value = self.ledger()
        value["summary"]["effective_risk_parameters"]["tick_size"] += 1
        with self.assertRaises(DomainError):
            self.assess(self.job(result=value))

    def test_mixed_risk_parameters_cannot_be_aggregated(self):
        changed = {**self.risk, "max_order_size": 99}
        with self.assertRaises(DomainError):
            self.assess(self.job(), self.job(risk=changed))

    def test_mixed_economic_parameters_cannot_be_aggregated(self):
        changed = {**self.parameters, "latency_ns": 2000000}
        with self.assertRaises(DomainError):
            self.assess(self.job(), self.job(parameters=changed))

    def test_unsupported_schema_and_model_rejected(self):
        for mutation in ("schema", "model", "currency", "units"):
            value = self.ledger()
            if mutation == "schema":
                value["schema_version"] = 999
            else:
                key, setting = {"model": ("model_version", "untrusted-model"), "currency": ("currency", "EUR"), "units": ("price_units_per_dollar", 100)}[mutation]
                value["summary"]["simulation"][key] = setting
            with self.subTest(mutation=mutation), self.assertRaises(DomainError):
                self.assess(self.job(result=value))

    def test_stress_parameter_is_bounded_exact_integer(self):
        job = self.job()
        for value in (-1, 10001, True, 1.5, "10"):
            with self.subTest(value=value), self.assertRaises(DomainError):
                self.assess(job, extra_cost_per_share_units=value)

    def test_unverified_or_deleted_user_cannot_save_new_snapshot(self):
        job = self.job()
        get_user_model().objects.filter(pk=self.user.pk).update(email_verified=False)
        with self.assertRaises(DomainError):
            self.assess(job)
        get_user_model().objects.filter(pk=self.user.pk).update(email_verified=True, is_active=False, deleted_at=timezone.now())
        with self.assertRaises(DomainError):
            self.assess(job)
        self.assertFalse(ResearchAssessment.objects.exists())

    def test_api_requires_authentication_and_verification(self):
        job = self.job()
        self.client.logout()
        self.assertEqual(self.post([job]).status_code, 401)
        self.client.force_login(self.user)
        get_user_model().objects.filter(pk=self.user.pk).update(email_verified=False)
        self.assertEqual(self.post([job]).status_code, 403)

    def test_api_ownership_and_unsupported_method(self):
        item = create_assessment(self.other, job_ids=[str(self.job(owner=self.other).pk)])[0]
        url = reverse("trading:api_assessment", args=[item.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.patch(url, "{}", content_type="application/json").status_code, 405)
        self.assertEqual(self.client.delete(url).status_code, 405)

    def test_csrf_is_required_for_assessment_create(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(reverse("trading:api_assessments"), json.dumps({"job_ids": [str(self.job().pk)]}), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ResearchAssessment.objects.exists())

    def test_admin_cannot_create_edit_or_delete_reports(self):
        item = self.assess(self.job())
        request = RequestFactory().get("/admin/")
        request.user = get_user_model().objects.create_superuser(email="assessment-admin@example.test", password="Admin#Assessment123", email_verified=True)
        model_admin = admin.site._registry[ResearchAssessment]
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request, item))
        self.assertFalse(model_admin.has_delete_permission(request, item))

    def test_account_export_contains_only_owner_assessments(self):
        own = self.assess(self.job())
        foreign = create_assessment(self.other, job_ids=[str(self.job(owner=self.other).pk)])[0]
        exported = export_user_data(self.user)
        serialized = json.dumps(exported, default=str)
        self.assertIn(str(own.pk), serialized)
        self.assertNotIn(str(foreign.pk), serialized)

    def test_account_deletion_clears_private_assessment_snapshots(self):
        item = self.assess(self.job())
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=self.user.pk)
            prepare_account_deletion(self.user)
        item.refresh_from_db()
        self.assertEqual(item.snapshot, {})
        self.assertEqual(item.result, {})
        self.assertIsNotNone(item.deleted_at)
        self.assertEqual(self.client.get(reverse("trading:api_assessment", args=[item.pk])).status_code, 404)

    def test_model_rejects_mutating_a_saved_assessment(self):
        item = self.assess(self.job())
        item.result = {"execution_authorized": True}
        with self.assertRaises(ValidationError):
            item.save()
        item.refresh_from_db()
        self.assertFalse(item.result["execution_authorized"])

    def test_positive_sample_only_allows_further_paper_research(self):
        item = self.assess(*(self.job() for _ in range(5)))
        self.assertEqual(item.result["status"], "positive_research_sample")
        self.assertEqual(item.result["capture_count"], 5)
        self.assertEqual(item.result["filled_quantity"], 300)
        self.assertEqual(item.result["net_pnl_units"], 21000)
        self.assertEqual(item.result["stressed_net_pnl_units"], 18000)
        self.assertTrue(item.result["sample_threshold_met"])
        self.assertFalse(item.result["execution_authorized"])
        self.assertFalse(item.result["profitability_proven"])
        self.assertFalse(item.result["out_of_sample_verified"])
        self.assertTrue(item.result["scenario_sum_only"])
        self.assertTrue(item.result["overlapping_windows_not_excluded"])
        self.assertNotIn("combined_drawdown_units", item.result)

    def test_positive_but_few_captures_is_insufficient(self):
        item = self.assess(self.job())
        self.assertEqual(item.result["status"], "insufficient_sample")
        self.assertFalse(item.result["sample_threshold_met"])
        self.assertEqual(item.result["thresholds"], {"min_captures": 5, "min_filled_quantity": 100})

    @override_settings(ASSESSMENT_MIN_FILLED_QUANTITY=1000)
    def test_distinct_captures_do_not_override_volume_threshold(self):
        item = self.assess(*(self.job() for _ in range(5)))
        self.assertEqual(item.result["status"], "insufficient_sample")
        self.assertFalse(item.result["sample_threshold_met"])

    def test_no_fill_is_not_profitable_even_when_thresholds_overridden(self):
        value = self.ledger()
        sim = value["summary"]["simulation"]
        for key in ("fill_count", "filled_quantity", "realized_pnl_units", "unrealized_pnl_units", "fees_units", "rebates_units", "net_pnl_units", "drawdown_units", "cash_before_fees_units", "inventory"):
            sim[key] = 0
        sim["pricing_economics"]["fee_charged_orders"] = 0
        sim["pricing_economics"]["zero_fill_outcome"] = True
        item = self.assess(self.job(result=value))
        self.assertEqual(item.result["status"], "no_fills")
        self.assertEqual(item.result["filled_quantity"], 0)
        self.assertFalse(item.result["execution_authorized"])

    def test_no_fill_with_fake_positive_pnl_is_rejected(self):
        value = self.ledger()
        sim = value["summary"]["simulation"]
        sim["fill_count"] = sim["filled_quantity"] = 0
        with self.assertRaises(DomainError):
            self.assess(self.job(result=value))

    def test_invalid_mark_omits_unreliable_aggregate_net(self):
        value = self.ledger()
        value["summary"]["simulation"]["pnl_mark_valid"] = False
        item = self.assess(self.job(result=value))
        self.assertEqual(item.result["status"], "invalid_valuation")
        self.assertFalse(item.result["all_marks_valid"])
        self.assertIsNone(item.result["net_pnl_units"])
        self.assertIsNone(item.result["stressed_net_pnl_units"])
        self.assertIsNone(item.result["totals"]["unrealized_pnl_units"])
        self.assertFalse(item.result["execution_authorized"])

    def test_killed_positive_run_is_risk_stopped(self):
        value = self.ledger()
        value["summary"]["simulation"]["killed"] = True
        item = self.assess(self.job(result=value))
        self.assertEqual(item.result["status"], "risk_stopped")
        self.assertTrue(item.result["risk_killed"])
        self.assertFalse(item.result["execution_authorized"])

    def test_open_orders_block_positive_research_label(self):
        value = self.ledger()
        value["summary"]["simulation"]["open_orders_at_end"] = 1
        value["summary"]["simulation"]["orders_canceled"] = 3
        item = self.assess(self.job(result=value))
        self.assertEqual(item.result["status"], "open_exposure")
        self.assertTrue(item.result["open_exposure"])

    def test_open_inventory_and_unrealized_profit_are_disclosed(self):
        value = self.ledger()
        sim = value["summary"]["simulation"]
        sim["inventory"] = 4
        sim["cash_before_fees_units"] = -494000
        sim["unrealized_pnl_units"] = 1000
        sim["net_pnl_units"] += 1000
        item = self.assess(self.job(result=value))
        self.assertEqual(item.result["status"], "open_exposure")
        self.assertTrue(item.result["open_exposure"])
        self.assertEqual(item.result["totals"]["unrealized_pnl_units"], 1000)

    def test_stress_removes_rebates_then_adds_incremental_cost_once(self):
        params = {**self.parameters, "rebate_per_share_units": 100}
        item = self.assess(self.job(parameters=params), extra_cost_per_share_units=10)
        self.assertEqual(item.result["net_pnl_units"], 10200)
        self.assertEqual(item.result["removed_rebates_units"], 6000)
        self.assertEqual(item.result["additional_cost_units"], 600)
        self.assertEqual(item.result["stressed_net_pnl_units"], 3600)
        self.assertEqual(item.result["totals"]["fees_units"], 1800)

    def test_rebate_driven_profit_is_cost_sensitive(self):
        params = {**self.parameters, "rebate_per_share_units": 100}
        value = self.ledger(parameters=params)
        sim = value["summary"]["simulation"]
        sim["realized_pnl_units"] = sim["cash_before_fees_units"] = 0
        sim["net_pnl_units"] = sim["rebates_units"] - sim["fees_units"]
        item = self.assess(self.job(parameters=params, result=value))
        self.assertEqual(item.result["status"], "cost_sensitive_sample")
        self.assertGreater(item.result["net_pnl_units"], 0)
        self.assertLess(item.result["stressed_net_pnl_units"], 0)

    def test_cost_pressure_can_turn_positive_sample_negative(self):
        item = self.assess(*(self.job() for _ in range(5)), extra_cost_per_share_units=100)
        self.assertEqual(item.result["status"], "cost_sensitive_sample")
        self.assertEqual(item.result["stressed_net_pnl_units"], -9000)

    def test_negative_and_flat_samples_are_not_positive(self):
        for gross, expected in ((0, "losing_sample"), (1800, "flat_sample")):
            value = self.ledger()
            sim = value["summary"]["simulation"]
            sim["realized_pnl_units"] = sim["cash_before_fees_units"] = gross
            sim["net_pnl_units"] = gross - sim["fees_units"]
            item = self.assess(self.job(result=value))
            self.assertEqual(item.result["status"], expected)
            self.assertFalse(item.result["execution_authorized"])

    def legacy_job(self):
        job = self.job()
        new_names = ("min_fee_per_order_units", "slippage_buffer_units", "min_net_edge_units")
        job.simulation_parameters = {key: value for key, value in job.simulation_parameters.items() if key not in new_names}
        sim = job.result["summary"]["simulation"]
        sim["model_version"] = "displayed_fifo_conservative_v1"
        sim.pop("pricing_economics")
        job.result["summary"].pop("pricing_economics")
        for key in (*new_names, "cost_policy_version", "max_order_size", "soft_inventory_limit", "hard_inventory_limit", "orders_per_second", "tick_size"):
            sim["config"].pop(key)
        job.save(update_fields=["simulation_parameters", "result"])
        return job

    def test_legacy_completed_result_is_labeled_without_mutation(self):
        job = self.legacy_job()
        previous = copy.deepcopy(job.simulation_parameters)
        item = self.assess(job)
        self.assertTrue(item.result["legacy_pricing_model"])
        job.refresh_from_db()
        self.assertEqual(job.simulation_parameters, previous)
        self.assertNotIn("min_fee_per_order_units", job.simulation_parameters)

    def test_legacy_and_new_cost_model_cannot_mix_even_with_zero_new_costs(self):
        with self.assertRaises(DomainError) as error:
            self.assess(self.legacy_job(), self.job())
        self.assertEqual(error.exception.code, "INCOMPATIBLE_PARAMETERS")

    def test_idempotent_retry_does_not_make_new_assessment(self):
        job = self.job()
        key = uuid.uuid4().hex
        first, replayed = create_assessment(self.user, job_ids=[str(job.pk)], idempotency_key=key)
        self.assertFalse(replayed)
        second, replayed = create_assessment(self.user, job_ids=[str(job.pk)], idempotency_key=key)
        self.assertTrue(replayed)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ResearchAssessment.objects.count(), 1)
        with self.assertRaises(DomainError) as error:
            create_assessment(self.user, job_ids=[str(job.pk)], extra_cost_per_share_units=11, idempotency_key=key)
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")

    @override_settings(MAX_ASSESSMENTS_PER_USER=1)
    def test_assessment_quota_enforced_after_idempotent_lookup(self):
        job = self.job()
        key = uuid.uuid4().hex
        create_assessment(self.user, job_ids=[str(job.pk)], idempotency_key=key)
        self.assertTrue(create_assessment(self.user, job_ids=[str(job.pk)], idempotency_key=key)[1])
        with self.assertRaises(DomainError) as error:
            self.assess(job)
        self.assertEqual(error.exception.code, "ASSESSMENT_QUOTA")

    def test_minimum_commission_cannot_be_undercharged(self):
        params = {**self.parameters, "min_fee_per_order_units": 1000}
        value = self.ledger(parameters=params)
        # Two charged orders require at least 2 * 1000, but this corrupt result
        # reports only the 1800 per-share fee subtotal.
        with self.assertRaises(DomainError):
            self.assess(self.job(parameters=params, result=value))

    def test_research_threshold_misconfiguration_fails_closed(self):
        job = self.job()
        for invalid in (0, 51, True, "5"):
            with self.subTest(invalid=invalid), override_settings(ASSESSMENT_MIN_CAPTURES=invalid), self.assertRaises(DomainError) as error:
                self.assess(job)
            self.assertEqual(error.exception.status, 503)

    def test_successful_api_round_trip_and_owner_scoped_list(self):
        create_assessment(self.other, job_ids=[str(self.job(owner=self.other).pk)])
        response = self.post([self.job()])
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(set(body), {"success", "code", "message", "data"})
        self.assertFalse(body["data"]["execution_authorized"])
        self.assertFalse(body["data"]["result"]["execution_authorized"])
        detail = self.client.get(response["Location"])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["data"]["snapshot"], body["data"]["snapshot"])
        self.assertIn("no-store", detail["Cache-Control"])
        listing = self.client.get(reverse("trading:api_assessments")).json()["data"]
        self.assertEqual(listing["total"], 1)
        self.assertFalse(listing["execution_authorized"])
        self.assertEqual(listing["items"][0]["id"], body["data"]["id"])

    def test_api_idempotency_header_survives_retries(self):
        key = uuid.uuid4().hex
        payload = json.dumps({"job_ids": [str(self.job().pk)]})
        url = reverse("trading:api_assessments")
        first = self.client.post(url, payload, content_type="application/json", HTTP_IDEMPOTENCY_KEY=key)
        second = self.client.post(url, payload, content_type="application/json", HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["data"]["idempotency_replayed"])
        self.assertEqual(first.json()["data"]["id"], second.json()["data"]["id"])
        self.assertEqual(ResearchAssessment.objects.count(), 1)

    def test_assessment_rate_limit_fails_closed_with_retry_header(self):
        with patch("trading.views_assessments.allow_request", return_value=False):
            response = self.post([self.job()])
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "60")
        self.assertFalse(response.json()["data"]["execution_authorized"])
        self.assertFalse(ResearchAssessment.objects.exists())

    def test_bad_api_body_has_closed_execution_authorization(self):
        for payload in ({"job_ids": []}, {"job_ids": [str(self.job().pk)], "execution_authorized": True}):
            response = self.client.post(reverse("trading:api_assessments"), json.dumps(payload), content_type="application/json")
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.json()["success"])
            self.assertFalse(response.json()["data"]["execution_authorized"])

    def test_pages_render_only_owned_candidates_and_snapshot(self):
        own, foreign = self.job(), self.job(owner=self.other)
        page = self.client.get(reverse("trading:assessments"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, str(own.pk))
        self.assertNotContains(page, str(foreign.pk))
        item = self.assess(own)
        page = self.client.get(reverse("trading:assessment_detail", args=[item.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "USD")
        self.assertContains(page, item.snapshot_sha256)

    def test_minimum_commission_already_in_baseline_is_not_charged_twice(self):
        params = {**self.parameters, "min_fee_per_order_units": 1000}
        value = self.ledger(parameters=params)
        sim = value["summary"]["simulation"]
        sim["fees_units"] = 2000
        sim["net_pnl_units"] = sim["realized_pnl_units"] - sim["fees_units"]
        item = self.assess(self.job(parameters=params, result=value))
        self.assertEqual(item.result["net_pnl_units"], 4000)
        self.assertEqual(item.result["additional_cost_units"], 600)
        self.assertEqual(item.result["stressed_net_pnl_units"], 3400)

    def test_pricing_floor_false_boolean_is_not_integer_one(self):
        value = self.ledger()
        value["summary"]["simulation"]["pricing_economics"]["minimum_commission_allocation_shares"] = True
        with self.assertRaises(DomainError):
            self.assess(self.job(result=value))

    def test_total_filled_quantity_and_inventory_must_have_same_parity(self):
        value = self.ledger()
        sim = value["summary"]["simulation"]
        sim["filled_quantity"] = 61
        sim["fees_units"] = 61 * self.parameters["fee_per_share_units"]
        sim["net_pnl_units"] = sim["realized_pnl_units"] - sim["fees_units"]
        with self.assertRaises(DomainError):
            self.assess(self.job(result=value))

    def test_flat_round_trip_cannot_claim_only_one_commission_order(self):
        params = {**self.parameters, "min_fee_per_order_units": 1000}
        value = self.ledger(parameters=params)
        value["summary"]["simulation"]["pricing_economics"]["fee_charged_orders"] = 1
        with self.assertRaises(DomainError):
            self.assess(self.job(parameters=params, result=value))

    @skipUnless(Path(settings.ENGINE_BINARY).is_file(), "Compile real C++ engine for assessment integration")
    def test_real_engine_result_round_trips_to_assessment(self):
        # Real synthetic ITCH parsing/simulation feeds assessment validation; no
        # mocked accounting result and no expectation of a profitable outcome.
        with tempfile.TemporaryDirectory() as temporary, override_settings(MEDIA_ROOT=Path(temporary)):
            dataset = upload_dataset(self.user, "Engine integration test input", SimpleUploadedFile("test.itch", fixture_bytes()))
            job = create_replay(self.user, dataset, mode="simulation")
            run_replay(str(job.pk))
            job.refresh_from_db()
            self.assertEqual(job.status, "succeeded", job.error_message)
            item = self.assess(job)
            self.assertFalse(item.result["execution_authorized"])
            self.assertIn(job.dataset.sha256, json.dumps(item.snapshot))
