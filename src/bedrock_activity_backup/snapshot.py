from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .console import BdsConsole
from .state import BackupReason


_SNAPSHOT_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[a-f0-9]{8}$")
_MANIFEST_PATTERN = re.compile(r"^MANIFEST-\d+$")


class SnapshotRotator:
    def __init__(self, config: Config):
        self.config = config

    def prune(self) -> list[str]:
        root = self.config.snapshot_root
        if not root.is_dir():
            return []
        complete = sorted(
            path
            for path in root.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and _SNAPSHOT_PATTERN.fullmatch(path.name)
            and (path / "manifest.json").is_file()
        )
        protected = {
            path.resolve() for path in complete[-self.config.keep_snapshots :]
        }
        latest_link = self.config.backup_root / "latest"
        if latest_link.is_symlink():
            try:
                latest = latest_link.resolve(strict=True)
                latest.relative_to(root.resolve())
                protected.add(latest.resolve())
            except (OSError, ValueError):
                pass
        removable = [path for path in complete if path.resolve() not in protected]
        removed = []
        for path in removable:
            shutil.rmtree(path)
            removed.append(path.name)

        stale_before = dt.datetime.now(dt.timezone.utc).timestamp() - 24 * 60 * 60
        for path in root.glob(".incomplete-*"):
            try:
                if path.is_dir() and path.stat().st_mtime < stale_before:
                    shutil.rmtree(path)
            except FileNotFoundError:
                pass
        return removed


class SnapshotManager:
    def __init__(
        self,
        config: Config,
        console: BdsConsole,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        now: Callable[[], dt.datetime] | None = None,
        token: Callable[[], str] | None = None,
    ):
        self.config = config
        self.console = console
        self._run = run
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self._token = token or (lambda: secrets.token_hex(4))
        self.rotator = SnapshotRotator(config)

    def create(self, reason: BackupReason) -> Path:
        self.config.backup_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.config.snapshot_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        lock_path = self.config.backup_root / "backup.lock"
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("another snapshot is already running") from error
            return self._create_locked(reason)

    def _create_locked(self, reason: BackupReason) -> Path:
        self.console.assert_available()
        if not self.config.world_path.is_dir():
            raise RuntimeError("configured world directory is missing")
        available = shutil.disk_usage(self.config.backup_root).free
        if available < self.config.min_free_bytes:
            raise RuntimeError("free disk space is below the configured safety threshold")

        created = self._now().astimezone(dt.timezone.utc)
        timestamp = created.strftime("%Y%m%dT%H%M%SZ")
        final = self.config.snapshot_root / f"{timestamp}-{self._token()}"
        if final.exists():
            raise RuntimeError("snapshot destination already exists")
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".incomplete-{timestamp}-", dir=self.config.snapshot_root
            )
        )
        try:
            with self.console.paused_saves(
                self.config.ready_timeout_seconds,
                self.config.resume_timeout_seconds,
            ):
                stats = self._copy_payload(temporary)
            metadata = self._validate_and_describe(temporary, created, reason, stats)
            self._write_json(temporary / "manifest.json", metadata)
            os.rename(temporary, final)
            self._fsync_directory(self.config.snapshot_root)
            self._update_latest(final)
            self.rotator.prune()
            return final
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _copy_payload(self, temporary: Path) -> str:
        payload = temporary / "payload"
        payload.mkdir(mode=0o750)
        previous = self._latest_snapshot()
        command = [
            "rsync",
            "-a",
            "--delete",
            "--numeric-ids",
            "--stats",
            "--chown=root:root",
            "--chmod=Du=rwx,Dg=rx,Do=,Fu=rw,Fg=r,Fo=",
        ]
        if previous is not None:
            command.append(f"--link-dest={previous / 'payload'}")
        command.extend(
            [
                "--include=/worlds/",
                f"--include=/worlds/{self.config.world_name}/",
                f"--include=/worlds/{self.config.world_name}/***",
                "--include=/server.properties",
                "--include=/allowlist.json",
                "--include=/permissions.json",
                "--include=/valid_known_packs.json",
                "--exclude=*",
                f"{self.config.bds_root}/",
                f"{payload}/",
            ]
        )
        result = self._run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        (temporary / "rsync-stats.txt").write_text(result.stdout, encoding="utf-8")
        return result.stdout

    def _validate_and_describe(
        self,
        temporary: Path,
        created: dt.datetime,
        reason: BackupReason,
        stats: str,
    ) -> dict[str, object]:
        payload = temporary / "payload"
        world = payload / "worlds" / self.config.world_name
        current = world / "db" / "CURRENT"
        level = world / "level.dat"
        properties = payload / "server.properties"
        for required in (current, level, properties):
            if not required.is_file():
                raise RuntimeError("snapshot validation failed: a required file is missing")
        manifest_name = current.read_text(encoding="ascii").strip()
        if not _MANIFEST_PATTERN.fullmatch(manifest_name):
            raise RuntimeError("snapshot validation failed: invalid LevelDB manifest name")
        manifest = world / "db" / manifest_name
        if not manifest.is_file():
            raise RuntimeError("snapshot validation failed: LevelDB manifest is missing")

        checksums = {
            str(path.relative_to(temporary)): self._sha256(path)
            for path in (level, current, manifest, properties)
        }
        apparent_size = sum(
            path.stat().st_size
            for path in payload.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        return {
            "schema_version": 1,
            "created_at": created.isoformat(),
            "reason": reason.value,
            "world_name": self.config.world_name,
            "method": "bds-save-hold-rsync-link-dest",
            "keep_snapshots": self.config.keep_snapshots,
            "leveldb_manifest": manifest_name,
            "apparent_payload_bytes": apparent_size,
            "checksums_sha256": checksums,
            "rsync_stats_recorded": bool(stats),
        }

    def _latest_snapshot(self) -> Path | None:
        latest = self.config.backup_root / "latest"
        if not latest.is_symlink():
            return None
        try:
            candidate = latest.resolve(strict=True)
            candidate.relative_to(self.config.snapshot_root.resolve())
        except (OSError, ValueError):
            return None
        if not (candidate / "manifest.json").is_file():
            return None
        return candidate

    def _update_latest(self, final: Path) -> None:
        latest = self.config.backup_root / "latest"
        temporary = self.config.backup_root / f".latest-{self._token()}"
        relative = Path("snapshots") / final.name
        os.symlink(relative, temporary)
        os.replace(temporary, latest)
        self._fsync_directory(self.config.backup_root)

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o640)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
