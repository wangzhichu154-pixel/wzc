#!/usr/bin/env python3
"""Fail-closed release gate for a complete Trivy report and in-image inventory.

The trusted build job must run runtime_inventory.py in the pristine image whose
Docker image ID it supplies, then scan the same image. JSON hashes and matching
image IDs bind these inputs; they do not constitute remote attestation or prove
that an untrusted report is authentic. This gate never executes exploit probes.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat

IMAGE_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
VERSION_RE = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)\Z")
NUMERIC_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}\Z")
PACKAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SEVERITIES = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
MAX_INPUT_BYTES = 64 * 1024 * 1024
REQUIRED_ADVISORY = "CVE-2026-119373"
MAX_EVIDENCE_AGE_SECONDS = 24 * 60 * 60
MAX_FUTURE_CLOCK_SKEW_SECONDS = 5 * 60


class EvidenceError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _require(condition, code):
    if not condition:
        raise EvidenceError(code)


def _text(value, maximum=512):
    return isinstance(value, str) and 0 < len(value) <= maximum and all(ord(c) >= 32 for c in value)


def _timestamp(value):
    if not _text(value, 64):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
    except ValueError:
        return False


def _version(value, code):
    _require(isinstance(value, str) and len(value) <= 32, code)
    match = VERSION_RE.fullmatch(value)
    _require(match is not None, code)
    return tuple(int(part) for part in match.groups())


def _unique_object(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise EvidenceError("DUPLICATE_JSON_KEY")
        output[key] = value
    return output


def _nonfinite(_value):
    raise EvidenceError("NONFINITE_JSON_NUMBER")


def read_evidence(path):
    """Read, bound and hash the same regular-file descriptor; reject links/FIFOs."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            _require(stat.S_ISREG(metadata.st_mode) and 0 < metadata.st_size <= MAX_INPUT_BYTES,
                     "EVIDENCE_FILE_INVALID")
            raw = stream.read(MAX_INPUT_BYTES + 1)
        _require(len(raw) == metadata.st_size and len(raw) <= MAX_INPUT_BYTES, "EVIDENCE_FILE_CHANGED")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_nonfinite)
        _require(isinstance(value, dict), "EVIDENCE_OBJECT_REQUIRED")
        return value, hashlib.sha256(raw).hexdigest()
    except EvidenceError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise EvidenceError("EVIDENCE_UNREADABLE") from exc


def validate_inventory(value):
    _require(isinstance(value, dict) and type(value.get("schema_version")) is int
             and value["schema_version"] == 1, "INVENTORY_SCHEMA_INVALID")
    _require(value.get("status") == "verified" and value.get("errors") == [], "INVENTORY_UNVERIFIED")
    _require(isinstance(value.get("image_id"), str) and IMAGE_RE.fullmatch(value["image_id"]),
             "INVENTORY_IMAGE_ID_INVALID")
    _require(value.get("image_identity_source") == "caller_supplied_docker_inspect", "INVENTORY_IDENTITY_UNKNOWN")
    _require(_timestamp(value.get("collected_at")), "INVENTORY_TIMESTAMP_INVALID")
    release = value.get("os_release")
    _require(isinstance(release, dict) and all(_text(release.get(key), 256)
             for key in ("id", "version_id", "name")), "INVENTORY_OS_UNKNOWN")
    python = value.get("python")
    _require(isinstance(python, dict) and _text(python.get("implementation")), "PYTHON_RUNTIME_UNKNOWN")
    _version(python.get("version"), "PYTHON_RUNTIME_UNKNOWN")
    openssl = value.get("openssl")
    _require(isinstance(openssl, dict) and _text(openssl.get("version"))
             and isinstance(openssl.get("version_info"), list) and len(openssl["version_info"]) == 5
             and all(type(item) is int and item >= 0 for item in openssl["version_info"]),
             "OPENSSL_RUNTIME_UNKNOWN")
    libc = value.get("libc")
    _require(isinstance(libc, dict) and libc.get("family") in {"glibc", "musl"}
             and isinstance(libc.get("version"), str) and NUMERIC_VERSION_RE.fullmatch(libc["version"]),
             "LIBC_RUNTIME_UNKNOWN")
    packages = value.get("python_packages")
    _require(isinstance(packages, list) and len(packages) <= 4096, "PYTHON_PACKAGES_UNKNOWN")
    names = set()
    for package in packages:
        _require(isinstance(package, dict) and isinstance(package.get("name"), str)
                 and PACKAGE_RE.fullmatch(package["name"]) and _text(package.get("version"), 128),
                 "PYTHON_PACKAGES_UNKNOWN")
        name = re.sub(r"[-_.]+", "-", package["name"]).lower()
        _require(name not in names, "PYTHON_PACKAGE_DUPLICATE")
        names.add(name)
    sqlite = value.get("sqlite")
    _require(isinstance(sqlite, dict), "SQLITE_RUNTIME_UNKNOWN")
    _version(sqlite.get("version"), "SQLITE_VERSION_UNKNOWN")
    options = sqlite.get("compile_options")
    _require(isinstance(options, list) and 0 < len(options) <= 1024
             and all(_text(option, 256) for option in options), "SQLITE_COMPILE_OPTIONS_UNKNOWN")
    _require(len(options) == len(set(options)), "SQLITE_COMPILE_OPTIONS_INVALID")
    _require(type(sqlite.get("fts5_enabled")) is bool and type(sqlite.get("fts5_compile_option")) is bool,
             "SQLITE_FTS5_UNKNOWN")
    compiled = "ENABLE_FTS5" in options or "ENABLE_FTS5=1" in options
    _require(compiled == sqlite["fts5_compile_option"] == sqlite["fts5_enabled"]
             and sqlite.get("fts5_probe") == ("available" if compiled else "unavailable"),
             "SQLITE_CAPABILITY_METADATA_MISMATCH")
    return value


