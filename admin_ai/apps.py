from django.apps import AppConfig


class AdminAiConfig(AppConfig):
    name = 'admin_ai'
    verbose_name = 'Admin AI'

    def ready(self):
        # Import tool registry so built-in tools self-register on app load.
        from . import tools  # noqa: F401
