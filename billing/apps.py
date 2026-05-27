from django.apps import AppConfig


class BillingConfig(AppConfig):
    name = 'billing'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        # Import side-effects: registers post_save handlers.
        from . import signals  # noqa: F401
