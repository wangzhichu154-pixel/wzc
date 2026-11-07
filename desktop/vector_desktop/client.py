"""Bounded, session-authenticated client for the local Vector service.

Credentials and cookies live in memory only. There are no automatic mutation
retries: a timeout can mean the server accepted a request. Callers can retain
the idempotency key from ApiError and reconcile before explicitly retrying.
"""
from __future__ import annotations

import http.client
import http.cookiejar
import io
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import threading
from typing import Any, BinaryIO
from urllib import error, parse, request
import uuid


DEFAULT_BASE_URL = "http://127.0.0.1:8080"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_BYTES = 256 * 1024 * 1024


class ApiError(Exception):
    """Safe user-facing error with structured server and correlation fields."""

    def __init__(self, message: str, *, code: str = "CLIENT_ERROR", status: int = 0,
                 data: Any = None, request_id: str = "", idempotency_key: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.data = data
        self.request_id = request_id
        self.idempotency_key = idempotency_key


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even same-origin redirects could turn a POST into a GET. All actual
        # API paths have trailing slashes, so no redirect is needed here.
        fp.close()
        raise ApiError("服务返回了不允许的重定向。", code="REDIRECT_BLOCKED", status=code)


class _MultipartBody:
    """Stream a bounded file between two short multipart frames."""

    def __init__(self, head: bytes, file: BinaryIO, size: int, tail: bytes):
        self._head = io.BytesIO(head)
        self._file = file
        self._remaining = size
        self._tail = io.BytesIO(tail)
        self.length = len(head) + size + len(tail)

    def read(self, size: int = 64 * 1024) -> bytes:
        if size < 0:
            size = 64 * 1024
        value = self._head.read(size)
        if len(value) < size and self._remaining:
            chunk = self._file.read(min(size - len(value), self._remaining))
            if not chunk:
                raise ApiError("上传文件在读取过程中发生变化。", code="FILE_CHANGED")
            self._remaining -= len(chunk)
            value += chunk
        if len(value) < size and not self._remaining:
            value += self._tail.read(size - len(value))
        return value


def _base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ApiError("本地服务地址格式无效。", code="INVALID_BASE_URL")
    try:
        parsed = parse.urlsplit(value)
        port = parsed.port
    except (ValueError, TypeError):
        raise ApiError("本地服务地址格式无效。", code="INVALID_BASE_URL") from None
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
            or any(c.isspace() for c in value) or "\\" in value):
        raise ApiError("桌面客户端仅连接本机 http://127.0.0.1 或 http://localhost 服务。",
                       code="INVALID_BASE_URL")
    return f"http://{parsed.hostname}" + (f":{port}" if port is not None else "")


