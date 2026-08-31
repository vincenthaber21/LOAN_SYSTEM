import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("SESSION_SECRET", "local-loan-system-secret")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

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