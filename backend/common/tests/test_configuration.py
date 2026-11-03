import os
from pathlib import Path
import secrets
import subprocess
import sys

from django.test import SimpleTestCase


class DeploymentConfigurationTests(SimpleTestCase):
    def environment(self):
        # Explicit isolated config; never inherit actual deployment credentials.
        return {
            "PATH": os.defpath,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "APP_ENV": "production", "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
            "DJANGO_DEBUG": "false", "DJANGO_ALLOWED_HOSTS": "trading.example.test",
            "PUBLIC_BASE_URL": "https://trading.example.test",
            "DATABASE_URL": "postgresql://test:test@localhost/test",
            "REDIS_URL": "redis://localhost/0", "CELERY_BROKER_URL": "redis://localhost/1",
            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "EMAIL_HOST": "smtp.example.test", "DEFAULT_FROM_EMAIL": "test@example.test",
            "EMAIL_DELIVERY_EAGER": "false", "METRICS_TOKEN": secrets.token_urlsafe(32),
            "LEGAL_ENTITY_NAME": "Isolated configuration test", "SUPPORT_EMAIL": "support@example.test",
        }

    def validate(self, env):
        return subprocess.run([sys.executable, "-c", "import config.settings"], env=env,
                              capture_output=True, text=True, timeout=10)

    def test_complete_production_config_imports_without_network(self):
        self.assertEqual(self.validate(self.environment()).returncode, 0)

    def test_insecure_deployments_fail_closed(self):
        for key, value in (("DJANGO_DEBUG", "true"), ("DATABASE_URL", "sqlite:///:memory:"),
                           ("PUBLIC_BASE_URL", "http://example.test"),
                           ("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"),
                           ("RATE_LIMIT_ENABLED", "false"), ("LEGAL_ENTITY_NAME", "")):
            with self.subTest(key=key):
                env = self.environment()
                env[key] = value
                self.assertNotEqual(self.validate(env).returncode, 0)
