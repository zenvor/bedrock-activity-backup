import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bedrock_activity_backup.state import ActivityState, BackupReason, Phase
from bedrock_activity_backup.watcher import ActivityWatcher

from tests.helpers import make_config


class FakeSnapshots:
    def __init__(self, fail=False, fail_on_call=None):
        self.fail = fail
        self.fail_on_call = fail_on_call
        self.reasons = []

    def create(self, reason):
        self.reasons.append(reason)
        if self.fail or len(self.reasons) == self.fail_on_call:
            raise RuntimeError("snapshot failed")
        return Path("/snapshots/complete")


class FakeStore:
    def __init__(self):
        self.saved = []

    def save(self, state):
        self.saved.append(
            ActivityState(
                state.phase,
                state.next_due_epoch,
                state.pending_reason,
                state.retry_reason,
            )
        )


class FakeFollower:
    def __init__(self):
        self.events = queue.Queue()


class WatcherBackupTests(unittest.TestCase):
    def _watcher(self, base, snapshots, store):
        config = make_config(base)
        return ActivityWatcher(
            config,
            console=object(),
            snapshots=snapshots,
            store=store,
            follower=FakeFollower(),
            wall_clock=lambda: 1000,
            monotonic=lambda: 500,
            sleep=lambda _seconds: None,
        )

    def test_successful_exit_backup_goes_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshots = FakeSnapshots()
            store = FakeStore()
            watcher = self._watcher(Path(directory), snapshots, store)
            state = ActivityState(
                Phase.BACKING_UP, pending_reason=BackupReason.LAST_PLAYER_LEFT
            )
            with patch.object(watcher, "_query_players_with_retry", return_value=0):
                with self.assertLogs(
                    "bedrock_activity_backup.watcher", level="INFO"
                ):
                    watcher._perform_backup(state, BackupReason.LAST_PLAYER_LEFT)
            self.assertEqual(state.phase, Phase.IDLE)
            self.assertEqual(snapshots.reasons, [BackupReason.LAST_PLAYER_LEFT])

    def test_failed_backup_retries_in_five_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshots = FakeSnapshots(fail=True)
            store = FakeStore()
            watcher = self._watcher(Path(directory), snapshots, store)
            state = ActivityState(
                Phase.BACKING_UP, pending_reason=BackupReason.PERIODIC
            )
            with self.assertLogs(
                "bedrock_activity_backup.watcher", level="ERROR"
            ):
                watcher._perform_backup(state, BackupReason.PERIODIC)
            self.assertEqual(state.phase, Phase.ACTIVE)
            self.assertEqual(state.next_due_epoch, 1300)
            self.assertEqual(state.retry_reason, BackupReason.PERIODIC)

    def test_player_query_failure_after_snapshot_keeps_cycle_active(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshots = FakeSnapshots()
            store = FakeStore()
            watcher = self._watcher(Path(directory), snapshots, store)
            state = ActivityState(
                Phase.BACKING_UP, pending_reason=BackupReason.PERIODIC
            )
            with patch.object(
                watcher,
                "_query_players_with_retry",
                side_effect=RuntimeError("query failed"),
            ), self.assertLogs(
                "bedrock_activity_backup.watcher", level="ERROR"
            ):
                watcher._perform_backup(state, BackupReason.PERIODIC)
            self.assertEqual(state.phase, Phase.ACTIVE)
            self.assertEqual(state.next_due_epoch, 2800)

    def test_periodic_snapshot_with_zero_players_persists_and_creates_final(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshots = FakeSnapshots()
            store = FakeStore()
            watcher = self._watcher(Path(directory), snapshots, store)
            state = ActivityState(
                Phase.BACKING_UP, pending_reason=BackupReason.PERIODIC
            )
            with patch.object(
                watcher, "_query_players_with_retry", side_effect=[0, 0]
            ), self.assertLogs(
                "bedrock_activity_backup.watcher", level="INFO"
            ):
                watcher._perform_backup(state, BackupReason.PERIODIC)
            self.assertEqual(
                snapshots.reasons,
                [BackupReason.PERIODIC, BackupReason.LAST_PLAYER_LEFT],
            )
            self.assertEqual(state.phase, Phase.IDLE)
            self.assertTrue(
                any(
                    saved.phase is Phase.BACKING_UP
                    and saved.pending_reason is BackupReason.LAST_PLAYER_LEFT
                    for saved in store.saved
                )
            )

    def test_failed_persisted_final_retries_as_final(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshots = FakeSnapshots(fail_on_call=2)
            store = FakeStore()
            watcher = self._watcher(Path(directory), snapshots, store)
            state = ActivityState(
                Phase.BACKING_UP, pending_reason=BackupReason.PERIODIC
            )
            with patch.object(
                watcher, "_query_players_with_retry", return_value=0
            ), self.assertLogs("bedrock_activity_backup.watcher", level="ERROR"):
                watcher._perform_backup(state, BackupReason.PERIODIC)
            self.assertEqual(state.phase, Phase.ACTIVE)
            self.assertEqual(state.retry_reason, BackupReason.LAST_PLAYER_LEFT)


if __name__ == "__main__":
    unittest.main()
