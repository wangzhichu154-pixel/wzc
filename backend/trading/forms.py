from django import forms

from .models import Feedback, ReplayJob, RiskProfile
from .services import SIMULATION_DEFAULTS


class ReplayRequestForm(forms.Form):
    mode = forms.ChoiceField(label="运行模式", choices=ReplayJob.Mode.choices, initial=ReplayJob.Mode.REPLAY)
    latency_ns = forms.IntegerField(label="模拟下单延迟（ns）", required=False, initial=1000000, min_value=1000, max_value=10000000000)
    cancel_latency_ns = forms.IntegerField(label="模拟撤单延迟（ns）", required=False, initial=1000000, min_value=1000, max_value=10000000000)
    fee_per_share_units = forms.IntegerField(label="每股费用（0.0001 USD）", required=False, initial=30, min_value=0, max_value=1000000000)
    rebate_per_share_units = forms.IntegerField(label="每股假设返佣（0.0001 USD）", required=False, initial=0, min_value=0, max_value=1000000)
    min_fee_per_order_units = forms.IntegerField(label="每单最低佣金（0.0001 USD）", required=False, initial=0, min_value=0, max_value=1000000000, help_text="按每个已成交订单累计收费；报价按最小 1 股部分成交保守预留。")
    slippage_buffer_units = forms.IntegerField(label="每股报价滑点缓冲（0.0001 USD）", required=False, initial=0, min_value=0, max_value=1000000000, help_text="扩大报价成本门槛，不是已实现的额外费用扣款。")
    min_net_edge_units = forms.IntegerField(label="每股最小净边际（0.0001 USD）", required=False, initial=0, min_value=0, max_value=1000000000, help_text="研究报价门槛；不代表可保证获得的收益。")
    simulation_window_ns = forms.IntegerField(label="单次模拟报价生命周期（ns）", required=False, initial=1000000000, min_value=1000000, max_value=60000000000)

    def simulation_parameters(self):
        if self.cleaned_data["mode"] != ReplayJob.Mode.SIMULATION:
            return {}
        return {name: self.cleaned_data[name] if self.cleaned_data.get(name) is not None else default
                for name, default in SIMULATION_DEFAULTS.items()}


class DatasetUploadForm(forms.Form):
    name = forms.CharField(label="数据集名称", max_length=120)
    file = forms.FileField(label="ITCH 逐笔文件", help_text="上传采用 2 字节大端长度前缀的 NASDAQ ITCH 文件（.itch）。不接收 PCAP、压缩包或外部 URL。")


class RiskProfileForm(forms.ModelForm):
    class Meta:
        model = RiskProfile
        fields = ["max_order_size", "tick_size", "soft_inventory_limit", "hard_inventory_limit", "daily_loss_limit", "orders_per_second"]
        labels = {"max_order_size": "最大单笔数量", "tick_size": "报价最小价位单位（1/10000 USD）", "soft_inventory_limit": "库存软限额", "hard_inventory_limit": "库存硬限额", "daily_loss_limit": "日内最大亏损（USD）", "orders_per_second": "每秒订单上限"}
        help_texts = {"tick_size": "默认 100 = 0.01 USD，仅为研究假设。请依据标的、价格区间和交易场所规则配置，不代表所有美股均使用一美分 tick。范围 1–100000。"}


class FeedbackForm(forms.ModelForm):
    class Meta:
        model = Feedback
        fields = ["subject", "message"]
        labels = {"subject": "主题", "message": "问题描述"}
        widgets = {"message": forms.Textarea(attrs={"rows": 5})}


class SafetyForm(forms.Form):
    action = forms.ChoiceField(choices=[("pause", "暂停新研究任务"), ("resume", "恢复新研究任务")])
    reason = forms.CharField(label="操作原因", min_length=5, max_length=500, widget=forms.Textarea(attrs={"rows": 3}))
    password = forms.CharField(label="重新输入当前密码", widget=forms.PasswordInput)
