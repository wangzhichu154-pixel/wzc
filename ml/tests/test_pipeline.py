"""Actual C++/Ridge research orchestration, plus interrupted-stage fault tests."""
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from ml.data import load_manifest, sha256_file
from ml.fixtures import generate_dataset
from ml.model import LinearModel, write_json
from ml.pipeline import PipelineError, _execute, _process_identity, default_config, run_pipeline
from ml.prepare import prepare
from ml.registry import Registry, _write, read_json
from ml.reservations import LEDGER_PATH, read_ledger

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "build/trading-ml-replay"


@unittest.skipUnless(ENGINE.is_file(), "Build the actual C++ replay engine before pipeline tests")
class PipelineIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        generate_dataset(cls.root / "dataset", sessions=10, steps=128, seed=17, start_date="2026-10-01")
        cls.manifest = prepare(cls.root / "dataset/source-manifest.json", ENGINE)
        cls.admission = cls.root / "admission.json"
        shutil.copyfile(ROOT / "ml/admission.example.json", cls.admission)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.directory = self.root / self._testMethodName
        self.directory.mkdir()
        self.run = self.directory / "run"
        self.store = self.directory / "store"
        self.config = default_config()

    def call(self, **overrides):
        values = {"manifest": self.manifest, "run_dir": self.run, "store": self.store,
                  "admission": self.admission, "binary": ENGINE, "config": self.config}
        values.update(overrides)
        return run_pipeline(**values)

    def test_actual_closed_loop_records_rejected_research_without_activating(self):
        result = self.call()
        self.assertEqual(result["state"], "completed")
        self.assertFalse(result["admission_passed"])
        self.assertFalse(result["automatic_promotion"])
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(result["stages"]["simulation_gate"]["state"], "rejected")
        self.assertFalse(list(self.store.glob("active-*.json")))
        for stage in result["stages"]:
            directory = self.run / "stages" / stage
            self.assertTrue((directory / "stdout.log").is_file())
            self.assertTrue((directory / "stderr.log").is_file())
            receipt, status = read_json(directory / "receipt.json"), read_json(directory / "exit.json")
            self.assertIn(status["returncode"], (0, 2))
            self.assertEqual(receipt["request_sha256"], result["request_sha256"])
        model = LinearModel.load(self.run / "training/model.json")
        manifest = Registry(self.store)._manifest(model.model_version)
        training = read_json(self.run / "training/report.json")
        self.assertLessEqual(manifest["policy_registered_at_ns"], training["trained_at_ns"])
        self.assertEqual(model.training_metadata["config"]["training"], self.config["training"])
        report = read_json(self.run / "backtest/report.json")
        self.assertEqual(report["capture_ids"], training["splits"]["test"])
        self.assertEqual(report["metrics"]["fills"], 0)
        self.assertEqual(report["broker_latency"]["orders_sent"], 0)

    def test_resume_complete_job_does_not_start_any_child_or_change_evidence(self):
        first = self.call()
        hashes = {p.relative_to(self.run).as_posix(): sha256_file(p) for p in self.run.rglob("*")
                  if p.is_file() and p.name not in ("status.json", ".pipeline.lock")}
        with patch("ml.pipeline._execute", side_effect=AssertionError("Completed job must not execute")):
            second = self.call(resume=True)
        self.assertEqual(first["model_version"], second["model_version"])
        self.assertEqual(first["stages"], second["stages"])
        self.assertEqual(hashes, {name: sha256_file(self.run / name) for name in hashes})

    def test_resume_rejects_configuration_and_engine_or_code_change(self):
        self.call()
        changed = deepcopy(self.config)
        changed["training"]["seed"] += 1
        with self.assertRaisesRegex(PipelineError, "consumed test"):
            self.call(resume=True, config=changed)
        # A different executable path/hash cannot claim this run's holdout.
        copied = self.directory / "changed-engine"
        shutil.copyfile(ENGINE, copied)
        with copied.open("ab") as stream:
            stream.write(b"\0")
        copied.chmod(0o700)
        with self.assertRaises(PipelineError) as error:
            self.call(resume=True, binary=copied)
        self.assertEqual(error.exception.code, "REQUEST_CHANGED")

    def test_tampered_completed_artifact_is_rejected_even_if_status_says_complete(self):
        self.call()
        path = self.run / "backtest/report.json"
        value = read_json(path)
        value["metrics"]["events"] += 1
        path.write_text(json.dumps(value))
        with self.assertRaises(PipelineError) as error:
            self.call(resume=True)
        self.assertEqual(error.exception.code, "ARTIFACT_MISMATCH")
        self.assertEqual(read_json(self.run / "status.json")["state"], "blocked")

    def test_complete_training_before_checkpoint_is_verified_and_recovered(self):
        original = _execute
        def interrupt_after_train(command, directory, timeout):
            result = original(command, directory, timeout)
            if directory.name == "training":
                raise KeyboardInterrupt("simulated supervisor crash before receipt")
            return result
        with patch("ml.pipeline._execute", side_effect=interrupt_after_train):
            with self.assertRaises(KeyboardInterrupt):
                self.call()
        self.assertFalse((self.run / "stages/training/receipt.json").exists())
        model_hash = sha256_file(self.run / "training/model.json")
        def forbid_retrain(command, directory, timeout):
            self.assertNotEqual(directory.name, "training")
            return original(command, directory, timeout)
        with patch("ml.pipeline._execute", side_effect=forbid_retrain):
            result = self.call(resume=True)
        self.assertTrue(result["stages"]["training"]["recovered"])
        self.assertEqual(sha256_file(self.run / "training/model.json"), model_hash)

    def test_incomplete_training_remains_blocked_without_child_retry(self):
        original = _execute
        def interrupt_during_train(command, directory, timeout):
            if directory.name != "training":
                return original(command, directory, timeout)
            directory.mkdir()
            write_json(directory / "invocation.json", {"schema_version": 1, "command": command, "timeout_seconds": timeout})
            (self.run / "training").mkdir()
            (self.run / "training/train.log").write_text('{"event":"dataset_verified"}\n')
            raise KeyboardInterrupt("incomplete training")
        with patch("ml.pipeline._execute", side_effect=interrupt_during_train):
            with self.assertRaises(KeyboardInterrupt):
                self.call()
        with patch("ml.pipeline._execute", side_effect=AssertionError("Partial training must not restart")):
            with self.assertRaises(PipelineError) as error:
                self.call(resume=True)
        self.assertEqual(error.exception.code, "INCOMPLETE_STAGE")
        self.assertEqual(read_json(self.run / "status.json")["state"], "blocked")
        with patch("ml.pipeline._execute", side_effect=AssertionError("A new run must not bypass the consumed reservation")):
            with self.assertRaises(PipelineError) as error:
                self.call(run_dir=self.directory / "another-run")
        self.assertEqual(error.exception.code, "HOLDOUT_ALREADY_RESERVED")

    def test_unknown_exit_is_explicit_after_recovering_complete_training(self):
        original = _execute
        def interrupt_after_outputs(command, directory, timeout):
            result = original(command, directory, timeout)
            if directory.name == "training":
                (directory / "exit.json").unlink()
                raise KeyboardInterrupt("parent died before exit checkpoint")
            return result
        with patch("ml.pipeline._execute", side_effect=interrupt_after_outputs):
            with self.assertRaises(KeyboardInterrupt):
                self.call()
        result = self.call(resume=True)
        self.assertTrue(result["stages"]["training"]["recovered_unknown_exit"])
        self.assertFalse((self.run / "stages/training/exit.json").exists())

    def test_gate_crash_cannot_reuse_a_recorded_rejection_as_success(self):
        original = _execute
        def replace_gate_exit(command, directory, timeout):
            result = original(command, directory, timeout)
            if directory.name == "simulation_gate":
                self.assertEqual(result["returncode"], 2)
                result["returncode"] = 1
                (directory / "exit.json").chmod(0o600)
                (directory / "exit.json").write_text(json.dumps(result))
            return result
        with patch("ml.pipeline._execute", side_effect=replace_gate_exit):
            with self.assertRaises(PipelineError) as error:
                self.call()
        self.assertEqual(error.exception.code, "STAGE_FAILED")
        self.assertFalse((self.run / "stages/simulation_gate/receipt.json").exists())
        with self.assertRaises(PipelineError) as error:
            self.call(resume=True)
        self.assertEqual(error.exception.code, "STAGE_FAILED")

    def test_recovery_rejects_old_gate_attempt_even_when_exit_is_unknown(self):
        self.call()
        stage = self.run / "stages/simulation_gate"
        (stage / "receipt.json").unlink()
        (stage / "exit.json").unlink()
        invocation = read_json(stage / "invocation.json")
        invocation["started_at_ns"] = time.time_ns()
        _write(stage / "invocation.json", invocation, replace=True)
        with patch("ml.pipeline._execute", side_effect=AssertionError("An interrupted gate may not rerun")):
            with self.assertRaises(PipelineError) as error:
                self.call(resume=True)
        self.assertEqual(error.exception.code, "INCOMPLETE_STAGE")

    def test_reservation_is_durable_before_any_subprocess_and_blocks_new_request(self):
        with patch("ml.pipeline._execute", side_effect=KeyboardInterrupt("before the policy child starts")):
            with self.assertRaises(KeyboardInterrupt):
                self.call()
        registry = Registry(self.store, create=False)
        ledger = read_ledger(registry)
        self.assertEqual(len(ledger["requests"]), 1)
        self.assertEqual(next(iter(ledger["requests"].values()))["owner"], "pipeline")
        ledger_hash = sha256_file(self.store / LEDGER_PATH)
        changed = deepcopy(self.config)
        changed["training"]["seed"] += 1
        with patch("ml.pipeline._execute", side_effect=AssertionError("A reserved test cannot be tuned in a new run")):
            with self.assertRaises(PipelineError) as error:
                self.call(run_dir=self.directory / "tuned-run", config=changed)
        self.assertEqual(error.exception.code, "HOLDOUT_ALREADY_RESERVED")
        self.assertEqual(sha256_file(self.store / LEDGER_PATH), ledger_hash)

    def test_legacy_registered_candidate_blocks_training_before_prediction_evaluation(self):
        self.call()
        legacy = Registry(self.directory / "legacy-store")
        policy = legacy.install_policy(read_json(self.admission))
        legacy.register(self.run / "training/model.json", admission_sha256=policy, data_manifest=self.manifest)
        self.assertFalse((legacy.root / "holdouts.json").exists())
        self.assertFalse((legacy.root / LEDGER_PATH).exists())
        with patch("ml.pipeline._execute", side_effect=AssertionError("Even an unapproved candidate consumed its test")):
            with self.assertRaises(PipelineError) as error:
                self.call(run_dir=self.directory / "legacy-reuse", store=legacy.root)
        self.assertEqual(error.exception.code, "HOLDOUT_ALREADY_RESERVED")

    def test_legacy_retrain_failure_reservation_blocks_pipeline(self):
        registry = Registry(self.store)
        data = load_manifest(self.manifest)
        sessions = data.captures[int(len(data.captures) * .8):]
        keys = {"sha256:" + c.itch_sha256 for c in sessions} | {"session:" + c.symbol + ":" + c.session_date for c in sessions}
        _write(registry.root / "retrain/attempts.json", {"schema_version": 2,
               "datasets": {data.dataset_sha256: "a" * 24},
               "holdout_keys": {key: data.dataset_sha256 for key in keys}})
        with patch("ml.pipeline._execute", side_effect=AssertionError("Failed retraining cannot be bypassed by pipeline")):
            with self.assertRaises(PipelineError) as error:
                self.call()
        self.assertEqual(error.exception.code, "HOLDOUT_ALREADY_RESERVED")

    def test_resume_blocks_while_same_child_identity_is_still_running(self):
        original = _execute
        def interrupt_after_train(command, directory, timeout):
            result = original(command, directory, timeout)
            if directory.name == "training":
                identity = {"schema_version": 1, "pid": os.getpid(), **_process_identity(os.getpid())}
                (directory / "process.json").chmod(0o600)
                (directory / "process.json").write_text(json.dumps(identity))
                raise KeyboardInterrupt("parent died while child still had work")
            return result
        with patch("ml.pipeline._execute", side_effect=interrupt_after_train):
            with self.assertRaises(KeyboardInterrupt):
                self.call()
        with self.assertRaises(PipelineError) as error:
            self.call(resume=True)
        self.assertEqual(error.exception.code, "STAGE_STILL_RUNNING")

    def test_real_child_timeout_is_recorded_and_process_is_stopped(self):
        import sys
        output = self.directory / "timeout"
        result = _execute([sys.executable, "-c", "import time; time.sleep(60)"], output, .1)
        self.assertTrue(result["timed_out"])
        self.assertNotEqual(result["returncode"], 0)
        process = read_json(output / "process.json")
        self.assertIsNone(_process_identity(process["pid"])["start_ticks"])

    def test_real_training_failure_preserves_exit_and_does_not_claim_gate_rejection(self):
        dataset = self.directory / "bad-data"
        shutil.copytree(self.manifest.parent, dataset)
        document = read_json(dataset / "manifest.json")
        first = document["captures"][0]
        raw = dataset / first["itch_path"]
        raw.write_bytes(b"broken-input")
        first["itch_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
        (dataset / "manifest.json").write_text(json.dumps(document))
        with self.assertRaises(PipelineError) as error:
            self.call(manifest=dataset / "manifest.json")
        self.assertEqual(error.exception.code, "STAGE_FAILED")
        self.assertEqual(read_json(self.run / "status.json")["state"], "failed")
        exit_status = read_json(self.run / "stages/training/exit.json")
        self.assertNotEqual(exit_status["returncode"], 0)
        self.assertTrue((self.run / "stages/training/stderr.log").read_text())
        self.assertFalse((self.run / "stages/training/receipt.json").exists())

    def test_run_symlinks_public_permissions_and_busy_lock_are_rejected(self):
        target = self.directory / "target"
        target.mkdir()
        self.run.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.call(resume=True)
        self.run.unlink()
        self.run.mkdir(mode=0o777)
        self.run.chmod(0o777)
        with self.assertRaises(ValueError):
            self.call(resume=True)
        self.run.chmod(0o700)
        descriptor = os.open(self.run / ".pipeline.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(PipelineError) as error:
                self.call(resume=True)
            self.assertEqual(error.exception.code, "PIPELINE_BUSY")
        finally:
            os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
