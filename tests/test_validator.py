from dataclasses import replace
import json
import os
import subprocess
import time
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bedrock_activity_backup.snapshot import IsolatedBdsValidator, _runtime_bytes
from tests.helpers import create_owned_snapshot, make_config


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        # Fake BDS scripts are safe on the development host. Production BDS must
        # be run in the systemd service's separate Linux network namespace.
        self.network_guard = patch.object(IsolatedBdsValidator, "assert_isolated_network")
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)

    def test_network_guard_refuses_the_host_namespace(self):
        self.network_guard.stop()
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            with patch("bedrock_activity_backup.snapshot.Path.stat") as stat:
                stat.return_value.st_dev = 1
                stat.return_value.st_ino = 2
                with self.assertRaisesRegex(RuntimeError, "private network namespace"):
                    IsolatedBdsValidator(config).assert_isolated_network()

    def fixture(self, base, program):
        config = replace(make_config(base), verification_timeout_seconds=10)
        config.bds_root.mkdir(parents=True)
        config.snapshot_root.mkdir(parents=True)
        snapshot = create_owned_snapshot(config, "20261024T000000Z-deadbeef")
        binary = config.bds_root / "bedrock_server"
        binary.write_text(f"#!{sys.executable}\nimport sys, time\n{program}\n")
        binary.chmod(0o700)
        (config.bds_root / "server.properties").write_text("level-name=Test World\n")
        return config, snapshot

    def verify_with_slow_reader(self, config, snapshot):
        # Reproduce a reader being descheduled while BDS fills its stdout pipe.
        real_read = os.read
        stdout_fd = None

        def start(*args, **kwargs):
            nonlocal stdout_fd
            process = subprocess.Popen(*args, **kwargs)
            stdout_fd = process.stdout.fileno()
            return process

        def delayed_read(fd, size):
            chunk = real_read(fd, size)
            if fd == stdout_fd:
                time.sleep(0.2)
            return chunk

        with patch("bedrock_activity_backup.snapshot.os.read", side_effect=delayed_read):
            return IsolatedBdsValidator(config, popen=start).verify(snapshot)

    def test_queued_corruption_cannot_pass_at_startup_grace_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory),
                "print('Server started.', flush=True); time.sleep(0.85); "
                "sys.stdout.write('noise\\n' * 25000 + 'LevelDB corruption\\n'); "
                "sys.stdout.flush(); sys.stdin.readline()")
            result = self.verify_with_slow_reader(config, snapshot)
            self.assertEqual(result["result"], "failed", result)
            self.assertTrue(result["output_complete"], result)
            self.assertTrue(result["leveldb_failure_detected"])
            self.assertEqual(result["log_bytes"], len(b"Server started.\n" + b"noise\n" * 25000 + b"LevelDB corruption\n"))
            self.assertIn(b"LevelDB corruption", (snapshot / "validation.log").read_bytes())

    def test_crash_tail_is_drained_after_exit_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory),
                "print('loading', flush=True); time.sleep(0.05); "
                "print('LevelDB corruption: evidence at crash', flush=True); sys.exit(7)")
            result = self.verify_with_slow_reader(config, snapshot)
            self.assertEqual(result["result"], "failed")
            self.assertEqual(result["exit_code"], 7)
            self.assertTrue(result["output_complete"])
            self.assertIn("LevelDB corruption: evidence at crash", (snapshot / "validation.log").read_text())

    def test_corruption_emitted_during_stop_rejects_startup_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory),
                "print('Server started.', flush=True); sys.stdin.readline(); "
                "print('LevelDB corruption during shutdown', flush=True)")
            result = IsolatedBdsValidator(config).verify(snapshot)
            self.assertEqual(result["result"], "failed", result)
            self.assertTrue(result["output_complete"])
            self.assertTrue(result["leveldb_failure_detected"])
            self.assertIn("LevelDB corruption during shutdown", (snapshot / "validation.log").read_text())

    def test_nonzero_exit_during_stop_rejects_startup_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory),
                "print('Server started.', flush=True); sys.stdin.readline(); "
                "print('fatal shutdown diagnostic', flush=True); sys.exit(7)")
            result = IsolatedBdsValidator(config).verify(snapshot)
            self.assertEqual(result["result"], "failed", result)
            self.assertEqual(result["exit_code"], 7)
            self.assertTrue(result["output_complete"])
            self.assertIn("fatal shutdown diagnostic", (snapshot / "validation.log").read_text())

    def test_forced_shutdown_stays_failed_and_drains_signal_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory),
                "import signal\n"
                "def terminate(*_args):\n"
                "    print('LevelDB corruption from termination', flush=True)\n"
                "    sys.exit(0)\n"
                "signal.signal(signal.SIGTERM, terminate)\n"
                "print('Server started.', flush=True)\n"
                "while True: time.sleep(0.01)")
            with patch("bedrock_activity_backup.snapshot._SHUTDOWN_TIMEOUT_SECONDS", 0.2), patch("bedrock_activity_backup.snapshot._FORCE_SHUTDOWN_TIMEOUT_SECONDS", 1):
                result = IsolatedBdsValidator(config).verify(snapshot)
            self.assertEqual(result["result"], "failed", result)
            self.assertTrue(result["output_complete"])
            self.assertIn("timed out", result["shutdown_error"])
            self.assertIn("LevelDB corruption from termination", (snapshot / "validation.log").read_text())
            self.assertEqual(list(config.backup_root.glob(".verify-*")), [])

    def test_exited_process_with_open_pipe_cannot_complete_drain(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            read_fd, write_fd = os.pipe()
            try:
                with os.fdopen(read_fd, "rb", buffering=0) as stdout:
                    process = SimpleNamespace(stdout=stdout, poll=lambda: 0)
                    self.assertFalse(IsolatedBdsValidator(config)._drain_until_exit(process, lambda _: None, 0.15))
            finally:
                os.close(write_fd)

    def test_burst_output_and_partial_line_do_not_hide_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory),
                "sys.stdout.write('line\\n' * 100 + 'Server started.\\n' + 'partial'); sys.stdout.flush(); sys.stdin.readline()")
            result = IsolatedBdsValidator(config).verify(snapshot)
            self.assertEqual(result["result"], "passed", result)
            self.assertEqual(result["log_lines"], 101)
            self.assertIn("runtime-copy", result["stage_seconds"])
            self.assertEqual((snapshot / "validation.log").stat().st_mode & 0o777, 0o600)
            self.assertTrue((snapshot / "validation.log").read_text().endswith("partial"))
            self.assertEqual(list(config.backup_root.glob(".verify-*")), [])

    def test_partial_line_timeout_is_bounded_and_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory),
                "sys.stdout.write('partial'); sys.stdout.flush(); sys.stdin.readline()")
            config = replace(config, verification_timeout_seconds=1)
            result = IsolatedBdsValidator(config).verify(snapshot)
            self.assertEqual(result["result"], "failed")
            self.assertIn("timed out", result["reason"])
            self.assertLess(result["elapsed_seconds"], 4)
            self.assertEqual((snapshot / "validation.log").read_text(), "partial")

    def test_output_between_32_and_64_kib_is_preserved_without_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory),
                "sys.stdout.write('x' * 50000 + '\\nServer started.\\n'); sys.stdout.flush(); sys.stdin.readline()")
            result = IsolatedBdsValidator(config).verify(snapshot)
            self.assertEqual(result["result"], "passed", result)
            self.assertFalse(result["log_truncated"])
            self.assertEqual((snapshot / "validation.log").read_text(), "x" * 50000 + "\nServer started.\n")

    def test_diagnostic_write_failure_cannot_pass_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory), "print('Server started.', flush=True); sys.stdin.readline()")
            with patch("bedrock_activity_backup.snapshot._replace_private_log", side_effect=OSError(28, "private path")):
                result = IsolatedBdsValidator(config).verify(snapshot)
            self.assertEqual(result["result"], "failed")
            self.assertIn("errno=28", result["diagnostics_error"])
            self.assertNotIn("private path", str(result))
            self.assertEqual(list(config.backup_root.glob(".verify-*")), [])

    def test_large_output_is_bounded_and_corruption_still_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory),
                "sys.stdout.write('a' * 150000 + '\\nLevelDB corruption\\n'); sys.stdout.flush(); sys.stdin.readline()")
            result = IsolatedBdsValidator(config).verify(snapshot)
            self.assertEqual(result["result"], "failed")
            self.assertIn("LevelDB", result["reason"])
            self.assertTrue(result["log_truncated"])
            log = (snapshot / "validation.log").read_bytes()
            self.assertLess(len(log), 66000)
            self.assertIn(b"LevelDB corruption", log)

    def test_exit_after_startup_marker_is_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory), "print('Server started.', flush=True); sys.exit(7)")
            result = IsolatedBdsValidator(config).verify(snapshot)
            self.assertEqual(result["result"], "failed")
            self.assertIn("exit=7", result["reason"])

    def test_space_failure_never_copies_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory), "pass")
            low = shutil.disk_usage(config.backup_root)._replace(free=config.min_free_bytes)
            validator = IsolatedBdsValidator(config)
            with patch("bedrock_activity_backup.snapshot.shutil.disk_usage", return_value=low), patch.object(validator, "_copy_runtime") as copy:
                result = validator.verify(snapshot)
            copy.assert_not_called()
            self.assertIn("deficit_bytes=", result["reason"])
            self.assertEqual(result["failed_stage"], "space-check")

    def test_runtime_budget_matches_exclusions_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory), "pass")
            expected = sum(path.stat().st_size for path in config.bds_root.iterdir() if path.is_file())
            self.assertEqual(_runtime_bytes(config), expected)
            (config.bds_root / "resource_packs").symlink_to(snapshot)
            with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                _runtime_bytes(config)

    def test_validation_properties_only_rewrite_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory), "pass")
            stage = Path(directory) / "stage"
            stage.mkdir()
            (snapshot / "payload/server.properties").write_text("level-name=Other World\n")
            IsolatedBdsValidator(config)._copy_runtime(stage, snapshot)
            properties = (stage / "server.properties").read_text()
            self.assertIn("level-name=Test World", properties)
            self.assertIn("max-threads=1", properties)
            self.assertIn("server-port=19134", properties)
            self.assertNotIn("max-threads=1", (config.bds_root / "server.properties").read_text())
            self.assertEqual(json.loads((snapshot / "manifest.json").read_text())["snapshot_state"], "verified")

    def test_archives_and_hidden_rollback_folders_are_not_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            config, snapshot = self.fixture(Path(directory), "pass")
            expected = _runtime_bytes(config)
            for name in ("addon-archive", ".staging", "imports", ".rollback"):
                folder = config.bds_root / name
                folder.mkdir()
                (folder / "large-archive").write_bytes(b"archive" * 1000)
            self.assertEqual(_runtime_bytes(config), expected)
            stage = Path(directory) / "stage"
            stage.mkdir()
            IsolatedBdsValidator(config)._copy_runtime(stage, snapshot)
            for name in ("addon-archive", ".staging", "imports", ".rollback"):
                self.assertFalse((stage / name).exists())
