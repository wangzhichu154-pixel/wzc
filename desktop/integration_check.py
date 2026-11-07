#!/usr/bin/env python3
"""Exercise the native client's real HTTP contract against an isolated service.

Uses a temporary SQLite database and a synchronous real Celery worker/C++
engine. Never connects to the existing deployment, a broker, or real funds.
Generated credentials remain in memory and temporary test state only; reports
contain flow names and checksums, never request bodies, cookies or passwords.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib import error, request

from vector_desktop.client import ApiError, VectorClient


ROOT = Path(__file__).resolve().parents[1]


class CheckFailure(RuntimeError):
    """Carries only a static, non-sensitive assertion description."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def expect_api_error(action, *, status: int, code: str) -> None:
    try:
        action()
    except ApiError as exc:
        require(exc.status == status and exc.code == code, "Unexpected API error contract")
    else:
        raise CheckFailure("Expected request rejection")


def wait_ready(client: VectorClient, server: subprocess.Popen) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError("Isolated test server terminated before readiness")
        try:
            if client.health().get("ready") is True:
                return
        except ApiError:
            pass
        time.sleep(0.1)
    raise TimeoutError("Isolated service readiness timeout")


def check_csrf(base_url: str) -> None:
    opener = request.build_opener(request.ProxyHandler({}))
    req = request.Request(base_url + "/api/v1/auth/login/", data=b"{}", method="POST",
                          headers={"Content-Type": "application/json"})
    try:
        opener.open(req, timeout=3)
    except error.HTTPError as exc:
        with exc:
            require(exc.status == 403, "Missing CSRF request must fail")
            payload = json.loads(exc.read(8192))
            require(payload["code"] == "CSRF_FAILED", "Unexpected CSRF failure response")
    else:
        raise CheckFailure("Login accepted without CSRF")


