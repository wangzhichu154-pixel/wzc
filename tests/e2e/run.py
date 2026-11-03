#!/usr/bin/env python3
"""Run real Chromium acceptance against isolated Django + C++ replay.

No broker, production database, SMTP delivery, payment or live trading is used.
All generated market messages and accounts are explicit test fixtures.
"""
from __future__ import annotations

import argparse
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
VIEWPORTS = [(1920, 1080), (1440, 900), (1366, 768), (1024, 768), (768, 1024), (390, 844), (375, 667)]


def wait_for_server(base_url: str, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 30
    # Local test transport must not travel through an external proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Isolated Django server exited before becoming ready")
        try:
            with opener.open(base_url + "/health/live/", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise RuntimeError("Isolated Django server readiness timed out")


def verification_link(mail_directory: Path, base_url: str) -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for path in mail_directory.glob("*.log"):
            message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            for part in message.walk():
                if part.get_content_type() != "text/plain":
                    continue
                match = re.search(r"https?://[^\s<>]+/accounts/verify/[^\s<>]+/", part.get_content())
                if match and match.group(0).startswith(base_url + "/"):
                    return match.group(0)
        time.sleep(0.1)
    raise AssertionError("No local test verification email arrived; email was not mocked")


def run_browser(base_url: str, workdir: Path, artifacts: Path, environment: dict) -> dict:
    from playwright.sync_api import expect, sync_playwright

    result = {"test_environment": "isolated-local-sqlite-eager-real-cpp", "flows": [], "viewports": [], "browser_errors": []}
    email = f"acceptance-{secrets.token_hex(5)}@example.test"
    password = secrets.token_urlsafe(24) + "!4"
    fixture = workdir / "protocol-test.itch"
    simulation_fixture = workdir / "simulation-test.itch"
    subprocess.run([sys.executable, str(ROOT / "engine/tests/make_fixture.py"), str(fixture)], check=True)
    subprocess.run([sys.executable, str(ROOT / "engine/tests/make_simulation_fixture.py"), str(simulation_fixture)], check=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = context.new_page()
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: result["browser_errors"].append(str(error)))
        page.on("console", lambda message: result["browser_errors"].append(message.text) if message.type == "error" else None)
        try:
            page.goto(base_url + "/accounts/register/", wait_until="networkidle")
            page.locator("#id_email").fill(email)
            page.locator("#id_password1").fill(password)
            page.locator("#id_password2").fill(password)
            page.locator("#id_accept_terms").check()
            page.get_by_role("button", name="创建账户并申请验证邮件", exact=True).click()
            expect(page.get_by_role("heading", name="请检查邮箱")).to_be_visible()
            result["flows"].append("register")

            page.goto(verification_link(workdir / "emails", base_url))
            page.get_by_role("button", name="验证我的邮箱", exact=True).click()
            expect(page.get_by_role("heading", name="邮箱验证成功")).to_be_visible()
            result["flows"].append("verify-real-file-email")

            page.goto(base_url + "/accounts/login/")
            page.locator("#id_email").fill(email)
            page.locator("#id_password").fill(password)
            page.locator("main").get_by_role("button", name="登录", exact=True).click()
            page.wait_for_url(base_url + "/")
            result["flows"].append("login")

            def create_job(source: Path, mode: str) -> tuple[str, str, dict]:
                page.goto(base_url + "/datasets/")
                page.locator("#id_name").fill(f"Acceptance protocol fixture — {mode}")
                page.locator("#id_file").set_input_files(source)
                page.get_by_role("button", name="上传并验证文件结构", exact=True).click()
                page.wait_for_url(re.compile(re.escape(base_url) + r"/datasets/[0-9a-f-]+/$"))
                dataset_url = page.url
                page.locator("#id_mode").select_option(mode)
                page.get_by_role("button", name="创建并运行任务", exact=True).click()
                page.wait_for_url(re.compile(re.escape(base_url) + r"/jobs/[0-9a-f-]+/$"), timeout=30000)
                job_url = page.url
                job_id = job_url.rstrip("/").split("/")[-1]
                response = context.request.get(base_url + f"/api/v1/jobs/{job_id}/")
                assert response.status == 200, "Job status API did not succeed"
                payload = response.json()
                assert payload["success"] is True
                job = payload["data"]
                assert job["status"] == "succeeded", f"Actual C++ task failed: {job.get('error_code')}"
                assert job["mode"] == mode
                assert job["result"]["book_valid"] is True
                assert job["result"]["messages"] > 0
                assert job["result"]["checksum"]
                expect(page.locator("[data-job-status]")).to_have_text("已完成")
                return dataset_url, job_url, job

            dataset_url, job_url, replay = create_job(fixture, "replay")
            assert replay["result"]["messages"] == 8
            result["flows"].append("upload-and-real-cpp-replay")
            simulation_dataset_url, simulation_url, simulation = create_job(simulation_fixture, "simulation")
            assert isinstance(simulation["result"]["summary"]["simulation"], dict)
            result["flows"].append("real-cpp-market-making-simulation")

            page.goto(base_url + "/automations/")
            page.locator("#id_name").fill("Acceptance automatic simulation")
            page.locator("#id_dataset").select_option(simulation_dataset_url.rstrip("/").split("/")[-1])
            page.locator("#id_interval_seconds").fill("60")
            page.locator("#id_max_runs").fill("2")
            page.get_by_role("button", name="保存为停止状态", exact=True).click()
            page.wait_for_url(re.compile(re.escape(base_url) + r"/automations/[0-9a-f-]+/$"))
            automation_url = page.url
            automation_id = automation_url.rstrip("/").split("/")[-1]
            page.locator("#id_start-password").fill(password)
            page.get_by_role("button", name="验证密码并启动自动模拟", exact=True).click()
            page.wait_for_url(automation_url)
            # Invoke the genuine scheduler once in this isolated process. The
            # real task executes C++; no DB status/result is written by the test.
            subprocess.run([sys.executable, str(ROOT / "backend/manage.py"), "shell", "-c",
                "from trading.tasks_automations import tick_automations; tick_automations()"],
                env=environment, cwd=ROOT / "backend", check=True, stdout=subprocess.DEVNULL)
            auto_payload = context.request.get(base_url + f"/api/v1/automations/{automation_id}/").json()["data"]
            assert auto_payload["runs_created"] == 1 and auto_payload["last_job_id"]
            auto_job = context.request.get(base_url + f"/api/v1/jobs/{auto_payload['last_job_id']}/").json()["data"]
            assert auto_job["status"] == "succeeded" and auto_job["mode"] == "simulation"
            page.reload()
            page.locator("#id_stop-password").fill(password)
            page.get_by_role("button", name="验证密码并停止", exact=True).click()
            page.wait_for_url(automation_url)
            assert context.request.get(base_url + f"/api/v1/automations/{automation_id}/").json()["data"]["state"] == "stopped"
            result["flows"].append("automatic-scheduler-real-cpp-start-stop")

            # Assess actual engine output without forcing a favorable outcome.
            # One protocol fixture cannot establish a profitable strategy.
            page.goto(base_url + "/assessments/")
            page.locator("#id_job_ids").select_option([simulation["id"]])
            page.locator("#id_extra_cost_per_share_units").fill("10")
            page.get_by_role("button", name="生成研究评估", exact=True).click()
            page.wait_for_url(re.compile(re.escape(base_url) + r"/assessments/[0-9a-f-]+/$"))
            assessment_url = page.url
            assessment_id = assessment_url.rstrip("/").split("/")[-1]
            expect(page.get_by_role("heading", name="研究收益评估", exact=True)).to_be_visible()
            assessed = context.request.get(base_url + f"/api/v1/assessments/{assessment_id}/")
            assert assessed.status == 200
            assessment = assessed.json()["data"]
            assert assessment["result"]["execution_authorized"] is False
            csrf = next(cookie["value"] for cookie in context.cookies(base_url) if cookie["name"] == "csrftoken")
            duplicate = context.request.post(base_url + "/api/v1/assessments/",
                headers={"X-CSRFToken": csrf, "Referer": base_url + "/assessments/"},
                data={"job_ids": [simulation["id"], auto_job["id"]], "extra_cost_per_share_units": 10})
            assert duplicate.status in (400, 409), "Repeated historical source was accepted as independent evidence"
            result["flows"].append("cost-stress-assessment-and-duplicate-history-rejection")

            # Permission checks use the real HTTP endpoint, no DB or view stubs.
            stranger = browser.new_context()
            denied = stranger.request.get(dataset_url, max_redirects=0)
            assert denied.status == 302
            assert stranger.request.get(base_url + f"/api/v1/assessments/{assessment_id}/").status == 401
            stranger.close()
            denied_operation = context.request.get(base_url + "/operations/")
            assert denied_operation.status == 403
            result["flows"].append("anonymous-and-operator-access-denied")

            routes = [("dashboard", base_url + "/"), ("dataset", dataset_url), ("replay", job_url),
                      ("simulation", simulation_url), ("account", base_url + "/accounts/profile/"),
                      ("risk", base_url + "/risk/"), ("automation", automation_url),
                      ("assessments", base_url + "/assessments/"), ("assessment", assessment_url),
                      ("about", base_url + "/about/")]
            for width, height in VIEWPORTS:
                page.set_viewport_size({"width": width, "height": height})
                for name, url in routes:
                    response = page.goto(url, wait_until="networkidle")
                    assert response is not None and response.status == 200, f"Page {name} unavailable"
                    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
                    if overflow:
                        page.screenshot(path=str(artifacts / f"overflow-{name}-{width}.png"), full_page=True)
                    assert not overflow, f"Horizontal document overflow: {name} at {width}×{height}"
                    assert page.locator("main h1").count() == 1, f"Expected one page title on {name}"
                    page.screenshot(path=str(artifacts / f"{name}-{width}x{height}.png"), full_page=True)
                if width <= 640:
                    toggle = page.locator(".nav-toggle")
                    toggle.click()
                    expect(toggle).to_have_attribute("aria-expanded", "true")
                    page.keyboard.press("Escape")
                    expect(toggle).to_have_attribute("aria-expanded", "false")
                result["viewports"].append({"width": width, "height": height, "pages": len(routes), "document_overflow": False})
            result["flows"].append("responsive-seven-viewports")

            page.get_by_role("button", name="退出", exact=True).click()
            page.wait_for_url(base_url + "/accounts/login/")
            response = context.request.get(base_url + "/api/v1/session/")
            assert response.status == 401
            result["flows"].append("logout-revokes-access")
            assert not result["browser_errors"], f"Browser errors: {result['browser_errors']}"
            return result
        except Exception:
            page.screenshot(path=str(artifacts / "failure.png"), full_page=True)
            raise
        finally:
            context.close()
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts/e2e")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use a non-privileged local port")
    engine = ROOT / "build/trading-replay"
    if not engine.is_file() or not os.access(engine, os.X_OK):
        parser.error("Build the real C++ engine at build/trading-replay before browser acceptance")
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".cache/ms-playwright"))
    base_url = f"http://127.0.0.1:{args.port}"
    summary = {"status": "failed", "scope": "Local Chromium acceptance only; no deployment or broker validation"}
    with tempfile.TemporaryDirectory(prefix="vector-e2e-") as temporary:
        workdir = Path(temporary)
        environment = dict(os.environ)
        environment.update({"DJANGO_SETTINGS_MODULE": "e2e.settings", "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
            "VECTOR_E2E_WORKDIR": str(workdir), "VECTOR_E2E_BASE_URL": base_url,
            "PYTHONPATH": os.pathsep.join([str(ROOT / "tests"), str(ROOT / "backend")]),
            "ENGINE_BINARY": str(engine), "LOG_LEVEL": "WARNING"})
        manage = [sys.executable, str(ROOT / "backend/manage.py")]
        subprocess.run([*manage, "migrate", "--noinput"], env=environment, cwd=ROOT / "backend", check=True,
                       stdout=subprocess.DEVNULL)
        with (artifacts / "server.log").open("w") as log:
            server = subprocess.Popen([*manage, "runserver", f"127.0.0.1:{args.port}", "--noreload"],
                                      env=environment, cwd=ROOT / "backend", stdout=log, stderr=log)
            try:
                wait_for_server(base_url, server)
                summary.update(run_browser(base_url, workdir, artifacts, environment))
                summary["status"] = "passed"
            except Exception as exc:
                # Never persist credentials or email verification URLs in summaries.
                summary["error_type"] = type(exc).__name__
                print(f"E2E failed: {type(exc).__name__}; inspect local server.log and failure screenshot", file=sys.stderr)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
    (artifacts / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
