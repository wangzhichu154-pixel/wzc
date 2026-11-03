import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import authenticate
from django.core import mail
from django.core.cache import cache
from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .audit import record_event
from .models import AccountToken, AuditEvent, DeviceSession, EmailJob, LoginRecord, User
from .services import enqueue_email, issue_token, token_digest
from .tasks import deliver_email, drain_email_outbox, prune_account_history

PASSWORD = "A-strong-passphrase-487!"
NEW_PASSWORD = "A-new-strong-passphrase-589!"


@override_settings(EMAIL_DELIVERY_EAGER=False)
class AccountTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="owner@example.com", password=PASSWORD, email_verified=True)

    def login(self, client=None):
        client = client or self.client
        return client.post(reverse("accounts:login"), {"email": self.user.email, "password": PASSWORD})

    def test_password_hashed_and_email_normalized(self):
        user = User.objects.create_user(email=" Case@Example.com ", password=PASSWORD)
        self.assertEqual(user.email, "case@example.com")
        self.assertNotEqual(user.password, PASSWORD)
        self.assertTrue(user.check_password(PASSWORD))
        self.assertTrue(user.password.startswith("argon2$"))

    def test_case_insensitive_email_uniqueness(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(email="OWNER@EXAMPLE.COM", password=PASSWORD)

    def test_email_normalization_also_applies_to_account_edits(self):
        self.user.email = "EDITED@EXAMPLE.COM"
        self.user.save(update_fields=["email"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "edited@example.com")
        self.assertIsNotNone(authenticate(email="edited@example.com", password=PASSWORD))

    def test_registration_does_not_accept_role_escalation(self):
        response = self.client.post(reverse("accounts:register"), {
            "email": "new@example.com", "password1": PASSWORD, "password2": PASSWORD,
            "accept_terms": "on", "role": "superadmin", "is_staff": "true", "is_superuser": "true",
        })
        self.assertEqual(response.status_code, 200)
        user = User.objects.get(email="new@example.com")
        self.assertEqual(user.role, "user")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.email_verified)
        self.assertEqual(EmailJob.objects.filter(user=user, kind="verify").count(), 1)
        self.assertTrue(AuditEvent.objects.filter(actor=user, action="account.register").exists())

    def test_duplicate_registration_is_generic(self):
        data = {"email": self.user.email, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD, "accept_terms": "on"}
        result = self.client.post(reverse("accounts:register"), data)
        self.assertEqual(result.status_code, 200)
        self.assertContains(result, "如果该邮箱可用于注册")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_weak_password_rejected(self):
        result = self.client.post(reverse("accounts:register"), {
            "email": "new@example.com", "password1": "password", "password2": "password", "accept_terms": "on",
        })
        self.assertEqual(result.status_code, 200)
        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    def test_login_creates_audited_device(self):
        response = self.login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 200)
        self.assertEqual(DeviceSession.objects.filter(user=self.user, revoked_at__isnull=True).count(), 1)
        self.assertEqual(LoginRecord.objects.filter(user=self.user, success=True).count(), 1)
        self.assertTrue(AuditEvent.objects.filter(actor=self.user, action="account.login").exists())

    def test_unsafe_login_redirect_rejected(self):
        response = self.client.post(reverse("accounts:login"), {
            "email": self.user.email, "password": PASSWORD, "next": "https://evil.example/steal",
        })
        self.assertEqual(response.url, "/")

    def test_inactive_and_nonexistent_authentication_both_fail(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertIsNone(authenticate(email=self.user.email, password=PASSWORD))
        self.assertIsNone(authenticate(email="missing@example.com", password=PASSWORD))

    def test_verification_requires_post_and_is_single_use(self):
        self.user.email_verified = False
        self.user.save(update_fields=["email_verified"])
        entry, raw = issue_token(self.user, "verify")
        self.assertEqual(entry.digest, token_digest(raw))
        self.assertNotEqual(entry.digest, raw)
        url = reverse("accounts:verify", kwargs={"token": raw})
        self.assertEqual(self.client.get(url).status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)
        self.assertEqual(self.client.post(url).status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)
        self.assertEqual(self.client.post(url).status_code, 400)

    def test_expired_verification_rejected(self):
        entry, raw = issue_token(self.user, "verify")
        entry.expires_at = timezone.now() - timedelta(seconds=1)
        entry.save(update_fields=["expires_at"])
        result = self.client.post(reverse("accounts:verify", kwargs={"token": raw}))
        self.assertEqual(result.status_code, 400)

    def test_password_reset_request_does_not_enumerate(self):
        existing = self.client.post(reverse("accounts:password_reset"), {"email": self.user.email})
        missing = self.client.post(reverse("accounts:password_reset"), {"email": "missing@example.com"})
        self.assertEqual(existing.status_code, missing.status_code)
        self.assertEqual(existing.context["notice"], missing.context["notice"])
        self.assertEqual(EmailJob.objects.filter(kind="reset").count(), 1)

    def test_reset_consumes_all_reset_links_and_revokes_sessions(self):
        self.login()
        other = Client()
        self.login(other)
        entry, raw = issue_token(self.user, "reset")
        second, second_raw = issue_token(self.user, "reset")
        response = self.client.post(reverse("accounts:password_reset_confirm", kwargs={"token": raw}), {
            "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD,
        })
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertEqual(DeviceSession.objects.filter(user=self.user, revoked_at__isnull=True).count(), 0)
        self.assertEqual(other.get(reverse("accounts:profile")).status_code, 302)
        self.assertEqual(self.client.get(reverse("accounts:password_reset_confirm", kwargs={"token": second_raw})).status_code, 400)

    def test_change_password_invalidates_existing_reset_link(self):
        self.login()
        _, raw = issue_token(self.user, "reset")
        response = self.client.post(reverse("accounts:password_change"), {
            "old_password": PASSWORD, "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse("accounts:password_reset_confirm", kwargs={"token": raw})).status_code, 400)
        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 302)

    def test_tokens_bound_to_credential_state_even_outside_portal(self):
        entry, raw = issue_token(self.user, "reset")
        self.user.set_password(NEW_PASSWORD)
        self.user.save(update_fields=["password"])
        self.assertEqual(self.client.get(reverse("accounts:password_reset_confirm", kwargs={"token": raw})).status_code, 400)

    def test_email_change_invalidates_old_verification_token(self):
        entry, raw = issue_token(self.user, "verify")
        self.user.email = "changed@example.com"
        self.user.save(update_fields=["email"])
        self.assertEqual(self.client.post(reverse("accounts:verify", kwargs={"token": raw})).status_code, 400)

    @patch("accounts.ratelimit.time")
    def test_admin_login_is_also_rate_limited(self, limiter_clock):
        limiter_clock.time.return_value = 60_030.0
        for _ in range(10):
            self.client.post("/admin/login/", {"username": "missing@example.com", "password": PASSWORD})
        response = self.client.post("/admin/login/", {"username": "missing@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 429)

    def test_unverified_staff_cannot_login_to_admin(self):
        self.user.is_staff = True
        self.user.role = "admin"
        self.user.email_verified = False
        self.user.save(update_fields=["is_staff", "role", "email_verified"])
        response = self.client.post("/admin/login/", {"username": self.user.email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_public_session_cannot_bypass_admin_verification(self):
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.email_verified = False
        self.user.save(update_fields=["is_staff", "is_superuser", "email_verified"])
        self.login()
        self.assertEqual(self.client.get("/admin/").status_code, 403)
        self.assertEqual(self.client.get("/admin/auth/group/").status_code, 403)
        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 200)

    @patch("accounts.forms.authenticate")
    def test_oversized_password_not_sent_to_hash_function(self, authenticate):
        response = self.client.post(reverse("accounts:login"), {"email": self.user.email, "password": "A" * 4097})
        self.assertEqual(response.status_code, 200)
        authenticate.assert_not_called()

    def test_other_user_session_cannot_be_revoked(self):
        other = User.objects.create_user(email="other@example.com", password=PASSWORD, email_verified=True)
        other_client = Client()
        other_client.force_login(other)
        device = DeviceSession.objects.get(user=other)
        self.login()
        response = self.client.post(reverse("accounts:revoke_device", kwargs={"device_id": device.pk}))
        self.assertEqual(response.status_code, 404)
        device.refresh_from_db()
        self.assertIsNone(device.revoked_at)

    def test_revoke_device_immediately_blocks_access(self):
        self.login()
        other = Client()
        self.login(other)
        device = DeviceSession.objects.get(session_key=other.session.session_key)
        response = self.client.post(reverse("accounts:revoke_device", kwargs={"device_id": device.pk}))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(other.get(reverse("accounts:profile")).status_code, 302)
        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 200)

    def test_absolute_session_expiry_enforced(self):
        self.login()
        DeviceSession.objects.filter(user=self.user).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 302)

    def test_security_version_invalidates_session(self):
        self.login()
        self.user.security_version += 1
        self.user.save(update_fields=["security_version"])
        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 302)

    def test_logout_is_post_only(self):
        self.login()
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)
        self.assertEqual(self.client.post(reverse("accounts:logout")).status_code, 302)
        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 302)

    def test_csrf_required_for_login(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("accounts:login"), {"email": self.user.email, "password": PASSWORD})
        self.assertEqual(response.status_code, 403)

    def test_data_export_requires_password_and_excludes_secrets(self):
        self.login()
        failed = self.client.post(reverse("accounts:export"), {"password": "wrong"})
        self.assertEqual(failed.status_code, 200)
        self.assertNotIn("Content-Disposition", failed)
        response = self.client.post(reverse("accounts:export"), {"password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        body = response.content.decode()
        self.assertNotIn(self.user.password, body)
        self.assertNotIn(self.client.session.session_key, body)
        self.assertEqual(response.json()["account"]["email"], self.user.email)

    def test_data_export_rechecks_password_under_account_lock(self):
        from .forms import ReauthenticateForm
        original = ReauthenticateForm.is_valid

        def concurrent_password_change(form):
            valid = original(form)
            User.objects.filter(pk=self.user.pk).update(password=make_password(NEW_PASSWORD))
            return valid

        self.login()
        with patch("accounts.views.ReauthenticateForm.is_valid", autospec=True, side_effect=concurrent_password_change):
            response = self.client.post(reverse("accounts:export"), {"password": PASSWORD})
        self.assertContains(response, "账户或密码已发生变更")
        self.assertNotIn("Content-Disposition", response)
        self.assertFalse(AuditEvent.objects.filter(action="account.data_export").exists())

    def test_account_delete_anonymizes_and_revokes(self):
        self.login()
        response = self.client.post(reverse("accounts:delete"), {"password": PASSWORD, "confirm": "DELETE"})
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertFalse(self.user.has_usable_password())
        self.assertIsNotNone(self.user.deleted_at)
        self.assertNotEqual(self.user.email, "owner@example.com")
        self.assertEqual(DeviceSession.objects.filter(user=self.user, revoked_at__isnull=True).count(), 0)

    def test_account_delete_rechecks_password_after_acquiring_account_lock(self):
        from .forms import ReauthenticateForm
        original = ReauthenticateForm.is_valid

        def concurrent_password_change(form):
            valid = original(form)
            User.objects.filter(pk=self.user.pk).update(password=make_password(NEW_PASSWORD))
            return valid

        self.login()
        with patch("accounts.views.ReauthenticateForm.is_valid", autospec=True, side_effect=concurrent_password_change):
            response = self.client.post(reverse("accounts:delete"), {"password": PASSWORD, "confirm": "DELETE"})
        self.assertContains(response, "账户或密码已发生变更")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertIsNone(self.user.deleted_at)

    def test_admin_cannot_self_delete(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.login()
        response = self.client.post(reverse("accounts:delete"), {"password": PASSWORD, "confirm": "DELETE"})
        self.assertContains(response, "管理员账户需先")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_membership_is_not_operator_permission(self):
        self.user.role = "premium"
        self.assertFalse(self.user.is_operator)
        self.user.role = "admin"
        self.assertFalse(self.user.is_operator)
        self.user.is_staff = True
        self.assertTrue(self.user.is_operator)

    def test_audit_redacts_secrets(self):
        event = record_event(None, "test.event", user=self.user, metadata={
            "password": PASSWORD, "api_token": "secret-value", "session_key": "session", "count": 2,
        })
        self.assertEqual(event.metadata, {"count": 2})

    def test_nested_audit_metadata_is_redacted(self):
        event = record_event(None, "test.event", user=self.user,
            metadata={"details": {"password": PASSWORD, "value": 2}, "objects": [PASSWORD]})
        self.assertEqual(event.metadata, {"details": {"value": 2}, "objects": "[unsupported]"})

    def test_admin_role_does_not_allow_privilege_escalation(self):
        self.user.role = "admin"
        self.user.is_staff = True
        self.user.save(update_fields=["role", "is_staff"])
        self.login()
        ordinary = User.objects.create_user(email="ordinary@example.com", password=PASSWORD)
        root = User.objects.create_superuser(email="root@example.com", password=PASSWORD)
        self.assertTrue(self.user.has_perm("accounts.change_user"))
        change_page = self.client.get(reverse("admin:accounts_user_change", args=[ordinary.pk]))
        self.assertEqual(change_page.status_code, 200)
        self.assertNotContains(change_page, 'value="superadmin"')
        self.assertEqual(self.client.get(reverse("admin:auth_user_password_change", args=[ordinary.pk])).status_code, 403)
        # Hidden privileged users cannot be accessed by guessing the primary key.
        response = self.client.get(reverse("admin:accounts_user_change", args=[root.pk]))
        self.assertNotEqual(response.status_code, 200)

    def test_failed_login_is_visible_in_own_history(self):
        self.client.post(reverse("accounts:login"), {"email": self.user.email, "password": "bad"})
        self.assertTrue(LoginRecord.objects.filter(user=self.user, success=False).exists())

    def test_login_limiter_blocks_burst(self):
        for _ in range(10):
            self.client.post(reverse("accounts:login"), {"email": "missing@example.com", "password": PASSWORD})
        result = self.client.post(reverse("accounts:login"), {"email": "missing@example.com", "password": PASSWORD})
        self.assertEqual(result.status_code, 429)

    @patch("accounts.services.dispatch_email")
    def test_email_outbox_is_durable_and_coalesces_requests(self, dispatch):
        with self.captureOnCommitCallbacks(execute=True):
            first = enqueue_email(self.user, "reset")
            second = enqueue_email(self.user, "reset")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(EmailJob.objects.filter(user=self.user).count(), 1)
        dispatch.assert_called_once_with(first.pk)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_worker_sends_real_email_backend_and_hashes_token(self):
        job = EmailJob.objects.create(user=self.user, kind="reset")
        deliver_email(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "sent")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/accounts/password/reset/", mail.outbox[0].body)
        self.assertEqual(AccountToken.objects.filter(user=self.user, kind="reset").count(), 1)
        # Outbox rows contain intent, not message bodies or plaintext reset tokens.
        self.assertNotIn("body", {field.name for field in EmailJob._meta.fields})

    @patch("accounts.tasks.send_mail", side_effect=RuntimeError("provider unavailable"))
    def test_failed_email_is_not_claimed_as_sent(self, send):
        job = EmailJob.objects.create(user=self.user, kind="verify")
        deliver_email(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "pending")
        self.assertIsNone(job.sent_at)
        self.assertEqual(job.last_error_code, "RuntimeError")
        self.assertGreater(job.next_attempt_at, timezone.now())

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_outbox_recovers_expired_worker_lease(self):
        job = EmailJob.objects.create(user=self.user, kind="verify", status="sending",
            lease_until=timezone.now() - timedelta(minutes=1))
        drain_email_outbox()
        job.refresh_from_db()
        self.assertEqual(job.status, "sent")

    def test_retention_keeps_active_tokens_and_pending_mail(self):
        old, raw = issue_token(self.user, "verify")
        active, raw = issue_token(self.user, "reset")
        AccountToken.objects.filter(pk=old.pk).update(expires_at=timezone.now() - timedelta(days=400))
        pending = EmailJob.objects.create(user=self.user, kind="reset")
        sent = EmailJob.objects.create(user=self.user, kind="verify", status="sent")
        EmailJob.objects.filter(pk__in=[pending.pk, sent.pk]).update(created_at=timezone.now() - timedelta(days=400))
        prune_account_history()
        self.assertFalse(AccountToken.objects.filter(pk=old.pk).exists())
        self.assertTrue(AccountToken.objects.filter(pk=active.pk).exists())
        self.assertTrue(EmailJob.objects.filter(pk=pending.pk).exists())
        self.assertFalse(EmailJob.objects.filter(pk=sent.pk).exists())


class AccountApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="api@example.com", password=PASSWORD, email_verified=True)

    def test_session_api_login_and_me(self):
        response = self.client.post("/api/v1/auth/login/", data=json.dumps({"email": self.user.email, "password": PASSWORD}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(self.client.get("/api/v1/auth/me/").json()["data"]["id"], self.user.pk)

    def test_api_rejects_malformed_json(self):
        response = self.client.post("/api/v1/auth/login/", data="[", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_REQUEST")

    def test_api_method_errors_preserve_json_contract(self):
        response = self.client.get("/api/v1/auth/login/")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")
        self.assertEqual(response.json()["code"], "METHOD_NOT_ALLOWED")

    def test_api_rejects_wrong_content_type(self):
        response = self.client.post("/api/v1/auth/login/", data={"email": self.user.email, "password": PASSWORD})
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["code"], "UNSUPPORTED_CONTENT_TYPE")

    def test_api_me_requires_authentication(self):
        response = self.client.get("/api/v1/auth/me/")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json()["success"])

    def test_api_csrf_mutations_protected(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post("/api/v1/auth/login/", data=json.dumps({"email": self.user.email, "password": PASSWORD}), content_type="application/json")
        self.assertEqual(response.status_code, 403)


@override_settings(EMAIL_DELIVERY_EAGER=False)
class ResearchAssessmentPrivacyTests(TestCase):
    """Exercise the actual account export/closure boundary with persisted research."""

    def setUp(self):
        from trading.models import ResearchAssessment
        cache.clear()
        self.user = User.objects.create_user(email="assessment-owner@example.com", password=PASSWORD, email_verified=True)
        self.other = User.objects.create_user(email="assessment-other@example.com", password=PASSWORD, email_verified=True)
        self.assessment = ResearchAssessment.objects.create(
            owner=self.user, policy_version="privacy-test-v1",
            snapshot={"private_history": [{"note": "owner-private-history", "net_pnl": "15.10"}]},
            result={"private_evidence": "owner-private-result"},
            snapshot_sha256="a" * 64, request_sha256="b" * 64,
            idempotency_key="owner-private-request",
        )
        self.other_assessment = ResearchAssessment.objects.create(
            owner=self.other, policy_version="privacy-test-v1",
            snapshot={"private_history": [{"note": "other-private-history"}]},
            result={"private_evidence": "other-private-result"},
            snapshot_sha256="c" * 64, request_sha256="d" * 64,
        )
        self.client.force_login(self.user)

    def test_export_includes_only_owned_assessment_snapshots(self):
        response = self.client.post(reverse("accounts:export"), {"password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        entries = response.json()["research"]["assessments"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], str(self.assessment.pk))
        self.assertEqual(entries[0]["snapshot"], self.assessment.snapshot)
        self.assertEqual(entries[0]["result"], self.assessment.result)
        self.assertNotIn("other-private-history", response.content.decode())
        self.assertNotIn("other-private-result", response.content.decode())

    def test_closure_erases_private_assessment_and_revokes_all_access(self):
        second_device = Client()
        second_device.force_login(self.user)
        url = f"/api/v1/assessments/{self.assessment.pk}/"
        self.assertEqual(second_device.get(url).status_code, 200)
        response = self.client.post(reverse("accounts:delete"), {"password": PASSWORD, "confirm": "DELETE"})
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assessment.refresh_from_db()
        self.other_assessment.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertIsNotNone(self.assessment.deleted_at)
        self.assertEqual(self.assessment.snapshot, {})
        self.assertEqual(self.assessment.result, {})
        self.assertEqual(self.assessment.snapshot_sha256, "")
        self.assertEqual(self.assessment.request_sha256, "")
        self.assertIsNone(self.assessment.idempotency_key)
        self.assertEqual(self.other_assessment.snapshot["private_history"][0]["note"], "other-private-history")
        self.assertIsNone(self.other_assessment.deleted_at)
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(second_device.get(url).status_code, 401)
        self.assertEqual(self.client.post(reverse("accounts:export"), {"password": PASSWORD}).status_code, 302)
        # Security audit is retained, but it must not duplicate cleared evidence.
        metadata = json.dumps(list(AuditEvent.objects.filter(actor=self.user).values("metadata")))
        self.assertNotIn("owner-private-history", metadata)
        self.assertNotIn("owner-private-result", metadata)

    def test_assessment_api_cannot_read_another_users_snapshot(self):
        response = self.client.get(f"/api/v1/assessments/{self.other_assessment.pk}/")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])
        self.assertNotIn("other-private-history", response.content.decode())
        self.assertNotIn("other-private-result", response.content.decode())
