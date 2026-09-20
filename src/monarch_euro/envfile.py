"""Surgical edits to a .env file.

Rewriting this file carelessly is how a working configuration gets destroyed:
a naive template substitution once inserted empty keys above the real ones
here, leaving duplicates and shifting a CSRF token into the wrong variable.
The failure surfaced as "no credentials configured", which points at
authentication rather than at the file.

So: touch only the named keys, preserve every other line including comments
and ordering, write atomically, and keep a backup.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path


class EnvFileError(RuntimeError):
    pass


def read_key(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    pattern = re.compile(rf"^{re.escape(key)}=(.*)$")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1)
    return None


def duplicate_keys(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    seen: set[str] = set()
    duplicates: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.partition("=")[0].strip()
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return duplicates


def _prune_backups(directory: Path, keep: int) -> None:
    """Old backups hold superseded credentials; keep only a few."""
    backups = sorted(directory.glob("*.env.*"), reverse=True)
    for stale in backups[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def update(
    path: Path,
    values: dict[str, str],
    backup: bool = True,
    backup_dir_override: Path | None = None,
) -> Path | None:
    """Set `values` in `path`, leaving everything else byte-for-byte alone.

    A key already present is rewritten where it stands, so ordering and the
    comments explaining it stay put. A key that is absent is appended.
    Returns the backup path, if one was made.
    """
    if not path.is_file():
        raise EnvFileError(f"{path} does not exist.")

    duplicates = duplicate_keys(path)
    overlap = duplicates & set(values)
    if overlap:
        raise EnvFileError(
            f"{path} defines {', '.join(sorted(overlap))} more than once. "
            f"Refusing to edit a file that is already inconsistent - remove the "
            f"duplicate line(s) first."
        )

    backup_path: Path | None = None
    if backup:
        # Deliberately outside the repository. A backup written beside .env
        # once reached a public repo because .gitignore covered `.env` but not
        # `.env.bak-<timestamp>`. Keeping them elsewhere removes the chance
        # entirely rather than relying on an ignore rule staying correct.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = Path(backup_dir_override or Path.home() / ".monarch-euro" / "env-backups")
        backup_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(backup_dir, 0o700)
        backup_path = backup_dir / f"{path.name}.{stamp}"
        shutil.copy2(path, backup_path)
        os.chmod(backup_path, 0o600)
        _prune_backups(backup_dir, keep=5)

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    remaining = dict(values)
    out: list[str] = []
    for line in lines:
        stripped = line.rstrip("\n")
        key = stripped.partition("=")[0].strip()
        if "=" in stripped and not stripped.lstrip().startswith("#") and key in remaining:
            ending = "\n" if line.endswith("\n") else ""
            out.append(f"{key}={remaining.pop(key)}{ending}")
        else:
            out.append(line)

    if remaining:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        for key, value in remaining.items():
            out.append(f"{key}={value}\n")

    # Write via a temporary file in the same directory so the replacement is
    # atomic: an interrupted write can never leave a half-file holding
    # credentials.
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(out), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return backup_path


COOKIE_SPLIT = re.compile(r";\s*")


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse a pasted Cookie header into a mapping.

    Accepts what DevTools actually gives you, including a leading "Cookie:"
    and stray whitespace or newlines from wrapping.
    """
    text = raw.strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1]
    text = " ".join(text.split())

    cookies: dict[str, str] = {}
    for part in COOKIE_SPLIT.split(text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies[name.strip()] = value.strip()
    return cookies
