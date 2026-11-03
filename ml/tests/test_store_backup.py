"""Actual numerical model snapshots plus adversarial archive recovery contracts."""
from __future__ import annotations

from dataclasses import replace
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from ml.fixtures import generate_dataset
from ml.model import canonical_bytes
from ml.prepare import prepare
from ml.registry import Registry, RegistryError, load_approved_model, read_status
from ml.store_backup import (AUDIT_NAME, BackupLimits, backup_store,
                             restore_store, snapshot_store, validate_store)
from ml.training import train_model

ROOT = Path(__file__).resolve().parents[2]
EXPORTER = ROOT / "build" / "trading-ml-replay"


def rewrite(source, target, change):
    """Only read a trusted test archive here; the production reader is stricter."""
    with tarfile.open(source, "r:gz") as archive:
        entries = [(member, archive.extractfile(member).read()) for member in archive.getmembers()]
    entries = change(entries)
    with tarfile.open(target, "w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for member, raw in entries:
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return target


@unittest.skipUnless(EXPORTER.is_file(), "Build the real C++ exporter before registry backup tests")
class RegistryBackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.store = cls.root / "store"
        registry = Registry(cls.store)
        admission = registry.install_policy(ROOT / "ml" / "admission.example.json")
        cls.versions = []
        for index, start in enumerate(("2026-03-02", "2026-04-01")):
            dataset = cls.root / f"dataset-{index}"
            generate_dataset(dataset, sessions=10, steps=128, seed=70 + index, start_date=start)
            manifest = prepare(dataset / "source-manifest.json", EXPORTER)
            run = cls.root / f"training-{index}"
            report = train_model(manifest, run)
            version = registry.register(run / "model.json", admission_sha256=admission, data_manifest=manifest)
            try:
                registry.evaluate(version, report)
            except RegistryError:
                # A trained candidate failing predeclared metrics still needs a backup.
                pass
            cls.versions.append(version)
        cls.archive = cls.root / "models.tar.gz"
        cls.backup = backup_store(cls.store, cls.archive)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(dir=self.root)
        self.path = Path(self.work.name)

    def tearDown(self):
        for item in self.path.rglob("*"):
            if item.is_dir():
                item.chmod(0o700)
        self.work.cleanup()

    def copy_store(self):
        return Path(shutil.copytree(self.store, self.path / "source"))

    def test_actual_two_trained_models_and_all_hashes_survive_isolated_restore(self):
        destination = self.path / "restored"
        result = restore_store(self.archive, destination, expected_sha256=self.backup["archive_sha256"])
        self.assertEqual(result["state"], "restored_inactive")
        self.assertFalse(result["execution_authorized"])
        self.assertTrue(result["external_data_recovery_required"])
        self.assertEqual(result["validation"]["versions_checked"], 2)
        self.assertEqual(set(validate_store(destination)["versions"]), set(self.versions))
        self.assertEqual(len(read_status(destination)["models"]), 2)
        self.assertFalse(list(destination.glob("active-*.json")))
        for source in self.store.rglob("*"):
            if source.is_file() and source.name != ".writer.lock":
                self.assertEqual(source.read_bytes(), (destination / source.relative_to(self.store)).read_bytes())
        self.assertEqual(json.loads((destination / AUDIT_NAME).read_text())["state"], "restored_inactive")

    def test_backup_checks_unindexed_versions_and_never_discards_them(self):
        source = self.copy_store()
        index = json.loads((source / "index.json").read_text())
        index["models"] = index["models"][:1]
        (source / "index.json").write_bytes(canonical_bytes(index))
        result = backup_store(source, self.path / "truncated-index.tar.gz")
        self.assertEqual(result["validation"]["versions_checked"], 2)
        self.assertEqual(len(result["validation"]["unindexed_versions"]), 1)

    def test_failed_unregistered_research_reservations_survive_restore_and_block_reuse(self):
        from ml.data import load_manifest
        from ml.reservations import LEDGER_PATH, ReservationConflict, check_available, reserve
        source = self.path / "reserved-store"
        registry = Registry(source)
        data = load_manifest(self.root / "dataset-0" / "manifest.json")
        # Reserve before training; an interrupted request may have no model.
        with registry._lock():
            reserve(registry, data, owner="pipeline", request_sha256="a" * 64)
        original = (source / LEDGER_PATH).read_bytes()
        archive = self.path / "reserved.tar.gz"
        backup_store(source, archive)
        destination = self.path / "reserved-restored"
        restore_store(archive, destination)
        self.assertEqual((destination / LEDGER_PATH).read_bytes(), original)
        recovered = Registry(destination, create=False)
        with recovered._lock(), self.assertRaises(ReservationConflict):
            check_available(recovered, data)
        broken = json.loads(original)
        next(iter(broken["requests"].values()))["holdout_keys"].pop()
        (source / LEDGER_PATH).write_bytes(canonical_bytes(broken))
        with self.assertRaisesRegex(RegistryError, "inconsistent holdouts"):
            backup_store(source, self.path / "broken-reservation.tar.gz")

    def test_corrupt_model_and_missing_consumption_ledger_are_rejected(self):
        source = self.copy_store()
        model = source / "versions" / self.versions[0] / "model.json"
        model.chmod(0o600)
        model.write_text("{}")
        with self.assertRaisesRegex(RegistryError, "hash"):
            backup_store(source, self.path / "corrupt.tar.gz")
        self.assertFalse((self.path / "corrupt.tar.gz").exists())
        # Use an explicit contract fixture to guarantee an evaluated stage;
        # this evidence is unit-only, not claimed as market profitability.
        from ml.tests.test_registry import candidate
        item = candidate(self.path / "evaluated-fixture")
        item["registry"].evaluate(item["version"], item["training"])
        (item["registry"].root / "holdouts.json").unlink()
        with self.assertRaisesRegex(RegistryError, "consumed holdout ledger"):
            backup_store(item["registry"].root, self.path / "missing-ledger.tar.gz")

    def test_partial_or_contradictory_consumption_ledger_is_rejected(self):
        from ml.tests.test_registry import candidate
        for mode in ("missing-key", "wrong-experiment", "malformed-key"):
            with self.subTest(mode=mode):
                item = candidate(self.path / mode)
                registry = item["registry"]
                registry.evaluate(item["version"], item["training"])
                path = registry.root / "holdouts.json"
                ledger = json.loads(path.read_text())
                key = next(iter(ledger["entries"]))
                if mode == "missing-key":
                    del ledger["entries"][key]
                elif mode == "wrong-experiment":
                    ledger["entries"][key] = "0" * 64
                else:
                    ledger["entries"]["sha256:invalid"] = "0" * 64
                path.write_bytes(canonical_bytes(ledger))
                with self.assertRaisesRegex(RegistryError, "holdout"):
                    backup_store(registry.root, self.path / (mode + ".tar.gz"))

    def test_self_consistent_forged_holdout_cannot_replace_model_test_identity(self):
        from ml.tests.test_registry import candidate
        from ml.registry import digest
        item = candidate(self.path / "forged-holdout")
        registry = item["registry"]
        registry.evaluate(item["version"], item["training"])
        event_path = registry.root / "events" / item["version"] / "evaluated.json"
        event = json.loads(event_path.read_text())
        report = json.loads((registry.root / "evidence" / f"{event['report_sha256']}.json").read_text())
        original = report["holdout"]["test_source_sha256"][0]
        forged = "f" * 64
        report["holdout"]["test_source_sha256"][0] = forged
        event["report_sha256"] = digest(report)
        (registry.root / "evidence" / f"{event['report_sha256']}.json").write_bytes(canonical_bytes(report))
        event_path.chmod(0o600)
        event_path.write_bytes(canonical_bytes(event))
        ledger_path = registry.root / "holdouts.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["entries"]["sha256:" + forged] = ledger["entries"].pop("sha256:" + original)
        ledger_path.write_bytes(canonical_bytes(ledger))
        with self.assertRaisesRegex(RegistryError, "immutable model test partition"):
            backup_store(registry.root, self.path / "forged-holdout.tar.gz")

    def test_source_symlink_fifo_hardlink_and_writable_files_are_rejected(self):
        for kind in ("symlink", "fifo", "hardlink", "publicly-writable"):
            with self.subTest(kind=kind):
                source = Path(shutil.copytree(self.store, self.path / kind))
                dangerous = source / "retrain" / "unsafe.log"
                if kind == "symlink":
                    dangerous.symlink_to(source / "index.json")
                elif kind == "fifo":
                    os.mkfifo(dangerous, 0o600)
                elif kind == "hardlink":
                    os.link(source / "index.json", dangerous)
                else:
                    dangerous.write_text("sensitive")
                    dangerous.chmod(0o666)
                with self.assertRaises(RegistryError):
                    backup_store(source, self.path / (kind + ".tar.gz"))

    def test_no_overwrite_and_target_parent_symlink(self):
        existing = self.path / "exists.tar.gz"
        existing.write_bytes(b"preserve")
        with self.assertRaises(RegistryError):
            backup_store(self.store, existing)
        self.assertEqual(existing.read_bytes(), b"preserve")
        destination = self.path / "existing-directory"
        destination.mkdir()
        with self.assertRaises(RegistryError):
            restore_store(self.archive, destination)
        link = self.path / "parent-link"
        link.symlink_to(self.path, target_is_directory=True)
        with self.assertRaises(RegistryError):
            restore_store(self.archive, link / "new")

    def test_file_count_size_and_aggregate_bounds_are_enforced(self):
        for limits in (replace(BackupLimits(), max_files=1),
                       replace(BackupLimits(), max_file_bytes=32),
                       replace(BackupLimits(), max_file_bytes=32, max_total_bytes=64)):
            with self.subTest(limits=limits), self.assertRaises(RegistryError):
                backup_store(self.store, self.path / "bounded.tar.gz", limits=limits)
        with self.assertRaises(RegistryError):
            restore_store(self.archive, self.path / "oversize", limits=replace(BackupLimits(), max_archive_bytes=10))

    def test_changed_content_missing_member_and_duplicate_member_fail_before_publish(self):
        def corrupt(entries):
            member, raw = entries[1]
            entries[1] = (member, b"x" + raw[1:])
            return entries
        cases = {"changed": corrupt, "missing": lambda entries: entries[:-1],
                 "duplicate": lambda entries: entries + [entries[1]]}
        for name, change in cases.items():
            with self.subTest(name=name):
                archive = rewrite(self.archive, self.path / (name + ".tar.gz"), change)
                destination = self.path / (name + "-destination")
                with self.assertRaises(RegistryError):
                    restore_store(archive, destination)
                self.assertFalse(destination.exists())
                self.assertFalse(list(self.path.glob(".pending-recovery-*")))

    def test_path_traversal_and_special_members_are_rejected(self):
        cases = [("traversal", tarfile.REGTYPE, "store/../../outside", ""),
                 ("absolute", tarfile.REGTYPE, "/tmp/outside", ""),
                 ("symlink", tarfile.SYMTYPE, "store/link", "../../outside"),
                 ("hardlink", tarfile.LNKTYPE, "store/link", "store/index.json"),
                 ("fifo", tarfile.FIFOTYPE, "store/fifo", ""),
                 ("sparse", tarfile.GNUTYPE_SPARSE, "store/sparse", "")]
        for name, kind, path, link in cases:
            with self.subTest(name=name):
                def malicious(entries):
                    member = tarfile.TarInfo(path)
                    member.type, member.linkname = kind, link
                    return entries + [(member, b"")]
                archive = rewrite(self.archive, self.path / (name + ".tar.gz"), malicious)
                with self.assertRaises(RegistryError):
                    restore_store(archive, self.path / (name + "-result"))

    def test_extension_header_bombs_are_rejected_before_reading_their_payload(self):
        for kind in (tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            with self.subTest(kind=kind):
                header = tarfile.TarInfo("extension")
                header.type, header.size = kind, 512 * 1024 * 1024
                archive = self.path / (kind.decode() + ".tar.gz")
                archive.write_bytes(gzip.compress(header.tobuf(format=tarfile.USTAR_FORMAT)))
                with self.assertRaises(RegistryError):
                    restore_store(archive, self.path / (kind.decode() + "-result"))

    def test_invalid_deflate_stream_is_a_structured_rejection(self):
        raw = bytearray(gzip.compress(b"archive payload"))
        raw[10] = 7  # Invalid DEFLATE block type, after the fixed gzip header.
        archive = self.path / "invalid-deflate.tar.gz"
        archive.write_bytes(raw)
        with self.assertRaisesRegex(RegistryError, "Malformed or truncated"):
            restore_store(archive, self.path / "invalid-deflate")
        self.assertFalse((self.path / "invalid-deflate").exists())
        self.assertFalse(list(self.path.glob(".pending-recovery-*")))

    def test_expected_digest_is_checked_against_the_consumed_open_descriptor(self):
        replacement = self.path / "archive.tar.gz"
        shutil.copyfile(self.archive, replacement)
        with self.assertRaisesRegex(RegistryError, "out-of-band"):
            restore_store(replacement, self.path / "bad-digest", expected_sha256="0" * 64)
        from ml import store_backup
        original = store_backup._hash_descriptor
        replaced = False
        def replace_path(descriptor, limit):
            nonlocal replaced
            actual = original(descriptor, limit)
            if not replaced:
                new = self.path / "new.tar.gz"
                shutil.copyfile(self.archive, new)
                os.replace(new, replacement)
                replaced = True
            return actual
        with patch("ml.store_backup._hash_descriptor", side_effect=replace_path):
            with self.assertRaisesRegex(RegistryError, "changed during"):
                restore_store(replacement, self.path / "race", expected_sha256=self.backup["archive_sha256"])
        self.assertFalse((self.path / "race").exists())

    def test_active_pointer_is_quarantined_and_restore_cannot_enable_simulation_or_paper(self):
        from ml.tests.test_registry import candidate
        item = candidate(self.path / "active-unit-contract")
        registry = item["registry"]
        registry.evaluate(item["version"], item["training"])
        registry.validate_simulation(item["version"], item["simulation"])
        registry.activate(item["version"], mode="simulation")
        active_bytes = (registry.root / "active-simulation.json").read_bytes()
        archive = self.path / "active.tar.gz"
        backup_store(registry.root, archive)
        destination = self.path / "inactive"
        audit = restore_store(archive, destination)
        self.assertEqual(audit["quarantined_active_pointers"][0]["mode"], "simulation")
        self.assertFalse((destination / "active-simulation.json").exists())
        self.assertEqual(next((destination / "recovery-quarantine").rglob("active-simulation.json")).read_bytes(), active_bytes)
        for mode in ("simulation", "paper", "live"):
            with self.assertRaises(RegistryError):
                load_approved_model(destination, mode)
        self.assertEqual((registry.root / "active-simulation.json").read_bytes(), active_bytes)

    def test_atomic_publication_failure_leaves_no_usable_target_or_staging_tree(self):
        destination = self.path / "publication-failure"
        with patch("ml.store_backup._publish_directory", side_effect=OSError("injected disk failure")):
            with self.assertRaises(OSError):
                restore_store(self.archive, destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.path.glob(".pending-recovery-*")))

    def test_nested_directories_are_synced_before_publication(self):
        from ml import store_backup
        synced = []
        original = store_backup._sync_dir
        def observe(path):
            synced.append(Path(path))
            original(path)
        destination = self.path / "durable-recovery"
        with patch("ml.store_backup._sync_dir", side_effect=observe):
            restore_store(self.archive, destination)
        roots = [path for path in synced if path.name.startswith(".pending-recovery-")]
        self.assertEqual(len(roots), 1)
        root = roots[0]
        children = [path for path in synced if path != root and path.is_relative_to(root)]
        self.assertTrue(any(path.parent.name == "versions" for path in children))
        self.assertTrue(any(path.parent.name == "recovery-quarantine" for path in children))
        self.assertTrue(all(synced.index(path) < synced.index(root) for path in children))
        self.assertEqual(synced[-1], destination.parent)
        with patch("ml.store_backup._sync_tree", side_effect=OSError("injected directory sync failure")):
            with self.assertRaises(OSError):
                restore_store(self.archive, self.path / "unsynced")
        self.assertFalse((self.path / "unsynced").exists())

    def test_post_publication_sync_failure_reports_existing_inactive_destination(self):
        from ml import store_backup
        original = store_backup._sync_dir
        def fail_parent(path):
            if Path(path) == self.path:
                raise OSError("injected parent sync failure")
            original(path)
        destination = self.path / "published-durability-unknown"
        with patch("ml.store_backup._sync_dir", side_effect=fail_parent):
            with self.assertRaisesRegex(RegistryError, "published inactive.*fsync failed"):
                restore_store(self.archive, destination)
        self.assertTrue(destination.is_dir())
        self.assertFalse(list(destination.glob("active-*.json")))
        self.assertEqual(validate_store(destination)["versions_checked"], 2)
        self.assertFalse(list(self.path.glob(".pending-recovery-*")))
        with self.assertRaisesRegex(RegistryError, "must not exist"):
            restore_store(self.archive, destination)

    def test_read_only_snapshot_does_not_mutate_original_or_change_ownership(self):
        original_index = (self.store / "index.json").read_bytes()
        destination = self.path / "web-snapshot"
        audit = snapshot_store(self.store, destination, expected_reader_uid=os.geteuid())
        self.assertTrue(audit["read_only_snapshot"])
        self.assertEqual(len(read_status(destination)["models"]), 2)
        self.assertEqual((destination.stat().st_mode & 0o777), 0o500)
        self.assertEqual((destination / "index.json").stat().st_mode & 0o777, 0o400)
        self.assertEqual(destination.stat().st_uid, os.geteuid())
        self.assertEqual(original_index, (self.store / "index.json").read_bytes())
        with self.assertRaisesRegex(RegistryError, "provisioned reader"):
            snapshot_store(self.store, self.path / "wrong-uid", expected_reader_uid=os.geteuid() + 1)


if __name__ == "__main__":
    unittest.main()
