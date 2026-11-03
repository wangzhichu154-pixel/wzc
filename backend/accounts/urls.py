from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.register, name="register"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("profile/", views.profile, name="profile"),
    path("verify/<str:token>/", views.verify, name="verify"),
    path("verification/resend/", views.resend_verification, name="resend_verification"),
    path("password/change/", views.password_change, name="password_change"),
    path("password/reset/", views.password_reset, name="password_reset"),
    path("password/reset/<str:token>/", views.password_reset_confirm, name="password_reset_confirm"),
    path("devices/", views.devices, name="devices"),
    path("devices/revoke-others/", views.revoke_other_devices, name="revoke_other_devices"),
    path("devices/<uuid:device_id>/revoke/", views.revoke_device, name="revoke_device"),
    path("data/export/", views.export_data, name="export"),
    path("delete/", views.delete_account, name="delete"),
]
