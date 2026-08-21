from __future__ import annotations

import contextlib
import logging
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path


_PLAYER_COUNT_PATTERN = re.compile(r"There are (\d+)/(\d+) players online:")
_READY_TO_COPY_PATTERN = re.compile(
    re.escape("Data saved. Files are now ready to be copied.")
)
_SAVE_PENDING_PATTERN = re.compile(
    re.escape("A previous save has not been completed.")
)
_SAVES_RESUMED_PATTERN = re.compile(
    re.escape("Changes to the world are resumed.")
)
_INITIAL_SAVE_QUERY_DELAY_SECONDS = 1.0
_MAX_SAVE_QUERY_DELAY_SECONDS = 5.0
_RESUME_SEND_ATTEMPTS = 3
_RESUME_RETRY_DELAY_SECONDS = 1.0


LOGGER = logging.getLogger(__name__)


class BdsConsole:
    def __init__(
        self,
        service: str,
        fifo: Path,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.service = service
        self.fifo = fifo
        self._run = run
        self._monotonic = monotonic
        self._sleep = sleep

    def assert_available(self) -> None:
        result = self._run(
            ["systemctl", "is-active", self.service],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 or result.stdout.strip() != "active":
            raise RuntimeError("BDS service is not active")
        try:
            mode = self.fifo.stat().st_mode
        except OSError as error:
            raise RuntimeError("BDS console FIFO is unavailable") from error
        if not stat.S_ISFIFO(mode):
            raise RuntimeError("BDS console path is not a FIFO")

    def query_player_count(self, timeout: int) -> int:
        cursor = self._journal_cursor()
        self._send("list")
        output = self._wait_for(_PLAYER_COUNT_PATTERN, cursor, timeout)
        match = _PLAYER_COUNT_PATTERN.search(output)
        if match is None:
            raise RuntimeError("BDS list response could not be parsed")
        return int(match.group(1))

    @contextlib.contextmanager
    def paused_saves(self, ready_timeout: int, resume_timeout: int) -> Iterator[None]:
        cursor = self._journal_cursor()
        hold_started = self._monotonic()
        self._send("save hold")
        try:
            deadline = self._monotonic() + ready_timeout
            delay = _INITIAL_SAVE_QUERY_DELAY_SECONDS
            attempts = 0
            ready = False
            while self._monotonic() < deadline:
                remaining = deadline - self._monotonic()
                if attempts == 0 and remaining <= delay:
                    LOGGER.info(
                        "BDS ready timeout is too short for the initial query delay; "
                        "querying immediately"
                    )
                elif remaining <= delay:
                    break
                else:
                    self._sleep(delay)
                self._send("save query")
                attempts += 1
                output = self._logs_after(cursor)
                if _READY_TO_COPY_PATTERN.search(output):
                    LOGGER.info(
                        "BDS save is ready for copying (query_attempts=%d elapsed_seconds=%.2f)",
                        attempts,
                        self._monotonic() - hold_started,
                    )
                    ready = True
                    break
                if _SAVE_PENDING_PATTERN.search(output):
                    LOGGER.info(
                        "BDS save transaction observed a previous-save warning; "
                        "backing off before the next query "
                        "(query_attempts=%d next_delay_seconds=%.2f)",
                        attempts,
                        min(delay * 2, _MAX_SAVE_QUERY_DELAY_SECONDS),
                    )
                else:
                    LOGGER.info(
                        "BDS save is not ready yet; backing off before the next query "
                        "(query_attempts=%d next_delay_seconds=%.2f)",
                        attempts,
                        min(delay * 2, _MAX_SAVE_QUERY_DELAY_SECONDS),
                    )
                delay = min(delay * 2, _MAX_SAVE_QUERY_DELAY_SECONDS)
            if not ready:
                raise RuntimeError("BDS did not reach backup-ready state")
            yield
        finally:
            try:
                resume_cursor = self._journal_cursor()
            except Exception:
                resume_cursor = None
            resume_started = self._monotonic()
            self._send_resume_with_retry()
            if resume_cursor is None:
                raise RuntimeError("save resume was sent but could not be confirmed")
            self._wait_for(_SAVES_RESUMED_PATTERN, resume_cursor, resume_timeout)
            LOGGER.info(
                "BDS save resume confirmed (elapsed_seconds=%.2f)",
                self._monotonic() - resume_started,
            )

    def _send_resume_with_retry(self) -> None:
        last_error: Exception | None = None
        for attempt in range(1, _RESUME_SEND_ATTEMPTS + 1):
            try:
                self._send("save resume")
                return
            except Exception as error:
                last_error = error
                LOGGER.critical(
                    "Could not send BDS save resume (attempt=%d/%d)",
                    attempt,
                    _RESUME_SEND_ATTEMPTS,
                )
                if attempt < _RESUME_SEND_ATTEMPTS:
                    self._sleep(_RESUME_RETRY_DELAY_SECONDS)
        raise RuntimeError("could not send BDS save resume after retries") from last_error

    def _journal_cursor(self) -> str:
        result = self._run(
            [
                "journalctl",
                "-u",
                self.service,
                "-n",
                "0",
                "--show-cursor",
                "--no-pager",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in reversed(result.stdout.splitlines()):
            if line.startswith("-- cursor: "):
                return line.removeprefix("-- cursor: ")
        raise RuntimeError("systemd journal cursor is unavailable")

    def _logs_after(self, cursor: str) -> str:
        result = self._run(
            [
                "journalctl",
                "-u",
                self.service,
                "--after-cursor",
                cursor,
                "-o",
                "cat",
                "--no-pager",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout

    def _wait_for(self, pattern: re.Pattern[str], cursor: str, timeout: int) -> str:
        deadline = self._monotonic() + timeout
        latest = ""
        while self._monotonic() < deadline:
            latest = self._logs_after(cursor)
            if pattern.search(latest):
                return latest
            self._sleep(0.25)
        raise RuntimeError("timed out waiting for a BDS console response")

    def _send(self, command: str) -> None:
        if "\n" in command or "\r" in command:
            raise ValueError("BDS command must be one line")
        try:
            descriptor = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as error:
            raise RuntimeError("could not open BDS console FIFO") from error
        try:
            os.write(descriptor, f"{command}\n".encode("utf-8"))
        finally:
            os.close(descriptor)
