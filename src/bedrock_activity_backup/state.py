from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path


class Phase(str, Enum):
    IDLE = "idle"
    ACTIVE = "active"
    BACKING_UP = "backing_up"


class BackupReason(str, Enum):
    PERIODIC = "periodic"
    LAST_PLAYER_LEFT = "last-player-left"
    RECOVERY = "recovery"
    OPERATOR = "operator"


@dataclass
class ActivityState:
    phase: Phase = Phase.IDLE
    next_due_epoch: float | None = None
    pending_reason: BackupReason | None = None

    def player_connected(self, now: float, interval_seconds: int) -> bool:
        if self.phase is not Phase.IDLE:
            return False
        self.phase = Phase.ACTIVE
        self.next_due_epoch = now + interval_seconds
        self.pending_reason = None
        return True

    def player_disconnected(self, online_count: int) -> BackupReason | None:
        if online_count < 0:
            raise ValueError("online_count must not be negative")
        if self.phase is not Phase.ACTIVE or online_count != 0:
            return None
        self.phase = Phase.BACKING_UP
        self.next_due_epoch = None
        self.pending_reason = BackupReason.LAST_PLAYER_LEFT
        return self.pending_reason

    def force_final_backup(self) -> BackupReason:
        if self.phase is not Phase.IDLE:
            raise RuntimeError("force_final_backup requires idle state")
        self.phase = Phase.BACKING_UP
        self.next_due_epoch = None
        self.pending_reason = BackupReason.LAST_PLAYER_LEFT
        return self.pending_reason

    def timer_due(self, now: float) -> BackupReason | None:
        if self.phase is not Phase.ACTIVE or self.next_due_epoch is None:
            return None
        if now < self.next_due_epoch:
            return None
        self.phase = Phase.BACKING_UP
        self.next_due_epoch = None
        self.pending_reason = BackupReason.PERIODIC
        return self.pending_reason

    def reconcile_startup(
        self, now: float, online_count: int, interval_seconds: int
    ) -> BackupReason | None:
        if online_count < 0:
            raise ValueError("online_count must not be negative")
        if self.phase is Phase.BACKING_UP:
            self.pending_reason = BackupReason.RECOVERY
            return self.pending_reason
        if online_count == 0:
            if self.phase is Phase.ACTIVE:
                self.phase = Phase.BACKING_UP
                self.next_due_epoch = None
                self.pending_reason = BackupReason.RECOVERY
                return self.pending_reason
            self.phase = Phase.IDLE
            self.next_due_epoch = None
            self.pending_reason = None
            return None
        if self.phase is Phase.ACTIVE and self.next_due_epoch is not None:
            if self.next_due_epoch <= now:
                self.phase = Phase.BACKING_UP
                self.next_due_epoch = None
                self.pending_reason = BackupReason.RECOVERY
                return self.pending_reason
            return None
        self.phase = Phase.ACTIVE
        self.next_due_epoch = now + interval_seconds
        self.pending_reason = None
        return None

    def backup_succeeded(
        self, now: float, online_count: int, interval_seconds: int
    ) -> None:
        if self.phase is not Phase.BACKING_UP:
            raise RuntimeError("backup_succeeded requires backing_up state")
        self.pending_reason = None
        if online_count > 0:
            self.phase = Phase.ACTIVE
            self.next_due_epoch = now + interval_seconds
        else:
            self.phase = Phase.IDLE
            self.next_due_epoch = None

    def backup_failed(self, now: float, retry_seconds: int) -> None:
        if self.phase is not Phase.BACKING_UP:
            raise RuntimeError("backup_failed requires backing_up state")
        self.phase = Phase.ACTIVE
        self.next_due_epoch = now + retry_seconds
        self.pending_reason = None

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["phase"] = self.phase.value
        result["pending_reason"] = (
            self.pending_reason.value if self.pending_reason is not None else None
        )
        return {"version": 1, **result}

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "ActivityState":
        if raw.get("version") != 1:
            raise ValueError("unsupported state version")
        phase = Phase(str(raw["phase"]))
        due = raw.get("next_due_epoch")
        if due is not None and not isinstance(due, (int, float)):
            raise ValueError("invalid next_due_epoch")
        pending = raw.get("pending_reason")
        reason = BackupReason(str(pending)) if pending is not None else None
        state = cls(phase=phase, next_due_epoch=due, pending_reason=reason)
        if phase is Phase.IDLE and (due is not None or reason is not None):
            raise ValueError("idle state has pending work")
        if phase is Phase.ACTIVE and due is None:
            raise ValueError("active state has no deadline")
        if phase is Phase.BACKING_UP and reason is None:
            raise ValueError("backing_up state has no reason")
        return state


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> ActivityState:
        if not self.path.exists():
            return ActivityState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state root must be an object")
            return ActivityState.from_dict(raw)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ValueError("activity state is unreadable or invalid") from error

    def save(self, state: ActivityState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state.to_dict(), handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
            parent_descriptor = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        finally:
            temporary.unlink(missing_ok=True)