def validate_advisories(value):
    _require(isinstance(value, dict) and type(value.get("schema_version")) is int
             and value["schema_version"] == 1, "ADVISORY_POLICY_INVALID")
    rules = value.get("advisories")
    _require(isinstance(rules, list) and 0 < len(rules) <= 256, "ADVISORY_POLICY_EMPTY")
    identifiers = set()
    for rule in rules:
        _require(isinstance(rule, dict) and _text(rule.get("id"), 64), "ADVISORY_RULE_INVALID")
        _require(rule["id"] not in identifiers, "ADVISORY_RULE_DUPLICATE")
        identifiers.add(rule["id"])
        _require(rule.get("component") == "sqlite" and rule.get("rule") == "sqlite_version_and_fts5"
                 and rule.get("required_capability") == "fts5" and rule.get("policy_severity") in {"HIGH", "CRITICAL"}
                 and _text(rule.get("source_url"), 1024) and rule["source_url"].startswith("https://"),
                 "ADVISORY_RULE_UNSUPPORTED")
        _version(rule.get("affected_below"), "ADVISORY_VERSION_INVALID")
        # A known independent rule must not silently disappear or weaken while
        # the vendor database has a coverage gap. Removal needs a code review.
        if rule["id"] == REQUIRED_ADVISORY:
            _require(rule["affected_below"] == "3.54.0", "REQUIRED_ADVISORY_CHANGED")
    _require(REQUIRED_ADVISORY in identifiers, "REQUIRED_ADVISORY_MISSING")
    return rules


