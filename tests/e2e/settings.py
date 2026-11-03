"""E2E-only settings: temporary database, files and synchronous real worker."""
import os
from pathlib import Path

workspace = Path(os.environ["VECTOR_E2E_WORKDIR"]).resolve()
if not workspace.is_dir():
    raise RuntimeError("E2E workspace must be created by the acceptance runner")
os.environ["DATABASE_URL"] = f"sqlite:///{workspace / 'acceptance.sqlite3'}"
os.environ["APP_ENV"] = "testing"
os.environ["DJANGO_DEBUG"] = "true"
os.environ["DJANGO_ALLOWED_HOSTS"] = "127.0.0.1,localhost,testserver"
os.environ["PUBLIC_BASE_URL"] = os.environ["VECTOR_E2E_BASE_URL"]
os.environ["REDIS_URL"] = ""
os.environ["TEST_USE_REDIS"] = "0"
os.environ["DJANGO_SECURE_SSL_REDIRECT"] = "false"
os.environ["TRUST_PROXY"] = "false"

from config.testing import *  # noqa: E402,F403

DEBUG = True  # Local runserver serves static assets for this isolated test only.
EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
EMAIL_FILE_PATH = workspace / "emails"
MEDIA_ROOT = workspace / "media"
EMAIL_DELIVERY_EAGER = True
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
LEGAL_ENTITY_NAME = "Automated acceptance test — not a deployed operator"
SUPPORT_EMAIL = "support@example.test"
