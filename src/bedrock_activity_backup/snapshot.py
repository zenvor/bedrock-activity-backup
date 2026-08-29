from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import select
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath

from .config import Config
from .console import BdsConsole
from .errors import safe_error_label
from .state import BackupReason


LOGGER = logging.getLogger(__name__)
TOOL_ID = "bedrock-activity-backup"
SCHEMA_VERSION = 3
SNAPSHOT_METHOD = "bds-save-hold-rsync-link-dest"
_SNAPSHOT_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[a-f0-9]{8}$")
_MANIFEST_PATTERN = re.compile(r"^MANIFEST-\d+$")
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_SNAPSHOT_STATES = frozenset({"pending", "verified", "failed"})
_LEVELDB_FAILURE_PATTERN = re.compile(
    r"(?:leveldb.*(?:corruption|repair)|(?:corruption|repair).*leveldb|"
    r"(?:missing|not found).{0,80}\.ldb|\.ldb.{0,80}(?:missing|not found))",
    re.IGNORECASE,
)


class SnapshotValidationFailed(RuntimeError):
    """A durable snapshot failed isolated LevelDB recovery validation."""

    def __init__(
        self, snapshot: Path, cause: Exception, details: dict[str, object] | None = None
    ):
        super().__init__("isolated snapshot validation failed")
        self.snapshot = snapshot
        self.cause = cause
        self.details = details


