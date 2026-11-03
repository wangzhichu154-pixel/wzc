#!/usr/bin/env python3
"""Restore a backup into a NEW isolated database; never overwrite trading."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

from backup_lib import checksum, compose


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--identity", type=Path, help="age identity file, required for encrypted backups")
    parser.add_argument("--target-db", help="Must start with restored_; database must not already exist")
    parser.add_argument("--keep", action="store_true", help="Keep the successfully restored isolated database")
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text())
    file_name = manifest["file"]
    if Path(file_name).name != file_name:
        parser.error("Manifest archive path must be a filename")
    archive = manifest_path.parent / file_name
    if archive.is_symlink() or not archive.is_file():
        parser.error("Archive must be a regular file")
    if checksum(archive) != manifest["sha256"] or archive.stat().st_size != manifest["size_bytes"]:
        parser.error("Archive integrity verification failed")
    if manifest.get("encrypted") and not args.identity:
        parser.error("Encrypted backup requires --identity")
    target = args.target_db or f"restored_{secrets.token_hex(8)}"
    if not re.fullmatch(r"restored_[a-z0-9_]{1,40}", target):
        parser.error("Target must match restored_[a-z0-9_]{1,40}; production databases are forbidden")
    created = False
    success = False
    started = time.monotonic()
    try:
        compose("exec", "-T", "database", "createdb", "-U", "postgres", "--owner=trading_migrator", target)
        created = True
        with tempfile.TemporaryFile() as plaintext:
            if manifest.get("encrypted"):
                subprocess.run(["age", "--decrypt", "--identity", str(args.identity), str(archive)],
                               stdout=plaintext, check=True)
                plaintext.seek(0)
                source = plaintext
            else:
                source = archive.open("rb")
            try:
                compose("exec", "-T", "database", "pg_restore", "-U", "postgres",
                        "--role=trading_migrator", "--dbname", target, "--exit-on-error",
                        "--no-owner", "--no-acl", stdin=source)
            finally:
                if source is not plaintext:
                    source.close()
        # --no-acl intentionally omits grants. Reapply the least-privilege contract.
        grants = (
            f'REVOKE ALL ON DATABASE "{target}" FROM PUBLIC; '
            f'GRANT CONNECT ON DATABASE "{target}" TO trading_app; '
            'REVOKE ALL ON SCHEMA public FROM PUBLIC; '
            'GRANT USAGE ON SCHEMA public TO trading_app; '
            'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO trading_app; '
            'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO trading_app; '
            'ALTER DEFAULT PRIVILEGES FOR ROLE trading_migrator IN SCHEMA public '
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO trading_app; '
            'ALTER DEFAULT PRIVILEGES FOR ROLE trading_migrator IN SCHEMA public '
            'GRANT USAGE, SELECT ON SEQUENCES TO trading_app;'
        )
        compose("exec", "-T", "database", "psql", "-U", "postgres", "-d", target,
                "--set=ON_ERROR_STOP=1", "-c", grants, stdout=subprocess.DEVNULL)
        result = compose("exec", "-T", "database", "psql", "-U", "postgres", "-d", target,
                         "--set=ON_ERROR_STOP=1", "-Atc", "SELECT count(*) FROM django_migrations",
                         capture_output=True, text=True)
        migration_count = int(result.stdout.strip())
        if migration_count < 1:
            raise RuntimeError("Restored database has no Django migration history")
        check_code = (
            "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); "
            "import django; django.setup(); from django.db import connections; "
            "from django.db.migrations.executor import MigrationExecutor; "
            "c=connections['default']; c.settings_dict['NAME']=os.environ['RESTORE_DATABASE_NAME']; "
            "e=MigrationExecutor(c); e.loader.check_consistent_history(c); "
            "print('Migration history is consistent; pending migrations:', "
            "len(e.migration_plan(e.loader.graph.leaf_nodes())))"
        )
        compose("run", "--rm", "--no-deps", "-T", "-e", f"RESTORE_DATABASE_NAME={target}",
                "--entrypoint", "python", "backend", "-c", check_code)
        success = True
        report = {
            "archive_sha256": manifest["sha256"], "database": target,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(time.monotonic() - started, 3),
            "migration_count": migration_count,
            "database_restore_passed": True,
            "business_e2e_passed": False,
            "media_restore_passed": False,
            "retained": args.keep,
        }
        report_path = manifest_path.with_suffix(".restore-report.json")
        temp_report = report_path.with_name(report_path.name + "." + secrets.token_hex(8) + ".partial")
        fd = os.open(temp_report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(report, output, indent=2)
            output.write("\n")
        temp_report.replace(report_path)
        print(f"Database restore verified; report: {report_path.name}. Business and media tests remain separate.")
    finally:
        if created and not (success and args.keep):
            compose("exec", "-T", "database", "dropdb", "-U", "postgres", "--force", target)


if __name__ == "__main__":
    main()
