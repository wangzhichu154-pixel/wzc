import uuid
from django import forms
from .models import ReplayJob


class AssessmentForm(forms.Form):
    job_ids = forms.MultipleChoiceField(label="已完成的模拟任务", widget=forms.SelectMultiple(attrs={"size": 8}),
        help_text="选择 1–50 个不同 SHA256 文件对应的任务。相同文件的重复运行不能累计；不同文件也未必来自独立交易日。")
    extra_cost_per_share_units = forms.IntegerField(label="每成交股额外压力成本（0.0001 USD）", initial=10, min_value=0, max_value=10000,
        help_text="默认 10 = 每成交股额外 0.001 USD。压力情景还将移除全部假设返佣；已有费用不会重复扣除。")
    idempotency_key = forms.CharField(widget=forms.HiddenInput, initial=uuid.uuid4, max_length=128, required=False)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        candidates = ReplayJob.objects.filter(owner=user, mode="simulation", status="succeeded", dataset__deleted_at__isnull=True).select_related("dataset")[:200]
        self.fields["job_ids"].choices = [(str(job.pk), f"{job.dataset.name} · {job.created_at:%Y-%m-%d %H:%M} · {str(job.pk)[:8]}") for job in candidates]

    def clean_job_ids(self):
        values = self.cleaned_data["job_ids"]
        if not 1 <= len(values) <= 50:
            raise forms.ValidationError("请选择 1–50 个模拟任务。")
        return values
