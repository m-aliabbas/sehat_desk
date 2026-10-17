"""Django settings for SehatDesk.

Every environment-specific value comes from environment variables, or from a
.env file next to manage.py (SEHATDESK_ENV_FILE points at another one). See
.env.example and deploy/*.env.example for the full list.
"""

import os
from datetime import timedelta
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    SEHATDESK_ALLOWED_HOSTS=(list, ["sehatdesk.local", "localhost", "127.0.0.1"]),
    SEHATDESK_CSRF_TRUSTED_ORIGINS=(list, ["http://sehatdesk.local"]),
    SEHATDESK_SECURE_COOKIES=(bool, False),
    SEHATDESK_HTTPS=(bool, False),
    SEHATDESK_TIME_ZONE=(str, "Asia/Karachi"),
    SEHATDESK_LOG_LEVEL=(str, "INFO"),
    SEHATDESK_SESSION_IDLE_MINUTES=(int, 60),
    SEHATDESK_LOGIN_FAILURE_LIMIT=(int, 5),
    SEHATDESK_LOCKOUT_MINUTES=(int, 15),
)

_env_file = Path(os.environ.get("SEHATDESK_ENV_FILE", BASE_DIR / ".env"))
if _env_file.exists():
    environ.Env.read_env(_env_file, overwrite=False)
elif "SEHATDESK_ENV_FILE" in os.environ:
    raise ImproperlyConfigured(f"SEHATDESK_ENV_FILE not found: {_env_file}")

# --- Environment and secrets -------------------------------------------------
# development: local work and tests.
# staging:     a production build on a test machine with demo data, used to try a release
#              before it ships. Same safety rules as production, plus a visible banner.
# production:  customer machines.
ENVIRONMENTS = ("development", "staging", "production")
SEHATDESK_ENV = env("SEHATDESK_ENV", default="development")
if SEHATDESK_ENV not in ENVIRONMENTS:
    raise ImproperlyConfigured(f"SEHATDESK_ENV must be one of {', '.join(ENVIRONMENTS)}.")
DEPLOYED = SEHATDESK_ENV != "development"

DEBUG = env("DJANGO_DEBUG")
if DEPLOYED and DEBUG:
    # Debug pages show settings and local variables, which can include patient data.
    raise ImproperlyConfigured(f"DJANGO_DEBUG must be false when SEHATDESK_ENV={SEHATDESK_ENV}.")

# --- Data storage ------------------------------------------------------------
# Holds the SQLite database, media, and logs. The installer launcher sets this per OS.
DATA_DIR = Path(env("SEHATDESK_DATA_DIR", default=str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)  # first run on a fresh machine has no data folder

# Secret key: DJANGO_SECRET_KEY, else the key file in the data folder. Staging and production
# create that file on first start (random, readable by this OS user only), so customer machines
# never ship or type a secret. Development falls back to a fixed dev key.
SECRET_KEY_FILE = Path(env("SEHATDESK_SECRET_KEY_FILE", default=str(DATA_DIR / "secret.key")))
SECRET_KEY = env("DJANGO_SECRET_KEY", default="")
if not SECRET_KEY and (DEPLOYED or SECRET_KEY_FILE.exists()):
    from config.secrets import load_or_create_secret_key

    SECRET_KEY = load_or_create_secret_key(SECRET_KEY_FILE, create=DEPLOYED)
if not SECRET_KEY:
    SECRET_KEY = "insecure-development-key-do-not-use-in-production"

ALLOWED_HOSTS = env("SEHATDESK_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("SEHATDESK_CSRF_TRUSTED_ORIGINS")


# SQLite by default (docs/PROJECT_NOTES.md §2). SEHATDESK_DATABASE_URL switches to PostgreSQL,
# e.g. postgres://sehatdesk:secret@localhost:5432/sehatdesk (needs requirements-postgres.txt).
if env("SEHATDESK_DATABASE_URL", default=""):
    DATABASES = {"default": env.db("SEHATDESK_DATABASE_URL")}
    DATABASES["default"].setdefault("CONN_MAX_AGE", 60)
    DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": DATA_DIR / "sehatdesk.sqlite3",
            "OPTIONS": {
                "timeout": 20,
                # IMMEDIATE: every transaction takes the write lock when it starts, so two desks
                # writing at once wait their turn instead of failing with "database is locked"
                # when a read lock is upgraded mid-transaction (billing, stock, numbering).
                "transaction_mode": "IMMEDIATE",
                # WAL lets staff keep reading while one request writes.
                "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            },
        }
    }
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Applications ------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_htmx",
    "axes",
    "apps.core",
    "apps.organizations",
    "apps.accounts",
    "apps.audit",
    "apps.masterdata",
    "apps.patients",
    "apps.appointments",
    "apps.queue",
    "apps.billing",
    "apps.encounters",
    "apps.vitals",
    "apps.prescriptions",
    "apps.lab",
    "apps.pharmacy",
    "apps.ipd",
    "apps.nursing",
    "apps.documents",
    "apps.notifications",
    "apps.reports",
    "apps.search",
    "apps.procedures",
    "apps.hr",
    "apps.finance",
]

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = [
    # Axes must come first so locked-out users are refused before the password is checked.
    "axes.backends.AxesStandaloneBackend",
    "apps.accounts.backends.SehatDeskBackend",
]

