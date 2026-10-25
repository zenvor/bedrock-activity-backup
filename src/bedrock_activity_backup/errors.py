from __future__ import annotations

import subprocess


class DiagnosticError(RuntimeError):
    """An application-authored message safe for public operational logs.

    Never construct this with subprocess output, player data or external text.
    """


def safe_error_label(error: BaseException) -> str:
    """Return a diagnostic label without commands, paths, or identifiers."""
    if isinstance(error, subprocess.CalledProcessError):
        return f"subprocess-exit-{error.returncode}"
    if isinstance(error, subprocess.TimeoutExpired):
        return "subprocess-timeout"
    if isinstance(error, DiagnosticError):
        return str(error)
    if isinstance(error, OSError) and error.errno is not None:
        return f"{type(error).__name__}(errno={error.errno})"
    return type(error).__name__