class IsolatedBdsValidator:
    """Boot a copied snapshot with BDS away from the production world.

    BDS is the LevelDB reader of record.  A successful process start is insufficient:
    repair, corruption, and missing-ldb log signatures are hard failures.
    """

    def __init__(
        self,
        config: Config,
        *,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self._popen = popen
        self._monotonic = monotonic
        self._sleep = sleep

    def verify(self, snapshot: Path) -> dict[str, object]:
        stage = Path(
            tempfile.mkdtemp(prefix=f".verify-{snapshot.name}-", dir=self.config.backup_root)
        )
        transcript: list[str] = []
        process: subprocess.Popen[str] | None = None
        try:
            self._copy_runtime(stage, snapshot)
            binary = stage / "bedrock_server"
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise RuntimeError("isolated BDS executable is unavailable")
            process = self._popen(
                [str(binary)],
                cwd=stage,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            if process.stdout is None:
                raise RuntimeError("isolated BDS has no log stream")
            deadline = self._monotonic() + self.config.verification_timeout_seconds
            startup_grace_deadline: float | None = None
            while self._monotonic() < deadline:
                ready, _unused, _errors = select.select([process.stdout], [], [], 0.1)
                line = process.stdout.readline() if ready else ""
                if line:
                    transcript.append(line.rstrip())
                    if _LEVELDB_FAILURE_PATTERN.search(line):
                        raise RuntimeError("isolated BDS reported LevelDB repair or missing files")
                    if "Server started." in line:
                        startup_grace_deadline = self._monotonic() + 1.0
                    continue
                if process.poll() is not None:
                    raise RuntimeError("isolated BDS exited before startup")
                if (
                    startup_grace_deadline is not None
                    and self._monotonic() >= startup_grace_deadline
                ):
                    return {
                        "validator": "isolated-bds",
                        "result": "passed",
                        "log_lines": len(transcript),
                    }
                self._sleep(0.1)
            raise RuntimeError("isolated BDS validation timed out")
        except Exception as error:
            return {
                "validator": "isolated-bds",
                "result": "failed",
                "reason": safe_error_label(error),
                "log_lines": len(transcript),
            }
        finally:
            if process is not None and process.poll() is None:
                try:
                    if process.stdin is not None:
                        process.stdin.write("stop\n")
                        process.stdin.flush()
                    process.wait(timeout=15)
                except Exception:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            shutil.rmtree(stage, ignore_errors=True)

    def _copy_runtime(self, stage: Path, snapshot: Path) -> None:
        excluded_top_level = {"worlds"}
        try:
            excluded_top_level.add(
                self.config.backup_root.relative_to(self.config.bds_root).parts[0]
            )
        except ValueError:
            pass

        def ignore(_directory: str, names: list[str]) -> set[str]:
            if Path(_directory) == self.config.bds_root:
                return excluded_top_level.intersection(names)
            return set()

        shutil.copytree(self.config.bds_root, stage, ignore=ignore, dirs_exist_ok=True)
        shutil.copytree(snapshot / "payload" / "worlds", stage / "worlds")
        for name in (
            "server.properties",
            "allowlist.json",
            "permissions.json",
            "valid_known_packs.json",
        ):
            source = snapshot / "payload" / name
            if source.is_file():
                shutil.copy2(source, stage / name)
        properties = stage / "server.properties"
        lines = properties.read_text(encoding="utf-8").splitlines()
        overrides = {
            "server-port": str(self.config.verification_server_port),
            "server-portv6": str(self.config.verification_server_port + 1),
            "online-mode": "false",
            "allow-list": "false",
        }
        replaced: set[str] = set()
        rewritten = []
        for line in lines:
            key, separator, _value = line.partition("=")
            if separator and key in overrides:
                rewritten.append(f"{key}={overrides[key]}")
                replaced.add(key)
            else:
                rewritten.append(line)
        rewritten.extend(f"{key}={value}" for key, value in overrides.items() if key not in replaced)
        properties.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


@contextlib.contextmanager
def repository_lock(config: Config) -> Iterator[None]:
    config.backup_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    lock_path = config.backup_root / "backup.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("snapshot repository is busy") from error
        yield


def resolve_snapshot(config: Config, name: str) -> Path:
    if not _SNAPSHOT_PATTERN.fullmatch(name) or Path(name).name != name:
        raise ValueError("snapshot name is invalid")
    return config.snapshot_root / name


class SnapshotVerifier:
    def __init__(self, config: Config):
        self.config = config

    def is_owned_complete(self, path: Path) -> bool:
        return self.is_owned_verified(path)

    def is_owned_verified(self, path: Path) -> bool:
        try:
            result = self.verify(path, require_current_schema=True)
            return result["snapshot_state"] == "verified"
        except (OSError, UnicodeError, ValueError, RuntimeError):
            return False

    def verify(
        self, path: Path, *, require_current_schema: bool = False
    ) -> dict[str, object]:
        manifest = self._validate_structure(path)
        if require_current_schema and manifest["schema_version"] != SCHEMA_VERSION:
            raise RuntimeError("snapshot schema is read-only legacy data")
        checksums = manifest.get("checksums_sha256")
        if not isinstance(checksums, dict) or not checksums:
            raise RuntimeError("snapshot manifest has no checksums")
        payload = path / "payload"
        if manifest["schema_version"] == SCHEMA_VERSION:
            payload_entries = list(payload.rglob("*"))
            if any(entry.is_symlink() for entry in payload_entries):
                raise RuntimeError("snapshot payload contains a symbolic link")
            regular_files = {
                str(entry.relative_to(path))
                for entry in payload_entries
                if entry.is_file()
            }
            if set(checksums) != regular_files:
                raise RuntimeError("snapshot checksums do not cover the full payload")
        manifest_name = str(manifest["leveldb_manifest"])
        required_checksums = {
            f"payload/worlds/{self.config.world_name}/level.dat",
            f"payload/worlds/{self.config.world_name}/db/CURRENT",
            f"payload/worlds/{self.config.world_name}/db/{manifest_name}",
            "payload/server.properties",
        }
        if not required_checksums.issubset(checksums):
            raise RuntimeError("snapshot manifest omits a required checksum")
        verified = 0
        root = path.resolve(strict=True)
        for relative_raw, expected_raw in checksums.items():
            if not isinstance(relative_raw, str) or not isinstance(expected_raw, str):
                raise RuntimeError("snapshot manifest contains an invalid checksum entry")
            relative = PurePosixPath(relative_raw)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("snapshot manifest contains an unsafe relative path")
            candidate = path.joinpath(*relative.parts)
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as error:
                raise RuntimeError("snapshot checksum path is unavailable") from error
            if not candidate.is_file() or candidate.is_symlink():
                raise RuntimeError("snapshot checksum target is not a regular file")
            if not re.fullmatch(r"[a-f0-9]{64}", expected_raw):
                raise RuntimeError("snapshot manifest contains an invalid SHA-256")
            if self._sha256(candidate) != expected_raw:
                raise RuntimeError("snapshot checksum verification failed")
            verified += 1
        return {
            "snapshot": path.name,
            "schema_version": manifest["schema_version"],
            "verified_files": verified,
            "world_matches": True,
            "snapshot_state": manifest.get("snapshot_state", "legacy"),
        }

    def manifest(self, path: Path) -> dict[str, object]:
        return self._validate_structure(path)

    def _validate_structure(self, path: Path) -> dict[str, object]:
        if (
            not _SNAPSHOT_PATTERN.fullmatch(path.name)
            or not path.is_dir()
            or path.is_symlink()
            or path.parent.resolve(strict=False)
            != self.config.snapshot_root.resolve(strict=False)
        ):
            raise RuntimeError("snapshot is not an owned repository entry")
        manifest_path = path / "manifest.json"
        if (
            not manifest_path.is_file()
            or manifest_path.is_symlink()
            or manifest_path.stat().st_size > _MAX_MANIFEST_BYTES
        ):
            raise RuntimeError("snapshot manifest is unavailable")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("snapshot manifest is invalid") from error
        if not isinstance(manifest, dict):
            raise RuntimeError("snapshot manifest root is invalid")
        schema = manifest.get("schema_version")
        if schema not in {1, 2, SCHEMA_VERSION}:
            raise RuntimeError("snapshot schema is unsupported")
        if schema in {2, SCHEMA_VERSION} and manifest.get("tool") != TOOL_ID:
            raise RuntimeError("snapshot ownership marker is invalid")
        if schema in {2, SCHEMA_VERSION}:
            owner_path = path / ".owner.json"
            if (
                not owner_path.is_file()
                or owner_path.is_symlink()
                or owner_path.stat().st_size > _MAX_MANIFEST_BYTES
            ):
                raise RuntimeError("snapshot owner marker is unavailable")
            try:
                owner = json.loads(owner_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise RuntimeError("snapshot owner marker is unavailable") from error
            if (
                not isinstance(owner, dict)
                or owner.get("tool") != TOOL_ID
                or owner.get("schema_version") != schema
            ):
                raise RuntimeError("snapshot owner marker is invalid")
        if schema == SCHEMA_VERSION:
            state = manifest.get("snapshot_state")
            if state not in _SNAPSHOT_STATES:
                raise RuntimeError("snapshot state is invalid")
        if manifest.get("method") != SNAPSHOT_METHOD:
            raise RuntimeError("snapshot method marker is invalid")
        if manifest.get("world_name") != self.config.world_name:
            raise RuntimeError("snapshot belongs to a different world")
        world = path / "payload" / "worlds" / self.config.world_name
        current = world / "db" / "CURRENT"
        level = world / "level.dat"
        properties = path / "payload" / "server.properties"
        for required in (current, level, properties):
            if not required.is_file() or required.is_symlink():
                raise RuntimeError("snapshot required file is unavailable")
        try:
            manifest_name = current.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as error:
            raise RuntimeError("snapshot LevelDB CURRENT is invalid") from error
        if (
            not _MANIFEST_PATTERN.fullmatch(manifest_name)
            or manifest.get("leveldb_manifest") != manifest_name
        ):
            raise RuntimeError("snapshot LevelDB manifest reference is invalid")
        database_manifest = world / "db" / manifest_name
        if not database_manifest.is_file() or database_manifest.is_symlink():
            raise RuntimeError("snapshot LevelDB manifest is unavailable")
        return manifest

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


class SnapshotRotator:
    def __init__(self, config: Config, *, token: Callable[[], str] | None = None):
        self.config = config
        self.verifier = SnapshotVerifier(config)
        self._token = token or (lambda: secrets.token_hex(4))

    def complete_snapshots(self) -> list[Path]:
        root = self.config.snapshot_root
        if not root.is_dir():
            return []
        return sorted(
            path for path in root.iterdir() if self.verifier.is_owned_complete(path)
        )

    def maintain(self) -> list[str]:
        with repository_lock(self.config):
            complete = self.complete_snapshots()
            desired = self._head_snapshot(complete)
            if desired is not None:
                self._set_latest_locked(desired)
            return self._prune_locked()

    def prune(self) -> list[str]:
        with repository_lock(self.config):
            return self._prune_locked()

    def _prune_locked(self, extra_protected: tuple[Path, ...] = ()) -> list[str]:
        root = self.config.snapshot_root
        if not root.is_dir():
            return []
        complete = self.complete_snapshots()
        newest_first = self._newest_first(complete)
        protected = {
            path.resolve() for path in newest_first[: self.config.keep_snapshots]
        }
        protected.update(path.resolve() for path in extra_protected if path.exists())
        latest_link = self.config.backup_root / "latest"
        if latest_link.is_symlink():
            try:
                latest = latest_link.resolve(strict=True)
                latest.relative_to(root.resolve())
                if self.verifier.is_owned_complete(latest):
                    protected.add(latest)
            except (OSError, ValueError):
                pass
        removable = [path for path in complete if path.resolve() not in protected]
        removed = []
        for path in removable:
            shutil.rmtree(path)
            removed.append(path.name)

        return removed

    def _head_snapshot(self, complete: list[Path]) -> Path | None:
        if not complete:
            return None
        names = {path.name: path for path in complete}
        referenced = set()
        for path in complete:
            previous = self.verifier.manifest(path).get("previous_snapshot")
            if isinstance(previous, str) and previous in names:
                referenced.add(previous)
        heads = [path for path in complete if path.name not in referenced]
        if len(heads) == 1:
            return heads[0]
        return max(heads or complete, key=lambda path: path.name)

    def _newest_first(self, complete: list[Path]) -> list[Path]:
        if not complete:
            return []
        names = {path.name: path for path in complete}
        ordered: list[Path] = []
        seen: set[str] = set()
        current = self._head_snapshot(complete)
        while current is not None and current.name not in seen:
            ordered.append(current)
            seen.add(current.name)
            previous = self.verifier.manifest(current).get("previous_snapshot")
            current = names.get(previous) if isinstance(previous, str) else None
        ordered.extend(
            path for path in sorted(complete, reverse=True) if path.name not in seen
        )
        return ordered

    def _set_latest_locked(self, final: Path) -> None:
        latest = self.config.backup_root / "latest"
        if latest.is_symlink():
            try:
                if latest.resolve(strict=True) == final.resolve(strict=True):
                    return
            except OSError:
                pass
        temporary = self.config.backup_root / f".latest-{self._token()}"
        try:
            os.symlink(Path("snapshots") / final.name, temporary)
            os.replace(temporary, latest)
            _fsync_directory(self.config.backup_root)
        finally:
            temporary.unlink(missing_ok=True)


class SnapshotManager:
    def __init__(
        self,
        config: Config,
        console: BdsConsole,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        now: Callable[[], dt.datetime] | None = None,
        token: Callable[[], str] | None = None,
        validator: IsolatedBdsValidator | None = None,
    ):
        self.config = config
        self.console = console
        self._run = run
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self._token = token or (lambda: secrets.token_hex(4))
        self.rotator = SnapshotRotator(config, token=self._token)
        self.verifier = self.rotator.verifier
        self.validator = validator or IsolatedBdsValidator(config)

    def maintain(self) -> list[str]:
        return self.rotator.maintain()

    def create(self, reason: BackupReason) -> Path:
        self.config.snapshot_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        with repository_lock(self.config):
            run_started = self._timestamp()
            before = self._snapshot_lists()
            attempts: list[dict[str, object]] = []
            for attempt in range(1, self.config.verification_attempts + 1):
                try:
                    final, details = self._create_locked(reason, attempt)
                    attempts.append(details)
                    removed = self._post_publish_maintenance(final)
                    self._write_run_record(
                        run_started, reason, before, attempts, "verified", None, removed
                    )
                    return final
                except SnapshotValidationFailed as error:
                    details = error.details or {
                        "attempt": attempt,
                        "snapshot_path": str(error.snapshot),
                    }
                    details["verification_result"] = "failed"
                    details["failure_reason"] = safe_error_label(error.cause)
                    details["finished_at"] = self._timestamp()
                    attempts.append(details)
                    if attempt == self.config.verification_attempts:
                        self._write_run_record(
                            run_started,
                            reason,
                            before,
                            attempts,
                            "failed",
                            safe_error_label(error.cause),
                            [],
                        )
                        raise
                    LOGGER.warning(
                        "Snapshot verification failed; creating an immediate retry "
                        "(attempt=%d/%d)",
                        attempt,
                        self.config.verification_attempts,
                    )
                except Exception as error:
                    attempts.append(
                        {
                            "attempt": attempt,
                            "snapshot_path": None,
                            "verification_result": "not-reached",
                            "failure_reason": safe_error_label(error),
                            "finished_at": self._timestamp(),
                        }
                    )
                    self._write_run_record(
                        run_started, reason, before, attempts, "failed", safe_error_label(error), []
                    )
                    raise
        raise AssertionError("snapshot retry loop ended unexpectedly")

    def _create_locked(self, reason: BackupReason, attempt: int) -> tuple[Path, dict[str, object]]:
        self.console.assert_available()
        if not self.config.world_path.is_dir():
            raise RuntimeError("configured world directory is missing")
        available = shutil.disk_usage(self.config.backup_root).free
        worst_case_copy = _apparent_bytes(self.config.world_path)
        for name in (
            "server.properties",
            "allowlist.json",
            "permissions.json",
            "valid_known_packs.json",
        ):
            path = self.config.bds_root / name
            if path.is_file() and not path.is_symlink():
                worst_case_copy += path.stat().st_size
        if available < self.config.min_free_bytes + worst_case_copy:
            raise RuntimeError("free disk space is below the configured safety threshold")

        created = self._now().astimezone(dt.timezone.utc)
        timestamp = created.strftime("%Y%m%dT%H%M%SZ")
        final = self.config.snapshot_root / f"{timestamp}-{self._token()}"
        if final.exists():
            raise RuntimeError("snapshot destination already exists")
        temporary = Path(tempfile.mkdtemp(prefix=f".pending-{timestamp}-", dir=self.config.snapshot_root))
        published = False
        details: dict[str, object] = {
            "attempt": attempt,
            "created_at": created.isoformat(),
            "snapshot_path": str(final),
            "save_hold_started_at": self._timestamp(),
            "save_pause_confirmed_at": None,
            "copy_started_at": None,
            "copy_finished_at": None,
            "save_resume_confirmed_at": None,
            "file_count": None,
            "apparent_payload_bytes": None,
            "checksum_entries": None,
            "verification_result": "pending",
        }
        try:
            _write_json(
                temporary / ".owner.json",
                {"tool": TOOL_ID, "schema_version": SCHEMA_VERSION},
                mode=0o600,
            )
            previous = self._latest_snapshot()
            with self.console.paused_saves(
                self.config.ready_timeout_seconds,
                self.config.resume_timeout_seconds,
            ):
                details["save_pause_confirmed_at"] = self._timestamp()
                details["copy_started_at"] = self._timestamp()
                stats = self._copy_payload(temporary, previous)
                details["copy_finished_at"] = self._timestamp()
            details["save_resume_confirmed_at"] = self._timestamp()
            metadata = self._validate_and_describe(
                temporary, created, reason, stats, previous
            )
            metadata["snapshot_state"] = "pending"
            _write_json(
                temporary / "manifest.json",
                metadata,
                mode=0o640,
                max_bytes=_MAX_MANIFEST_BYTES,
            )
            _fsync_directory(temporary)
            os.rename(temporary, final)
            published = True
            details.update(
                {
                    "file_count": len(metadata["checksums_sha256"]),
                    "apparent_payload_bytes": metadata["apparent_payload_bytes"],
                    "checksum_entries": len(metadata["checksums_sha256"]),
                    "pending_published_at": self._timestamp(),
                }
            )
            try:
                validation = self.validator.verify(final)
                if not isinstance(validation, dict):
                    raise RuntimeError("isolated validator returned an invalid result")
            except Exception as error:
                validation = {
                    "validator": "isolated-bds",
                    "result": "failed",
                    "reason": safe_error_label(error),
                }
                self._set_snapshot_state(final, "failed", validation)
                raise SnapshotValidationFailed(final, error, details) from error
            details["verification"] = validation
            if validation.get("result") != "passed":
                self._set_snapshot_state(final, "failed", validation)
                raise SnapshotValidationFailed(
                    final, RuntimeError("isolated validator rejected snapshot"), details
                )
            try:
                self.verifier.verify(final, require_current_schema=True)
            except Exception as error:
                validation = {
                    "validator": "post-validation-checksum",
                    "result": "failed",
                    "reason": safe_error_label(error),
                }
                self._set_snapshot_state(final, "failed", validation)
                raise SnapshotValidationFailed(final, error, details) from error
            self._set_snapshot_state(final, "verified", validation)
            details["verification_result"] = "verified"
            details["finished_at"] = self._timestamp()
            return final, details
        except Exception:
            if not published:
                shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _post_publish_maintenance(self, final: Path) -> list[str]:
        try:
            _fsync_directory(self.config.snapshot_root)
        except Exception as error:
            LOGGER.error(
                "Snapshot was published but snapshot-directory-sync failed (%s); "
                "rotation was skipped",
                safe_error_label(error),
            )
            return []
        try:
            self.rotator._set_latest_locked(final)
        except Exception as error:
            LOGGER.error(
                "Snapshot was published but latest-update failed (%s)",
                safe_error_label(error),
            )
        try:
            return self.rotator._prune_locked((final,))
        except Exception as error:
            LOGGER.error(
                "Snapshot was published but snapshot-rotation failed (%s)",
                safe_error_label(error),
            )
        return []

    def _copy_payload(self, temporary: Path, previous: Path | None) -> str:
        payload = temporary / "payload"
        payload.mkdir(mode=0o750)
        command = [
            "rsync",
            "-a",
            "--delete",
            "--numeric-ids",
            "--stats",
            "--fsync",
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
        _write_text(temporary / "rsync-stats.txt", result.stdout, mode=0o640)
        _fsync_tree_directories(payload)
        return result.stdout

    def _validate_and_describe(
        self,
        temporary: Path,
        created: dt.datetime,
        reason: BackupReason,
        stats: str,
        previous: Path | None,
    ) -> dict[str, object]:
        payload = temporary / "payload"
        world = payload / "worlds" / self.config.world_name
        current = world / "db" / "CURRENT"
        level = world / "level.dat"
        properties = payload / "server.properties"
        for required in (current, level, properties):
            if not required.is_file() or required.is_symlink():
                raise RuntimeError("snapshot validation failed: a required file is missing")
        manifest_name = current.read_text(encoding="ascii").strip()
        if not _MANIFEST_PATTERN.fullmatch(manifest_name):
            raise RuntimeError("snapshot validation failed: invalid LevelDB manifest name")
        database_manifest = world / "db" / manifest_name
        if not database_manifest.is_file() or database_manifest.is_symlink():
            raise RuntimeError("snapshot validation failed: LevelDB manifest is missing")

        checksum_paths = sorted(
            path
            for path in payload.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        if any(path.is_symlink() for path in payload.rglob("*")):
            raise RuntimeError("snapshot validation failed: payload contains a symlink")
        checksums = {
            str(path.relative_to(temporary)): SnapshotVerifier._sha256(path)
            for path in checksum_paths
        }
        apparent_size = sum(
            path.stat().st_size
            for path in payload.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "tool": TOOL_ID,
            "created_at": created.isoformat(),
            "reason": reason.value,
            "world_name": self.config.world_name,
            "method": SNAPSHOT_METHOD,
            "keep_snapshots": self.config.keep_snapshots,
            "previous_snapshot": previous.name if previous is not None else None,
            "leveldb_manifest": manifest_name,
            "apparent_payload_bytes": apparent_size,
            "checksums_sha256": checksums,
            "rsync_stats_recorded": bool(stats),
        }

    def _latest_snapshot(self) -> Path | None:
        return self.rotator._head_snapshot(self.rotator.complete_snapshots())

    def _set_snapshot_state(
        self, snapshot: Path, state: str, validation: dict[str, object]
    ) -> None:
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["snapshot_state"] = state
        manifest["validation"] = {**validation, "completed_at": self._timestamp()}
        _replace_json(manifest_path, manifest, mode=0o640, max_bytes=_MAX_MANIFEST_BYTES)
        _fsync_directory(snapshot)
        _fsync_directory(self.config.snapshot_root)

    def _snapshot_lists(self) -> dict[str, list[str]]:
        result = {"verified": [], "pending": [], "failed": [], "protected_legacy": []}
        if not self.config.snapshot_root.is_dir():
            return result
        for path in sorted(self.config.snapshot_root.iterdir()):
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                manifest = self.verifier.manifest(path)
                state = manifest.get("snapshot_state")
                if state in {"pending", "failed"}:
                    result[str(state)].append(path.name)
                elif self.verifier.is_owned_verified(path):
                    result["verified"].append(path.name)
                else:
                    result["protected_legacy"].append(path.name)
            except (OSError, RuntimeError, UnicodeError, ValueError):
                result["protected_legacy"].append(path.name)
        return result

    def _write_run_record(
        self,
        started: str,
        reason: BackupReason,
        before: dict[str, list[str]],
        attempts: list[dict[str, object]],
        result: str,
        failure_reason: str | None,
        removed: list[str],
    ) -> None:
        root = self.config.backup_root / "runs"
        root.mkdir(parents=True, exist_ok=True, mode=0o750)
        payload = {
            "tool": TOOL_ID,
            "started_at": started,
            "finished_at": self._timestamp(),
            "reason": reason.value,
            "result": result,
            "failure_reason": failure_reason,
            "attempts": attempts,
            "before": before,
            "after": self._snapshot_lists(),
            "pruned_verified": removed,
        }
        _write_json(root / f"{started.replace(':', '').replace('+00:00', 'Z')}-{self._token()}.json", payload, mode=0o640)

    def _timestamp(self) -> str:
        return self._now().astimezone(dt.timezone.utc).isoformat()


class SnapshotRehearsal:
    def __init__(self, config: Config):
        self.config = config
        self.verifier = SnapshotVerifier(config)

    def run(self, snapshot: Path, destination: Path) -> dict[str, object]:
        with repository_lock(self.config):
            return self._run_locked(snapshot, destination)

    def _run_locked(self, snapshot: Path, destination: Path) -> dict[str, object]:
        result = self.verifier.verify(snapshot, require_current_schema=True)
        if result["snapshot_state"] != "verified":
            raise RuntimeError("only a verified snapshot can be rehearsed")
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("rehearsal destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        required = _apparent_bytes(snapshot / "payload")
        if shutil.disk_usage(destination.parent).free < self.config.min_free_bytes + required:
            raise RuntimeError("free disk space is below the rehearsal safety threshold")
        try:
            shutil.copytree(snapshot / "payload", destination)
            world = destination / "worlds" / self.config.world_name
            current = world / "db" / "CURRENT"
            manifest_name = current.read_text(encoding="ascii").strip()
            if not (world / "db" / manifest_name).is_file():
                raise RuntimeError("rehearsed LevelDB manifest is unavailable")
            manifest = self.verifier.manifest(snapshot)
            checksums = manifest["checksums_sha256"]
            assert isinstance(checksums, dict)
            for relative_raw, expected_raw in checksums.items():
                assert isinstance(relative_raw, str) and isinstance(expected_raw, str)
                relative = PurePosixPath(relative_raw)
                if not relative.parts or relative.parts[0] != "payload":
                    raise RuntimeError("rehearsal checksum path is invalid")
                copied = destination.joinpath(*relative.parts[1:])
                if not copied.is_file() or SnapshotVerifier._sha256(copied) != expected_raw:
                    raise RuntimeError("rehearsal checksum verification failed")
            result = {**result, "rehearsal": "passed", "destination": destination.name}
            _write_json(destination / "rehearsal-report.json", result, mode=0o640)
            _fsync_tree(destination)
            return result
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise


class SnapshotRestorePlanner:
    def __init__(self, config: Config):
        self.config = config
        self.verifier = SnapshotVerifier(config)

    def build(self, snapshot: Path) -> dict[str, object]:
        with repository_lock(self.config):
            return self._build_locked(snapshot)

    def _build_locked(self, snapshot: Path) -> dict[str, object]:
        verification = self.verifier.verify(snapshot, require_current_schema=True)
        if verification["snapshot_state"] != "verified":
            raise RuntimeError("only a verified snapshot can be restored")
        manifest_hash = SnapshotVerifier._sha256(snapshot / "manifest.json")
        current_level = self.config.world_path / "level.dat"
        current_level_hash = (
            SnapshotVerifier._sha256(current_level)
            if current_level.is_file() and not current_level.is_symlink()
            else None
        )
        plan = {
            "plan_version": 1,
            "tool": TOOL_ID,
            "snapshot": snapshot.name,
            "snapshot_manifest_sha256": manifest_hash,
            "current_world_level_sha256": current_level_hash,
            "required_service": self.config.bds_service,
            "requires_all_players_offline": True,
            "requires_service_inactive": True,
            "requires_pre_restore_offline_backup": True,
            "restore_scope": "world-with-optional-bds-json-config",
            "verified_files": verification["verified_files"],
        }
        canonical = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        return {
            **plan,
            "plan_sha256": hashlib.sha256(canonical).hexdigest(),
        }


def _write_json(
    path: Path,
    payload: dict[str, object],
    *,
    mode: int,
    max_bytes: int | None = None,
) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if max_bytes is not None and len(encoded) > max_bytes:
        raise RuntimeError("snapshot manifest exceeds the supported size")
    with path.open("x", encoding="utf-8") as handle:
        handle.write(encoded.decode("utf-8"))
        handle.flush()
        path.chmod(mode)
        os.fsync(handle.fileno())


def _replace_json(
    path: Path,
    payload: dict[str, object],
    *,
    mode: int,
    max_bytes: int | None = None,
) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if max_bytes is not None and len(encoded) > max_bytes:
        raise RuntimeError("snapshot manifest exceeds the supported size")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_text(path: Path, payload: str, *, mode: int) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        path.chmod(mode)
        os.fsync(handle.fileno())


def _fsync_tree_directories(root: Path) -> None:
    directories = [root]
    directories.extend(path for path in root.rglob("*") if path.is_dir() and not path.is_symlink())
    for path in reversed(directories):
        _fsync_directory(path)


def _fsync_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    _fsync_tree_directories(root)


def _apparent_bytes(root: Path) -> int:
    return sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
