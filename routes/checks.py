from django.conf import settings
from django.core.checks import Error, register


@register()
def required_configuration(app_configs, **kwargs):
    errors = []
    if settings.SECRET_KEY in {"development-key-not-configured", "replace-with-a-long-random-value"}:
        errors.append(Error("Set DJANGO_SECRET_KEY to a real secret.", id="routes.E001"))
    if not settings.DATABASES["default"]["PASSWORD"] or settings.DATABASES["default"]["PASSWORD"] == "replace-with-a-local-database-password":
        errors.append(Error("Set DB_PASSWORD for the PostGIS database.", id="routes.E002"))
    if not settings.ROUTING_PROVIDER_API_KEY or settings.ROUTING_PROVIDER_API_KEY == "replace-with-a-provider-key":
        errors.append(Error("Set ROUTING_PROVIDER_API_KEY before route planning.", id="routes.E003"))
    return errors
