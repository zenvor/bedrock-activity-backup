from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bedrock_activity_backup.config import Config
from bedrock_activity_backup.snapshot import SCHEMA_VERSION, SNAPSHOT_METHOD, TOOL_ID


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
        verification_timeout_seconds=90,
        verification_server_port=19134,
        verification_attempts=2,
    )
    config.validate(require_managed_layout=False)
    return config


def create_owned_snapshot(
    config: Config,
    name: str,
    *,
    previous: str | None = None,
    marker: bytes = b"fixture",
) -> Path:
    snapshot = config.snapshot_root / name
    world = snapshot / "payload" / "worlds" / config.world_name
    database = world / "db"
    database.mkdir(parents=True)
    (world / "level.dat").write_bytes(b"level-" + marker)
    (database / "CURRENT").write_text("MANIFEST-000001\n", encoding="ascii")
    (database / "MANIFEST-000001").write_bytes(b"manifest-" + marker)
    (snapshot / "payload" / "server.properties").write_text(
        f"level-name={config.world_name}\n", encoding="utf-8"
    )
    owner = {"tool": TOOL_ID, "schema_version": SCHEMA_VERSION}
    (snapshot / ".owner.json").write_text(json.dumps(owner), encoding="utf-8")
    paths = (
        world / "level.dat",
        database / "CURRENT",
        database / "MANIFEST-000001",
        snapshot / "payload" / "server.properties",
    )
    checksums = {
        str(path.relative_to(snapshot)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_ID,
        "method": SNAPSHOT_METHOD,
        "world_name": config.world_name,
        "leveldb_manifest": "MANIFEST-000001",
        "previous_snapshot": previous,
        "checksums_sha256": checksums,
        "snapshot_state": "verified",
    }
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return snapshot
