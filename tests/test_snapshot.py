from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bedrock_activity_backup.snapshot as snapshot_module
from bedrock_activity_backup.snapshot import (
    SnapshotManager,
    SnapshotRehearsal,
    SnapshotRestorePlanner,
    SnapshotRotator,
    SnapshotVerifier,
)
from bedrock_activity_backup.state import BackupReason

from tests.helpers import create_owned_snapshot, make_config


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

    def test_snapshot_reserves_space_for_worst_case_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FixtureRsync()
            config, console, manager = self._manager(Path(directory), runner)
            (config.world_path / "chunk.ldb").write_bytes(b"world-data")
            usage = shutil.disk_usage(config.backup_root)
            low = usage._replace(free=config.min_free_bytes)
            with patch(
                "bedrock_activity_backup.snapshot.shutil.disk_usage",
                return_value=low,
            ), self.assertRaisesRegex(RuntimeError, "free disk space"):
                manager.create(BackupReason.PERIODIC)
            self.assertEqual(console.pause_entries, 0)

    def test_concurrent_snapshot_is_refused_before_save_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FixtureRsync()
            config, console, manager = self._manager(Path(directory), runner)
            lock_path = config.backup_root / "backup.lock"
            with lock_path.open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "repository is busy"):
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
            self.assertIn("--fsync", runner.commands[-1])

    def test_rotation_ignores_manual_and_unvalidated_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = make_config(base, keep_snapshots=2)
            config.snapshot_root.mkdir(parents=True)
            for index in range(3):
                create_owned_snapshot(
                    config,
                    f"20260801T00000{index}Z-{index:08x}",
                    marker=str(index).encode(),
                )
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
                snapshot = create_owned_snapshot(
                    config,
                    f"20260801T00000{index}Z-{index:08x}",
                    marker=str(index).encode(),
                )
                snapshots.append(snapshot)
            os.symlink(Path("snapshots") / snapshots[0].name, config.backup_root / "latest")
            SnapshotRotator(config).prune()
            self.assertTrue(snapshots[0].exists())
            self.assertTrue(snapshots[-1].exists())
            self.assertTrue(snapshots[-2].exists())
            self.assertFalse(snapshots[1].exists())

    def test_prune_refuses_to_run_while_snapshot_lock_is_held(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory), keep_snapshots=2)
            config.snapshot_root.mkdir(parents=True)
            snapshots = [
                create_owned_snapshot(
                    config,
                    f"20260801T00000{index}Z-{index:08x}",
                    marker=str(index).encode(),
                )
                for index in range(3)
            ]
            lock_path = config.backup_root / "backup.lock"
            with lock_path.open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "repository is busy"):
                    SnapshotRotator(config).prune()
            self.assertTrue(all(path.exists() for path in snapshots))

    def test_latest_failure_after_publish_does_not_fail_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FixtureRsync()
            config, _console, manager = self._manager(Path(directory), runner)
            with patch.object(
                manager.rotator,
                "_set_latest_locked",
                side_effect=OSError("private path"),
            ), self.assertLogs("bedrock_activity_backup.snapshot", level="ERROR") as logs:
                result = manager.create(BackupReason.PERIODIC)
            self.assertTrue(result.is_dir())
            self.assertEqual(len(manager.rotator.complete_snapshots()), 1)
            self.assertNotIn("private path", "\n".join(logs.output))

    def test_next_snapshot_uses_published_head_after_latest_update_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            times = [
                dt.datetime(2026, 8, 1, hour=1, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 8, 1, hour=2, tzinfo=dt.timezone.utc),
            ]
            runner = FixtureRsync()
            _config, _console, manager = self._manager(
                Path(directory), runner, times=times
            )
            with patch.object(
                manager.rotator,
                "_set_latest_locked",
                side_effect=OSError("private path"),
            ), self.assertLogs("bedrock_activity_backup.snapshot", level="ERROR"):
                first = manager.create(BackupReason.PERIODIC)
            second = manager.create(BackupReason.PERIODIC)
            second_manifest = json.loads(
                (second / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(second_manifest["previous_snapshot"], first.name)

    def test_previous_snapshot_verification_finishes_before_save_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FixtureRsync()
            config, console, manager = self._manager(Path(directory), runner)
            create_owned_snapshot(config, "20260801T000000Z-000000ff")
            observations = []
            original_verify = manager.verifier.verify

            def observed_verify(*args, **kwargs):
                observations.append(console.pause_entries)
                return original_verify(*args, **kwargs)

            with patch.object(manager.verifier, "verify", side_effect=observed_verify):
                manager.create(BackupReason.PERIODIC)
            self.assertTrue(observations)
            self.assertEqual(observations[0], 0)

    def test_prune_failure_after_publish_does_not_fail_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FixtureRsync()
            _config, _console, manager = self._manager(Path(directory), runner)
            with patch.object(
                manager.rotator,
                "_prune_locked",
                side_effect=OSError("private path"),
            ), self.assertLogs("bedrock_activity_backup.snapshot", level="ERROR"):
                result = manager.create(BackupReason.PERIODIC)
            self.assertTrue(result.is_dir())

    def test_repository_fsync_failure_publishes_but_skips_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FixtureRsync()
            config, _console, manager = self._manager(Path(directory), runner)
            real_fsync = snapshot_module._fsync_directory

            def fail_repository_sync(path):
                if path == config.snapshot_root:
                    raise OSError("private path")
                return real_fsync(path)

            with patch(
                "bedrock_activity_backup.snapshot._fsync_directory",
                side_effect=fail_repository_sync,
            ), patch.object(manager.rotator, "_prune_locked") as prune, self.assertLogs(
                "bedrock_activity_backup.snapshot", level="ERROR"
            ):
                result = manager.create(BackupReason.PERIODIC)
            self.assertTrue(result.is_dir())
            prune.assert_not_called()

    def test_maintenance_repairs_latest_from_snapshot_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory), keep_snapshots=4)
            config.snapshot_root.mkdir(parents=True)
            first = create_owned_snapshot(config, "20260801T010000Z-00000001")
            second = create_owned_snapshot(
                config,
                "20260731T230000Z-00000002",
                previous=first.name,
                marker=b"second",
            )
            os.symlink(Path("snapshots") / first.name, config.backup_root / "latest")
            SnapshotRotator(config).maintain()
            self.assertEqual((config.backup_root / "latest").resolve(), second.resolve())

    def test_rotation_follows_snapshot_chain_when_clock_moves_backward(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory), keep_snapshots=2)
            config.snapshot_root.mkdir(parents=True)
            first = create_owned_snapshot(config, "20260801T010000Z-00000001")
            second = create_owned_snapshot(
                config,
                "20260801T020000Z-00000002",
                previous=first.name,
                marker=b"second",
            )
            third = create_owned_snapshot(
                config,
                "20260731T230000Z-00000003",
                previous=second.name,
                marker=b"third",
            )
            os.symlink(Path("snapshots") / second.name, config.backup_root / "latest")
            SnapshotRotator(config).maintain()
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            self.assertTrue(third.exists())
            self.assertEqual((config.backup_root / "latest").resolve(), third.resolve())

    def test_verifier_rejects_tampered_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            config.snapshot_root.mkdir(parents=True)
            snapshot = create_owned_snapshot(config, "20260801T010000Z-00000001")
            (snapshot / "payload/server.properties").write_text(
                "level-name=tampered\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                SnapshotVerifier(config).verify(snapshot)
            self.assertEqual(SnapshotRotator(config).complete_snapshots(), [])

    def test_new_snapshot_checksums_cover_every_payload_file(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FixtureRsync()
            _config, _console, manager = self._manager(Path(directory), runner)
            snapshot = manager.create(BackupReason.PERIODIC)
            manifest = json.loads(
                (snapshot / "manifest.json").read_text(encoding="utf-8")
            )
            payload_files = {
                str(path.relative_to(snapshot))
                for path in (snapshot / "payload").rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            self.assertEqual(set(manifest["checksums_sha256"]), payload_files)

    def test_verifier_rejects_unchecked_payload_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            config.snapshot_root.mkdir(parents=True)
            snapshot = create_owned_snapshot(config, "20260801T010000Z-00000001")
            (snapshot / "payload/untracked.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "full payload"):
                SnapshotVerifier(config).verify(snapshot)

    def test_snapshot_creation_rejects_payload_symlink(self):
        class SymlinkRsync(FixtureRsync):
            def __call__(self, command, **kwargs):
                result = super().__call__(command, **kwargs)
                payload = Path(command[-1].removesuffix("/"))
                os.symlink("server.properties", payload / "linked.properties")
                return result

        with tempfile.TemporaryDirectory() as directory:
            config, _console, manager = self._manager(
                Path(directory), SymlinkRsync()
            )
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                manager.create(BackupReason.PERIODIC)
            self.assertEqual(SnapshotRotator(config).complete_snapshots(), [])

    def test_snapshot_creation_rejects_manifest_over_reader_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FixtureRsync()
            config, _console, manager = self._manager(Path(directory), runner)
            with patch.object(snapshot_module, "_MAX_MANIFEST_BYTES", 128):
                with self.assertRaisesRegex(RuntimeError, "manifest exceeds"):
                    manager.create(BackupReason.PERIODIC)
            self.assertEqual(SnapshotRotator(config).complete_snapshots(), [])

    def test_verifier_accepts_strict_legacy_schema_one_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            config.snapshot_root.mkdir(parents=True)
            snapshot = create_owned_snapshot(config, "20260801T010000Z-00000001")
            manifest_path = snapshot / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 1
            manifest.pop("tool")
            manifest.pop("previous_snapshot")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (snapshot / ".owner.json").unlink()
            result = SnapshotVerifier(config).verify(snapshot)
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(SnapshotRotator(config).complete_snapshots(), [])
            with self.assertRaisesRegex(RuntimeError, "read-only legacy"):
                SnapshotRehearsal(config).run(
                    snapshot, Path(directory) / "legacy-rehearsal"
                )

    def test_rehearsal_copies_and_revalidates_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = make_config(base)
            config.snapshot_root.mkdir(parents=True)
            snapshot = create_owned_snapshot(config, "20260801T010000Z-00000001")
            destination = base / "rehearsal" / "case-1"
            result = SnapshotRehearsal(config).run(snapshot, destination)
            self.assertEqual(result["rehearsal"], "passed")
            self.assertTrue((destination / "rehearsal-report.json").is_file())

    def test_unowned_snapshot_like_directory_is_never_pruned(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory), keep_snapshots=2)
            config.snapshot_root.mkdir(parents=True)
            for index in range(3):
                create_owned_snapshot(
                    config,
                    f"20260801T00000{index}Z-{index:08x}",
                    marker=str(index).encode(),
                )
            unowned = config.snapshot_root / "20260801T000009Z-deadbeef"
            unowned.mkdir()
            (unowned / "manifest.json").write_text("{}", encoding="utf-8")
            SnapshotRotator(config).prune()
            self.assertTrue(unowned.exists())

    def test_restore_plan_binds_verified_snapshot_and_current_world(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = make_config(base)
            config.snapshot_root.mkdir(parents=True)
            config.world_path.mkdir(parents=True)
            (config.world_path / "level.dat").write_bytes(b"current-world")
            snapshot = create_owned_snapshot(config, "20260801T010000Z-00000001")
            plan = SnapshotRestorePlanner(config).build(snapshot)
            self.assertEqual(plan["snapshot"], snapshot.name)
            self.assertRegex(str(plan["plan_sha256"]), r"^[a-f0-9]{64}$")
            self.assertTrue(plan["requires_service_inactive"])

    def test_rehearsal_refuses_to_race_repository_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = make_config(base)
            config.snapshot_root.mkdir(parents=True)
            snapshot = create_owned_snapshot(config, "20260801T010000Z-00000001")
            destination = base / "rehearsal" / "case-1"
            lock_path = config.backup_root / "backup.lock"
            with lock_path.open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "repository is busy"):
                    SnapshotRehearsal(config).run(snapshot, destination)
            self.assertFalse(destination.exists())

    def test_rehearsal_reserves_space_before_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = make_config(base)
            config.snapshot_root.mkdir(parents=True)
            snapshot = create_owned_snapshot(config, "20260801T010000Z-00000001")
            destination = base / "rehearsal" / "case-1"
            usage = shutil.disk_usage(base)
            low = usage._replace(free=config.min_free_bytes)
            with patch(
                "bedrock_activity_backup.snapshot.shutil.disk_usage",
                return_value=low,
            ), self.assertRaisesRegex(RuntimeError, "rehearsal safety"):
                SnapshotRehearsal(config).run(snapshot, destination)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
