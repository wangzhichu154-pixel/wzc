"""Bounded, consistent registry archives and inactive, verified recovery.

This is a local operator tool. It never activates a model, changes ownership,
launches a broker, or recovers external market datasets. Archives contain private
model and approval evidence and require separately managed encryption/storage.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from datetime import date
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile
import time
import zlib

from .model import canonical_bytes
from .registry import (HASH_RE, STAGES, VERSION_RE, Registry, RegistryError,
                       _attempt_summary, _decode, _open, _path, _sync_dir,
                       _trusted_directory, digest, read_json)

MANIFEST_NAME = "BACKUP_MANIFEST.json"
FORMAT = "vector-registry-backup-v1"
PART_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}\Z")
RESTORE_DIRECTORY = "recovery-quarantine"
AUDIT_NAME = "recovery-audit.json"


@dataclass(frozen=True)
class BackupLimits:
    max_files: int = 4096
    max_file_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_archive_bytes: int = 300 * 1024 * 1024
    max_manifest_bytes: int = 2 * 1024 * 1024

    def validate(self):
        for value in self.__dict__.values():
            if type(value) is not int or value <= 0:
                raise RegistryError("Backup bounds must be positive integers")
        if (self.max_files > 10000 or self.max_total_bytes > 1024**3
                or self.max_file_bytes > self.max_total_bytes or self.max_archive_bytes > 2 * 1024**3
                or self.max_manifest_bytes > 16 * 1024**2):
            raise RegistryError("Backup bounds exceed supported limits")
        return self


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise RegistryError("Invalid archive path")
    parts = value.split("/")
    if any(not PART_RE.fullmatch(part) for part in parts):
        raise RegistryError("Archive traversal or unsupported filename")
    if str(PurePosixPath(value)) != value:
        raise RegistryError("Archive path must be canonical")
    return value


def _regular(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid not in {0, os.geteuid()} or stat.S_IMODE(info.st_mode) & 0o022):
        raise RegistryError("Snapshot requires trusted regular files without hard links")


def _directory(info):
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise RegistryError("Snapshot requires trusted non-writable directories")


@contextmanager
def _file(path):
    descriptor = _open(path)
    try:
        _regular(os.fstat(descriptor))
        yield descriptor
    finally:
        os.close(descriptor)


def _inventory(root, limits):
    """Walk relative openat descriptors; reject symlinks, FIFOs and partial writes."""
    files, directories, total = [], [], 0
    def visit(descriptor, prefix):
        nonlocal total
        _directory(os.fstat(descriptor))
        with os.scandir(descriptor) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            if not prefix and name == ".writer.lock":
                continue
            relative = _relative(f"{prefix}/{name}" if prefix else name)
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
                if len(directories) > limits.max_files:
                    raise RegistryError("Too many registry directories")
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    visit(child, relative)
                finally:
                    os.close(child)
            else:
                _regular(info)
                if len(files) >= limits.max_files or info.st_size > limits.max_file_bytes:
                    raise RegistryError("Registry file count or size exceeds backup limit")
                total += info.st_size
                if total > limits.max_total_bytes:
                    raise RegistryError("Registry exceeds cumulative backup byte limit")
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                try:
                    actual = os.fstat(child)
                    _regular(actual)
                    if (actual.st_dev, actual.st_ino, actual.st_size) != (info.st_dev, info.st_ino, info.st_size):
                        raise RegistryError("Registry changed during snapshot")
                    checksum = hashlib.sha256()
                    observed = 0
                    while chunk := os.read(child, 1024 * 1024):
                        observed += len(chunk)
                        if observed > limits.max_file_bytes or observed > info.st_size:
                            raise RegistryError("Registry file grew during snapshot")
                        checksum.update(chunk)
                    if observed != info.st_size:
                        raise RegistryError("Registry file changed during snapshot")
                    files.append({"path": relative, "bytes": observed, "sha256": checksum.hexdigest()})
                finally:
                    os.close(child)
    descriptor = _open(root, directory=True)
    try:
        visit(descriptor, "")
    finally:
        os.close(descriptor)
    return sorted(files, key=lambda item: item["path"]), sorted(directories), total


def _pointer(registry, value, mode):
    if (not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("mode") != mode
            or type(value.get("generation")) is not int or value["generation"] < 1):
        raise RegistryError("Invalid activation/history pointer")
    manifest = registry._manifest(value.get("version"))
    for key in ("model_sha256", "policy_sha256", "gates_sha256"):
        if value.get(key) != manifest[key]:
            raise RegistryError("Activation/history pointer hash mismatch")
    registry._event(manifest["version"], "evaluated")
    registry._event(manifest["version"], "simulation_validated")
    if mode == "paper":
        if manifest["provenance_kind"] != "licensed_market" or not manifest["authorized_use_attested"]:
            raise RegistryError("Paper pointer has unapproved provenance")
        _, approval = registry._event(manifest["version"], "operator_paper_approved")
        if approval.get("approved") is not True or approval.get("action") != "approve_paper":
            raise RegistryError("Paper pointer lacks explicit approval evidence")
    return manifest


def _attempt(registry, version, value, kind):
    manifest = registry._manifest(version)
    if (value.get("schema_version") != 1 or value.get("version") != version or value.get("kind") != kind
            or value.get("model_sha256") != manifest["model_sha256"] or type(value.get("accepted")) is not bool
            or not HASH_RE.fullmatch(str(value.get("report_sha256", "")))):
        raise RegistryError("Invalid evaluation attempt binding")
    report = read_json(registry.root / "evidence" / f"{value['report_sha256']}.json")
    if digest(report) != value["report_sha256"]:
        raise RegistryError("Evaluation attempt evidence hash mismatch")


def _holdout_key(key):
    if not isinstance(key, str):
        return False
    if key.startswith("sha256:"):
        return bool(HASH_RE.fullmatch(key[7:]))
    match = re.fullmatch(r"session:([A-Z][A-Z0-9.]{0,7}):(\d{4}-\d{2}-\d{2})", key)
    if not match:
        return False
    try:
        return date.fromisoformat(match[2]).isoformat() == match[2]
    except ValueError:
        return False


def validate_store(store):
    """Check every version/evidence, including versions pruned from the UI index.

    This validates recovery integrity, not current model profitability, freshness
    or execution permission. Old/expired evidence remains legitimate history.
    """
    registry = Registry(store, create=False)
    versions, consumed = [], {}
    directory = registry.root / "versions"
    for child in sorted(directory.iterdir()):
        if not VERSION_RE.fullmatch(child.name) or not child.is_dir() or child.is_symlink():
            raise RegistryError("Unexpected or partial model version directory")
        manifest = registry._manifest(child.name)
        model = registry._check_model(child.name, manifest)
        versions.append(child.name)
        if (registry.root / "events" / child.name / "evaluated.json").exists():
            _, report = registry._event(child.name, "evaluated")
            experiment = digest({key: value for key, value in model.to_dict().items() if key != "model_version"})
            holdout = report.get("holdout", {})
            if holdout.get("experiment_sha256") != experiment or holdout.get("locked") is not True:
                raise RegistryError("Evaluated model is not bound to its consumed holdout")
            sources = holdout.get("test_source_sha256")
            if not isinstance(sources, list) or not sources or any(not HASH_RE.fullmatch(str(value)) for value in sources):
                raise RegistryError("Evaluated model lacks consumed test source hashes")
            sessions = model.training_metadata.get("split", {}).get("test", {}).get("sessions")
            captures = {capture["capture_id"]: capture for capture in manifest["captures"]}
            if (not isinstance(sessions, list) or not sessions
                    or report.get("split") != model.training_metadata.get("split")
                    or any(item.get("capture_id") not in captures
                           or item.get("session_date") != captures[item["capture_id"]]["session_date"]
                           for item in sessions)
                    or set(sources) != {captures[item["capture_id"]]["itch_sha256"] for item in sessions}
                    or len(sources) != len(set(sources))):
                raise RegistryError("Evaluated holdout does not match the immutable model test partition")
            keys = ["sha256:" + value for value in sources]
            keys += ["session:" + manifest["symbol"] + ":" + item["session_date"]
                     for item in sessions]
            for key in keys:
                if key in consumed and consumed[key] != experiment:
                    raise RegistryError("Stored evaluations reused an independent test holdout")
                consumed[key] = experiment
    for child in sorted((registry.root / "policies").iterdir()):
        if child.suffix != ".json" or not HASH_RE.fullmatch(child.stem):
            raise RegistryError("Invalid policy filename")
        registry._policy(child.stem)
    evidence_count = 0
    for child in sorted((registry.root / "evidence").iterdir()):
        if child.suffix != ".json" or not HASH_RE.fullmatch(child.stem) or digest(read_json(child)) != child.stem:
            raise RegistryError("Evidence content does not match its identity")
        evidence_count += 1
    for child in sorted((registry.root / "events").iterdir()):
        if child.name not in versions or not child.is_dir() or child.is_symlink():
            raise RegistryError("Event references an unknown model")
        for event in child.iterdir():
            if event.name in {stage + ".json" for stage in STAGES[1:]}:
                registry._event(child.name, event.stem)
            elif event.name in ("latest-training-attempt.json", "latest-simulation-attempt.json"):
                kind = event.name.split("-")[1]
                _attempt_summary(registry, child.name, kind)
                value = read_json(event)
                _attempt(registry, child.name, value, kind)
                if read_json(child / f"{kind}-attempt-{digest(value)}.json") != value:
                    raise RegistryError("Latest attempt has no identical immutable record")
            elif match := re.fullmatch(r"(training|simulation)-attempt-([0-9a-f]{64})\.json", event.name):
                value = read_json(event)
                if digest(value) != match[2]:
                    raise RegistryError("Immutable attempt content hash mismatch")
                _attempt(registry, child.name, value, match[1])
            else:
                raise RegistryError("Unsupported event file; recovery must preserve a known protocol")
    index_path = registry.root / "index.json"
    index = read_json(index_path) if index_path.exists() else {"schema_version": 1, "models": []}
    if index.get("schema_version") != 1 or not isinstance(index.get("models"), list):
        raise RegistryError("Invalid registry index")
    indexed = []
    for entry in index["models"]:
        if (not isinstance(entry, dict) or entry.get("version") not in versions
                or entry.get("version") in indexed or entry.get("stage") not in STAGES):
            raise RegistryError("Model index does not match stored versions")
        indexed.append(entry["version"])
        if entry["stage"] != "candidate":
            registry._event(entry["version"], entry["stage"])
    active = {}
    for mode in ("simulation", "paper"):
        path = registry.root / f"active-{mode}.json"
        if path.exists() or path.is_symlink():
            value = read_json(path)
            _pointer(registry, value, mode)
            history = read_json(registry.root / "history" / f"{mode}-{value['generation']:012d}.json")
            if history != value:
                raise RegistryError("Active pointer has no identical immutable history")
            active[mode] = value["version"]
    if (registry.root / "active-live.json").exists() or (registry.root / "active-live.json").is_symlink():
        raise RegistryError("Live activation is unsupported")
    for child in (registry.root / "history").iterdir():
        match = re.fullmatch(r"(paper|simulation)-([0-9]{12})\.json", child.name)
        if not match:
            raise RegistryError("Invalid activation history filename")
        value = read_json(child)
        if value.get("generation") != int(match[2]):
            raise RegistryError("Activation history generation mismatch")
        _pointer(registry, value, match[1])
    ledger = registry.root / "holdouts.json"
    if consumed and not ledger.exists():
        raise RegistryError("Evaluated models require their consumed holdout ledger")
    if ledger.exists():
        value = read_json(ledger)
        if value.get("schema_version") != 1 or not isinstance(value.get("entries"), dict):
            raise RegistryError("Invalid holdout consumption ledger")
        if any(not _holdout_key(key) or not HASH_RE.fullmatch(str(identity))
               for key, identity in value["entries"].items()):
            raise RegistryError("Malformed consumed holdout identity")
        if any(value["entries"].get(key) != experiment for key, experiment in consumed.items()):
            raise RegistryError("Consumed holdout ledger omits or contradicts evaluated evidence")
    attempts = registry.root / "retrain" / "attempts.json"
    if attempts.exists():
        value = read_json(attempts)
        if value.get("schema_version") not in (1, 2) or not isinstance(value.get("datasets"), dict):
            raise RegistryError("Invalid retraining consumption ledger")
        for dataset, request in value["datasets"].items():
            if not HASH_RE.fullmatch(str(dataset)) or not re.fullmatch(r"[0-9a-f]{24}", str(request)):
                raise RegistryError("Malformed consumed dataset identity")
        if value["schema_version"] == 2:
            if not isinstance(value.get("holdout_keys"), dict):
                raise RegistryError("Missing retraining holdout reservations")
            for key, dataset in value["holdout_keys"].items():
                if not _holdout_key(key) or dataset not in value["datasets"]:
                    raise RegistryError("Invalid retraining holdout reservation")
    reservations = registry.root / "research" / "reservations.json"
    if reservations.exists() or reservations.is_symlink():
        from .reservations import validate_ledger
        validate_ledger(read_json(reservations))
    return {"versions_checked": len(versions), "versions": versions, "indexed_versions_checked": len(indexed),
            "unindexed_versions": sorted(set(versions) - set(indexed)), "evidence_checked": evidence_count,
            "active_before_recovery": active, "execution_authorized": False}


def _new_destination(path):
    path = _path(path)
    if path.exists() or path.is_symlink():
        raise RegistryError("Destination must not exist; no overwrite or in-place recovery")
    _trusted_directory(path.parent)
    return path


def _new_file(path, raw, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def _publish_directory(source, destination):
    """Linux RENAME_NOREPLACE keeps final publication atomic and non-overwriting."""
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise RegistryError("Atomic no-replace directory publication requires Linux renameat2")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        raise OSError(ctypes.get_errno(), "Cannot publish recovery without overwriting a destination")
    try:
        _sync_dir(destination.parent)
    except OSError as exc:
        # The directory is already visible. Do not delete a successfully
        # published inactive recovery just because durability is unconfirmed.
        raise RegistryError("Recovery was published inactive, but parent directory fsync failed; "
                            "preserve the destination and verify storage durability before use") from exc


def _sync_tree(directory):
    # fsync(root) alone does not make nested directory entries crash durable.
    directories = [path for path in directory.rglob("*") if path.is_dir()]
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _sync_dir(path)
    _sync_dir(directory)


def backup_store(store, output, *, limits=None):
    limits = (limits or BackupLimits()).validate()
    registry = Registry(store, create=False)
    destination = _new_destination(output)
    if destination.is_relative_to(registry.root):
        raise RegistryError("Archive destination must be outside the source registry")
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-backup-", dir=destination.parent)
    os.close(descriptor)
    try:
        with registry._lock():
            files, directories, total = _inventory(registry.root, limits)
            validation = validate_store(registry.root)
            manifest = {"schema_version": 1, "format": FORMAT, "created_at_ns": time.time_ns(),
                        "source_validation": validation, "files": files, "directories": directories,
                        "total_bytes": total, "includes_registry_run_logs": True,
                        "external_pipeline_runs_included": False,
                        "external_datasets_included": False, "execution_authorized": False}
            raw_manifest = canonical_bytes(manifest) + b"\n"
            if len(raw_manifest) > limits.max_manifest_bytes:
                raise RegistryError("Backup manifest exceeds limit")
            with tarfile.open(temporary, "w:gz", format=tarfile.USTAR_FORMAT) as archive:
                info = tarfile.TarInfo(MANIFEST_NAME)
                info.size, info.mode = len(raw_manifest), 0o600
                archive.addfile(info, io.BytesIO(raw_manifest))
                for item in files:
                    with _file(registry.root / item["path"]) as descriptor:
                        with os.fdopen(os.dup(descriptor), "rb") as source:
                            raw = source.read(limits.max_file_bytes + 1)
                        if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                            raise RegistryError("Registry changed after snapshot inventory")
                    info = tarfile.TarInfo("store/" + item["path"])
                    info.size, info.mode = len(raw), 0o600
                    archive.addfile(info, io.BytesIO(raw))
            # A legitimate writer owns this same lock. This also detects a source
            # changed outside the normal writer protocol during our read.
            after, after_directories, _ = _inventory(registry.root, limits)
            if files != after or directories != after_directories:
                raise RegistryError("Registry changed during archive construction")
        if Path(temporary).stat().st_size > limits.max_archive_bytes:
            raise RegistryError("Compressed archive exceeds size limit")
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        os.link(temporary, destination, follow_symlinks=False)
        os.unlink(temporary)
        _sync_dir(destination.parent)
        return {"schema_version": 1, "state": "backed_up", "archive_sha256": _hash_file(destination),
                "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(), "files": len(files),
                "total_bytes": total, "validation": validation, "execution_authorized": False}
    finally:
        Path(temporary).unlink(missing_ok=True)


def _hash_file(path):
    checksum = hashlib.sha256()
    with _file(path) as descriptor:
        while chunk := os.read(descriptor, 1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def _hash_descriptor(descriptor, limit):
    checksum, offset = hashlib.sha256(), 0
    while chunk := os.pread(descriptor, min(1024 * 1024, limit - offset + 1), offset):
        offset += len(chunk)
        if offset > limit:
            raise RegistryError("Open archive grew beyond its byte limit")
        checksum.update(chunk)
    return checksum.hexdigest()


def _manifest(raw, limits):
    value = _decode(raw)
    required = {"schema_version", "format", "created_at_ns", "source_validation", "files", "directories",
                "total_bytes", "includes_registry_run_logs", "external_pipeline_runs_included",
                "external_datasets_included", "execution_authorized"}
    if (set(value) != required or value["schema_version"] != 1 or value["format"] != FORMAT
            or type(value["schema_version"]) is not int or type(value["created_at_ns"]) is not int
            or value["created_at_ns"] <= 0
            or value["execution_authorized"] is not False or value["external_datasets_included"] is not False
            or value["includes_registry_run_logs"] is not True or value["external_pipeline_runs_included"] is not False):
        raise RegistryError("Unsupported backup manifest schema")
    files, directories = value["files"], value["directories"]
    if (not isinstance(files, list) or len(files) > limits.max_files
            or not isinstance(directories, list) or len(directories) > limits.max_files):
        raise RegistryError("Archive member count exceeds bound")
    names, total = set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise RegistryError("Invalid file manifest")
        name = _relative(item["path"])
        if name in names or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= limits.max_file_bytes:
            raise RegistryError("Duplicate or oversized file manifest")
        if not HASH_RE.fullmatch(str(item["sha256"])):
            raise RegistryError("Invalid file content digest")
        names.add(name)
        total += item["bytes"]
    if total > limits.max_total_bytes or type(value["total_bytes"]) is not int or total != value["total_bytes"]:
        raise RegistryError("Archive total byte bound or accounting failed")
    directory_names = set()
    for name in directories:
        name = _relative(name)
        if name in names or name in directory_names:
            raise RegistryError("Duplicate or conflicting archive directory")
        directory_names.add(name)
    for name in names | directory_names:
        parent = PurePosixPath(name).parent
        if str(parent) != "." and str(parent) not in directory_names:
            raise RegistryError("Archive omits a required parent directory")
    return value


def _member(member, *, name=None, max_bytes):
    if (not member.isfile() or member.issparse() or member.linkname
            or member.size < 0 or member.size > max_bytes or name is not None and member.name != name):
        raise RegistryError("Archive accepts bounded regular members only")
    if member.pax_headers:
        raise RegistryError("PAX extensions are not part of the supported archive format")


def _archive_records(stream, limits):
    """Parse only our USTAR regular-file subset before allocating any payload.

    General tar readers consume PAX/GNU extension bodies while interpreting the
    next header. Rejecting their TarInfo afterwards is too late for a gzip bomb.
    """
    with gzip.GzipFile(fileobj=stream, mode="rb") as archive:
        count, payload_bytes = 0, 0
        while True:
            header = archive.read(512)
            if len(header) != 512:
                raise RegistryError("Truncated archive header")
            if header == b"\0" * 512:
                if archive.read(512) != b"\0" * 512:
                    raise RegistryError("Archive must end with two zero blocks")
                tail = archive.read(10241)
                if len(tail) > 10240 or tail.strip(b"\0"):
                    raise RegistryError("Unexpected or unbounded trailing archive data")
                break
            if header[257:265] != b"ustar\00000":
                raise RegistryError("Only the bounded USTAR backup format is supported")
            member = tarfile.TarInfo.frombuf(header, encoding="utf-8", errors="strict")
            bound = limits.max_manifest_bytes if count == 0 else limits.max_file_bytes
            _member(member, max_bytes=bound)
            count += 1
            payload_bytes += member.size
            if count > limits.max_files + 1 or payload_bytes > limits.max_total_bytes + limits.max_manifest_bytes:
                raise RegistryError("Archive cumulative allocation bound exceeded")
            raw = archive.read(member.size)
            padding = archive.read((-member.size) % 512)
            if len(raw) != member.size or len(padding) != (-member.size) % 512 or padding.strip(b"\0"):
                raise RegistryError("Truncated payload or invalid tar padding")
            yield member, raw


def restore_store(archive_path, destination, *, limits=None, expected_reader_uid=None, read_only=False,
                  expected_sha256=None):
    limits = (limits or BackupLimits()).validate()
    if expected_reader_uid is not None and (type(expected_reader_uid) is not int or expected_reader_uid != os.geteuid()):
        raise RegistryError("Run as the provisioned reader UID; this tool never sudo/chowns model data")
    archive_path, destination = _path(archive_path), _new_destination(destination)
    if archive_path.is_relative_to(destination):
        raise RegistryError("Archive cannot be inside its recovery destination")
    temporary = Path(tempfile.mkdtemp(prefix=".pending-recovery-", dir=destination.parent))
    try:
        with _file(archive_path) as descriptor:
            opened = os.fstat(descriptor)
            if opened.st_size > limits.max_archive_bytes:
                raise RegistryError("Archive exceeds compressed byte limit")
            source_hash = _hash_descriptor(descriptor, limits.max_archive_bytes)
            if expected_sha256 is not None and (not HASH_RE.fullmatch(str(expected_sha256)) or expected_sha256 != source_hash):
                raise RegistryError("Archive does not match the expected out-of-band SHA256")
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                records = _archive_records(stream, limits)
                first_record = next(records, None)
                if first_record is None:
                    raise RegistryError("Empty backup archive")
                first, raw = first_record
                _member(first, name=MANIFEST_NAME, max_bytes=limits.max_manifest_bytes)
                if len(raw) != first.size:
                    raise RegistryError("Truncated backup manifest")
                manifest = _manifest(raw, limits)
                expected = {item["path"]: item for item in manifest["files"]}
                for name in sorted(manifest["directories"], key=lambda path: (path.count("/"), path)):
                    (temporary / name).mkdir(mode=0o700)
                seen, total = set(), 0
                for member, raw in records:
                    _member(member, max_bytes=limits.max_file_bytes)
                    if not member.name.startswith("store/"):
                        raise RegistryError("Unexpected archive root member")
                    name = _relative(member.name[6:])
                    if name in seen or name not in expected or member.size != expected[name]["bytes"]:
                        raise RegistryError("Duplicate, unexpected or resized archive member")
                    seen.add(name)
                    total += member.size
                    if total > limits.max_total_bytes:
                        raise RegistryError("Archive exceeds cumulative extracted byte limit")
                    if len(raw) != member.size or hashlib.sha256(raw).hexdigest() != expected[name]["sha256"]:
                        raise RegistryError("Archive file digest mismatch or truncated content")
                    _new_file(temporary / name, raw)
                if seen != set(expected) or total != manifest["total_bytes"]:
                    raise RegistryError("Archive is missing manifested files")
            current = os.stat(archive_path, follow_symlinks=False)
            if ((current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                    or _hash_descriptor(descriptor, limits.max_archive_bytes) != source_hash):
                raise RegistryError("Archive changed during recovery")
        validation = validate_store(temporary)
        if validation != manifest["source_validation"]:
            raise RegistryError("Recovered registry does not match source validation")
        # Active pointers never exist at the public destination. Keep their exact
        # original bytes only under a path no execution loader will recognize.
        quarantined = []
        recovery = temporary / RESTORE_DIRECTORY / hashlib.sha256(raw_manifest_bytes(manifest)).hexdigest()
        recovery.mkdir(parents=True, mode=0o700)
        for mode in ("simulation", "paper"):
            path = temporary / f"active-{mode}.json"
            if path.exists():
                quarantined.append({"mode": mode, "version": validation["active_before_recovery"][mode],
                                    "sha256": _hash_file(path)})
                os.rename(path, recovery / path.name)
        audit = {"schema_version": 1, "state": "restored_inactive", "created_at_ns": time.time_ns(),
                 "archive_sha256": source_hash, "validation": validation, "quarantined_active_pointers": quarantined,
                 "execution_authorized": False, "automatic_activation": False, "external_data_recovery_required": True,
                 "external_pipeline_run_recovery_required": True,
                 "read_only_snapshot": read_only, "reader_uid": os.geteuid(),
                 "note": "All consumption ledgers preserved; external datasets and their local holdout locks require separate backup"}
        audit_path = temporary / AUDIT_NAME
        if audit_path.exists():
            os.rename(audit_path, recovery / "previous-recovery-audit.json")
        _new_file(audit_path, canonical_bytes(audit) + b"\n")
        _new_file(recovery / "source-backup-manifest.json", raw_manifest_bytes(manifest))
        if read_only:
            for path in temporary.rglob("*"):
                path.chmod(0o500 if path.is_dir() else 0o400)
            temporary.chmod(0o500)
        _sync_tree(temporary)
        _publish_directory(temporary, destination)
        return audit
    except (tarfile.TarError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise RegistryError("Malformed or truncated archive") from exc
    finally:
        if temporary.exists():
            # Only remove our private unpublished staging directory.
            for path in temporary.rglob("*"):
                if path.is_dir():
                    path.chmod(0o700)
            temporary.chmod(0o700)
            shutil.rmtree(temporary)


def raw_manifest_bytes(manifest):
    return canonical_bytes(manifest) + b"\n"


def snapshot_store(store, destination, *, expected_reader_uid=None, limits=None):
    destination = _new_destination(destination)
    if expected_reader_uid is not None and expected_reader_uid != os.geteuid():
        raise RegistryError("Snapshot must be created by the provisioned reader UID")
    with tempfile.TemporaryDirectory(prefix=".snapshot-archive-", dir=destination.parent) as work:
        archive = Path(work) / "snapshot.tar.gz"
        backup_store(store, archive, limits=limits)
        return restore_store(archive, destination, limits=limits, expected_reader_uid=expected_reader_uid, read_only=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--store", required=True)
    backup.add_argument("--output", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--destination", required=True)
    restore.add_argument("--expected-reader-uid", type=int)
    restore.add_argument("--expected-sha256")
    restore.add_argument("--read-only", action="store_true")
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--store", required=True)
    snapshot.add_argument("--destination", required=True)
    snapshot.add_argument("--expected-reader-uid", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = backup_store(args.store, args.output)
        elif args.command == "restore":
            result = restore_store(args.archive, args.destination, expected_reader_uid=args.expected_reader_uid,
                                   read_only=args.read_only, expected_sha256=args.expected_sha256)
        else:
            result = snapshot_store(args.store, args.destination, expected_reader_uid=args.expected_reader_uid)
        print(json.dumps(result, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        print(json.dumps({"state": "rejected", "message": str(exc), "execution_authorized": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
