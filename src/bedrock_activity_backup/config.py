from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


_UNIT_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")
_RSYNC_FILTER_META_PATTERN = re.compile(r"[?*\[\\\r\n]")

MANAGED_BDS_ROOT = Path("/opt/minecraft-bedrock")
MANAGED_BDS_SERVICE = "minecraft-bedrock.service"
MANAGED_CONSOLE_FIFO = Path("/run/minecraft-bedrock/console")
MANAGED_STATE_FILE = Path("/run/minecraft-bedrock/activity-backup-state.json")
MANAGED_BACKUP_ROOT = Path("/opt/minecraft-bedrock/backups/automatic")


@dataclass(frozen=True)
class Config:
    bds_root: Path
    world_name: str
    bds_service: str
    console_fifo: Path
    state_file: Path
    backup_root: Path
    interval_seconds: int = 1800
    retry_seconds: int = 300
    keep_snapshots: int = 4
    min_free_bytes: int = 5 * 1024**3
    ready_timeout_seconds: int = 30
    resume_timeout_seconds: int = 10
    list_timeout_seconds: int = 8
    verification_timeout_seconds: int = 90
    verification_server_port: int = 19134
    verification_attempts: int = 2

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("configuration is unreadable or invalid JSON") from error

        required = {
            "bds_root",
            "world_name",
            "bds_service",
            "console_fifo",
            "state_file",
            "backup_root",
        }
        missing = sorted(required.difference(raw))
        if missing:
            raise ValueError(f"configuration is missing required fields: {', '.join(missing)}")

        known = {field.name for field in cls.__dataclass_fields__.values()}
        unknown = sorted(set(raw).difference(known))
        if unknown:
            raise ValueError(f"configuration contains unknown fields: {', '.join(unknown)}")

        for key in ("bds_root", "console_fifo", "state_file", "backup_root"):
            raw[key] = Path(raw[key])
        config = cls(**raw)
        config.validate()
        return config

    def validate(self, *, require_managed_layout: bool = True) -> None:
        for label, value in (
            ("bds_root", self.bds_root),
            ("console_fifo", self.console_fifo),
            ("state_file", self.state_file),
            ("backup_root", self.backup_root),
        ):
            if not value.is_absolute():
                raise ValueError(f"{label} must be an absolute path")

        if not self.world_name or self.world_name in {".", ".."}:
            raise ValueError("world_name must not be empty")
        if "/" in self.world_name or "\0" in self.world_name:
            raise ValueError("world_name must be one directory name")
        if _RSYNC_FILTER_META_PATTERN.search(self.world_name):
            raise ValueError("world_name contains unsupported rsync filter characters")
        if not _UNIT_PATTERN.fullmatch(self.bds_service):
            raise ValueError("bds_service must be a systemd .service unit name")
        if not 60 <= self.interval_seconds <= 24 * 60 * 60:
            raise ValueError("interval_seconds must be between 60 seconds and 24 hours")
        if not 30 <= self.retry_seconds <= self.interval_seconds:
            raise ValueError("retry_seconds must be between 30 seconds and interval_seconds")
        if not 2 <= self.keep_snapshots <= 20:
            raise ValueError("keep_snapshots must be between 2 and 20")
        if self.min_free_bytes < 512 * 1024**2:
            raise ValueError("min_free_bytes must be at least 512 MiB")
        for label, value in (
            ("ready_timeout_seconds", self.ready_timeout_seconds),
            ("resume_timeout_seconds", self.resume_timeout_seconds),
            ("list_timeout_seconds", self.list_timeout_seconds),
            ("verification_timeout_seconds", self.verification_timeout_seconds),
        ):
            if not 1 <= value <= 300:
                raise ValueError(f"{label} must be between 1 and 300")
        if not 1024 <= self.verification_server_port <= 65534:
            raise ValueError("verification_server_port must be between 1024 and 65534")
        if not 1 <= self.verification_attempts <= 3:
            raise ValueError("verification_attempts must be between 1 and 3")
        if (
            self.ready_timeout_seconds
            + self.resume_timeout_seconds
            + self.list_timeout_seconds
            > 120
        ):
            raise ValueError("console timeouts exceed the managed stop-time budget")

        world = self.world_path.resolve(strict=False)
        backup = self.backup_root.resolve(strict=False)
        if world == backup or world in backup.parents or backup in world.parents:
            raise ValueError("backup_root and world_path must not overlap")

        if require_managed_layout:
            expected = {
                "bds_root": MANAGED_BDS_ROOT,
                "bds_service": MANAGED_BDS_SERVICE,
                "console_fifo": MANAGED_CONSOLE_FIFO,
                "state_file": MANAGED_STATE_FILE,
                "backup_root": MANAGED_BACKUP_ROOT,
            }
            actual = {
                "bds_root": self.bds_root,
                "bds_service": self.bds_service,
                "console_fifo": self.console_fifo,
                "state_file": self.state_file,
                "backup_root": self.backup_root,
            }
            if actual != expected:
                raise ValueError("configuration does not match the managed-v1 layout")

    @property
    def world_path(self) -> Path:
        return self.bds_root / "worlds" / self.world_name

    @property
    def snapshot_root(self) -> Path:
        return self.backup_root / "snapshots"
