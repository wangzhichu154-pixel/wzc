"""Safety checks that do not require Docker or access to deployment credentials."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import stat
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def run_script(name, *args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / name), *map(str, args)],
                          capture_output=True, text=True, timeout=10)


class ConfigurationTests(unittest.TestCase):
    def test_generated_configuration_is_private_and_does_not_print_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".env"
            result = run_script("init_env.py", "--output", target)
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(line.split("=", 1) for line in target.read_text().splitlines()
                          if line and not line.startswith("#"))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertGreaterEqual(len(values["DJANGO_SECRET_KEY"]), 50)
            self.assertNotEqual(values["DB_APP_PASSWORD"], values["DB_MIGRATOR_PASSWORD"])
            self.assertIn("trading_app:", values["DATABASE_URL"])
            self.assertIn("trading_migrator:", values["MIGRATION_DATABASE_URL"])
            for key in ("DJANGO_SECRET_KEY", "POSTGRES_PASSWORD", "DB_APP_PASSWORD", "METRICS_TOKEN"):
                self.assertNotIn(values[key], result.stdout + result.stderr)
            self.assertEqual((target.parent / ".secrets" / "metrics_token").read_text(), values["METRICS_TOKEN"])
            before = target.read_bytes()
            again = run_script("init_env.py", "--output", target)
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual(target.read_bytes(), before)

    def test_existing_metrics_secret_does_not_create_partial_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".secrets").mkdir()
            (root / ".secrets" / "metrics_token").write_text("existing")
            result = run_script("init_env.py", "--output", root / ".env")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / ".env").exists())

    def test_dangling_environment_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "unrelated"
            (root / ".env").symlink_to(target)
            result = run_script("init_env.py", "--output", root / ".env")
            self.assertEqual(result.returncode, 2)
            self.assertFalse(target.exists())


class RestoreSafetyTests(unittest.TestCase):
    def test_database_manifest_path_traversal_rejected_before_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "backup.json"
            manifest.write_text(json.dumps({"file": "../outside.dump"}))
            result = run_script("restore_verify.py", manifest)
            self.assertEqual(result.returncode, 2)
            self.assertIn("must be a filename", result.stderr)

    def test_corrupt_backup_is_rejected_before_database_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backup.dump").write_bytes(b"corrupt")
            manifest = root / "backup.json"
            manifest.write_text(json.dumps({"file": "backup.dump", "sha256": "0" * 64, "size_bytes": 7}))
            result = run_script("restore_verify.py", manifest)
            self.assertEqual(result.returncode, 2)
            self.assertIn("integrity verification failed", result.stderr)

    def test_media_tar_traversal_and_links_rejected_before_volume_creation(self):
        for member_name, link in (("../escape", False), ("/absolute", False), ("link", True)):
            with self.subTest(name=member_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "media.tar.gz"
                with tarfile.open(archive, "w:gz") as stream:
                    member = tarfile.TarInfo(member_name)
                    if link:
                        member.type = tarfile.SYMTYPE
                        member.linkname = "/etc/passwd"
                        stream.addfile(member)
                    else:
                        member.size = 1
                        stream.addfile(member, io.BytesIO(b"x"))
                manifest = root / "media.json"
                manifest.write_text(json.dumps({
                    "file": archive.name, "format": "media-tar-gzip", "size_bytes": archive.stat().st_size,
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                }))
                result = run_script("restore_media.py", manifest)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Unsafe archive member", result.stderr)


@unittest.skipUnless(shutil.which("docker"), "Docker CLI is required for Compose normalization")
class ComposeSafetyTests(unittest.TestCase):
    def test_tmpfs_options_are_not_parsed_as_separate_mounts(self):
        # compose config accepts invalid mount targets; validate normalized values too.
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / ".env"
            generated = run_script("init_env.py", "--output", environment)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            result = subprocess.run(
                ["docker", "compose", "--env-file", str(environment), "config", "--format", "json"],
                cwd=ROOT, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            # Never print the normalized config: it contains ephemeral credentials.
            config = json.loads(result.stdout)
            for name, service in config["services"].items():
                for mount in service.get("tmpfs", []):
                    self.assertTrue(mount.startswith("/"), f"Nonabsolute tmpfs target in {name}")
                    self.assertIn("mode=1777", mount, f"Missing intended tmpfs mode in {name}")


if __name__ == "__main__":
    unittest.main()
