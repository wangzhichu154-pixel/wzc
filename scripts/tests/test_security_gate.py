"""Security release checks use synthetic reports, never exploit payloads."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load_script("security_gate")
inventory_script = load_script("runtime_inventory")
IMAGE_ID = "sha256:" + "a" * 64
OTHER_IMAGE_ID = "sha256:" + "b" * 64
ADVISORIES = json.loads((ROOT / "infrastructure/security/known_advisories.json").read_text())


def inventory(version="3.54.0", fts5=True):
    return {
        "schema_version": 1, "image_id": IMAGE_ID,
        "image_identity_source": "caller_supplied_docker_inspect", "collected_at": datetime.now(UTC).isoformat(),
        "status": "verified", "errors": [], "application_version": "1.0.0-rc.4",
        "python": {"implementation": "CPython", "version": "3.12.16"},
        "sqlite": {"version": version, "compile_options": ["THREADSAFE=1"] + (["ENABLE_FTS5"] if fts5 else []),
                   "fts5_compile_option": fts5, "fts5_enabled": fts5,
                   "fts5_probe": "available" if fts5 else "unavailable"},
        "openssl": {"version": "OpenSSL 3.5.0", "version_info": [3, 5, 0, 0, 15]},
        "libc": {"family": "glibc", "version": "2.41", "method": "confstr"},
        "os_release": {"id": "debian", "version_id": "13", "name": "Debian GNU/Linux 13 (trixie)"},
        "python_packages": [{"name": "example-library", "version": "1.0.0"}],
    }


def scan():
    return {
        "SchemaVersion": 2, "ArtifactType": "container_image", "ArtifactName": "fixture:only",
        "CreatedAt": datetime.now(UTC).isoformat(), "Trivy": {"Version": "0.76.1"},
        "Metadata": {"ImageID": IMAGE_ID, "OS": {"Family": "debian", "Name": "13.2"}},
        "Results": [{"Target": "fixture:only (debian 13.2)", "Class": "os-pkgs", "Type": "debian",
                     "Packages": [{"Name": "libsqlite3-0", "Version": "3.54.0"}]},
                    {"Target": "Python", "Class": "lang-pkgs", "Type": "python-pkg",
                     "Packages": [{"Name": "example-library", "Version": "1.0.0"}]}],
    }


def finding(severity="HIGH", identifier="CVE-2099-12345"):
    return {"VulnerabilityID": identifier, "PkgName": "fixture-package", "InstalledVersion": "1.0.0",
            "Severity": severity, "FixedVersion": "1.0.1"}


class GatePolicyTests(unittest.TestCase):
    def check_blocked(self, report, code):
        self.assertFalse(report["release_allowed"])
        self.assertIn(report["status"], {"blocked", "unverified"})
        self.assertIn(code, report["failure_codes"])

    def test_matching_image_with_complete_clean_evidence_passes(self):
        report = gate.evaluate(scan(), inventory(), ADVISORIES)
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["release_allowed"])
        self.assertEqual(report["blocked_findings"], [])

    def test_clean_vendor_scan_does_not_hide_upstream_sqlite_fts5(self):
        report = gate.evaluate(scan(), inventory("3.53.4"), ADVISORIES)
        self.check_blocked(report, "UPSTREAM_ADVISORY_MATCH")
        self.assertEqual(report["trivy_severity_counts"]["HIGH"], 0)
        self.assertEqual(report["blocked_findings"][0]["id"], "CVE-2026-119373")
        self.assertFalse(report["blocked_findings"][0]["exploitability_assessed"])

    def test_sqlite_versions_compared_numerically_at_fixed_boundary(self):
        for version, passes in (("3.9.99", False), ("3.53.99", False), ("3.54.0", True),
                                ("3.54.1", True), ("4.0.0", True)):
            with self.subTest(version=version):
                self.assertEqual(gate.evaluate(scan(), inventory(version), ADVISORIES)["release_allowed"], passes)

    def test_older_sqlite_without_fts5_does_not_match_conditional_rule(self):
        self.assertTrue(gate.evaluate(scan(), inventory("3.53.4", fts5=False), ADVISORIES)["release_allowed"])

    def test_fts5_compile_and_capability_disagreement_blocks(self):
        runtime = inventory("3.54.0")
        for key, replacement in (("fts5_enabled", False), ("fts5_compile_option", False),
                                 ("fts5_probe", "unavailable"), ("compile_options", ["THREADSAFE=1"])):
            with self.subTest(field=key):
                changed = deepcopy(runtime)
                changed["sqlite"][key] = replacement
                self.check_blocked(gate.evaluate(scan(), changed, ADVISORIES), "SQLITE_CAPABILITY_METADATA_MISMATCH")

    def test_unknown_sqlite_version_or_capability_never_passes(self):
        for version in (None, "", "unknown", "3.54", "3.54.0-backported", True, [], "nan"):
            with self.subTest(version=version):
                self.check_blocked(gate.evaluate(scan(), inventory(version), ADVISORIES), "SQLITE_VERSION_UNKNOWN")
        for field in ("fts5_enabled", "fts5_compile_option", "compile_options", "fts5_probe"):
            with self.subTest(field=field):
                runtime = inventory()
                runtime["sqlite"][field] = None
                self.assertFalse(gate.evaluate(scan(), runtime, ADVISORIES)["release_allowed"])

    def test_high_and_critical_hits_block_and_count_unique_ids(self):
        report = scan()
        report["Results"][0]["Vulnerabilities"] = [finding("HIGH"), finding("CRITICAL"), finding("LOW")]
        decision = gate.evaluate(report, inventory(), ADVISORIES)
        self.check_blocked(decision, "TRIVY_HIGH_OR_CRITICAL")
        self.assertEqual(decision["blocked_finding_count"], 2)
        self.assertEqual(decision["blocked_unique_vulnerability_count"], 1)
        self.assertEqual(decision["trivy_severity_counts"]["LOW"], 1)

    def test_unknown_or_invalid_finding_severity_blocks(self):
        for severity in ("UNKNOWN", "high", None, [], {}):
            with self.subTest(severity=severity):
                report = scan()
                report["Results"][0]["Vulnerabilities"] = [finding(severity)]
                self.assertFalse(gate.evaluate(report, inventory(), ADVISORIES)["release_allowed"])

    def test_mixed_unknown_severity_preserves_all_known_findings_and_counts(self):
        report = scan()
        report["Results"][0]["Vulnerabilities"] = [finding("UNKNOWN", "CVE-2099-00001"),
                                                  finding("HIGH", "CVE-2099-00002"),
                                                  finding("LOW", "CVE-2099-00003"),
                                                  finding("MEDIUM", "CVE-2099-00004")]
        report["Results"][1]["Vulnerabilities"] = [finding("CRITICAL", "CVE-2099-00005"),
                                                  finding("UNKNOWN", "CVE-2099-00006")]
        decision = gate.evaluate(report, inventory(), ADVISORIES)
        self.check_blocked(decision, "TRIVY_FINDING_SEVERITY_UNKNOWN")
        self.assertIn("TRIVY_HIGH_OR_CRITICAL", decision["failure_codes"])
        self.assertEqual(decision["trivy_severity_counts"],
                         {"UNKNOWN": 2, "LOW": 1, "MEDIUM": 1, "HIGH": 1, "CRITICAL": 1})
        self.assertEqual({row["id"] for row in decision["blocked_findings"]},
                         {"CVE-2099-00002", "CVE-2099-00005"})
        self.assertEqual(decision["blocked_finding_count"], 2)
        self.assertEqual({row["id"] for row in decision["unverified_findings"]},
                         {"CVE-2099-00001", "CVE-2099-00006"})
        self.assertEqual(decision["unverified_finding_count"], 2)

    def test_unknown_only_report_remains_unverified_with_reviewable_evidence(self):
        report = scan()
        report["Results"][0]["Vulnerabilities"] = [finding("UNKNOWN")]
        decision = gate.evaluate(report, inventory(), ADVISORIES)
        self.check_blocked(decision, "TRIVY_FINDING_SEVERITY_UNKNOWN")
        self.assertEqual(decision["status"], "unverified")
        self.assertEqual(decision["blocked_finding_count"], 0)
        self.assertEqual(decision["unverified_finding_count"], 1)
        self.assertEqual(decision["trivy_severity_counts"]["UNKNOWN"], 1)

    def test_incomplete_scan_cannot_be_treated_as_zero_findings(self):
        modifications = [
            ("Results", None), ("Results", []), ("Results", {}), ("Results", [scan()["Results"][1]]),
            ("Metadata", {}), ("SchemaVersion", True), ("SchemaVersion", 1),
            ("ArtifactType", "filesystem"), ("CreatedAt", "2026-11-03T12:00:00"), ("Error", "scan failed"),
        ]
        for key, value in modifications:
            with self.subTest(key=key, value=value):
                report = scan()
                report[key] = value
                self.assertFalse(gate.evaluate(report, inventory(), ADVISORIES)["release_allowed"])

    def test_missing_os_packages_malformed_results_and_duplicate_os_block(self):
        for patch_value in (None, [], {}):
            report = scan()
            report["Results"][0]["Packages"] = patch_value
            self.check_blocked(gate.evaluate(report, inventory(), ADVISORIES), "TRIVY_OS_PACKAGES_MISSING")
        for key, value in (("Vulnerabilities", None), ("Vulnerabilities", {}), ("Class", []),
                           ("Type", "alpine"), ("Error", "partial scan")):
            with self.subTest(key=key):
                report = scan()
                report["Results"][0][key] = value
                self.assertFalse(gate.evaluate(report, inventory(), ADVISORIES)["release_allowed"])
        report = scan()
        report["Results"].append(deepcopy(report["Results"][0]))
        self.check_blocked(gate.evaluate(report, inventory(), ADVISORIES), "TRIVY_OS_RESULT_MISSING_OR_DUPLICATED")

    def test_os_package_details_are_required(self):
        report = scan()
        report["Results"][0]["Packages"] = [{"Name": "placeholder"}]
        self.check_blocked(gate.evaluate(report, inventory(), ADVISORIES), "TRIVY_OS_PACKAGES_INVALID")

    def test_python_runtime_inventory_must_be_covered_by_language_scan(self):
        for packages in ([], [{"Name": "example-library", "Version": "0.9.0"}],
                         [{"Name": "different-library", "Version": "1.0.0"}]):
            with self.subTest(packages=packages):
                report = scan()
                report["Results"][1]["Packages"] = packages
                self.check_blocked(gate.evaluate(report, inventory(), ADVISORIES), "TRIVY_PYTHON_RUNTIME_COVERAGE_INCOMPLETE")
        report = scan()
        report["Results"].pop()
        self.check_blocked(gate.evaluate(report, inventory(), ADVISORIES), "TRIVY_PYTHON_RUNTIME_COVERAGE_INCOMPLETE")
        report = scan()
        report["Results"][1]["Packages"][0]["Name"] = "Example_Library"
        self.assertTrue(gate.evaluate(report, inventory(), ADVISORIES)["release_allowed"])

    def test_freshness_gate_rejects_old_or_future_evidence_without_hiding_findings(self):
        now = datetime.now(UTC)
        for offset, suffix in ((-86401, "EXPIRED"), (301, "IN_FUTURE")):
            for source in ("TRIVY", "INVENTORY"):
                with self.subTest(source=source, offset=offset):
                    report, runtime = scan(), inventory("3.53.4")
                    value = (now + timedelta(seconds=offset)).isoformat()
                    if source == "TRIVY":
                        report["CreatedAt"] = value
                    else:
                        runtime["collected_at"] = value
                    result = gate.evaluate(report, runtime, ADVISORIES, now=now)
                    self.check_blocked(result, f"{source}_EVIDENCE_{suffix}")
                    self.assertIn("UPSTREAM_ADVISORY_MATCH", result["failure_codes"])
        report, runtime = scan(), inventory()
        report["CreatedAt"] = (now - timedelta(seconds=86400)).isoformat()
        runtime["collected_at"] = (now + timedelta(seconds=300)).isoformat()
        self.assertTrue(gate.evaluate(report, runtime, ADVISORIES, now=now)["release_allowed"])

    def test_exact_image_identity_required(self):
        report = scan()
        report["Metadata"]["ImageID"] = OTHER_IMAGE_ID
        self.check_blocked(gate.evaluate(report, inventory(), ADVISORIES), "IMAGE_ID_MISMATCH")
        for value in (None, "fixture:latest", "sha256:123", IMAGE_ID.upper()):
            with self.subTest(value=value):
                report["Metadata"]["ImageID"] = value
                self.check_blocked(gate.evaluate(report, inventory(), ADVISORIES), "TRIVY_IMAGE_ID_INVALID")

    def test_os_identity_end_of_support_and_inventory_failures_block(self):
        for family, name in (("alpine", "13"), ("debian", "12.1"), ("debian", "130")):
            report = scan()
            report["Metadata"]["OS"].update(Family=family, Name=name)
            report["Results"][0]["Type"] = family
            self.check_blocked(gate.evaluate(report, inventory(), ADVISORIES), "OS_IDENTITY_MISMATCH")
        report = scan()
        report["Metadata"]["OS"]["EOSL"] = True
        self.check_blocked(gate.evaluate(report, inventory(), ADVISORIES), "TRIVY_OS_END_OF_SUPPORT")
        for field, value in (("errors", ["SQLITE_RUNTIME_UNVERIFIED"]), ("status", "unverified"),
                             ("errors", None), ("schema_version", True), ("python", {}), ("openssl", {}),
                             ("libc", {"family": []}), ("python_packages", None), ("os_release", None)):
            with self.subTest(field=field):
                runtime = inventory()
                runtime[field] = value
                self.assertFalse(gate.evaluate(scan(), runtime, ADVISORIES)["release_allowed"])

    def test_advisory_policy_cannot_silently_weaken_or_disappear(self):
        for policy in (None, {}, {"schema_version": 1, "advisories": []}):
            self.assertFalse(gate.evaluate(scan(), inventory(), policy)["release_allowed"])
        for field, value in (("affected_below", "3.53.0"), ("rule", "ignore"), ("policy_severity", "LOW"),
                             ("id", "OTHER"), ("required_capability", "none")):
            with self.subTest(field=field):
                policy = deepcopy(ADVISORIES)
                policy["advisories"][0][field] = value
                self.assertFalse(gate.evaluate(scan(), inventory(), policy)["release_allowed"])


class EvidenceFileAndCliTests(unittest.TestCase):
    def run_gate(self, directory, report=None, runtime=None, *extra):
        root = Path(directory)
        (root / "trivy.json").write_text(json.dumps(report if report is not None else scan()))
        (root / "inventory.json").write_text(json.dumps(runtime if runtime is not None else inventory()))
        return subprocess.run([sys.executable, str(ROOT / "scripts/security_gate.py"),
                               "--trivy", str(root / "trivy.json"), "--inventory", str(root / "inventory.json"),
                               "--advisories", str(ROOT / "infrastructure/security/known_advisories.json"), *extra],
                              capture_output=True, text=True, timeout=10)

    def test_cli_pass_block_and_persistent_input_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "gate.json")
            result = self.run_gate(directory, None, None, "--output", target)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(json.loads(Path(target).read_text()), output)
            self.assertEqual(set(output["input_sha256"]), {"trivy", "inventory", "advisories"})
            self.assertTrue(all(len(value) == 64 for value in output["input_sha256"].values()))
            blocked = self.run_gate(directory, runtime=inventory("3.53.4"))
            self.assertEqual(blocked.returncode, 1, blocked.stderr)
            self.assertIn("UPSTREAM_ADVISORY_MATCH", json.loads(blocked.stdout)["failure_codes"])

    def test_cli_missing_evidence_is_recorded_and_blocked(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/security_gate.py"),
                                 "--trivy", "/nonexistent/trivy.json", "--inventory", "/nonexistent/inventory.json",
                                 "--advisories", str(ROOT / "infrastructure/security/known_advisories.json")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertFalse(report["release_allowed"])
        self.assertIn("TRIVY_EVIDENCE_UNREADABLE", report["failure_codes"])

    def test_bad_cli_and_existing_output_are_not_successful(self):
        bad = subprocess.run([sys.executable, str(ROOT / "scripts/security_gate.py")], capture_output=True, timeout=10)
        self.assertEqual(bad.returncode, 2)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing.json"
            target.write_text("keep original")
            result = self.run_gate(directory, None, None, "--output", str(target))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(target.read_text(), "keep original")

    def test_json_duplicates_nonfinite_truncated_and_nonobject_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"broken":', b'[]', b''):
                with self.subTest(raw=raw):
                    path.write_bytes(raw)
                    with self.assertRaises(gate.EvidenceError):
                        gate.read_evidence(path)

    def test_symlink_fifo_directory_and_oversized_evidence_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ordinary = root / "ordinary.json"
            ordinary.write_text("{}")
            link = root / "link.json"
            link.symlink_to(ordinary)
            fifo = root / "input.pipe"
            os.mkfifo(fifo)
            oversized = root / "oversized.json"
            with oversized.open("wb") as stream:
                stream.truncate(gate.MAX_INPUT_BYTES + 1)
            for path in (link, fifo, root, oversized):
                with self.subTest(path=path.name), self.assertRaises(gate.EvidenceError):
                    gate.read_evidence(path)


class RuntimeInventoryTests(unittest.TestCase):
    def test_actual_runtime_capability_probe_matches_python_sqlite(self):
        errors = []
        actual = inventory_script._sqlite_inventory(errors)
        self.assertEqual(actual["version"], sqlite3.sqlite_version)
        self.assertIs(type(actual["fts5_enabled"]), bool)
        self.assertEqual(actual["fts5_enabled"], "ENABLE_FTS5" in actual["compile_options"]
                         or "ENABLE_FTS5=1" in actual["compile_options"])
        self.assertEqual(errors, [])

    def test_missing_sqlite_probe_is_explicitly_unverified(self):
        errors = []
        with patch.object(sqlite3, "connect", side_effect=sqlite3.OperationalError("probe failed")):
            actual = inventory_script._sqlite_inventory(errors)
        self.assertEqual(actual["fts5_probe"], "error")
        self.assertIsNone(actual["fts5_enabled"])
        self.assertIn("SQLITE_RUNTIME_UNVERIFIED", errors)

    def test_inventory_exports_no_environment_or_package_paths(self):
        secret = "test-sentinel-must-never-be-exported"
        with patch.dict(os.environ, {"TEST_SECRET_TOKEN": secret}):
            actual = inventory_script.collect_inventory(IMAGE_ID)
        self.assertEqual(actual["image_id"], IMAGE_ID)
        self.assertNotIn(secret, json.dumps(actual))
        self.assertEqual(actual["image_identity_source"], "caller_supplied_docker_inspect")
        self.assertTrue(all(set(row) == {"name", "version"} for row in actual["python_packages"]))

    def test_invalid_identity_is_cli_error_not_attested(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/runtime_inventory.py"),
                                 "--image-id", "fixture:latest"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
