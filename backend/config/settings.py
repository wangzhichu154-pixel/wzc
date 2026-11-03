"""Environment validated settings. No credentials or live-trading enable switch here."""
import os
from pathlib import Path
from urllib.parse import urlparse

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BASE_DIR.parent
APP_VERSION = (PROJECT_DIR / "VERSION").read_text(encoding="ascii").strip()
APP_ENV = os.environ.get("APP_ENV", "development")
if APP_ENV not in {"development", "testing", "staging", "production"}:
    raise ImproperlyConfigured("APP_ENV must be development/testing/staging/production")
DEPLOYED = APP_ENV in {"staging", "production"}


def boolean(name, default=False):
    value = os.environ.get(name, "true" if default else "false").lower()
    if value not in {"true", "false", "1", "0"}:
        raise ImproperlyConfigured(f"{name} must be true or false")
    return value in {"true", "1"}


def integer(name, default, low=1, high=2**31 - 1):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer") from exc
    if not low <= value <= high:
        raise ImproperlyConfigured(f"{name} outside supported range")
    return value


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if len(SECRET_KEY) < 50:
    raise ImproperlyConfigured("Set a unique DJANGO_SECRET_KEY of at least 50 characters; use scripts/init_env.py")
DEBUG = boolean("DJANGO_DEBUG", APP_ENV == "development")
ALLOWED_HOSTS = [v.strip() for v in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",") if v.strip()]
CSRF_TRUSTED_ORIGINS = [v.strip() for v in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if v.strip()]
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "accounts.apps.AccountsConfig", "trading.apps.TradingConfig", "common.apps.CommonConfig",
]
MIDDLEWARE = [
    "common.middleware.RequestContextMiddleware",
    "common.middleware.SecurityHeadersMiddleware",
    "common.middleware.TrustedProxyMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.SessionSecurityMiddleware",
    "common.middleware.RateLimitMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages", "common.context.product_context",
    ]},
}]

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}")
DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=60, conn_health_checks=True)}
REDIS_URL = os.environ.get("REDIS_URL", "")
CACHES = {"default": {
    "BACKEND": "django.core.cache.backends.redis.RedisCache" if REDIS_URL else "django.core.cache.backends.locmem.LocMemCache",
    "LOCATION": REDIS_URL or "vector-development",
    "KEY_PREFIX": f"vector:{APP_ENV}",
}}
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["accounts.backends.EmailBackend"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher", "django.contrib.auth.hashers.PBKDF2PasswordHasher"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = integer("SESSION_COOKIE_AGE", 1800)
SESSION_COOKIE_SECURE = DEPLOYED
CSRF_COOKIE_SECURE = DEPLOYED
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_FAILURE_VIEW = "common.views.csrf_failure"
SECURE_SSL_REDIRECT = boolean("DJANGO_SECURE_SSL_REDIRECT", DEPLOYED)
SECURE_REDIRECT_EXEMPT = [r"^health/(live|ready)/$", r"^metrics/$"]
SECURE_HSTS_SECONDS = 31536000 if DEPLOYED else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = DEPLOYED
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
# Only enable behind an ingress that OVERWRITES forwarded headers and keeps the
# application port private. Direct deployments leave this false.
TRUST_PROXY = boolean("TRUST_PROXY", False)
TRUSTED_PROXY_CIDRS = [v.strip() for v in os.environ.get("TRUSTED_PROXY_CIDRS", "127.0.0.0/8,::1/128").split(",") if v.strip()]
if TRUST_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
ACCOUNT_VERIFICATION_TTL = integer("ACCOUNT_VERIFICATION_TTL", 86400)
ACCOUNT_SESSION_MAX_AGE = integer("ACCOUNT_SESSION_MAX_AGE", 604800)
ACCOUNT_IP_HASH_KEY = os.environ.get("ACCOUNT_IP_HASH_KEY", SECRET_KEY)

EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.filebased.EmailBackend")
EMAIL_FILE_PATH = Path(os.environ.get("EMAIL_FILE_PATH", PROJECT_DIR / "emails"))
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = integer("EMAIL_PORT", 587, high=65535)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = boolean("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = boolean("EMAIL_USE_SSL", False)
EMAIL_TIMEOUT = 15
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "")
EMAIL_DELIVERY_EAGER = boolean("EMAIL_DELIVERY_EAGER", APP_ENV == "development")

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", REDIS_URL or "redis://localhost:6379/1")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", REDIS_URL or "redis://localhost:6379/2")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TASK_IGNORE_RESULT = True
CELERY_IMPORTS = ("trading.tasks_automations",)
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_CONNECTION_TIMEOUT = 3
CELERY_TASK_PUBLISH_RETRY = False
CELERY_BEAT_SCHEDULE = {
    "email-outbox": {"task": "accounts.tasks.drain_email_outbox", "schedule": 30.0},
    "worker-heartbeat": {"task": "common.tasks.worker_heartbeat", "schedule": 30.0},
    "reconcile-replays": {"task": "trading.tasks.reconcile_replays", "schedule": 60.0},
    "purge-deleted-datasets": {"task": "trading.tasks.purge_deleted_datasets", "schedule": 300.0},
    "prune-account-history": {"task": "accounts.tasks.prune_account_history", "schedule": 3600.0},
    "automatic-strategy-scheduler": {"task": "trading.tasks_automations.tick_automations", "schedule": 15.0},
}

ENGINE_BINARY = os.environ.get("ENGINE_BINARY", str(PROJECT_DIR / "build" / "trading-replay"))
PAPER_STATUS_PATH = os.environ.get("PAPER_STATUS_PATH", "")
PAPER_STATUS_MAX_AGE_SECONDS = integer("PAPER_STATUS_MAX_AGE_SECONDS", 5, high=60)
REPLAY_TIMEOUT_SECONDS = integer("REPLAY_TIMEOUT_SECONDS", 120, high=3600)
REPLAY_MEMORY_BYTES = integer("REPLAY_MEMORY_BYTES", 1073741824, low=67108864, high=17179869184)
MAX_REPLAY_ATTEMPTS = integer("MAX_REPLAY_ATTEMPTS", 3, high=10)
MAX_DATASET_BYTES = integer("MAX_DATASET_BYTES", 268435456)
MAX_USER_DATASET_BYTES = integer("MAX_USER_DATASET_BYTES", 1073741824)
MAX_USER_DATASETS = integer("MAX_USER_DATASETS", 100, high=10000)
MAX_ASSESSMENTS_PER_USER = integer("MAX_ASSESSMENTS_PER_USER", 1000, high=10000)
ASSESSMENT_MIN_CAPTURES = integer("ASSESSMENT_MIN_CAPTURES", 5, high=50)
ASSESSMENT_MIN_FILLED_QUANTITY = integer("ASSESSMENT_MIN_FILLED_QUANTITY", 100, high=10**12)
MAX_ACTIVE_REPLAYS_PER_USER = integer("MAX_ACTIVE_REPLAYS_PER_USER", 2, high=16)
# Django excludes uploaded file bytes from this form-data budget. File contents
# have independent streaming and ingress limits; JSON/auth bodies stay small.
DATA_UPLOAD_MAX_MEMORY_SIZE = 1048576
DATA_UPLOAD_MAX_NUMBER_FIELDS = 64
DATA_UPLOAD_MAX_NUMBER_FILES = 1
FILE_UPLOAD_MAX_MEMORY_SIZE = 1048576
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
STATIC_URL = "/static/"
STATIC_ROOT = Path(os.environ.get("STATIC_ROOT", PROJECT_DIR / "staticfiles"))
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", PROJECT_DIR / "media"))
MEDIA_URL = "/private-media/"  # Deliberately NOT routed or served by nginx.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
METRICS_TOKEN = os.environ.get("METRICS_TOKEN", "")
ML_MODEL_STORE_DIR = os.environ.get("ML_MODEL_STORE_DIR", "")
LEGAL_ENTITY_NAME = os.environ.get("LEGAL_ENTITY_NAME", "")
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "")
DATA_RETENTION_DAYS = integer("DATA_RETENTION_DAYS", 30)
AUDIT_RETENTION_DAYS = integer("AUDIT_RETENTION_DAYS", 365)
BACKUP_RETENTION_DAYS = integer("BACKUP_RETENTION_DAYS", 30)
RATE_LIMIT_ENABLED = boolean("RATE_LIMIT_ENABLED", True)
LOGGING = {
    "version": 1, "disable_existing_loggers": False,
    "filters": {"safe": {"()": "common.logging.SafeLogFilter"}},
    "formatters": {"json": {"()": "common.logging.SafeJsonFormatter", "format": "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json", "filters": ["safe"]}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.server": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

if DEPLOYED:
    faults = []
    if DEBUG or "*" in ALLOWED_HOSTS or not ALLOWED_HOSTS:
        faults.append("DEBUG=false and explicit ALLOWED_HOSTS required")
    if not DATABASES["default"]["ENGINE"].endswith("postgresql"):
        faults.append("PostgreSQL required")
    if not REDIS_URL or not os.environ.get("CELERY_BROKER_URL"):
        faults.append("shared Redis and Celery required")
    if not SECURE_SSL_REDIRECT or urlparse(PUBLIC_BASE_URL).scheme != "https":
        faults.append("HTTPS PUBLIC_BASE_URL and redirect required")
    if EMAIL_BACKEND != "django.core.mail.backends.smtp.EmailBackend" or not EMAIL_HOST or not DEFAULT_FROM_EMAIL or EMAIL_DELIVERY_EAGER:
        faults.append("asynchronous SMTP configuration required")
    if not (EMAIL_USE_TLS or EMAIL_USE_SSL) or (EMAIL_USE_TLS and EMAIL_USE_SSL):
        faults.append("exactly one SMTP TLS mode required")
    if len(METRICS_TOKEN) < 32 or not LEGAL_ENTITY_NAME or not SUPPORT_EMAIL:
        faults.append("metrics credential, legal entity and support address required")
    if not RATE_LIMIT_ENABLED:
        faults.append("shared rate limiting cannot be disabled")
    if faults:
        raise ImproperlyConfigured("Deployment configuration invalid: " + "; ".join(faults))
