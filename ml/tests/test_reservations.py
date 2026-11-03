"""Cross-entrypoint training exclusions use the same real filesystem lock."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
import tempfile
from threading import Barrier
from types import SimpleNamespace
import unittest

from ml.registry import Registry, RegistryError, _write, digest
from ml.reservations import (LEDGER_PATH, ReservationConflict, check_available,
                             read_ledger, reserve, validate_ledger)


def dataset(start="2026-10-01", seed=1):
    first = date.fromisoformat(start)
    captures = tuple(SimpleNamespace(capture_id=f"capture-{index}", symbol="AAPL",
                     session_date=(first + timedelta(days=index)).isoformat(), itch_sha256=digest([seed, index]))
                     for index in range(10))
    return SimpleNamespace(captures=captures, dataset_sha256=digest([start, seed]), manifest_sha256=digest([seed, start]))


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.registry = Registry(Path(self.temporary.name) / "store")
        self.data = dataset()

    def tearDown(self):
        self.temporary.cleanup()

    def test_parallel_pipeline_and_retrain_reserve_only_one_request(self):
        barrier = Barrier(2)
        def claim(owner):
            registry = Registry(self.registry.root, create=False)
            barrier.wait(timeout=5)
            with registry._lock():
                try:
                    reserve(registry, self.data, owner=owner, request_sha256=digest(owner))
                    return "reserved"
                except ReservationConflict:
                    return "blocked"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, ("pipeline", "retrain")))
        self.assertEqual(sorted(outcomes), ["blocked", "reserved"])
        self.assertEqual(len(read_ledger(self.registry)["requests"]), 1)

    def test_same_request_resume_preserves_original_reservation(self):
        with self.registry._lock():
            first = reserve(self.registry, self.data, owner="pipeline", request_sha256="a" * 64, now_ns=100)
            resumed = reserve(self.registry, self.data, owner="pipeline", request_sha256="a" * 64,
                              allow_existing=True, now_ns=200)
            self.assertEqual(resumed, first)
            with self.assertRaises(ReservationConflict):
                reserve(self.registry, self.data, owner="pipeline", request_sha256="a" * 64)
            changed = deepcopy(self.data)
            changed.manifest_sha256 = "b" * 64
            with self.assertRaises(ReservationConflict):
                reserve(self.registry, changed, owner="pipeline", request_sha256="a" * 64, allow_existing=True)

    def test_different_source_same_session_dates_and_older_test_are_blocked(self):
        with self.registry._lock():
            reserve(self.registry, self.data, owner="pipeline", request_sha256="a" * 64)
            for alternate in (dataset(seed=2), dataset(start="2026-09-01", seed=3)):
                with self.subTest(start=alternate.captures[0].session_date):
                    with self.assertRaises(ReservationConflict):
                        check_available(self.registry, alternate)
            check_available(self.registry, dataset(start="2026-11-01", seed=4))

    def test_corrupt_or_legacy_ledgers_never_enable_training(self):
        with self.registry._lock():
            _write(self.registry.root / "retrain/attempts.json", {"schema_version": 1, "datasets": {}})
            with self.assertRaisesRegex(RegistryError, "explicit"):
                check_available(self.registry, self.data)
        self.assertFalse((self.registry.root / LEDGER_PATH).exists())

    def test_duplicate_holdouts_and_unbound_keys_fail_backup_validation(self):
        with self.registry._lock():
            reserve(self.registry, self.data, owner="pipeline", request_sha256="a" * 64)
        original = read_ledger(self.registry)
        corrupted = deepcopy(original)
        duplicate = deepcopy(next(iter(corrupted["requests"].values())))
        duplicate.update(owner="retrain", request_sha256="b" * 64)
        corrupted["requests"]["retrain:" + "b" * 64] = duplicate
        with self.assertRaisesRegex(RegistryError, "duplicate"):
            validate_ledger(corrupted)
        corrupted = deepcopy(original)
        next(iter(corrupted["requests"].values()))["holdout_keys"] = []
        with self.assertRaisesRegex(RegistryError, "inconsistent"):
            validate_ledger(corrupted)
