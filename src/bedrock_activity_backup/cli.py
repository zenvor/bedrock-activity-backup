from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .config import Config
from .console import BdsConsole
from .snapshot import SnapshotManager, SnapshotRotator
from .state import BackupReason, StateStore
from .watcher import ActivityWatcher, JournalFollower


DEFAULT_CONFIG = Path("/etc/bedrock-activity-backup/config.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bedrock-activity-backup")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("watch")
    backup = subparsers.add_parser("backup")
    backup.add_argument(
        "--reason",
        choices=[reason.value for reason in BackupReason],
        default=BackupReason.MANUAL.value,
    )
    subparsers.add_parser("prune")
    subparsers.add_parser("query-players")
    subparsers.add_parser("status")
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
            complete = sorted(
                path.name
                for path in config.snapshot_root.glob("*")
                if path.is_dir() and (path / "manifest.json").is_file()
            )
            print(
                json.dumps(
                    {
                        "state": state.to_dict(),
                        "complete_snapshots": len(complete),
                        "latest_snapshot": complete[-1] if complete else None,
                        "keep_snapshots": config.keep_snapshots,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        return 0
    except Exception as error:
        logging.getLogger(__name__).error("%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
