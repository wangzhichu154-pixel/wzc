#!/usr/bin/env python3
"""Generate local-only configuration without displaying or overwriting secrets."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        parser.error(f"Refusing to overwrite existing configuration: {output}")
    secret_dir = output.parent / ".secrets"
    token_file = secret_dir / "metrics_token"
    if secret_dir.is_symlink():
        parser.error("Refusing a symlinked credential directory")
    if token_file.exists() or token_file.is_symlink():
        parser.error("Metrics credential already exists; refusing to create inconsistent secrets")
    values = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    for key in ("DJANGO_SECRET_KEY", "POSTGRES_PASSWORD", "DB_MIGRATOR_PASSWORD",
                "DB_APP_PASSWORD", "DB_MONITOR_PASSWORD", "REDIS_PASSWORD", "METRICS_TOKEN"):
        values[key] = secrets.token_urlsafe(48)
    values["DATABASE_URL"] = f"postgresql://trading_app:{values['DB_APP_PASSWORD']}@database:5432/trading"
    values["MIGRATION_DATABASE_URL"] = f"postgresql://trading_migrator:{values['DB_MIGRATOR_PASSWORD']}@database:5432/trading"
    values["DATABASE_EXPORTER_URL"] = f"postgresql://trading_monitor:{values['DB_MONITOR_PASSWORD']}@database:5432/trading?sslmode=disable"
    for key, db in (("REDIS_URL", 0), ("CELERY_BROKER_URL", 1), ("CELERY_RESULT_BACKEND", 2)):
        values[key] = f"redis://:{values['REDIS_PASSWORD']}@redis:6379/{db}"
    # Exclusive creation also protects against concurrent invocations and symlinks.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("# Generated local development configuration; never commit.\n")
        stream.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(secret_dir, 0o700)
    token_fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    os.fchmod(token_fd, 0o444)  # Parent is 0700; container-mounted file must be readable by Prometheus.
    with os.fdopen(token_fd, "w") as stream:
        stream.write(values["METRICS_TOKEN"])
    print(f"Created {output.name} (0600) and protected metrics credential directory.")
    print("Local development only. Configure real SMTP, HTTPS and release checks before production.")


if __name__ == "__main__":
    main()
