from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction

from accounts.audit import record_event
from .models import AutomationCommand, Dataset, FeatureFlag, Feedback, PlatformSafetyState, ReplayJob, ResearchAssessment, RiskProfile, StrategyAutomation


class ImmutableAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Dataset)
class DatasetAdmin(ImmutableAdmin):
    list_display = ["id", "name", "owner", "byte_size", "message_count", "created_at", "deleted_at", "purged_at"]
    search_fields = ["name", "owner__email", "sha256"]
    list_filter = ["created_at"]
    list_select_related = ["owner"]


@admin.register(ReplayJob)
class ReplayJobAdmin(ImmutableAdmin):
    list_display = ["id", "owner", "mode", "status", "attempt", "created_at", "started_at", "finished_at"]
    list_filter = ["mode", "status", "created_at"]
    search_fields = ["owner__email", "id"]
    list_select_related = ["owner"]


@admin.register(RiskProfile)
class RiskProfileAdmin(ImmutableAdmin):
    list_display = ["owner", "max_order_size", "tick_size", "soft_inventory_limit", "hard_inventory_limit", "updated_at"]
    list_select_related = ["owner"]


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ["subject", "owner", "status", "created_at", "updated_at"]
    list_filter = ["status", "created_at"]
    search_fields = ["subject", "owner__email"]
    readonly_fields = ["id", "owner", "subject", "message", "created_at", "updated_at"]
    list_select_related = ["owner"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            owner = get_user_model().objects.select_for_update().get(pk=obj.owner_id)
            if owner.deleted_at is not None:
                raise PermissionDenied("账户已注销，不能重新写入关联反馈。")
            # Never save stale read-only subject/message fields after a privacy
            # mutation. Serialize with the same owner lock as account deletion.
            obj.save(update_fields=["status", "response", "updated_at"])
            record_event(request, "feedback.admin_updated", target=str(obj.pk), metadata={"status": obj.status})


@admin.register(FeatureFlag)
class FeatureFlagAdmin(admin.ModelAdmin):
    list_display = ["name", "enabled", "rollout_percent", "staff_only", "updated_at"]
    readonly_fields = ["updated_at"]

    def save_model(self, request, obj, form, change):
        obj.full_clean()
        super().save_model(request, obj, form, change)
        record_event(request, "feature_flag.updated", target=obj.name, metadata={"enabled": obj.enabled, "rollout_percent": obj.rollout_percent, "staff_only": obj.staff_only})

    def has_delete_permission(self, request, obj=None):
        # Disable instead: deletion could restore default enabled behavior.
        return False


@admin.register(PlatformSafetyState)
class PlatformSafetyAdmin(ImmutableAdmin):
    list_display = ["halted", "generation", "changed_by", "updated_at"]
    # Mutations require password reauthentication at /operations/.


@admin.register(StrategyAutomation)
class AutomationAdmin(ImmutableAdmin):
    list_display = ["name", "owner", "state", "risk_latched", "runs_created", "max_runs", "next_run_at"]
    list_filter = ["state", "risk_latched"]
    list_select_related = ["owner"]
    search_fields = ["name", "owner__email"]


@admin.register(AutomationCommand)
class AutomationCommandAdmin(ImmutableAdmin):
    list_display = ["id", "owner", "automation", "action", "created_at"]
    list_filter = ["action"]
    list_select_related = ["owner", "automation"]


@admin.register(ResearchAssessment)
class ResearchAssessmentAdmin(ImmutableAdmin):
    list_display = ["id", "owner", "policy_version", "created_at", "deleted_at"]
    list_select_related = ["owner"]
    search_fields = ["owner__email", "id"]
