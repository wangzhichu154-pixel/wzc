from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        from . import signals  # noqa: F401
        from django.contrib import admin
        from .forms import OperatorAuthenticationForm
        admin.site.login_form = OperatorAuthenticationForm
