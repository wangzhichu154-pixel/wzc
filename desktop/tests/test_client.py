"""Transport-contract tests without network sockets, Qt, or a Django dependency."""
from __future__ import annotations

from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib import error, request, response
from unittest.mock import patch
import uuid

from vector_desktop.client import ApiError, VectorClient, _NoRedirect


def packet(data=None, *, status=200, success=True, code="OK", message="", headers=None, raw=None):
    value = json.dumps({"success": success, "code": code, "message": message, "data": data}).encode()
    return status, raw if raw is not None else value, headers or {}


class Transport(request.HTTPHandler):
    """Exercise urllib's real cookie/error/redirect processors, replacing only IO."""

    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.calls = []

    def http_open(self, req):
        data = req.data
        if hasattr(data, "read"):
            pieces = []
            while chunk := data.read(8192):
                pieces.append(chunk)
            data = b"".join(pieces)
        self.calls.append({"url": req.full_url, "method": req.get_method(),
                           "headers": {k.lower(): v for k, v in req.header_items()}, "body": data})
        current = self.responses.pop(0)
        if isinstance(current, Exception):
            raise current
        status, body, extra_headers = current
        headers = Message()
        headers["Content-Type"] = "application/json"
        for key, value in extra_headers.items():
            if key.lower() == "content-type":
                headers.replace_header("Content-Type", value)
            else:
                headers[key] = value
        result = response.addinfourl(io.BytesIO(body), headers, req.full_url, status)
        result.msg = "test response"
        return result


def client_with(*responses, **kwargs):
    client = VectorClient(**kwargs)
    transport = Transport(responses)
    client._opener = request.build_opener(request.ProxyHandler({}),
                                         request.HTTPCookieProcessor(client._cookies),
                                         _NoRedirect(), transport)
    return client, transport


CSRF_A = "A" * 32
CSRF_B = "B" * 32


def csrf_packet():
    return packet({"csrf_token": CSRF_A}, headers={"Set-Cookie": f"csrftoken={CSRF_A}; Path=/; HttpOnly"})


