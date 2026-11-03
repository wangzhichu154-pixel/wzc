import hashlib
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q


def dataset_path(instance, filename):
    return f"datasets/{instance.owner_id}/{uuid.uuid4().hex}.itch"


class Dataset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="datasets")
    name = models.CharField(max_length=120)
    original_name = models.CharField(max_length=200)
    file = models.FileField(upload_to=dataset_path, max_length=255)
    byte_size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64, db_index=True)
    message_count = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "created_at"])]
        constraints = [models.CheckConstraint(condition=Q(byte_size__gt=0), name="dataset_nonempty")]

    def __str__(self):
        return self.name


class ReplayJob(models.Model):
    class Mode(models.TextChoices):
        REPLAY = "replay", "行情研究回放"
        SIMULATION = "simulation", "自动做市模拟"

    class Status(models.TextChoices):
        QUEUED = "queued", "等待执行"
        RUNNING = "running", "正在执行"
        SUCCEEDED = "succeeded", "已完成"
        FAILED = "failed", "失败"
        CANCELED = "canceled", "已取消"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="replay_jobs")
    dataset = models.ForeignKey(Dataset, on_delete=models.PROTECT, related_name="jobs")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.REPLAY)
    attempt = models.PositiveSmallIntegerField(default=1)
    parent = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True)
    automation = models.ForeignKey("StrategyAutomation", on_delete=models.PROTECT, null=True, blank=True, related_name="jobs")
    automation_generation = models.PositiveBigIntegerField(null=True, blank=True)
    result = models.JSONField(default=dict, blank=True)
    risk_parameters = models.JSONField(default=dict)
    simulation_parameters = models.JSONField(default=dict)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=1000, blank=True)
    cancellation_requested = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "created_at"]),
            models.Index(fields=["owner", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["status", "heartbeat_at"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(attempt__gte=1), name="job_positive_attempt"),
            models.CheckConstraint(condition=Q(mode__in=["replay", "simulation"]), name="replay_valid_mode"),
            models.UniqueConstraint(fields=["owner", "idempotency_key"], name="replay_owner_idempotency"),
            models.UniqueConstraint(fields=["automation"], condition=Q(automation__isnull=False, status__in=["queued", "running"]), name="automation_one_active_job"),
        ]

    def __str__(self):
        return f"{self.id} {self.status}"


class RiskProfile(models.Model):
    """Research configuration. This model never grants live trading permission."""

    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="risk_profile")
    max_order_size = models.PositiveIntegerField(default=100, validators=[MinValueValidator(1), MaxValueValidator(10000)])
    tick_size = models.PositiveIntegerField(default=100, validators=[MinValueValidator(1), MaxValueValidator(100000)])
    soft_inventory_limit = models.PositiveIntegerField(default=500, validators=[MinValueValidator(1), MaxValueValidator(100000)])
    hard_inventory_limit = models.PositiveIntegerField(default=1000, validators=[MinValueValidator(2), MaxValueValidator(100000)])
    daily_loss_limit = models.DecimalField(max_digits=12, decimal_places=2, default="1000.00", validators=[MinValueValidator(1)])
    orders_per_second = models.PositiveIntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(50)])
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(soft_inventory_limit__lt=models.F("hard_inventory_limit")), name="risk_soft_below_hard"),
            models.CheckConstraint(condition=Q(max_order_size__lte=models.F("hard_inventory_limit")), name="risk_order_below_hard"),
            models.CheckConstraint(condition=Q(daily_loss_limit__gt=0), name="risk_positive_loss"),
            models.CheckConstraint(condition=Q(tick_size__gte=1, tick_size__lte=100000), name="risk_tick_size_range"),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.soft_inventory_limit >= self.hard_inventory_limit:
            raise ValidationError({"soft_inventory_limit": "软限额必须小于硬限额。"})
        if self.max_order_size > self.hard_inventory_limit:
            raise ValidationError({"max_order_size": "单笔数量不能超过库存硬限额。"})


class Feedback(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "待处理"
        IN_PROGRESS = "in_progress", "处理中"
        RESOLVED = "resolved", "已解决"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="feedback_items")
    subject = models.CharField(max_length=160)
    message = models.TextField(max_length=5000)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    response = models.TextField(max_length=5000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "status"])]

    def __str__(self):
        return self.subject


