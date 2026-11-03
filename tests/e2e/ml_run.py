#!/usr/bin/env python3
"""Read-only AI monitor acceptance against a real, pre-existing model registry.

Uses temporary SQLite, generated test identities and local Chromium. It never
trains, approves, activates, deletes or changes the supplied registry. Run only
after the independent training/registry workflow has produced real artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
VIEWPORTS = [(1920, 1080), (1440, 900), (1366, 768), (1024, 768), (768, 1024), (390, 844), (375, 667)]


def wait_for_server(base_url, process):
    deadline = time.monotonic() + 30
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Isolated test server exited")
        try:
            with opener.open(base_url + "/health/live/", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise RuntimeError("Isolated test server readiness timed out")


def registry_fingerprint(path):
    # Fixed control-file names only, no arbitrary filename scan or data export.
    result = {}
    for filename in ("index.json", "active-simulation.json", "active-paper.json"):
        item = path / filename
        if item.is_symlink():
            raise ValueError("Registry control files must not be symlinks")
        result[filename] = hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else None
    return result


def run_browser(base_url, artifacts, identities, registry, *, require_flat_ledger=False):
    from playwright.sync_api import expect, sync_playwright

    result = {"test_environment": "isolated-sqlite-real-readonly-registry", "flows": [], "viewports": [], "browser_errors": []}
    before = registry_fingerprint(registry)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = context.new_page()
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: result["browser_errors"].append(str(error)))
        page.on("console", lambda message: result["browser_errors"].append(message.text) if message.type == "error" else None)

        def login(target, account):
            target.goto(base_url + "/accounts/login/", wait_until="networkidle")
            target.locator("#id_email").fill(account["email"])
            target.locator("#id_password").fill(account["password"])
            target.locator("main").get_by_role("button", name="登录", exact=True).click()
            target.wait_for_url(base_url + "/")

        try:
            assert context.request.get(base_url + "/api/v1/ai/status/").status == 401
            assert context.request.get(base_url + "/ai/", max_redirects=0).status == 302
            login(page, identities[0])
            result["flows"].append("verified-operator-login")
            response = context.request.get(base_url + "/api/v1/ai/status/")
            assert response.status == 200, "Registry projection must be valid before browser acceptance"
            payload = response.json()
            assert payload["success"] is True and payload["data"]["available"] is True
            state = payload["data"]
            assert state["execution_authorized"] is False and state["live_enabled"] is False
            models = state["models"]
            assert models, "No actual model artifact is available; a blank page cannot pass model acceptance"
            synthetic = [model for model in models if model["provenance_kind"] == "synthetic"]
            assert synthetic, "This acceptance run expects the explicit synthetic engineering dataset"
            failed = [model for model in models if model.get("evaluation_passed") is False or model.get("simulation_passed") is False]
            assert failed, "Real failed gates must remain failed; do not alter thresholds to make this test pass"
            assert any(model.get("training_summary") is not None for model in models), "Actual training report is missing"
            assert any(model.get("simulation_summary") is not None for model in failed), "Actual failed simulation evidence is missing"
            for model in models:
                assert model["execution_authorized"] is False
                assert model["version"].startswith("ridge-")
            assert str(registry) not in json.dumps(payload)
            for forbidden in ("account_id", "capture_path", "password", "secret", "capture_id"):
                assert forbidden not in json.dumps(payload), "Private model evidence escaped the status projection"
            result["models"] = [{key: model.get(key) for key in ("version", "stage", "provenance_kind", "evaluation_passed", "simulation_passed")} for model in models]
            result["flows"].append("real-model-status-with-synthetic-provenance-and-failed-gates")
            flat = [model for model in models if (model.get("simulation_summary") or {}).get("valuation", {})
                    and model["simulation_summary"]["valuation"].get("basis") == "reconciled_flat_ledger"]
            if require_flat_ledger:
                assert flat, "Actual new-schema flat-account evidence is required; never synthesize a passing UI fixture"
                no_fills = [model for model in flat if model["simulation_summary"].get("fill_count") == 0]
                assert no_fills, "This acceptance checks that zero-trade flat accounts remain ineligible"
                for model in no_fills:
                    ledger = model["simulation_summary"]
                    assert ledger["net_pnl_units"] == 0 and ledger["unrealized_pnl_units"] == 0
                    assert ledger["net_pnl_valid"] is True and ledger["quote_mark_valid"] is False
                    assert ledger["inventory"] == 0 and ledger["open_orders_at_end"] == 0 and ledger["open_exposure"] is False
                    assert model["simulation_passed"] is False
                    assert "NO_FILLS" in model["simulation_failure_codes"], "A zero-PnL account is not an admission success"
                result["valuation_cases"] = [{"version": model["version"],
                    "basis": model["simulation_summary"]["valuation"]["basis"],
                    "net_pnl_valid": model["simulation_summary"]["net_pnl_valid"],
                    "quote_mark_valid": model["simulation_summary"]["quote_mark_valid"],
                    "drawdown_complete": model["simulation_summary"]["drawdown_complete"],
                    "failure_codes": model["simulation_failure_codes"]} for model in no_fills]
                result["flows"].append("settled-flat-account-does-not-restore-market-or-admit-zero-trades")

            page.goto(base_url + "/ai/", wait_until="networkidle")
            expect(page.get_by_role("heading", name="AI 模型与验证", exact=True)).to_be_visible()
            expect(page.locator("main")).to_contain_text(synthetic[0]["version"])
            expect(page.locator("main")).to_contain_text("合成测试数据")
            expect(page.locator("main")).to_contain_text("未通过")
            expect(page.locator("main")).to_contain_text("实盘执行授权始终关闭")
            if require_flat_ledger:
                card = page.locator(f'[data-model-version="{no_fills[0]["version"]}"]')
                expect(card.locator('[data-valuation-basis="reconciled_flat_ledger"]')).to_be_visible()
                expect(card.locator('[data-metric="net-pnl"]')).to_have_text("0.0000")
                expect(card.locator('[data-failure-code="NO_FILLS"]')).to_be_visible()
                expect(card).to_contain_text("不会因此恢复报价权限")
                expect(card).to_contain_text("零盈亏不能证明策略有效")
            assert page.locator('form[method="post"]').count() == 1, "Only the global logout form may mutate from this page"

            ordinary = browser.new_context(locale="zh-CN")
            ordinary_page = ordinary.new_page()
            try:
                login(ordinary_page, identities[1])
                assert ordinary.request.get(base_url + "/api/v1/ai/status/").status == 403
                assert ordinary.request.get(base_url + "/ai/").status == 403
            finally:
                ordinary.close()
            result["flows"].append("ordinary-user-and-anonymous-model-access-denied")
            csrf = next(cookie["value"] for cookie in context.cookies(base_url) if cookie["name"] == "csrftoken")
            rejected_write = context.request.post(base_url + "/api/v1/ai/status/",
                headers={"X-CSRFToken": csrf, "Referer": base_url + "/ai/"}, data={})
            assert rejected_write.status == 405, "The model status endpoint must never accept mutations"
            result["flows"].append("read-only-api-rejects-write")

            for width, height in VIEWPORTS:
                page.set_viewport_size({"width": width, "height": height})
                response = page.goto(base_url + "/ai/", wait_until="networkidle")
                assert response is not None and response.status == 200
                assert page.locator("main h1").count() == 1
                overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
                page.screenshot(path=str(artifacts / f"ai-{width}x{height}.png"), full_page=True)
                assert not overflow, f"AI page document overflow at {width}x{height}"
                if width <= 640:
                    toggle = page.locator(".nav-toggle")
                    toggle.click()
                    expect(toggle).to_have_attribute("aria-expanded", "true")
                    page.keyboard.press("Escape")
                    expect(toggle).to_have_attribute("aria-expanded", "false")
                result["viewports"].append({"width": width, "height": height, "document_overflow": False})
            result["flows"].append("seven-viewports-with-real-model-tables")
            page.get_by_role("button", name="读取并刷新状态", exact=True).click()
            page.wait_for_load_state("networkidle")
            expect(page.get_by_role("heading", name="AI 模型与验证", exact=True)).to_be_visible()
            assert registry_fingerprint(registry) == before, "Read-only monitor changed registry pointers or index"
            result["flows"].append("refresh-keeps-registry-unchanged")
            assert not result["browser_errors"], "Browser JavaScript or console errors occurred"
            return result
        except Exception:
            page.screenshot(path=str(artifacts / "failure.png"), full_page=True)
            raise
        finally:
            context.close()
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True, help="Existing operator-owned model store; never modified")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts/e2e-ml")
    parser.add_argument("--require-flat-ledger", action="store_true",
                        help="Require real rc.4 account-valuation evidence with zero fills and a failed admission gate")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use a non-privileged loopback port")
    if args.registry.is_symlink() or not args.registry.is_dir():
        parser.error("Registry must be an existing real directory")
    registry = args.registry.absolute()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".cache/ms-playwright"))
    base_url = f"http://127.0.0.1:{args.port}"
    summary = {"status": "failed", "scope": "Local read-only Chromium acceptance; no training, promotion or broker authorization"}
    identities = [{"email": f"ml-operator-{secrets.token_hex(5)}@example.test", "password": secrets.token_urlsafe(24) + "!4", "role": "admin", "is_staff": True},
                  {"email": f"ml-user-{secrets.token_hex(5)}@example.test", "password": secrets.token_urlsafe(24) + "!4", "role": "user", "is_staff": False}]
    with tempfile.TemporaryDirectory(prefix="vector-ml-e2e-") as temporary:
        workdir = Path(temporary)
        accounts_path = workdir / "test-identities.json"
        descriptor = os.open(accounts_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(identities, stream)
        environment = dict(os.environ)
        environment.update({"DJANGO_SETTINGS_MODULE": "e2e.settings", "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
                            "VECTOR_E2E_WORKDIR": str(workdir), "VECTOR_E2E_BASE_URL": base_url,
                            "ML_E2E_IDENTITIES": str(accounts_path), "ML_MODEL_STORE_DIR": str(registry),
                            "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "tests"), str(ROOT / "backend")]),
                            "ENGINE_BINARY": str(ROOT / "build/trading-replay"), "LOG_LEVEL": "WARNING"})
        manage = [sys.executable, str(ROOT / "backend/manage.py")]
        subprocess.run([*manage, "migrate", "--noinput"], env=environment, cwd=ROOT / "backend", check=True, stdout=subprocess.DEVNULL)
        code = ('import json,os\nfrom django.contrib.auth import get_user_model\n'
                'with open(os.environ["ML_E2E_IDENTITIES"]) as stream:\n accounts=json.load(stream)\n'
                'for account in accounts:\n get_user_model().objects.create_user(**account,email_verified=True)\n')
        subprocess.run([*manage, "shell", "-c", code], env=environment, cwd=ROOT / "backend", check=True, stdout=subprocess.DEVNULL)
        accounts_path.unlink()
        environment.pop("ML_E2E_IDENTITIES")
        with (artifacts / "server.log").open("w") as log:
            server = subprocess.Popen([*manage, "runserver", f"127.0.0.1:{args.port}", "--noreload"],
                                      env=environment, cwd=ROOT / "backend", stdout=log, stderr=log)
            try:
                wait_for_server(base_url, server)
                summary.update(run_browser(base_url, artifacts, identities, registry, require_flat_ledger=args.require_flat_ledger))
                summary["status"] = "passed"
            except Exception as exc:
                summary["error_type"] = type(exc).__name__
                print(f"AI E2E failed: {type(exc).__name__}; inspect the local screenshot and server.log", file=sys.stderr)
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
