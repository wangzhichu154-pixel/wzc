# Browser acceptance

Run from the repository root after Python dependencies and the real C++ executable have been built:

```sh
PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/ms-playwright" .venv/bin/python -m playwright install chromium
PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/ms-playwright" .venv/bin/python tests/e2e/run.py
```

The runner starts a loopback-only Django test server on port 8765 (`--port` overrides it), creates a temporary SQLite database, applies real migrations, and deletes the isolated data after completion. It never uses `DATABASE_URL` from a production deployment. Email is delivered to local test files; Celery eager mode executes the actual worker and compiled C++ program. This validates a local product flow, not PostgreSQL/Redis concurrency, SMTP delivery, a deployed staging environment, broker connectivity, or production readiness.

Coverage:

- Browser registration, actual local verification email, explicit verification POST, login.
- Upload a synthetic protocol fixture identified as test input; run real C++ replay.
- Run the real C++ market-making simulator, without a broker or real orders.
- Start/stop the genuine historical automation scheduler and execute its actual C++ task.
- Generate a persistent cost-stress assessment from real simulation output; no execution authorization is granted. Reject two jobs that reuse the same historical source.
- Anonymous access denied; ordinary user cannot operate the platform safety controls.
- Dashboard, dataset, replay, simulation, account, risk, automation, assessment list/detail and about pages at 1920×1080, 1440×900, 1366×768, 1024×768, 768×1024, 390×844 and 375×667.
- Document overflow, one main heading, mobile navigation, JavaScript errors and logout.

Results and screenshots are written to ignored `artifacts/e2e/`. Tests do not record browser traces, passwords, session tokens or verification links. The generated email and media files are only stored in the temporary test directory. `summary.json` reports pass/fail; merely possessing this test script is not proof of a passing run.

Never point these tests at a live trading or production endpoint. The test server cannot send trading orders.
