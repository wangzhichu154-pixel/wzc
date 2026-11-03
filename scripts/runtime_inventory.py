#!/usr/bin/env python3
"""Collect bounded, non-secret runtime versions inside the image being scanned.

The caller must supply the exact `docker image inspect --format '{{.Id}}'` value
and run this script in that pristine image. A container cannot attest its own
image digest: the recorded identity is a caller assertion, checked against Trivy
by security_gate.py. No environment variables, credentials or filesystem paths
from installed package metadata are exported.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess

IMAGE_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
PACKAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
PACKAGE_VERSION_RE = re.compile(r"[0-9][A-Za-z0-9.!+_-]{0,127}\Z")
NUMERIC_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}\Z")


def _utc_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sqlite_inventory(errors):
    import sqlite3

    output = {"version": None, "compile_options": None, "fts5_compile_option": None,
              "fts5_probe": "error", "fts5_enabled": None}
    try:
        with sqlite3.connect(":memory:") as connection:
            connection.enable_load_extension(False)
            version = connection.execute("SELECT sqlite_version()").fetchone()[0]
            options = sorted(row[0] for row in connection.execute("PRAGMA compile_options"))
            if version != sqlite3.sqlite_version or not NUMERIC_VERSION_RE.fullmatch(version):
                raise ValueError("SQLITE_VERSION_UNKNOWN")
            if not options or len(options) > 1024 or any(not isinstance(x, str) or len(x) > 256 for x in options):
                raise ValueError("SQLITE_COMPILE_OPTIONS_UNKNOWN")
            compile_enabled = "ENABLE_FTS5" in options or "ENABLE_FTS5=1" in options
            output.update(version=version, compile_options=options, fts5_compile_option=compile_enabled)
            try:
                # Capability check only, never an exploit or untrusted SQL input.
                connection.execute("CREATE VIRTUAL TABLE temp.security_capability_probe USING fts5(content)")
                connection.execute("DROP TABLE temp.security_capability_probe")
                output.update(fts5_probe="available", fts5_enabled=True)
                if not compile_enabled:
                    errors.append("SQLITE_CAPABILITY_METADATA_MISMATCH")
            except sqlite3.OperationalError as exc:
                if str(exc).lower() == "no such module: fts5" and not compile_enabled:
                    output.update(fts5_probe="unavailable", fts5_enabled=False)
                else:
                    errors.append("SQLITE_FTS5_PROBE_UNKNOWN")
    except (OSError, ValueError, AttributeError, sqlite3.Error):
        errors.append("SQLITE_RUNTIME_UNVERIFIED")
    return output


def _libc_inventory(errors):
    try:
        value = os.confstr("CS_GNU_LIBC_VERSION")
    except (ValueError, OSError):
        value = None
    if value:
        parts = value.split()
        if len(parts) == 2 and parts[0] == "glibc" and NUMERIC_VERSION_RE.fullmatch(parts[1]):
            return {"family": "glibc", "version": parts[1], "method": "confstr"}
    # musl's own loader prints its version on --version. Only a fixed system
    # location is used; no request-provided executable or shell is evaluated.
    loaders = sorted(Path("/lib").glob("ld-musl-*.so.1"))
    if len(loaders) == 1:
        try:
            result = subprocess.run([str(loaders[0]), "--version"], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=3, check=False,
                                    env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
            output = (result.stdout + result.stderr)[:16384]
            match = re.search(r"musl libc[^\n]*\nVersion ([0-9]+\.[0-9]+\.[0-9]+)(?:\n|$)", output)
            if match:
                return {"family": "musl", "version": match.group(1), "method": "system_loader_version"}
        except (OSError, subprocess.SubprocessError):
            pass
    errors.append("LIBC_VERSION_UNKNOWN")
    return {"family": None, "version": None, "method": "unavailable"}


def collect_inventory(image_id):
    if not isinstance(image_id, str) or not IMAGE_RE.fullmatch(image_id):
        raise ValueError("An exact sha256 image ID from docker inspect is required")
    errors = []
    try:
        release = platform.freedesktop_os_release()
        os_release = {"id": release.get("ID"), "version_id": release.get("VERSION_ID"),
                      "name": release.get("PRETTY_NAME", release.get("NAME"))}
        if any(not isinstance(value, str) or not 1 <= len(value) <= 256 for value in os_release.values()):
            raise ValueError("OS_RELEASE_UNKNOWN")
    except (OSError, ValueError):
        os_release = {"id": None, "version_id": None, "name": None}
        errors.append("OS_RELEASE_UNKNOWN")
    try:
        import ssl
        openssl = {"version": ssl.OPENSSL_VERSION, "version_info": list(ssl.OPENSSL_VERSION_INFO)}
    except (ImportError, AttributeError):
        openssl = {"version": None, "version_info": None}
        errors.append("OPENSSL_VERSION_UNKNOWN")
    try:
        sqlite = _sqlite_inventory(errors)
    except ImportError:
        sqlite = {"version": None, "compile_options": None, "fts5_compile_option": None,
                  "fts5_probe": "error", "fts5_enabled": None}
        errors.append("SQLITE_RUNTIME_UNVERIFIED")
    packages, names = [], set()
    try:
        for distribution in importlib.metadata.distributions():
            name, version = distribution.metadata.get("Name"), distribution.version
            if (not isinstance(name, str) or not PACKAGE_RE.fullmatch(name)
                    or not isinstance(version, str) or not PACKAGE_VERSION_RE.fullmatch(version)
                    or len(packages) >= 4096):
                raise ValueError("RUNTIME_PACKAGE_METADATA_UNKNOWN")
            normalized = re.sub(r"[-_.]+", "-", name).lower()
            if normalized in names:
                raise ValueError("RUNTIME_PACKAGE_IDENTITY_DUPLICATED")
            names.add(normalized)
            packages.append({"name": name, "version": version})
    except (OSError, ValueError, UnicodeError):
        errors.append("RUNTIME_PACKAGE_METADATA_UNKNOWN")
    packages.sort(key=lambda row: row["name"].lower())
    application_version = None
    try:
        version = Path("/app/VERSION").read_text(encoding="ascii")[:128].strip()
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version):
            application_version = version
        else:
            errors.append("APPLICATION_VERSION_INVALID")
    except FileNotFoundError:
        pass  # A base-image probe legitimately has no application VERSION.
    except (OSError, UnicodeError):
        errors.append("APPLICATION_VERSION_UNREADABLE")
    libc = _libc_inventory(errors)
    return {"schema_version": 1, "image_id": image_id,
            "image_identity_source": "caller_supplied_docker_inspect",
            "collected_at": _utc_now(), "application_version": application_version,
            "python": {"implementation": platform.python_implementation(), "version": platform.python_version()},
            "sqlite": sqlite, "openssl": openssl, "libc": libc, "os_release": os_release,
            "python_packages": packages, "errors": sorted(set(errors)),
            "status": "verified" if not errors else "unverified"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not IMAGE_RE.fullmatch(args.image_id):
        parser.error("--image-id must be the exact sha256 image ID from docker inspect")
    try:
        result = collect_inventory(args.image_id)
        raw = json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(raw)
        else:
            print(raw, end="")
        return 0 if result["status"] == "verified" else 1
    except (OSError, ValueError):
        print(json.dumps({"status": "unverified", "errors": ["INVENTORY_COLLECTION_FAILED"]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