def _identifier(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ApiError("资源编号必须是有效 UUID。", code="INVALID_IDENTIFIER") from None


def _page(value: int) -> str:
    if type(value) is not int or value < 1:
        raise ApiError("页码必须为正整数。", code="INVALID_PAGE")
    return str(value)


def _upload_filename(name: str) -> str:
    """Keep multipart headers ASCII and single-line on every OS."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:180] or "capture.itch"


def _key(value: str | None) -> str:
    if value is None:
        return str(uuid.uuid4())
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        raise ApiError("请求幂等编号格式无效。", code="INVALID_IDEMPOTENCY_KEY")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-finite JSON value")


class VectorClient:
    """Native UI transport; safe to call from a serialized background worker."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, timeout: float = 15,
                 max_response_bytes: int = MAX_RESPONSE_BYTES):
        self.base_url = _base_url(base_url)
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("timeout must be finite and between 0 and 300 seconds")
        if type(max_response_bytes) is not int or not 1024 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("invalid response size limit")
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._cookies = http.cookiejar.CookieJar()
        # Loopback authentication must never pass through an environment proxy.
        self._opener = request.build_opener(request.ProxyHandler({}),
                                           request.HTTPCookieProcessor(self._cookies), _NoRedirect())
        self._lock = threading.RLock()
        self._csrf_token = ""
        self.last_request_id = ""

    def close(self) -> None:
        with self._lock:
            self._cookies.clear()
            self._csrf_token = ""

    def _cookie_csrf(self) -> str:
        return next((cookie.value for cookie in self._cookies
                     if cookie.name == "csrftoken" and not cookie.is_expired()), "")

    def _csrf(self) -> str:
        token = self._cookie_csrf() or self._csrf_token
        if not token:
            data = self._request("GET", "/api/v1/auth/csrf/")
            token = data.get("csrf_token", "") if isinstance(data, dict) else ""
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9]{32,128}", token):
                raise ApiError("服务未返回有效的会话校验信息。", code="INVALID_CSRF_RESPONSE")
            self._csrf_token = token
        return token

    def _request(self, method: str, path: str, *, payload: Any = None,
                 body: _MultipartBody | None = None, content_type: str | None = None,
                 idempotency_key: str | None = None, timeout: float | None = None) -> Any:
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise ApiError("接口路径无效。", code="INVALID_PATH")
        with self._lock:
            headers = {"Accept": "application/json", "User-Agent": "VectorDesktop/1.0",
                       "Accept-Encoding": "identity"}
            data = body
            if payload is not None:
                try:
                    data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
                except (ValueError, TypeError):
                    raise ApiError("请求参数无法编码。", code="INVALID_PAYLOAD") from None
                content_type = "application/json"
            if method not in {"GET", "HEAD"}:
                headers.update({"X-CSRFToken": self._csrf(), "Origin": self.base_url,
                                "Referer": self.base_url + "/"})
            if content_type:
                headers["Content-Type"] = content_type
            if body is not None:
                headers["Content-Length"] = str(body.length)
            if idempotency_key is not None:
                headers["Idempotency-Key"] = idempotency_key
            req = request.Request(self.base_url + path, data=data, headers=headers, method=method)
            try:
                try:
                    response = self._opener.open(req, timeout=timeout or self.timeout)
                except error.HTTPError as exc:
                    response = exc
                with response:
                    status = response.status
                    self.last_request_id = response.headers.get("X-Request-ID", "")[:128]
                    declared = response.headers.get("Content-Length")
                    if declared and declared.isdigit() and int(declared) > self.max_response_bytes:
                        raise ApiError("服务响应过大，已停止读取。", code="RESPONSE_TOO_LARGE", status=status)
                    raw = response.read(self.max_response_bytes + 1)
                    if len(raw) > self.max_response_bytes:
                        raise ApiError("服务响应过大，已停止读取。", code="RESPONSE_TOO_LARGE", status=status)
                    if response.headers.get_content_type() != "application/json":
                        raise ApiError("服务未返回有效的 JSON 响应。请检查本地服务状态。",
                                       code="INVALID_RESPONSE", status=status)
                try:
                    envelope = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
                except (ValueError, UnicodeError):
                    raise ApiError("服务响应无法解析。", code="INVALID_RESPONSE", status=status) from None
                if (not isinstance(envelope, dict) or type(envelope.get("success")) is not bool
                        or not isinstance(envelope.get("code"), str) or "data" not in envelope):
                    raise ApiError("服务响应结构无效。", code="INVALID_RESPONSE", status=status)
                if status >= 400 or not envelope["success"]:
                    message = envelope.get("message")
                    raise ApiError(message if isinstance(message, str) and message else "请求未完成。",
                                   code=envelope["code"], status=status, data=envelope["data"],
                                   request_id=self.last_request_id, idempotency_key=idempotency_key)
                return envelope["data"]
            except ApiError as exc:
                if not exc.idempotency_key:
                    exc.idempotency_key = idempotency_key
                raise
            except (error.URLError, socket.timeout, TimeoutError, ConnectionError,
                    http.client.HTTPException, OSError):
                raise ApiError("无法连接本地服务或请求超时。请检查服务状态；提交操作请先核对结果，再重试。",
                               code="CONNECTION_FAILED", idempotency_key=idempotency_key) from None

    def health(self) -> dict:
        return self._request("GET", "/health/ready/")

    def login(self, email: str, password: str) -> dict:
        with self._lock:
            result = self._request("POST", "/api/v1/auth/login/", payload={"email": email, "password": password})
            # Django rotates the CSRF cookie on successful authentication.
            self._csrf_token = self._cookie_csrf()
            return result

    def logout(self) -> Any:
        try:
            return self._request("POST", "/api/v1/auth/logout/", payload={})
        finally:
            self.close()

    def me(self) -> dict:
        return self._request("GET", "/api/v1/auth/me/")

    def session(self) -> dict:
        return self._request("GET", "/api/v1/session/")

    def dashboard(self) -> dict:
        return self._request("GET", "/api/v1/dashboard/")

    def risk(self) -> dict:
        return self._request("GET", "/api/v1/risk/")

    get_risk = risk

    def update_risk(self, values: dict) -> dict:
        return self._request("PATCH", "/api/v1/risk/", payload=values)

    def datasets(self, page: int = 1) -> dict:
        return self._request("GET", "/api/v1/datasets/?page=" + _page(page))

    list_datasets = datasets

    def upload_dataset(self, path: str | Path, name: str | None = None) -> dict:
        path = Path(path)
        label = name if name is not None else path.stem
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 120:
            raise ApiError("数据集名称须为 1 至 120 个字符。", code="INVALID_DATASET_NAME")
        try:
            # Check before opening, so special files (for example FIFOs) cannot
            # block the worker before the bounded network timeout starts.
            if not stat.S_ISREG(path.stat().st_mode):
                raise ApiError("请选择普通行情文件。", code="INVALID_FILE")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                                 | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
            with os.fdopen(descriptor, "rb") as file:
                metadata = os.fstat(file.fileno())
                if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_UPLOAD_BYTES:
                    raise ApiError("文件必须非空且不超过 256 MiB。", code="INVALID_FILE_SIZE")
                boundary = "VectorDesktop" + uuid.uuid4().hex
                filename = _upload_filename(path.name)
                head = (f'--{boundary}\r\nContent-Disposition: form-data; name="name"\r\n\r\n'
                        f'{label}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                        f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode("utf-8")
                body = _MultipartBody(head, file, metadata.st_size, f"\r\n--{boundary}--\r\n".encode())
                return self._request("POST", "/api/v1/datasets/", body=body,
                                     content_type=f"multipart/form-data; boundary={boundary}", timeout=120)
        except OSError:
            raise ApiError("行情文件无法读取，请检查路径和访问权限。", code="FILE_UNREADABLE") from None

    def delete_dataset(self, dataset_id: str) -> Any:
        return self._request("DELETE", f"/api/v1/datasets/{_identifier(dataset_id)}/")

    def jobs(self, page: int = 1) -> dict:
        return self._request("GET", "/api/v1/jobs/?page=" + _page(page))

    list_runs = jobs
    list_jobs = jobs

    def create_job(self, dataset_id: str, mode: str = "replay", simulation_parameters: dict | None = None,
                   idempotency_key: str | None = None) -> dict:
        if mode not in {"replay", "simulation"}:
            raise ApiError("客户端仅支持研究回放和历史仿真。", code="INVALID_MODE")
        body = {"dataset_id": _identifier(dataset_id), "mode": mode}
        if simulation_parameters is not None:
            body["simulation_parameters"] = simulation_parameters
        return self._request("POST", "/api/v1/jobs/", payload=body, idempotency_key=_key(idempotency_key))

    create_run = create_job

    def job(self, job_id: str) -> dict:
        return self._request("GET", f"/api/v1/jobs/{_identifier(job_id)}/")

    run_detail = job

    def cancel_job(self, job_id: str) -> dict:
        return self._request("POST", f"/api/v1/jobs/{_identifier(job_id)}/cancel/", payload={})

    cancel_run = cancel_job

    def retry_job(self, job_id: str, idempotency_key: str | None = None) -> dict:
        return self._request("POST", f"/api/v1/jobs/{_identifier(job_id)}/retry/", payload={},
                             idempotency_key=_key(idempotency_key))

    def automations(self, page: int = 1) -> dict:
        return self._request("GET", "/api/v1/automations/?page=" + _page(page))

    list_automations = automations

    def automation(self, automation_id: str) -> dict:
        return self._request("GET", f"/api/v1/automations/{_identifier(automation_id)}/")

    def create_automation(self, name: str, dataset_id: str, interval_seconds: int = 300, max_runs: int = 10,
                          simulation_parameters: dict | None = None, idempotency_key: str | None = None) -> dict:
        payload = {"name": name, "dataset_id": _identifier(dataset_id), "mode": "simulation",
                   "interval_seconds": interval_seconds, "max_runs": max_runs}
        if simulation_parameters is not None:
            payload["simulation_parameters"] = simulation_parameters
        return self._request("POST", "/api/v1/automations/", payload=payload, idempotency_key=_key(idempotency_key))

    def automation_command(self, automation_id: str, action: str, password: str,
                           acknowledge_latch: bool = False, idempotency_key: str | None = None) -> dict:
        if action not in {"start", "stop"}:
            raise ApiError("自动仿真操作无效。", code="INVALID_ACTION")
        return self._request("POST", f"/api/v1/automations/{_identifier(automation_id)}/{action}/",
                             payload={"password": password, "acknowledge_latch": acknowledge_latch},
                             idempotency_key=_key(idempotency_key))

    def assessments(self, page: int = 1) -> dict:
        return self._request("GET", "/api/v1/assessments/?page=" + _page(page))

    list_assessments = assessments

    def assessment(self, assessment_id: str) -> dict:
        return self._request("GET", f"/api/v1/assessments/{_identifier(assessment_id)}/")

    def create_assessment(self, job_ids: list[str], extra_cost_per_share_units: int = 10,
                          idempotency_key: str | None = None) -> dict:
        return self._request("POST", "/api/v1/assessments/",
                             payload={"job_ids": [_identifier(x) for x in job_ids],
                                      "extra_cost_per_share_units": extra_cost_per_share_units},
                             idempotency_key=_key(idempotency_key))

    def ai_status(self) -> dict:
        return self._request("GET", "/api/v1/ai/status/")

    def execution_status(self) -> dict:
        return self._request("GET", "/api/v1/execution/status/")

    def devices(self) -> dict:
        return self._request("GET", "/api/v1/auth/devices/")

    def revoke_device(self, device_id: str) -> Any:
        return self._request("POST", f"/api/v1/auth/devices/{_identifier(device_id)}/revoke/", payload={})