class FeatureFlag(models.Model):
    name = models.CharField(max_length=64, unique=True, validators=[RegexValidator(r"^[a-z][a-z0-9_]*$")])
    description = models.CharField(max_length=240, blank=True)
    enabled = models.BooleanField(default=False)
    rollout_percent = models.PositiveSmallIntegerField(default=0, validators=[MaxValueValidator(100)])
    staff_only = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(rollout_percent__lte=100), name="flag_percent_range")]

    def allows(self, user):
        if not self.enabled or not user.is_authenticated or not user.is_active:
            return False
        if self.staff_only and not user.is_staff:
            return False
        bucket = int.from_bytes(hashlib.sha256(f"{self.name}:{user.pk}".encode()).digest()[:8], "big") % 100
        return bucket < self.rollout_percent

    def __str__(self):
        return self.name


class PlatformSafetyState(models.Model):
    """Research task intake latch; does NOT represent broker order cancellation."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    halted = models.BooleanField(default=False)
    reason = models.CharField(max_length=500, blank=True)
    generation = models.PositiveBigIntegerField(default=1)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(id=1), name="safety_singleton")]

    def __str__(self):
        return "Research intake paused" if self.halted else "Research intake enabled"


class StrategyAutomation(models.Model):
    """Explicitly authorized periodic historical simulation, never live trading."""

    class State(models.TextChoices):
        STOPPED = "stopped", "已停止"
        RUNNING = "running", "自动运行中"
        PAUSED = "paused", "已暂停"
        FAULTED = "faulted", "故障锁存"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="strategy_automations")
    name = models.CharField(max_length=120)
    dataset = models.ForeignKey(Dataset, on_delete=models.PROTECT, related_name="automations")
    risk_snapshot = models.JSONField(default=dict)
    simulation_parameters = models.JSONField(default=dict)
    interval_seconds = models.PositiveIntegerField(default=300, validators=[MinValueValidator(60), MaxValueValidator(86400)])
    max_runs = models.PositiveIntegerField(default=10, validators=[MinValueValidator(1), MaxValueValidator(1000)])
    state = models.CharField(max_length=12, choices=State.choices, default=State.STOPPED)
    generation = models.PositiveBigIntegerField(default=1)
    runs_created = models.PositiveIntegerField(default=0)
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    risk_latched = models.BooleanField(default=False)
    next_run_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_job = models.ForeignKey(ReplayJob, on_delete=models.SET_NULL, null=True, blank=True, related_name="latest_for_automations")
    last_accounted_job = models.ForeignKey(ReplayJob, on_delete=models.SET_NULL, null=True, blank=True, related_name="accounted_by_automations")
    last_reason_code = models.CharField(max_length=64, blank=True)
    last_reason = models.CharField(max_length=500, blank=True)
    creation_key = models.CharField(max_length=128, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "state"]), models.Index(fields=["state", "last_checked_at"])]
        constraints = [
            models.CheckConstraint(condition=Q(interval_seconds__gte=60, interval_seconds__lte=86400), name="automation_interval_range"),
            models.CheckConstraint(condition=Q(max_runs__gte=1, max_runs__lte=1000), name="automation_runs_range"),
            models.CheckConstraint(condition=Q(state__in=["stopped", "running", "paused", "faulted"]), name="automation_state_valid"),
            models.UniqueConstraint(fields=["owner", "creation_key"], name="automation_creation_key"),
        ]

    def __str__(self):
        return self.name


class AutomationCommand(models.Model):
    """Durable idempotency receipt; stores no password or authentication token."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    automation = models.ForeignKey(StrategyAutomation, on_delete=models.PROTECT, related_name="commands")
    key = models.CharField(max_length=128)
    action = models.CharField(max_length=8, choices=[("start", "Start"), ("stop", "Stop")])
    acknowledged_latch = models.BooleanField(default=False)
    outcome = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "key"], name="automation_command_key")]
        ordering = ["-created_at"]


class ResearchAssessment(models.Model):
    """Immutable research evidence; privacy erasure is the sole redaction path."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="research_assessments")
    policy_version = models.CharField(max_length=64)
    snapshot = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    snapshot_sha256 = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    request_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "created_at"])]
        constraints = [models.UniqueConstraint(fields=["owner", "idempotency_key"], name="assessment_owner_idempotency")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("研究评估不可变；请创建新的评估。")
        return super().save(*args, **kwargs)
