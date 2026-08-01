import json
import tempfile
import unittest
from pathlib import Path

from bedrock_activity_backup.state import (
    ActivityState,
    BackupReason,
    Phase,
    StateStore,
)


class ActivityStateTests(unittest.TestCase):
    def test_first_connection_starts_deadline(self):
        state = ActivityState()
        self.assertTrue(state.player_connected(1000, 1800))
        self.assertEqual(state.phase, Phase.ACTIVE)
        self.assertEqual(state.next_due_epoch, 2800)

    def test_later_connections_do_not_reset_deadline(self):
        state = ActivityState(Phase.ACTIVE, 2800)
        self.assertFalse(state.player_connected(1500, 1800))
        self.assertEqual(state.next_due_epoch, 2800)

    def test_nonfinal_disconnect_does_not_backup(self):
        state = ActivityState(Phase.ACTIVE, 2800)
        self.assertIsNone(state.player_disconnected(1))
        self.assertEqual(state.phase, Phase.ACTIVE)

    def test_last_disconnect_requests_immediate_backup(self):
        state = ActivityState(Phase.ACTIVE, 2800)
        self.assertEqual(
            state.player_disconnected(0), BackupReason.LAST_PLAYER_LEFT
        )
        self.assertEqual(state.phase, Phase.BACKING_UP)
        self.assertIsNone(state.next_due_epoch)

    def test_timer_requests_periodic_backup_only_at_deadline(self):
        state = ActivityState(Phase.ACTIVE, 2800)
        self.assertIsNone(state.timer_due(2799))
        self.assertEqual(state.timer_due(2800), BackupReason.PERIODIC)

    def test_success_goes_idle_when_empty(self):
        state = ActivityState(
            Phase.BACKING_UP, pending_reason=BackupReason.LAST_PLAYER_LEFT
        )
        state.backup_succeeded(3000, 0, 1800)
        self.assertEqual(state.phase, Phase.IDLE)
        self.assertIsNone(state.next_due_epoch)

    def test_success_continues_cycle_when_players_remain(self):
        state = ActivityState(Phase.BACKING_UP, pending_reason=BackupReason.PERIODIC)
        state.backup_succeeded(3000, 2, 1800)
        self.assertEqual(state.phase, Phase.ACTIVE)
        self.assertEqual(state.next_due_epoch, 4800)

    def test_failure_retries_without_losing_session(self):
        state = ActivityState(Phase.BACKING_UP, pending_reason=BackupReason.PERIODIC)
        state.backup_failed(3000, 300)
        self.assertEqual(state.phase, Phase.ACTIVE)
        self.assertEqual(state.next_due_epoch, 3300)

    def test_restart_preserves_future_deadline(self):
        state = ActivityState(Phase.ACTIVE, 2800)
        self.assertIsNone(state.reconcile_startup(2000, 1, 1800))
        self.assertEqual(state.next_due_epoch, 2800)

    def test_restart_with_empty_server_requests_recovery_snapshot(self):
        state = ActivityState(Phase.ACTIVE, 2800)
        self.assertEqual(
            state.reconcile_startup(2000, 0, 1800), BackupReason.RECOVERY
        )
        self.assertEqual(state.phase, Phase.BACKING_UP)

    def test_force_final_backup_covers_disconnect_during_periodic_copy(self):
        state = ActivityState()
        self.assertEqual(state.force_final_backup(), BackupReason.LAST_PLAYER_LEFT)
        self.assertEqual(state.phase, Phase.BACKING_UP)


class StateStoreTests(unittest.TestCase):
    def test_round_trip_and_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run" / "state.json"
            store = StateStore(path)
            expected = ActivityState(Phase.ACTIVE, 2800)
            store.save(expected)
            actual = store.load()
            self.assertEqual(actual, expected)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"version": 99}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid"):
                StateStore(path).load()


if __name__ == "__main__":
    unittest.main()