class ClientTests(unittest.TestCase):
    def test_only_loopback_http_origins(self):
        for value in ["https://example.com", "http://example.com", "http://127.0.0.1.evil.test",
                      "http://user:secret@localhost", "http://localhost/?token=x", "http://localhost/#x",
                      "http://localhost/other", "http://localhost:0", "http://localhost:99999",
                      "http://localhost\\@example.com", " http://localhost", "http://localhost\n", "//localhost"]:
            with self.subTest(url=value), self.assertRaises(ApiError) as caught:
                VectorClient(value)
            self.assertEqual(caught.exception.code, "INVALID_BASE_URL")
        self.assertEqual(VectorClient("http://localhost:8080/").base_url, "http://localhost:8080")

    def test_invalid_timeouts_and_bounds(self):
        for timeout in [0, -1, float("inf"), float("nan"), 301]:
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                VectorClient(timeout=timeout)
        with self.assertRaises(ValueError):
            VectorClient(max_response_bytes=0)

    def test_proxy_disabled_even_with_environment_proxy(self):
        seen = []

        class RecordingProxy(request.ProxyHandler):
            def __init__(self, proxies=None):
                seen.append(proxies)
                super().__init__(proxies)

        with patch.dict("os.environ", {"http_proxy": "http://proxy.invalid:8080"}):
            with patch("vector_desktop.client.request.ProxyHandler", RecordingProxy):
                VectorClient()
        self.assertEqual(seen, [{}])

    def test_login_rotates_csrf_and_passes_real_session_cookie(self):
        job_id = str(uuid.uuid4())
        client, transport = client_with(csrf_packet(),
            packet({"email": "operator@example.test"}, headers={"Set-Cookie": f"csrftoken={CSRF_B}; Path=/; HttpOnly"}),
            packet({"id": job_id}, status=202))
        client.login("operator@example.test", "test-password-only")
        result = client.create_run(job_id, mode="simulation", idempotency_key="stable-command")
        self.assertEqual(result["id"], job_id)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(transport.calls[1]["headers"]["x-csrftoken"], CSRF_A)
        self.assertEqual(transport.calls[2]["headers"]["x-csrftoken"], CSRF_B)
        self.assertIn("csrftoken=" + CSRF_B, transport.calls[2]["headers"]["cookie"])
        self.assertEqual(transport.calls[2]["headers"]["origin"], "http://127.0.0.1:8080")
        self.assertEqual(transport.calls[2]["headers"]["idempotency-key"], "stable-command")
        self.assertNotIn("password", vars(client))

    def test_reject_redirect_before_second_request(self):
        for destination in ["https://evil.test/collect", "http://127.0.0.1:8080/other"]:
            with self.subTest(destination=destination):
                client, transport = client_with(packet(status=302, headers={"Location": destination}))
                with self.assertRaises(ApiError) as caught:
                    client.health()
                self.assertEqual(caught.exception.code, "REDIRECT_BLOCKED")
                self.assertEqual(len(transport.calls), 1)

    def test_error_keeps_saved_job_and_request_id_without_retry(self):
        saved = {"id": str(uuid.uuid4()), "status": "queued"}
        client, transport = client_with(csrf_packet(), packet(saved, status=503, success=False,
            code="QUEUE_UNAVAILABLE", message="任务已保存。", headers={"X-Request-ID": "trace-123"}))
        with self.assertRaises(ApiError) as caught:
            client.create_run(str(uuid.uuid4()), idempotency_key="attempt-1")
        self.assertEqual(caught.exception.data, saved)
        self.assertEqual(caught.exception.request_id, "trace-123")
        self.assertEqual(caught.exception.idempotency_key, "attempt-1")
        self.assertEqual(len(transport.calls), 2)

    def test_network_failure_returns_reconciliation_key_and_does_not_retry(self):
        client, transport = client_with(csrf_packet(), error.URLError("unavailable"))
        with self.assertRaises(ApiError) as caught:
            client.create_run(str(uuid.uuid4()), idempotency_key="keep-me")
        self.assertEqual(caught.exception.code, "CONNECTION_FAILED")
        self.assertEqual(caught.exception.idempotency_key, "keep-me")
        self.assertEqual(len(transport.calls), 2)

    def test_response_size_enforced_with_and_without_content_length(self):
        for headers in [{}, {"Content-Length": "9999999"}]:
            client, _ = client_with(packet(raw=b"x" * 1025, headers=headers), max_response_bytes=1024)
            with self.assertRaises(ApiError) as caught:
                client.health()
            self.assertEqual(caught.exception.code, "RESPONSE_TOO_LARGE")

    def test_invalid_or_html_response_never_masquerades_as_success(self):
        cases = [packet(raw=b"not json"), packet(raw=b'{}'),
                 packet(raw=b'{"success":"true","code":"OK","data":{}}'),
                 packet(raw=b'{"success":true,"code":"OK","data":NaN}'),
                 packet(raw=b'<html>login</html>', headers={"Content-Type": "text/html"})]
        for value in cases:
            with self.subTest(value=value):
                client, _ = client_with(value)
                with self.assertRaises(ApiError) as caught:
                    client.health()
                self.assertEqual(caught.exception.code, "INVALID_RESPONSE")

    def test_invalid_identifier_mode_and_header_do_not_send(self):
        client, transport = client_with()
        calls = [lambda: client.job("../private"),
                 lambda: client.create_job(str(uuid.uuid4()), mode="live"),
                 lambda: client.create_job(str(uuid.uuid4()), idempotency_key="abc\r\nBad: yes"),
                 lambda: client.datasets(page=-1), lambda: client.datasets(page=True)]
        for call in calls:
            with self.assertRaises(ApiError):
                call()
        self.assertEqual(transport.calls, [])

    def test_file_upload_streams_correct_multipart_length(self):
        client, transport = client_with(csrf_packet(), packet({"id": "dataset"}, status=201))
        content = b"\x00ITCH\xff" * 3000
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'file"\r\nInjected.itch'
            path.write_bytes(content)
            client.upload_dataset(path, "测试行情")
        uploaded = transport.calls[-1]
        self.assertEqual(int(uploaded["headers"]["content-length"]), len(uploaded["body"]))
        self.assertIn(content, uploaded["body"])
        self.assertIn("测试行情".encode(), uploaded["body"])
        self.assertNotIn(b'file"\r\nInjected', uploaded["body"])
        self.assertEqual(uploaded["method"], "POST")

    def test_missing_empty_or_special_file_does_not_send(self):
        client, transport = client_with()
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty.itch"
            empty.touch()
            for path in [empty, Path(directory) / "missing", Path(directory)]:
                with self.subTest(path=path), self.assertRaises(ApiError):
                    client.upload_dataset(path)
        self.assertEqual(transport.calls, [])

    def test_logout_clears_local_session_even_when_server_unreachable(self):
        client, _ = client_with(csrf_packet(), error.URLError("unavailable"))
        with self.assertRaises(ApiError):
            client.logout()
        self.assertEqual(list(client._cookies), [])
        self.assertEqual(client._csrf_token, "")

    def test_risk_changes_use_partial_patch_and_preserve_server_error(self):
        client, transport = client_with(csrf_packet(), packet({"max_order_size": [{"message": "invalid"}]},
            status=400, success=False, code="VALIDATION_ERROR", message="参数无效"))
        with self.assertRaises(ApiError) as caught:
            client.update_risk({"max_order_size": -1})
        self.assertEqual(transport.calls[-1]["method"], "PATCH")
        self.assertEqual(caught.exception.code, "VALIDATION_ERROR")
        self.assertIn("max_order_size", caught.exception.data)

    def test_automation_control_requires_explicit_valid_action(self):
        client, transport = client_with()
        with self.assertRaises(ApiError):
            client.automation_command(str(uuid.uuid4()), "live", "example")
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
