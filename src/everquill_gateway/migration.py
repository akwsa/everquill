"""Copy-only migration helpers for Everquill local state."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


class MigrationConflictError(RuntimeError):
    """A legacy and target state file both exist with different bytes."""


class MigrationUnavailableError(RuntimeError):
    """The platform could not provide the required atomic no-overwrite copy."""


def _same_bytes(left: Path, right: Path) -> bool:
    if not left.is_file() or not right.is_file():
        return False
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_file, right.open("rb") as right_file:
        while True:
            left_chunk = left_file.read(1024 * 1024)
            right_chunk = right_file.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def migrate_file(source, target):
    """Copy *source* to *target* once, without moving or overwriting data.

    The staged bytes are fsynced, then installed with a same-directory hard-link
    so a concurrent creator cannot be silently overwritten. A conflicting target
    is reported and the legacy source remains untouched.
    """
    source = Path(source)
    target = Path(target)
    if not source.is_file():
        return "absent"

    if target.exists():
        if _same_bytes(source, target):
            return "identical"
        raise MigrationConflictError(
            f"migration conflict: both state files exist with different contents: "
            f"{source} and {target}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".everquill-", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination, source.open("rb") as original:
            shutil.copyfileobj(original, destination)
            destination.flush()
            os.fsync(destination.fileno())
        try:
            shutil.copystat(source, temporary, follow_symlinks=True)
        except OSError:
            pass
        try:
            os.link(temporary, target)
        except FileExistsError:
            if _same_bytes(source, target):
                return "identical-race"
            raise MigrationConflictError(
                f"migration conflict: target appeared with different contents: {target}"
            )
        except OSError as exc:
            raise MigrationUnavailableError(
                f"atomic migration is unavailable for target: {target}"
            ) from exc
        return "copied"
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def migrate_files(pairs):
    """Migrate a sequence of ``(legacy, target)`` files independently."""
    return {str(Path(target)): migrate_file(source, target) for source, target in pairs}


def write_text_atomic(target, text: str) -> None:
    """Replace a state file with complete UTF-8 text or leave it unchanged."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".everquill-", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