def validate_trivy(value):
    _require(isinstance(value, dict) and type(value.get("SchemaVersion")) is int
             and value["SchemaVersion"] == 2 and value.get("ArtifactType") == "container_image",
             "TRIVY_SCHEMA_INVALID")
    _require(_timestamp(value.get("CreatedAt")), "TRIVY_TIMESTAMP_INVALID")
    _require(not value.get("Error") and not value.get("Errors"), "TRIVY_SCAN_ERROR")
    metadata = value.get("Metadata")
    _require(isinstance(metadata, dict) and isinstance(metadata.get("ImageID"), str)
             and IMAGE_RE.fullmatch(metadata["ImageID"]), "TRIVY_IMAGE_ID_INVALID")
    operating_system = metadata.get("OS")
    _require(isinstance(operating_system, dict) and _text(operating_system.get("Family"), 64)
             and _text(operating_system.get("Name"), 64), "TRIVY_OS_UNKNOWN")
    _require(operating_system.get("EOSL", False) is False, "TRIVY_OS_END_OF_SUPPORT")
    results = value.get("Results")
    _require(isinstance(results, list) and 0 < len(results) <= 10000, "TRIVY_RESULTS_MISSING")
    os_results, findings, counts = 0, [], dict.fromkeys(sorted(SEVERITIES), 0)
    unverified_findings = []
    python_packages = set()
    for result in results:
        _require(isinstance(result, dict) and _text(result.get("Target"), 4096)
                 and result.get("Class") in {"os-pkgs", "lang-pkgs"} and _text(result.get("Type"), 128),
                 "TRIVY_RESULT_INVALID")
        _require(not result.get("Error") and not result.get("Errors"), "TRIVY_SCAN_ERROR")
        if result["Class"] == "os-pkgs":
            os_results += 1
            _require(result["Type"] == operating_system["Family"], "TRIVY_OS_RESULT_MISMATCH")
            # A detector record with no identified packages cannot prove the OS
            # packages were scanned. Both current official base images list these.
            _require(isinstance(result.get("Packages"), list) and len(result["Packages"]) > 0,
                     "TRIVY_OS_PACKAGES_MISSING")
            _require(all(isinstance(package, dict) and _text(package.get("Name"), 256)
                         and _text(package.get("Version"), 256) for package in result["Packages"]),
                     "TRIVY_OS_PACKAGES_INVALID")
        if result["Class"] == "lang-pkgs" and result["Type"] == "python-pkg":
            packages = result.get("Packages")
            _require(isinstance(packages, list), "TRIVY_PYTHON_PACKAGES_MISSING")
            for package in packages:
                _require(isinstance(package, dict) and isinstance(package.get("Name"), str)
                         and PACKAGE_RE.fullmatch(package["Name"]) and _text(package.get("Version"), 128),
                         "TRIVY_PYTHON_PACKAGES_INVALID")
                python_packages.add((re.sub(r"[-_.]+", "-", package["Name"]).lower(), package["Version"]))
        vulnerabilities = result.get("Vulnerabilities", [])
        _require(isinstance(vulnerabilities, list), "TRIVY_VULNERABILITIES_INVALID")
        for finding in vulnerabilities:
            _require(isinstance(finding, dict) and _text(finding.get("VulnerabilityID"), 128)
                     and _text(finding.get("PkgName"), 256) and _text(finding.get("InstalledVersion"), 256)
                     and finding.get("Severity") in SEVERITIES, "TRIVY_FINDING_INVALID")
            severity = finding["Severity"]
            counts[severity] += 1
            if severity in {"HIGH", "CRITICAL", "UNKNOWN"}:
                fixed = finding.get("FixedVersion", "")
                _require(isinstance(fixed, str) and len(fixed) <= 1024, "TRIVY_FINDING_INVALID")
                item = {"id": finding["VulnerabilityID"], "source": "trivy", "severity": severity,
                        "package": finding["PkgName"], "installed_version": finding["InstalledVersion"],
                        "fixed_version": fixed}
                # UNKNOWN is a structurally valid scanner result requiring
                # review. Retain it without discarding known findings elsewhere
                # in the same report, and keep the release blocked below.
                if severity == "UNKNOWN":
                    unverified_findings.append(item)
                else:
                    findings.append(item)
    _require(os_results == 1, "TRIVY_OS_RESULT_MISSING_OR_DUPLICATED")
    return {"image_id": metadata["ImageID"], "os": operating_system, "findings": findings,
            "severity_counts": counts, "created_at": value["CreatedAt"], "python_packages": python_packages,
            "unverified_findings": unverified_findings}


