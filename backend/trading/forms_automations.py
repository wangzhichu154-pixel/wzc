import uuid

from django import forms

from .forms import ReplayRequestForm
from .models import Dataset


class AutomationCreateForm(ReplayRequestForm):
    name = forms.CharField(label="自动运行名称", max_length=120)
    dataset = forms.ModelChoiceField(label="私有行情数据集", queryset=Dataset.objects.none())
    interval_seconds = forms.IntegerField(label="运行间隔（秒）", min_value=60, max_value=86400, initial=300)
    max_runs = forms.IntegerField(label="最多运行次数", min_value=1, max_value=1000, initial=10)
    key = forms.CharField(widget=forms.HiddenInput, initial=uuid.uuid4, max_length=128)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("mode")
        self.fields["dataset"].queryset = Dataset.objects.filter(owner=user, deleted_at__isnull=True)
        self.order_fields(["name", "dataset", "interval_seconds", "max_runs", "latency_ns", "cancel_latency_ns", "fee_per_share_units", "min_fee_per_order_units", "slippage_buffer_units", "min_net_edge_units", "rebate_per_share_units", "simulation_window_ns", "key"])

    def clean(self):
        cleaned = super().clean()
        cleaned["mode"] = "simulation"
        return cleaned


class AutomationCommandForm(forms.Form):
    password = forms.CharField(label="当前密码", max_length=1024, widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))
    acknowledge_latch = forms.BooleanField(label="我已检查上次风险或故障原因，明确允许恢复自动模拟。", required=False)
    key = forms.CharField(widget=forms.HiddenInput, initial=uuid.uuid4, max_length=128)

    def __init__(self, *args, action, **kwargs):
        super().__init__(*args, prefix=action, **kwargs)
        if action == "stop":
            self.fields.pop("acknowledge_latch")
