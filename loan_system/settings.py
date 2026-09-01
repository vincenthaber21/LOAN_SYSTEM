import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# Loads D:\loan-management\.env (gitignored) if present, e.g. for local
# DJANGO_DEBUG=1. Does nothing in production, where real env vars are set
# directly (e.g. via the PythonAnywhere WSGI file) and no .env file exists.
load_dotenv(BASE_DIR / ".env")

# Defaults to False (production-safe). For local development, set
# DJANGO_DEBUG=1 in .env (see README) to turn it on.
DEBUG = os.environ.get("DJANGO_DEBUG", "0").strip() == "1"

SECRET_KEY = os.environ.get("SESSION_SECRET")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "django-insecure-local-dev-only-do-not-use-in-production"
    else:
        raise ImproperlyConfigured(
            "SESSION_SECRET environment variable must be set when DJANGO_DEBUG is not '1'."
        )

# Comma-separated list of allowed hostnames, e.g. "myapp.pythonanywhere.com,example.com".
# Defaults to localhost only — set DJANGO_ALLOWED_HOSTS explicitly for any real deployment.
ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()
]

# Comma-separated list of scheme://host origins allowed to POST here, e.g.
# "https://myapp.pythonanywhere.com". Required by Django's CSRF checks for any
# HTTPS deployment; leave unset only for plain local HTTP development.
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]

# --- HTTPS enforcement -------------------------------------------------
# Off in DEBUG (plain http://127.0.0.1:8000 dev server) and on by default
# once DEBUG is False. Override with DJANGO_SECURE_SSL_REDIRECT=0 only if the
# deployment host already terminates/redirects TLS itself.
SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "0" if DEBUG else "1") == "1"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG

# Set DJANGO_BEHIND_PROXY=1 only when deployed behind a proxy/load balancer that
# terminates TLS and sets X-Forwarded-Proto itself (e.g. most PaaS setups). Do not
# set this if Django is reachable directly, or the header becomes spoofable.
if os.environ.get("DJANGO_BEHIND_PROXY", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "lending",
    "savings",
    "mutual_aid",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "lending.middleware.LastSeenMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "loan_system.urls"
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
                "lending.context_processors.product_context",
                "savings.context_processors.savings_context",
                "mutual_aid.context_processors.mutual_aid_context",
            ],
        },
    }
]
WSGI_APPLICATION = "loan_system.wsgi.application"

def get_secure_database_config(db_url):
    config = dj_database_url.parse(db_url, conn_max_age=300, conn_health_checks=True)
    config.setdefault("OPTIONS", {})
    config["OPTIONS"].update({
        "connect_timeout": 20,
        "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
        "charset": "utf8mb4",
    })
    return {"default": config}


_database_url = os.environ.get("DATABASE_URL")
if _database_url:
    DATABASES = get_secure_database_config(db_url=_database_url)
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": "loan_db",
            "USER": "root",
            "PASSWORD": "root",
            "HOST": "127.0.0.1",
            "PORT": "3307",
            # Recycle before MySQL wait_timeout; health-check drops dead sockets
            # (avoids "MySQL server has gone away" in long-lived scheduler threads).
            "CONN_MAX_AGE": 300,
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {
                "connect_timeout": 20,
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
                "charset": "utf8mb4",
            },
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]
AUTH_USER_MODEL = "lending.User"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Manila"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
MESSAGE_TAGS = {"error": "danger"}