from django.conf import settings


def product_context(request):
    names = ("LEGAL_ENTITY_NAME", "SUPPORT_EMAIL", "DATA_RETENTION_DAYS", "AUDIT_RETENTION_DAYS", "BACKUP_RETENTION_DAYS")
    return {name.lower(): getattr(settings, name) for name in names}
