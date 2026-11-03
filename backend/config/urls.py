from django.contrib import admin
from django.urls import include, path
from common import views

admin.site.site_header = "Vector 运营管理"
admin.site.site_title = "Vector Admin"
admin.site.index_title = "用户、研究任务与审计"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("api/v1/auth/", include("accounts.api_urls")),
    path("health/live/", views.live, name="health-live"),
    path("health/ready/", views.ready, name="health-ready"),
    path("metrics/", views.metrics, name="metrics"),
    path("privacy/", views.policy, {"page": "privacy"}, name="privacy"),
    path("terms/", views.policy, {"page": "terms"}, name="terms"),
    path("cookies/", views.policy, {"page": "cookies"}, name="cookies"),
    path("about/", views.policy, {"page": "about"}, name="about"),
    path("execution/", views.execution_console, name="execution-console"),
    path("api/v1/execution/status/", views.execution_api, name="execution-status"),
    path("ai/", views.ml_console, name="ai-console"),
    path("api/v1/ai/status/", views.ml_api, name="ai-status"),
    path("", include("trading.urls")),
]
handler400 = "common.views.bad_request"
handler403 = "common.views.forbidden"
handler404 = "common.views.not_found"
handler500 = "common.views.server_error"
