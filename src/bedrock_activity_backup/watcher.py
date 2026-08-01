from __future__ import annotations

import logging
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from .config import Config
from .console import BdsConsole
from .snapshot import SnapshotManager
from .state import ActivityState, BackupReason, Phase, StateStore


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class JournalEvent:
    kind: str
    observed_monotonic: float


class JournalFollower:
    def __init__(self, service: str):
        self.service = service
        self.events: queue.Queue[JournalEvent] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            [
                "journalctl",
                "-fu",
                self.service,
                "-n",
                "0",
                "-o",
                "cat",
                "--no-pager",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if self.process.stdout is None:
            raise RuntimeError("journal follower has no output stream")
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            if "Player connected:" in line:
                self.events.put(JournalEvent("connected", time.monotonic()))
            elif "Player disconnected:" in line:
                self.events.put(JournalEvent("disconnected", time.monotonic()))
        self.events.put(JournalEvent("journal-ended", time.monotonic()))

    def close(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


class ActivityWatcher:
    def __init__(
        self,
        config: Config,
        console: BdsConsole,
        snapshots: SnapshotManager,
        store: StateStore,
        follower: JournalFollower,
        *,
        wall_clock=time.time,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ):
        self.config = config
        self.console = console
        self.snapshots = snapshots
        self.store = store
        self.follower = follower
        self.wall_clock = wall_clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.stopping = False
        self.last_backup_started_monotonic: float | None = None
        self.last_backup_reason: BackupReason | None = None

    def stop(self, _signum=None, _frame=None) -> None:
        self.stopping = True
        self.follower.events.put(JournalEvent("wake", self.monotonic()))

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.follower.start()
        try:
            self.snapshots.rotator.prune()
            try:
                state = self.store.load()
            except ValueError:
                LOGGER.error("Activity state was invalid; requesting a recovery snapshot")
                state = ActivityState(
                    phase=Phase.BACKING_UP,
                    pending_reason=BackupReason.RECOVERY,
                )
                self.store.save(state)

            online = self._query_players_with_retry(120)
            reason = state.reconcile_startup(
                self.wall_clock(), online, self.config.interval_seconds
            )
            self.store.save(state)
            if reason is not None:
                self._perform_backup(state, reason)
            elif state.phase is Phase.ACTIVE:
                LOGGER.info("Players are online; keeping the existing activity deadline")
            else:
                LOGGER.info("No players online; waiting for a connection event")

            while not self.stopping:
                timeout = self._timeout_until_due(state)
                try:
                    event = self.follower.events.get(timeout=timeout)
                except queue.Empty:
                    reason = state.timer_due(self.wall_clock())
                    if reason is not None:
                        self.store.save(state)
                        self._perform_backup(state, reason)
                    continue

                if event.kind == "journal-ended":
                    raise RuntimeError("journal follower stopped unexpectedly")
                if event.kind == "connected":
                    if state.player_connected(
                        self.wall_clock(), self.config.interval_seconds
                    ):
                        self.store.save(state)
                        LOGGER.info(
                            "First player connection observed; backup scheduled in %d seconds",
                            self.config.interval_seconds,
                        )
                elif event.kind == "disconnected":
                    self.sleep(0.25)
                    online = self._query_players_with_retry(15)
                    reason = state.player_disconnected(online)
                    if (
                        reason is None
                        and online == 0
                        and state.phase is Phase.IDLE
                        and self.last_backup_reason is BackupReason.PERIODIC
                        and self.last_backup_started_monotonic is not None
                        and event.observed_monotonic
                        >= self.last_backup_started_monotonic
                    ):
                        reason = state.force_final_backup()
                    if reason is not None:
                        self.store.save(state)
                        LOGGER.info(
                            "Last player disconnected; creating the final activity snapshot"
                        )
                        self._perform_backup(state, reason)
        finally:
            self.follower.close()

    def _perform_backup(self, state: ActivityState, reason: BackupReason) -> None:
        self.last_backup_started_monotonic = self.monotonic()
        self.last_backup_reason = reason
        try:
            path = self.snapshots.create(reason)
        except Exception:
            state.backup_failed(self.wall_clock(), self.config.retry_seconds)
            self.store.save(state)
            LOGGER.exception("Snapshot failed; retry scheduled")
            return

        try:
            online = self._query_players_with_retry(15)
        except Exception:
            online = 1
            LOGGER.exception(
                "Snapshot succeeded but player query failed; keeping cycle active"
            )
        state.backup_succeeded(
            self.wall_clock(), online, self.config.interval_seconds
        )
        self.store.save(state)
        LOGGER.info("Snapshot completed successfully: %s", path.name)
        if state.phase is Phase.ACTIVE:
            LOGGER.info(
                "Players remain online; next backup scheduled in %d seconds",
                self.config.interval_seconds,
            )
        else:
            LOGGER.info("No players remain online; activity backup cycle is idle")

    def _query_players_with_retry(self, timeout: int) -> int:
        deadline = self.monotonic() + timeout
        last_error: Exception | None = None
        while self.monotonic() < deadline and not self.stopping:
            try:
                self.console.assert_available()
                return self.console.query_player_count(
                    self.config.list_timeout_seconds
                )
            except Exception as error:
                last_error = error
                self.sleep(1)
        raise RuntimeError("could not query the BDS player count") from last_error

    def _timeout_until_due(self, state: ActivityState) -> float | None:
        if state.phase is not Phase.ACTIVE or state.next_due_epoch is None:
            return None
        return max(0, state.next_due_epoch - self.wall_clock())
