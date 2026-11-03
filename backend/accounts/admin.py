from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied

from .audit import record_event
from .forms import BoundedAdminPasswordChangeForm
from .models import AuditEvent, DeviceSession, EmailJob, LoginRecord, User
from .services import revoke_sessions


@admin.register(User)
class AccountAdmin(UserAdmin):
    change_password_form = BoundedAdminPasswordChangeForm
    list_display = ("email", "role", "email_verified", "is_active", "is_staff", "date_joined")
    list_filter = ("role", "is_active", "email_verified", "is_staff")
    search_fields = ("email",)
    ordering = ("email",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Account", {"fields": ("role", "email_verified", "is_active")}),
        ("Administration", {"fields": ("is_staff", "is_superuser", "groups", "user_permissions")}),
        ("History", {"fields": ("date_joined", "last_login", "updated_at", "deleted_at", "security_version")}),
    )
    readonly_fields = ("date_joined", "last_login", "updated_at", "deleted_at", "security_version")

    def has_module_permission(self, request):
        return getattr(request.user, "is_operator", False) and super().has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return getattr(request.user, "is_operator", False) and super().has_change_permission(request, obj)

    def has_view_permission(self, request, obj=None):
        return getattr(request.user, "is_operator", False) and super().has_view_permission(request, obj)

    def user_change_password(self, request, id, form_url=""):
        if not request.user.is_superuser:
            raise PermissionDenied("Administrator password reset requires superuser authorization")
        response = super().user_change_password(request, id, form_url)
        if request.method == "POST" and response.status_code == 302:
            record_event(request, "admin.user_password_changed", target=id)
        return response

    def has_add_permission(self, request):
        return False  # Register normally; bootstrap the first superuser through management CLI.

    def has_delete_permission(self, request, obj=None):
        return False  # Account closure service preserves audit and data lifecycle guarantees.

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if not request.user.is_superuser:
            queryset = queryset.filter(is_staff=False, is_superuser=False).exclude(role__in=["admin", "superadmin"])
        return queryset

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            fields += ["email", "password", "email_verified", "is_staff", "is_superuser", "groups", "user_permissions"]
        return fields

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        if db_field.name == "role" and not request.user.is_superuser:
            kwargs["choices"] = [choice for choice in User.Role.choices if choice[0] in {"user", "member", "premium"}]
        return super().formfield_for_choice_field(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        old = User.objects.get(pk=obj.pk)
        protected = ("is_staff", "is_superuser", "email_verified", "email", "password")
        if not request.user.is_superuser and (any(getattr(old, k) != getattr(obj, k) for k in protected)
                or obj.role in {"admin", "superadmin"}):
            raise PermissionDenied("Only a superuser can change account privileges")
        if obj.is_superuser and (not obj.is_staff or not obj.is_active):
            raise PermissionDenied("Remove superuser privileges before deactivating an administrator")
        if obj.role == "superadmin" and not obj.is_superuser:
            raise PermissionDenied("Superadmin role requires explicit superuser authorization")
        if old.is_superuser and (not obj.is_superuser or not obj.is_active):
            # Serialize administrator removals, including concurrent demotions.
            list(User.objects.select_for_update().filter(is_superuser=True, is_active=True)
                 .order_by("pk").values_list("pk", flat=True))
            if not User.objects.filter(is_superuser=True, is_active=True).exclude(pk=obj.pk).exists():
                raise PermissionDenied("The last active superuser cannot be removed")
        # Revocation is also necessary when a session's access was reduced.
        if any(getattr(old, k) != getattr(obj, k) for k in ("is_active", "role", "is_staff", "is_superuser", "password", "email", "email_verified")):
            obj.security_version += 1
            revoke_sessions(obj)
        super().save_model(request, obj, form, change)
        record_event(request, "admin.user_changed", target=str(obj.pk), metadata={"fields": ",".join(form.changed_data)})


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "actor", "action", "target", "request_id")
    list_filter = ("action", "created_at")
    search_fields = ("target", "request_id")


@admin.register(LoginRecord)
class LoginRecordAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "user", "success", "new_device")
    list_filter = ("success", "new_device")


@admin.register(DeviceSession)
class DeviceSessionAdmin(ReadOnlyAdmin):
    exclude = ("session_key",)
    list_display = ("user", "created_at", "last_seen_at", "expires_at", "revoked_at")


@admin.register(EmailJob)
class EmailJobAdmin(ReadOnlyAdmin):
    list_display = ("user", "kind", "status", "attempts", "created_at", "sent_at", "last_error_code")
    list_filter = ("kind", "status")
