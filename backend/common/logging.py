"""Keep tokens, query strings, and request bodies out of structured logs."""
import logging
import re
from contextvars import ContextVar
from pythonjsonlogger.json import JsonFormatter

request_id = ContextVar("request_id", default="-")
_token_path = re.compile(r"(/accounts/(?:verify|password/reset|password-reset/confirm|reset)/)[^/\s?]+")
_secret_query = re.compile(r"([?&](?:token|password|secret|key|code)=)[^&\s]+", re.IGNORECASE)
_credential_url = re.compile(r"([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)


def redact(value):
    value = _token_path.sub(r"\1[redacted]", value)
    value = _secret_query.sub(r"\1[redacted]", value)
    return _credential_url.sub(r"\1[redacted]@", value)


class SafeJsonFormatter(JsonFormatter):
    def format(self, record):
        # Apply after formatting so exception text is covered too.
        return redact(super().format(record))


class SafeLogFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(record, "request_id", request_id.get())
        # Never serialize Django's HttpRequest object (headers may contain secrets).
        if hasattr(record, "request"):
            record.request = "[redacted]"
        text = record.getMessage()
        record.msg = redact(text)
        record.args = ()
        return True
