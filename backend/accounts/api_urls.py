from django.urls import path

from . import api

app_name = "accounts_api"
urlpatterns = [
    path("csrf/", api.csrf, name="csrf"),
    path("register/", api.register, name="register"),
    path("login/", api.login_view, name="login"),
    path("logout/", api.logout_view, name="logout"),
    path("me/", api.me, name="me"),
    path("devices/", api.devices, name="devices"),
    path("devices/<uuid:device_id>/revoke/", api.revoke_device, name="revoke_device"),
    path("password/reset/", api.password_reset, name="password_reset"),
]
