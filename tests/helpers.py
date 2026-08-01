from __future__ import annotations

from pathlib import Path

from bedrock_activity_backup.config import Config


def make_config(base: Path, *, keep_snapshots: int = 4) -> Config:
    config = Config(
        bds_root=base / "bds",
        world_name="Test World",
        bds_service="minecraft-bedrock.service",
        console_fifo=base / "run" / "console",
        state_file=base / "run" / "state.json",
        backup_root=base / "backups" / "automatic",
        interval_seconds=1800,
        retry_seconds=300,
        keep_snapshots=keep_snapshots,
        min_free_bytes=512 * 1024**2,
        ready_timeout_seconds=30,
        resume_timeout_seconds=10,
        list_timeout_seconds=8,
    )
    config.validate()
    return config
