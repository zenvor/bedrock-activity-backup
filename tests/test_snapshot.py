from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from bedrock_activity_backup.snapshot import SnapshotManager, SnapshotRotator
from bedrock_activity_backup.state import BackupReason

from tests.helpers import make_config


class FakeConsole:
    def __init__(self):
        self.pause_entries = 0
        self.pause_exits = 0

    def assert_available(self):
        return None

    @contextlib.contextmanager
    def paused_saves(self, _ready_timeout, _resume_timeout):
        self.pause_entries += 1
        try:
            yield
        finally:
            self.pause_exits += 1


class FixtureRsync:
    def __init__(self, fail=False):
        self.fail = fail
        self.commands = []

    def __call__(self, command, **_kwargs):
        self.commands.append(command)
        if self.fail:
            raise subprocess.CalledProcessError(23, command)
        payload = Path(command[-1].removesuffix("/"))
        world = payload / "worlds" / "Test World"
        database = world / "db"
        database.mkdir(parents=True)
        (world / "level.dat").write_bytes(b"level")
        (database / "CURRENT").write_text("MANIFEST-000001\n", encoding="ascii")
        (database / "MANIFEST-000001").write_bytes(b"manifest")
        (payload / "server.properties").write_text(
            "level-name=Test World\n", encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, "Total transferred file size: 42\n", "")


class SnapshotTests(unittest.TestCase):
    def _manager(self, base: Path, runner, *, keep=4, times=None):
        config = make_config(base, keep_snapshots=keep)
        config.world_path.mkdir(parents=True)
        config.backup_root.mkdir(parents=True)
        values = iter(times or [dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)])
        token_values = iter(f"{index:08x}" for index in range(100))
        console = FakeConsole()
        manager = SnapshotManager(
            config,
            console,
            run=runner,
            now=lambda: next(values),
            token=lambda: next(token_values),
        )
        return config, console, manager

    def test_snapshot_is_validated_published_and_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FixtureRsync()
            config, console, manager = self._manager(Path(directory), runner)
            result = manager.create(BackupReason.PERIODIC)
            self.assertTrue((result / "manifest.json").is_file())
            self.assertTrue((result / "payload/worlds/Test World/db/CURRENT").is_file())
            self.assertEqual(console.pause_entries, 1)
            self.assertEqual(console.pause_exits, 1)
            self.assertEqual((config.backup_root / "latest").resolve(), result.resolve())

    def test_copy_failure_is_not_published_and_resume_still_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FixtureRsync(fail=True)
            config, console, manager = self._manager(Path(directory), runner)
            with self.assertRaises(subprocess.CalledProcessError):
                manager.create(BackupReason.PERIODIC)
            self.assertEqual(console.pause_exits, 1)
            self.assertEqual(list(config.snapshot_root.glob(".incomplete-*")), [])
            self.assertEqual(
                [path for path in config.snapshot_root.iterdir() if path.is_dir()], []
            )

    def test_concurrent_snapshot_is_refused_before_save_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FixtureRsync()
            config, console, manager = self._manager(Path(directory), runner)
            lock_path = config.backup_root / "backup.lock"
            with lock_path.open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    manager.create(BackupReason.PERIODIC)
            self.assertEqual(console.pause_entries, 0)

    def test_rotation_keeps_four_complete_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            times = [
                dt.datetime(2026, 8, 1, hour=hour, tzinfo=dt.timezone.utc)
                for hour in range(5)
            ]
            runner = FixtureRsync()
            config, _console, manager = self._manager(
                Path(directory), runner, keep=4, times=times
            )
            results = [manager.create(BackupReason.PERIODIC) for _ in range(5)]
            complete = sorted(
                path
                for path in config.snapshot_root.iterdir()
                if path.is_dir() and (path / "manifest.json").is_file()
            )
            self.assertEqual(len(complete), 4)
            self.assertFalse(results[0].exists())
            self.assertEqual(
                (config.backup_root / "latest").resolve(), results[-1].resolve()
            )
            self.assertTrue(
                any("--link-dest=" in argument for argument in runner.commands[-1])
            )

    def test_rotation_ignores_manual_and_unvalidated_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = make_config(base, keep_snapshots=2)
            config.snapshot_root.mkdir(parents=True)
            for index in range(3):
                snapshot = config.snapshot_root / f"20260801T00000{index}Z-{index:08x}"
                snapshot.mkdir()
                (snapshot / "manifest.json").write_text("{}", encoding="utf-8")
            unvalidated = config.snapshot_root / "20260801T000009Z-deadbeef"
            unvalidated.mkdir()
            manual = config.backup_root.parent / "manual-milestone"
            manual.mkdir(parents=True)
            removed = SnapshotRotator(config).prune()
            self.assertEqual(len(removed), 1)
            self.assertTrue(unvalidated.exists())
            self.assertTrue(manual.exists())

    def test_rotation_preserves_latest_symlink_during_clock_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = make_config(base, keep_snapshots=2)
            config.snapshot_root.mkdir(parents=True)
            snapshots = []
            for index in range(4):
                snapshot = config.snapshot_root / f"20260801T00000{index}Z-{index:08x}"
                snapshot.mkdir()
                (snapshot / "manifest.json").write_text("{}", encoding="utf-8")
                snapshots.append(snapshot)
            os.symlink(Path("snapshots") / snapshots[0].name, config.backup_root / "latest")
            SnapshotRotator(config).prune()
            self.assertTrue(snapshots[0].exists())
            self.assertTrue(snapshots[-1].exists())
            self.assertTrue(snapshots[-2].exists())
            self.assertFalse(snapshots[1].exists())


if __name__ == "__main__":
    unittest.main()
