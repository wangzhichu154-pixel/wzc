import logging
import ipaddress
import time
import uuid

from django.conf import settings
from django.http import JsonResponse

from .logging import request_id as request_context
from .metrics import LATENCY, REQUESTS

logger = logging.getLogger("vector.http")


class TrustedProxyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.networks = [ipaddress.ip_network(cidr) for cidr in settings.TRUSTED_PROXY_CIDRS]

    def __call__(self, request):
        raw_peer = request.META.get("REMOTE_ADDR", "")
        request.client_ip = raw_peer
        try:
            peer = ipaddress.ip_address(raw_peer)
            trusted = settings.TRUST_PROXY and any(peer in network for network in self.networks)
        except ValueError:
            trusted = False
        if trusted:
            try:
                request.client_ip = str(ipaddress.ip_address(request.META.get("HTTP_X_REAL_IP", raw_peer)))
            except ValueError:
                request.client_ip = raw_peer
        else:
            # SecurityMiddleware may trust the header only after this check.
            request.META.pop("HTTP_X_FORWARDED_PROTO", None)
        return self.get_response(request)


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Generate locally; do not trust an attacker-supplied identifier.
        request.request_id = uuid.uuid4().hex
        token = request_context.set(request.request_id)
        start = time.monotonic()
        try:
            response = self.get_response(request)
            elapsed = time.monotonic() - start
            match = getattr(request, "resolver_match", None)
            route = (match.route or "root") if match else "unmatched"
            # Route patterns have bounded cardinality and no email/reset tokens.
            REQUESTS.labels(request.method, route, str(response.status_code)).inc()
            LATENCY.labels(request.method, route).observe(elapsed)
            response["X-Request-ID"] = request.request_id
            if not request.path.startswith(("/health/", "/metrics/", "/static/")):
                logger.info("http_request", extra={"route": route, "method": request.method,
                    "status": response.status_code, "duration_ms": round(elapsed * 1000, 3),
                    "user_id": getattr(getattr(request, "user", None), "pk", None)})
            return response
        finally:
            request_context.reset(token)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        if not request.path.startswith("/static/"):
            response["Cache-Control"] = "no-store"
        return response


class RateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.RATE_LIMIT_ENABLED and request.path.startswith("/api/"):
            from accounts.ratelimit import allow_request
            # REMOTE_ADDR is overwritten by trusted ingress only if configured;
            # never take an arbitrary client X-Forwarded-For value.
            identity = f"user:{request.user.pk}" if request.user.is_authenticated else f"ip:{request.client_ip}"
            limit = 120 if request.user.is_authenticated else 30
            if not allow_request("api", identity, limit=limit, seconds=60):
                response = JsonResponse({"success": False, "code": "RATE_LIMITED", "message": "请求过于频繁，请稍后重试。", "data": None}, status=429)
                response["Retry-After"] = "60"
                return response
        return self.get_response(request)
