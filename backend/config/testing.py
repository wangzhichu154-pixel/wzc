import os
import secrets

os.environ["APP_ENV"] = "testing"
os.environ.setdefault("DJANGO_SECRET_KEY", secrets.token_urlsafe(64))
os.environ["DJANGO_DEBUG"] = "false"
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost,127.0.0.1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
from .settings import *  # noqa: F403, E402

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_DELIVERY_EAGER = True
DEFAULT_FROM_EMAIL = "test@example.test"
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "tests"}}
if os.environ.get("TEST_USE_REDIS") == "1":
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": os.environ["REDIS_URL"], "KEY_PREFIX": "vector:tests"}}
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
