from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.test import RequestFactory
from common.logging import redact
from common.middleware import TrustedProxyMiddleware


class PlatformTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_public_pages_and_security_headers(self):
        for url in ("/about/", "/privacy/", "/terms/", "/cookies/", "/accounts/login/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertEqual(response["X-Frame-Options"], "DENY")
            self.assertIn("script-src 'self'", response["Content-Security-Policy"])
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertEqual(len(response["X-Request-ID"]), 32)

    def test_request_identifier_not_trusted(self):
        response = self.client.get("/health/live/", HTTP_X_REQUEST_ID="attacker")
        self.assertNotEqual(response["X-Request-ID"], "attacker")

    def test_missing_api_resource_is_structured(self):
        response = self.client.get("/api/v1/no-such-resource/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "NOT_FOUND")
        self.assertFalse(response.json()["success"])

    def test_no_live_trading_endpoint(self):
        self.assertEqual(self.client.post("/api/v1/orders/live/").status_code, 404)

    @override_settings(METRICS_TOKEN="test-metrics-token-not-used-in-production")
    def test_metrics_auth(self):
        self.assertEqual(self.client.get("/metrics/").status_code, 403)
        response = self.client.get("/metrics/", HTTP_AUTHORIZATION="Bearer test-metrics-token-not-used-in-production")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "vector_http_requests_total")

    def test_csrf_blocks_mutation_without_token(self):
        response = Client(enforce_csrf_checks=True).post("/api/v1/auth/register/", data={}, content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "CSRF_FAILED")

    def test_api_limit_is_enforced(self):
        with patch("accounts.ratelimit.allow_request", return_value=False):
            response = self.client.get("/api/v1/no-such-resource/")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "60")

    @override_settings(ENGINE_BINARY="/nonexistent/binary")
    def test_readiness_does_not_fake_engine_availability(self):
        response = self.client.get("/health/ready/")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["data"]["ready"])

    def test_private_media_not_public(self):
        self.assertEqual(self.client.get("/private-media/datasets/1/file.itch").status_code, 404)

    @override_settings(TRUST_PROXY=True, TRUSTED_PROXY_CIDRS=["10.0.0.0/8"])
    def test_proxy_headers_only_from_trusted_peer(self):
        middleware = TrustedProxyMiddleware(lambda request: request)
        request = RequestFactory().get("/", REMOTE_ADDR="8.8.8.8", HTTP_X_REAL_IP="1.2.3.4", HTTP_X_FORWARDED_PROTO="https")
        result = middleware(request)
        self.assertEqual(result.client_ip, "8.8.8.8")
        self.assertNotIn("HTTP_X_FORWARDED_PROTO", result.META)
        trusted = RequestFactory().get("/", REMOTE_ADDR="10.0.0.2", HTTP_X_REAL_IP="1.2.3.4")
        self.assertEqual(middleware(trusted).client_ip, "1.2.3.4")

    def test_log_redaction_covers_token_and_dsn(self):
        self.assertNotIn("sensitive-token", redact("error at /accounts/password/reset/sensitive-token/"))
        self.assertNotIn("my-password", redact("postgres://user:my-password@database/db"))
