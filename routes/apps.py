from django.apps import AppConfig


class RoutesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "routes"

    def ready(self) -> None:
        from . import checks  # noqa: F401