def evaluate(trivy, inventory, advisories, *, input_sha256=None, now=None):
    now = now if now is not None else datetime.now(UTC)
    errors, findings, values = [], [], {}
    for name, validator, value in (("scan", validate_trivy, trivy), ("inventory", validate_inventory, inventory),
                                   ("rules", validate_advisories, advisories)):
        try:
            values[name] = validator(value)
        except EvidenceError as exc:
            errors.append(exc.code)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError):
            # Unexpected JSON types must produce a blocked evidence record,
            # never an unhandled exception or implicit successful fallback.
            errors.append(f"{name.upper()}_EVIDENCE_MALFORMED")
    scan, runtime, rules = values.get("scan"), values.get("inventory"), values.get("rules")
    if scan:
        findings.extend(scan["findings"])
        if scan["unverified_findings"]:
            errors.append("TRIVY_FINDING_SEVERITY_UNKNOWN")
    if scan and runtime:
        if scan["image_id"] != runtime["image_id"]:
            errors.append("IMAGE_ID_MISMATCH")
        release = runtime["os_release"]
        # Debian's image can report 13 while Trivy reports 13.2. A more detailed
        # detector release is compatible; an unrelated major/distribution is not.
        version, scanned = release["version_id"], scan["os"]["Name"]
        if release["id"] != scan["os"]["Family"] or not (scanned == version or scanned.startswith(version + ".")):
            errors.append("OS_IDENTITY_MISMATCH")
        expected_packages = {(re.sub(r"[-_.]+", "-", package["name"]).lower(), package["version"])
                             for package in runtime["python_packages"]}
        if not expected_packages.issubset(scan["python_packages"]):
            errors.append("TRIVY_PYTHON_RUNTIME_COVERAGE_INCOMPLETE")
    for name, timestamp in (("TRIVY", scan["created_at"] if scan else None),
                            ("INVENTORY", runtime["collected_at"] if runtime else None)):
        if timestamp:
            age_seconds = (now - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds()
            if age_seconds > MAX_EVIDENCE_AGE_SECONDS:
                errors.append(f"{name}_EVIDENCE_EXPIRED")
            elif age_seconds < -MAX_FUTURE_CLOCK_SKEW_SECONDS:
                errors.append(f"{name}_EVIDENCE_IN_FUTURE")
    if runtime and rules:
        sqlite = runtime["sqlite"]
        for rule in rules:
            if sqlite["fts5_enabled"] and _version(sqlite["version"], "SQLITE_VERSION_UNKNOWN") < _version(
                    rule["affected_below"], "ADVISORY_VERSION_INVALID"):
                findings.append({"id": rule["id"], "source": "known_upstream_advisory", "severity": rule["policy_severity"],
                                 "package": "sqlite-runtime", "installed_version": sqlite["version"],
                                 "fixed_upstream_version": rule["affected_below"], "source_url": rule["source_url"],
                                 "condition": "upstream_version_below_fixed_and_fts5_enabled",
                                 "exploitability_assessed": False})
    failure_codes = set(errors)
    if any(row["source"] == "trivy" for row in findings):
        failure_codes.add("TRIVY_HIGH_OR_CRITICAL")
    if any(row["source"] == "known_upstream_advisory" for row in findings):
        failure_codes.add("UPSTREAM_ADVISORY_MATCH")
    allowed = not errors and not findings
    return {"schema_version": 1, "evaluated_at": now.isoformat().replace("+00:00", "Z"),
            "status": "passed" if allowed else ("blocked" if findings else "unverified"),
            "release_allowed": allowed, "image_id": runtime["image_id"] if runtime else None,
            "scan_created_at": scan["created_at"] if scan else None,
            "failure_codes": sorted(failure_codes), "unverified_codes": sorted(set(errors)),
            "blocked_findings": findings, "blocked_finding_count": len(findings),
            "unverified_findings": scan["unverified_findings"] if scan else [],
            "unverified_finding_count": len(scan["unverified_findings"]) if scan else 0,
            "blocked_unique_vulnerability_count": len({row["id"] for row in findings}),
            "trivy_severity_counts": scan["severity_counts"] if scan else None,
            "evidence_age_limit_seconds": MAX_EVIDENCE_AGE_SECONDS,
            "future_clock_skew_limit_seconds": MAX_FUTURE_CLOCK_SKEW_SECONDS,
            "input_sha256": input_sha256 or {},
            "scope": "Trusted CI inputs; caller-supplied image identity; no exploitability or profitability assurance."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trivy", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--advisories", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    values, hashes, input_errors = {}, {}, []
    for name in ("trivy", "inventory", "advisories"):
        try:
            values[name], hashes[name] = read_evidence(getattr(args, name))
        except EvidenceError as exc:
            values[name] = None
            input_errors.append(f"{name.upper()}_{exc.code}")
    report = evaluate(values["trivy"], values["inventory"], values["advisories"], input_sha256=hashes)
    if input_errors:
        report["unverified_codes"] = sorted(set(report["unverified_codes"] + input_errors))
        report["failure_codes"] = sorted(set(report["failure_codes"] + input_errors))
        report["release_allowed"] = False
        if report["status"] == "passed":
            report["status"] = "unverified"
    raw = json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
    try:
        if args.output:
            # Do not replace old release evidence, including symlinks.
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(raw)
        print(raw, end="")
    except OSError:
        parser.error("Cannot create gate output; existing evidence is never overwritten")
    return 0 if report["release_allowed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
