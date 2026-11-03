from django import forms
from django.contrib.auth import authenticate, password_validation
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AdminPasswordChangeForm, PasswordChangeForm, SetPasswordForm
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator

from .models import User


class RegisterForm(forms.Form):
    email = forms.EmailField(label="邮箱", widget=forms.EmailInput(attrs={"autocomplete": "email"}))
    password1 = forms.CharField(label="密码", max_length=4096, strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password2 = forms.CharField(label="确认密码", max_length=4096, strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    accept_terms = forms.BooleanField(label="我已阅读并同意用户协议与隐私政策")

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def clean(self):
        values = super().clean()
        first, second = values.get("password1"), values.get("password2")
        if first and second and first != second:
            self.add_error("password2", "两次输入的密码不一致。")
        if first:
            password_validation.validate_password(first, User(email=values.get("email", "")))
        return values


class LoginForm(forms.Form):
    email = forms.EmailField(label="邮箱", widget=forms.EmailInput(attrs={"autocomplete": "username"}))
    password = forms.CharField(label="密码", max_length=4096, strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))

    def __init__(self, request, *args, **kwargs):
        self.request = request
        self.user = None
        super().__init__(*args, **kwargs)

    def clean(self):
        values = super().clean()
        if values.get("email") and values.get("password"):
            self.user = authenticate(self.request, email=values["email"], password=values["password"])
            if self.user is None:
                raise ValidationError("邮箱或密码无效，或账户不可用。", code="invalid_login")
        return values


class EmailForm(forms.Form):
    email = forms.EmailField(label="邮箱", widget=forms.EmailInput(attrs={"autocomplete": "email"}))


class ReauthenticateForm(forms.Form):
    password = forms.CharField(label="当前密码", max_length=4096, strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_password(self):
        value = self.cleaned_data["password"]
        if not self.user.check_password(value):
            raise ValidationError("当前密码不正确。")
        return value


class PasswordLengthMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.PasswordInput):
                field.validators.append(MaxLengthValidator(4096))
                field.widget.attrs["maxlength"] = 4096


class BoundedSetPasswordForm(PasswordLengthMixin, SetPasswordForm):
    pass


class BoundedPasswordChangeForm(PasswordLengthMixin, PasswordChangeForm):
    pass


class BoundedAdminPasswordChangeForm(PasswordLengthMixin, AdminPasswordChangeForm):
    pass


class OperatorAuthenticationForm(PasswordLengthMixin, AdminAuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_operator:
            raise ValidationError("该账户未获管理后台访问权限。", code="invalid_login")
