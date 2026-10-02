"""
Django settings for the Fuel Route Optimization API.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dvg)#@rysr)wltx(&3%ec)ckq+yh%g$3+kr-roqon80mz+@%wz",
)
DEBUG = env_bool("DEBUG", True)
ALLOWED_HOSTS = [
    part.strip()
    for part in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if part.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "routes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Fuel route optimization configuration
# ---------------------------------------------------------------------------

MPG = 10.0
TANK_CAPACITY_GALLONS = 50.0
MAX_RANGE_MILES = 500.0

OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
NOMINATIM_BASE_URL = os.environ.get(
    "NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org"
)
GEOCODER_USER_AGENT = os.environ.get(
    "GEOCODER_USER_AGENT", "fuel-route-optimizer-exercise/assessment@digifuel.com"
)

PROVIDER_CONNECT_TIMEOUT_SECONDS = env_float("PROVIDER_CONNECT_TIMEOUT_SECONDS", 2.0)
PROVIDER_READ_TIMEOUT_SECONDS = env_float("PROVIDER_READ_TIMEOUT_SECONDS", 8.0)

GEOCODE_CACHE_TTL_HOURS = env_int("GEOCODE_CACHE_TTL_HOURS", 720)
ROUTE_CACHE_TTL_HOURS = env_int("ROUTE_CACHE_TTL_HOURS", 168)

ROUTE_CORRIDOR_MILES = env_float("ROUTE_CORRIDOR_MILES", 10.0)
MAX_STATION_DETOUR_MILES = env_float("MAX_STATION_DETOUR_MILES", 20.0)
DETOUR_PENALTY_USD_PER_MILE = env_float("DETOUR_PENALTY_USD_PER_MILE", 0.20)
STATION_GEOCODE_DELAY_SECONDS = env_float("STATION_GEOCODE_DELAY_SECONDS", 1.0)

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "EXCEPTION_HANDLER": "routes.exceptions.drf_exception_handler",
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "routes": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
