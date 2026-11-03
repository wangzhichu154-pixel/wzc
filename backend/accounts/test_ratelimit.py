"""Fixed-window semantics with a real cache and a local, controlled clock.

These checks do not claim a rolling-window limit: an epoch-aligned boundary
starts a new allowance. Only the limiter's clock is patched; Django's cache
expiration and the rest of the application's time remain real.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .ratelimit import allow_request


@override_settings(CACHES={"default": {
    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    "LOCATION": "rate-limit-contract-tests",
}})
class FixedWindowRateLimitTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    @patch("accounts.ratelimit.time")
    def test_exact_limit_and_next_epoch_aligned_window(self, limiter_clock):
        limiter_clock.time.return_value = 60_059.999
        for _ in range(5):
            self.assertTrue(allow_request("operations-user", "17", limit=5, seconds=60))
        self.assertFalse(allow_request("operations-user", "17", limit=5, seconds=60))

        # This is an intentional fixed-window reset, not a rolling 60s claim.
        limiter_clock.time.return_value = 60_060.0
        for _ in range(5):
            self.assertTrue(allow_request("operations-user", "17", limit=5, seconds=60))
        self.assertFalse(allow_request("operations-user", "17", limit=5, seconds=60))

    @patch("accounts.ratelimit.time")
    def test_identities_and_scopes_have_separate_counters(self, limiter_clock):
        limiter_clock.time.return_value = 60_030.0
        self.assertTrue(allow_request("operations-user", "17", limit=1, seconds=60))
        self.assertFalse(allow_request("operations-user", "17", limit=1, seconds=60))
        self.assertTrue(allow_request("operations-user", "18", limit=1, seconds=60))
        self.assertTrue(allow_request("operations-ip", "17", limit=1, seconds=60))

    def test_cache_add_and_increment_failures_reject_the_request(self):
        for operation in ("add", "incr"):
            with self.subTest(operation=operation):
                with patch("accounts.ratelimit.cache.add", return_value=False) as add:
                    with patch("accounts.ratelimit.cache.incr") as increment:
                        (add if operation == "add" else increment).side_effect = ConnectionError("cache unavailable")
                        with self.assertLogs("accounts.ratelimit", level="ERROR") as captured:
                            self.assertFalse(allow_request("operations-user", "17", limit=5, seconds=60))
                        self.assertIn("account_rate_limit_unavailable", captured.output[0])
