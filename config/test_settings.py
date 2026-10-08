import os

from .settings import *  # noqa: F403


DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": os.getenv("SPOTTER_TEST_DB_PATH", ":memory:")}}
SECRET_KEY = "test-only-not-for-deployment"
SECURE_SSL_REDIRECT = False
ROUTING_PROVIDER_API_KEY = "test-only"
DATABASES["default"]["PASSWORD"] = "test-only"
