from __future__ import annotations

import subprocess


def safe_error_label(error: BaseException) -> str:
    """Return a diagnostic label without commands, paths, or identifiers."""
    if isinstance(error, subprocess.CalledProcessError):
        return f"subprocess-exit-{error.returncode}"
    if isinstance(error, subprocess.TimeoutExpired):
        return "subprocess-timeout"
    return type(error).__name__