MIDDLEWARE = [
    # Request ID (X-Request-ID) for logs and error reports, plus request metrics.
    "apps.core.monitoring.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # CSP, Permissions-Policy, and no browser caching of signed-in pages.
    "apps.core.security.SecurityHeadersMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Every page needs a signed-in user unless the view is marked @login_not_required.
    "apps.accounts.middleware.SehatDeskLoginRequiredMiddleware",
    # Sets request.organization/branch/sd_perms; enforces branch access and password change.
    "apps.accounts.middleware.WorkspaceMiddleware",
    # Blocks pages of modules switched off under Settings → Preferences.
    "apps.core.features.FeatureMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Raises time-based notifications (reminders, stock alerts) at most every few minutes.
    "apps.notifications.middleware.NotificationSweepMiddleware",
    # Must stay last (django-axes docs).
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.shell",
                "apps.notifications.context_processors.notifications",
            ],
        },
    },
]

# --- Passwords ---------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Localization (Pakistan defaults; Urdu UI is a later phase) ----------------
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("ur", "Urdu")]
TIME_ZONE = env("SEHATDESK_TIME_ZONE")
USE_I18N = True
USE_TZ = True

# --- Static files (served by WhiteNoise, no separate web server) ----------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# static/css/app.css is built by `python manage.py tailwind build` and committed.
# static/vendor/ holds pinned htmx and Alpine.js so the UI works without internet.
STATICFILES_DIRS = [BASE_DIR / "static"]

# Uploaded files (hospital logo now; documents later) live in the data folder.
MEDIA_URL = "media/"
MEDIA_ROOT = DATA_DIR / "media"

# Patient documents (Phase 18) use their own storage, outside the public media route. The
# default keeps files in the data folder; set SEHATDESK_DOCUMENT_STORAGE to an S3-compatible
# backend (e.g. "storages.backends.s3.S3Storage" with django-storages) for object storage.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    "documents": {
        "BACKEND": env(
            "SEHATDESK_DOCUMENT_STORAGE",
            default="django.core.files.storage.FileSystemStorage",
        ),
        "OPTIONS": env.json("SEHATDESK_DOCUMENT_STORAGE_OPTIONS", default={})
        or {"location": str(DATA_DIR / "private" / "documents"), "base_url": None},
    },
}
SEHATDESK_DOCUMENT_MAX_MB = env.int("SEHATDESK_DOCUMENT_MAX_MB", default=15)
# Document links are signed for the signed-in user and expire after this many seconds.
SEHATDESK_DOCUMENT_LINK_SECONDS = env.int("SEHATDESK_DOCUMENT_LINK_SECONDS", default=300)

# --- Backups (apps/core/backup.py, docs/BACKUP_AND_RECOVERY.md) --------------------------
# serve.py makes a backup at start-up when the last one is older than SEHATDESK_BACKUP_HOURS,
# then checks every hour. 0 = no automatic backups.
SEHATDESK_BACKUP_DIR = env("SEHATDESK_BACKUP_DIR", default="")
SEHATDESK_BACKUP_HOURS = env.float("SEHATDESK_BACKUP_HOURS", default=24)
SEHATDESK_BACKUP_KEEP = env.int("SEHATDESK_BACKUP_KEEP", default=14)
# Base64 32-byte key (manage.py backup --generate-key). Set it to encrypt every backup; keep a
# copy OFF this computer, or the backups cannot be restored.
SEHATDESK_BACKUP_KEY = env("SEHATDESK_BACKUP_KEY", default="")

# --- Notifications -------------------------------------------------------------------
# Time-based checks (reminders, stock, expiry) run at most this often per branch; 0 = off.
SEHATDESK_NOTIFICATION_SWEEP_SECONDS = env.int("SEHATDESK_NOTIFICATION_SWEEP_SECONDS", default=300)
# Dotted paths to SMS / WhatsApp provider adapters (apps/notifications/channels.py). Empty =
# not set up: messages are logged as skipped.
SEHATDESK_SMS_ADAPTER = env("SEHATDESK_SMS_ADAPTER", default="")
SEHATDESK_WHATSAPP_ADAPTER = env("SEHATDESK_WHATSAPP_ADAPTER", default="")
# Staging may hold a copy of real phone numbers: patient messages stay off unless opted in.
if SEHATDESK_ENV == "staging" and not env.bool("SEHATDESK_STAGING_SEND_MESSAGES", default=False):
    SEHATDESK_SMS_ADAPTER = SEHATDESK_WHATSAPP_ADAPTER = ""

