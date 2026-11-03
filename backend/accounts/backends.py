from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model


class EmailBackend(ModelBackend):
    def get_group_permissions(self, user_obj, obj=None):
        permissions = super().get_group_permissions(user_obj, obj)
        if obj is None and user_obj.is_authenticated and user_obj.is_operator:
            permissions = permissions | {
                "accounts.view_user", "accounts.change_user",
                "accounts.view_auditevent", "accounts.view_loginrecord",
                "accounts.view_devicesession", "accounts.view_emailjob",
            }
        return permissions

    def authenticate(self, request, username=None, password=None, email=None, **kwargs):
        value = (email or username or "").strip().lower()
        if not password or not value:
            return None
        User = get_user_model()
        try:
            user = User.objects.get(email=value)
        except User.DoesNotExist:
            User().set_password(password)  # Comparable password hashing work; no account lookup leak.
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