def run_flows(client: VectorClient, workdir: Path, environment: dict, email: str,
              password: str, report: dict) -> None:
    def completed(name: str) -> None:
        report["flows"].append(name)

    report["stage"] = "authentication"
    expect_api_error(client.me, status=401, code="AUTHENTICATION_REQUIRED")
    check_csrf(client.base_url)
    completed("anonymous-read-and-missing-csrf-denied")
    logged_in = client.login(email, password)
    require(logged_in["email"] == email, "Login identity mismatch")
    require(client.me()["email"] == email, "Session cookie was not retained")
    require(client.session()["live_trading"] is False, "Live must remain disabled")
    completed("login-csrf-rotation-session-and-identity")

    report["stage"] = "risk"
    original = client.risk()
    require(original["persisted"] is False, "Risk GET unexpectedly persisted defaults")
    updated = client.update_risk({"max_order_size": 25, "daily_loss_limit": "275.50"})
    require(updated["max_order_size"] == 25 and updated["daily_loss_limit"] == "275.50", "Risk update mismatch")
    require(client.risk()["tick_size"] == original["tick_size"], "PATCH lost an untouched risk field")
    expect_api_error(lambda: client.update_risk({"orders_per_second": 51}), status=400, code="VALIDATION_ERROR")
    require(client.risk()["orders_per_second"] == original["orders_per_second"], "Rejected update changed risk")
    completed("risk-read-patch-form-validation-and-persistence")

    report["stage"] = "upload"
    fixture = workdir / "desktop-replay.itch"
    simulation_fixture = workdir / "desktop-simulation.itch"
    subprocess.run([sys.executable, str(ROOT / "engine/tests/make_fixture.py"), str(fixture)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([sys.executable, str(ROOT / "engine/tests/make_simulation_fixture.py"), str(simulation_fixture)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    dataset = client.upload_dataset(fixture, "Synthetic desktop replay acceptance")
    sim_dataset = client.upload_dataset(simulation_fixture, "Synthetic desktop simulation acceptance")
    require(client.datasets()["total"] == 2, "Uploads not visible in list")
    require(dataset["sha256"] == hashlib.sha256(fixture.read_bytes()).hexdigest(), "Uploaded file checksum mismatch")
    completed("streamed-multipart-itch-upload-and-owner-list")

    report["stage"] = "cpp-replay"
    key = "desktop-replay-" + secrets.token_hex(8)
    replay = client.create_job(dataset["id"], idempotency_key=key)
    require(replay["status"] == "succeeded", "Real C++ replay failed")
    replay = client.job(replay["id"])
    require(replay["result"]["book_valid"] is True and replay["result"]["messages"] > 0,
            "C++ replay output did not validate")
    repeated = client.create_job(dataset["id"], idempotency_key=key)
    require(repeated["id"] == replay["id"], "Idempotency created a duplicate job")
    require(client.jobs()["total"] == 1, "Replay duplicate affected count")
    expect_api_error(lambda: client.cancel_job(replay["id"]), status=409, code="JOB_FINISHED")
    completed("real-cpp-replay-idempotency-and-terminal-cancel-rejection")

    report["stage"] = "cpp-simulation"
    simulation = client.create_job(sim_dataset["id"], mode="simulation")
    require(simulation["status"] == "succeeded", "Real C++ simulation failed")
    require(simulation["result"]["book_valid"] is True, "Simulation book invalid")
    summary = simulation["result"]["summary"]["simulation"]
    require(isinstance(summary, dict), "Missing real simulation summary")
    report["engine_results"] = {
        "replay_messages": replay["result"]["messages"],
        "replay_checksum": replay["result"]["checksum"],
        "simulation_messages": simulation["result"]["messages"],
        "simulation_checksum": simulation["result"]["checksum"],
        "simulation_status": simulation["status"],
    }
    completed("real-cpp-historical-simulation")

    report["stage"] = "automations"
    create_key = "desktop-auto-" + secrets.token_hex(8)
    created = client.create_automation("Synthetic bounded automation", sim_dataset["id"],
                                       interval_seconds=60, max_runs=2, idempotency_key=create_key)
    automation = created["automation"]
    require(automation["state"] == "stopped", "New automation must be stopped")
    duplicate = client.create_automation("Synthetic bounded automation", sim_dataset["id"],
                                         interval_seconds=60, max_runs=2, idempotency_key=create_key)
    require(duplicate["replayed"] is True and duplicate["automation"]["id"] == automation["id"],
            "Automation idempotency failed")
    expect_api_error(lambda: client.automation_command(automation["id"], "start", "incorrect-test-password"),
                     status=403, code="REAUTHENTICATION_FAILED")
    started = client.automation_command(automation["id"], "start", password)
    require(started["automation"]["state"] == "running", "Simulation authorization did not start")
    # Invoke the genuine scheduler exactly once in the isolated service state.
    subprocess.run([sys.executable, str(ROOT / "backend/manage.py"), "shell", "-c",
                    "from trading.tasks_automations import tick_automations; tick_automations()"],
                   env=environment, cwd=ROOT / "backend", check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    after_tick = client.automation(automation["id"])
    require(after_tick["runs_created"] == 1 and after_tick["last_job_id"], "Scheduler did not create exactly one run")
    automatic_job = client.job(after_tick["last_job_id"])
    require(automatic_job["status"] == "succeeded" and automatic_job["mode"] == "simulation",
            "Scheduled C++ simulation failed")
    stop_key = "desktop-stop-" + secrets.token_hex(8)
    stopped = client.automation_command(automation["id"], "stop", password, idempotency_key=stop_key)
    require(stopped["automation"]["state"] == "stopped", "Automation stop failed")
    duplicate_stop = client.automation_command(automation["id"], "stop", password, idempotency_key=stop_key)
    require(duplicate_stop["command"]["replayed"] is True, "Stop idempotency failed")
    require(client.automations()["total"] == 1, "Unexpected automation count")
    completed("automation-create-idempotency-password-check-scheduler-real-cpp-stop")

    report["stage"] = "dashboard-and-logout"
    dashboard = client.dashboard()
    require(dashboard["dataset_count"] == 2 and dashboard["job_count"] == 3, "Dashboard count mismatch")
    require(dashboard["live_trading"] is False and dashboard["automation_running"] == 0,
            "Dashboard risk boundary mismatch")
    client.logout()
    expect_api_error(client.me, status=401, code="AUTHENTICATION_REQUIRED")
    expect_api_error(client.dashboard, status=401, code="AUTHENTICATION_REQUIRED")
    completed("owner-dashboard-and-logout-revokes-access")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8879)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/desktop-rc5/integration.json")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use a non-privileged local port")
    engine = ROOT / "build/trading-replay"
    if not engine.is_file() or not os.access(engine, os.X_OK):
        parser.error("Build the genuine C++ engine at build/trading-replay first")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"status": "failed", "stage": "setup", "flows": [],
              "scope": "isolated local HTTP / SQLite / synchronous real Celery and C++; no broker or real funds",
              "fixture_data": "synthetic protocol fixtures only", "gui_tested": False,
              "macos_tested": False, "production_database_touched": False,
              "created_at": datetime.now(timezone.utc).isoformat(),
              "engine_sha256": hashlib.sha256(engine.read_bytes()).hexdigest()}
    server = None
    client = None
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", args.port))
        with tempfile.TemporaryDirectory(prefix="vector-desktop-http-") as temporary:
            workdir = Path(temporary)
            base_url = f"http://127.0.0.1:{args.port}"
            environment = dict(os.environ)
            environment.update({"DJANGO_SETTINGS_MODULE": "e2e.settings", "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
                                "VECTOR_E2E_WORKDIR": str(workdir), "VECTOR_E2E_BASE_URL": base_url,
                                "PYTHONPATH": os.pathsep.join([str(ROOT / "tests"), str(ROOT / "backend")]),
                                "ENGINE_BINARY": str(engine), "LOG_LEVEL": "ERROR",
                                "ML_MODEL_STORE_DIR": "", "EXECUTION_STATUS_FILE": ""})
            manage = [sys.executable, str(ROOT / "backend/manage.py")]
            subprocess.run([*manage, "migrate", "--noinput"], env=environment, cwd=ROOT / "backend", check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            email = "desktop-http-acceptance@example.test"
            password = secrets.token_urlsafe(32) + "!9"
            # Supply transient test credentials over stdin; not command args or logs.
            subprocess.run([sys.executable, "-c", "import django,json,sys; django.setup(); "
                            "from django.contrib.auth import get_user_model; "
                            "data=json.load(sys.stdin); "
                            "get_user_model().objects.create_user(**data, email_verified=True)"],
                           input=json.dumps({"email": email, "password": password}), text=True,
                           env=environment, cwd=ROOT / "backend", check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            server = subprocess.Popen([*manage, "runserver", f"127.0.0.1:{args.port}", "--noreload"],
                                      env=environment, cwd=ROOT / "backend",
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            client = VectorClient(base_url, timeout=10)
            try:
                report["stage"] = "service-readiness"
                wait_ready(client, server)
                run_flows(client, workdir, environment, email, password, report)
                report.update(status="passed", stage="complete")
            finally:
                client.close()
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc, CheckFailure):
            report["failed_check"] = str(exc)
        if isinstance(exc, ApiError):
            report["error_code"] = exc.code
            report["http_status"] = exc.status
        # No exception string or traceback: they could include transient values.
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