# --- Authentication and sessions ----------------------------------------------------
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

# Idle timeout: the session expires after this many minutes without a request.
SESSION_COOKIE_AGE = env("SEHATDESK_SESSION_IDLE_MINUTES") * 60
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

# Login rate limiting (django-axes): lock a username+IP pair after repeated failures.
AXES_FAILURE_LIMIT = env("SEHATDESK_LOGIN_FAILURE_LIMIT")
AXES_COOLOFF_TIME = timedelta(minutes=env("SEHATDESK_LOCKOUT_MINUTES"))
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = "accounts/locked_out.html"
AXES_SENSITIVE_PARAMETERS = ["password"]

# --- Email (password reset links) --------------------------------------------------
# Hospitals without email reset passwords through an administrator (Staff → Reset password).
EMAIL_HOST = env("SEHATDESK_EMAIL_HOST", default="")
EMAIL_CONFIGURED = bool(EMAIL_HOST)
if EMAIL_CONFIGURED:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = env.int("SEHATDESK_EMAIL_PORT", default=587)
    EMAIL_HOST_USER = env("SEHATDESK_EMAIL_USER", default="")
    EMAIL_HOST_PASSWORD = env("SEHATDESK_EMAIL_PASSWORD", default="")
    EMAIL_USE_TLS = env.bool("SEHATDESK_EMAIL_TLS", default=True)
    DEFAULT_FROM_EMAIL = env("SEHATDESK_EMAIL_FROM", default=EMAIL_HOST_USER)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# --- Security ----------------------------------------------------------------
# Plain HTTP on the LAN is acceptable for the pilot (docs/PROJECT_NOTES.md §4).
# SEHATDESK_HTTPS=true once a TLS proxy sits in front of Waitress: secure cookies, redirect
# to https, HSTS, and trust the proxy's X-Forwarded-Proto header.
HTTPS = env("SEHATDESK_HTTPS")
SESSION_COOKIE_SECURE = HTTPS or env("SEHATDESK_SECURE_COOKIES")
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
# The CSRF token reaches htmx through the page (base.html hx-headers), never from the cookie.
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
# No CORS: no other site may call this server from a browser (no Access-Control-* headers).
if HTTPS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    # /health/ is probed by the launcher over plain HTTP on the same machine.
    SECURE_REDIRECT_EXEMPT = [r"^health/"]
    SECURE_HSTS_SECONDS = env.int("SEHATDESK_HSTS_SECONDS", default=31536000)
elif DEPLOYED:
    # Known pilot gaps that `check --deploy` reports while the LAN is plain HTTP:
    # HSTS, https redirect, and secure session/CSRF cookies. Everything else must pass.
    SILENCED_SYSTEM_CHECKS = ["security.W004", "security.W008"]
    if not SESSION_COOKIE_SECURE:
        SILENCED_SYSTEM_CHECKS += ["security.W012", "security.W016"]

# --- Logging -----------------------------------------------------------------
# Never log patient data (CLAUDE.md, rules for code work). Log IDs and event names only.
# Every handler masks phone numbers, CNICs and emails (apps/core/observability.py) and adds the
# request ID. SEHATDESK_LOG_FORMAT=json writes one JSON object per line (default on the log file).
LOG_FORMAT = env("SEHATDESK_LOG_FORMAT", default="text")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redact": {"()": "apps.core.observability.RedactFilter"},
        "request_id": {"()": "apps.core.observability.RequestIdFilter"},
    },
    "formatters": {
        "standard": {
            # Deployed machines drop exception messages from tracebacks (they can quote PHI).
            "()": "apps.core.observability.SafeTextFormatter" if DEPLOYED else "logging.Formatter",
            "format": "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s",
        },
        "json": {"()": "apps.core.observability.JsonFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json" if LOG_FORMAT == "json" else "standard",
            "filters": ["request_id", "redact"],
        },
    },
    "root": {"handlers": ["console"], "level": env("SEHATDESK_LOG_LEVEL")},
    # django-axes logs every sign-in at INFO; keep only lockouts and problems.
    "loggers": {"axes": {"level": "WARNING"}},
}
if DEPLOYED:
    # Customer machines have no terminal to read: keep a rotating log in the data folder.
    LOG_DIR = DATA_DIR / "logs"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOGGING["handlers"]["file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(LOG_DIR / "sehatdesk.log"),
        "maxBytes": 5 * 1024 * 1024,
        "backupCount": 5,
        "encoding": "utf-8",
        "formatter": "json",
        "filters": ["request_id", "redact"],
    }
    LOGGING["root"]["handlers"].append("file")
