from __future__ import annotations

import argparse
import json
import logging
import os
import re
import stat
from pathlib import Path

from .config import Config
from .console import BdsConsole
from .errors import safe_error_label
from .snapshot import (
    SnapshotManager,
    SnapshotRehearsal,
    SnapshotRestorePlanner,
    SnapshotRotator,
    SnapshotVerifier,
    repository_lock,
    resolve_snapshot,
)
from .state import BackupReason, StateStore
from .watcher import ActivityWatcher, JournalFollower


DEFAULT_CONFIG = Path("/etc/bedrock-activity-backup/config.json")
REHEARSAL_ROOT = Path("/var/lib/bedrock-activity-backup/rehearsals")
_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bedrock-activity-backup")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("watch")
    backup = subparsers.add_parser("backup")
    backup.add_argument(
        "--reason",
        choices=[reason.value for reason in BackupReason],
        default=BackupReason.OPERATOR.value,
    )
    subparsers.add_parser("prune")
    subparsers.add_parser("query-players")
    subparsers.add_parser("status")
    verify = subparsers.add_parser("verify")
    verify.add_argument("snapshot")
    rehearse = subparsers.add_parser("rehearse")
    rehearse.add_argument("snapshot")
    rehearse.add_argument("label")
    restore_plan = subparsers.add_parser("restore-plan")
    restore_plan.add_argument("snapshot")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = Config.load(args.config)
        console = BdsConsole(config.bds_service, config.console_fifo)
        snapshots = SnapshotManager(config, console)
        if args.command == "watch":
            watcher = ActivityWatcher(
                config,
                console,
                snapshots,
                StateStore(config.state_file),
                JournalFollower(config.bds_service),
            )
            watcher.run()
        elif args.command == "backup":
            path = snapshots.create(BackupReason(args.reason))
            print(path)
        elif args.command == "prune":
            removed = SnapshotRotator(config).prune()
            print(json.dumps({"removed": len(removed)}))
        elif args.command == "query-players":
            console.assert_available()
            print(console.query_player_count(config.list_timeout_seconds))
        elif args.command == "status":
            state = StateStore(config.state_file).load()
            rotator = SnapshotRotator(config)
            with repository_lock(config):
                complete = [path.name for path in rotator.complete_snapshots()]
                latest = config.backup_root / "latest"
                latest_name = None
                if latest.is_symlink():
                    try:
                        candidate = latest.resolve(strict=True)
                        if rotator.verifier.is_owned_complete(candidate):
                            latest_name = candidate.name
                    except OSError:
                        pass
            print(
                json.dumps(
                    {
                        "state": state.to_dict(),
                        "complete_snapshots": len(complete),
                        "latest_snapshot": latest_name,
                        "keep_snapshots": config.keep_snapshots,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "verify":
            snapshot = resolve_snapshot(config, args.snapshot)
            with repository_lock(config):
                result = SnapshotVerifier(config).verify(snapshot)
            print(
                json.dumps(
                    result,
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "rehearse":
            if not _LABEL_PATTERN.fullmatch(args.label) or args.label in {".", ".."}:
                raise ValueError("rehearsal label is invalid")
            _ensure_secure_rehearsal_root()
            snapshot = resolve_snapshot(config, args.snapshot)
            destination = REHEARSAL_ROOT / args.label
            print(
                json.dumps(
                    SnapshotRehearsal(config).run(snapshot, destination),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "restore-plan":
            snapshot = resolve_snapshot(config, args.snapshot)
            print(
                json.dumps(
                    SnapshotRestorePlanner(config).build(snapshot),
                    indent=2,
                    sort_keys=True,
                )
            )
        return 0
    except Exception as error:
        logging.getLogger(__name__).error(
            "Command failed (%s)", safe_error_label(error)
        )
        return 1


def _ensure_secure_rehearsal_root() -> None:
    parent = REHEARSAL_ROOT.parent
    parent.mkdir(parents=False, exist_ok=True, mode=0o700)
    REHEARSAL_ROOT.mkdir(parents=False, exist_ok=True, mode=0o700)
    for path in (parent, REHEARSAL_ROOT):
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o077
        ):
            raise RuntimeError("rehearsal storage is not root-owned and private")
    if os.geteuid() != 0:
        raise RuntimeError("rehearsal requires root privileges")


if __name__ == "__main__":
    raise SystemExit(main())
