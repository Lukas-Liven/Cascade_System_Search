"""
Security-focused filesystem utilities.

These helpers enforce defense-in-depth controls for application-managed local
cache artifacts. In particular, pickle files must be treated as trusted only
when they are created and controlled by this application in the user's private
cache directory.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cascade_research_tool.exceptions import CacheSecurityError


def ensure_pickle_cache_is_private(cache_file: Path) -> None:
    """
    Reject pickle caches writable by other users on POSIX systems.

    This is a defense-in-depth control only. Pickle remains unsafe if an
    attacker can modify the current user's cache directory or account.

    Windows permissions are governed by ACLs rather than POSIX mode bits, so
    this function does not attempt to make a misleading chmod-based decision
    on Windows.
    """

    if not cache_file.exists() or os.name == "nt":
        return

    mode = stat.S_IMODE(cache_file.stat().st_mode)

    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise CacheSecurityError(
            f"Refusing to load insecure pickle cache: {cache_file}. "
            "Delete it and rerun initialization."
        )


def atomic_write_bytes(destination: Path, data: bytes) -> None:
    """
    Atomically replace a file with the supplied bytes.

    The temporary file is written in the destination directory, flushed, and
    synchronized before os.replace() performs the replacement. A partially
    written file is therefore not accepted as a valid application cache
    artifact if a write fails or the process exits unexpectedly.

    On POSIX systems, the temporary file is restricted to the current user
    before replacing the destination.
    """

    temporary = destination.with_suffix(destination.suffix + ".tmp")

    try:
        with open(temporary, "wb") as output_file:
            output_file.write(data)
            output_file.flush()
            os.fsync(output_file.fileno())

        if os.name != "nt":
            os.chmod(temporary, 0o600)

        os.replace(temporary, destination)

    finally:
        # If an exception occurred before os.replace(), remove the incomplete
        # temporary file. missing_ok=True also safely handles a successful
        # replacement, where the temporary path no longer exists.
        temporary.unlink(missing_ok=True)