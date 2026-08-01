from __future__ import annotations

import contextlib
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path


_PLAYER_COUNT_PATTERN = re.compile(r"There are (\d+)/(\d+) players online:")


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
        self._send("save hold")
        try:
            deadline = self._monotonic() + ready_timeout
            ready_pattern = re.compile(
                re.escape("Data saved. Files are now ready to be copied.")
            )
            while self._monotonic() < deadline:
                self._send("save query")
                self._sleep(1)
                output = self._logs_after(cursor)
                if ready_pattern.search(output):
                    break
            else:
                raise RuntimeError("BDS did not reach backup-ready state")
            yield
        finally:
            try:
                resume_cursor = self._journal_cursor()
            except Exception:
                resume_cursor = None
            self._send("save resume")
            if resume_cursor is None:
                raise RuntimeError("save resume was sent but could not be confirmed")
            resumed_pattern = re.compile(
                re.escape("Changes to the world are resumed.")
            )
            self._wait_for(resumed_pattern, resume_cursor, resume_timeout)

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
