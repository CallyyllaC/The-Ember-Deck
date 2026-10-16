"""Conventions for Ember Deck's local secret/state files.

A worker that needs credentials keeps them out of its tracked YAML, in a
sibling ``configs/<component>_secrets.yaml`` that is:

* listed in ``.gitignore`` (with a placeholder-only ``*.example.yaml`` tracked);
* created and rewritten with mode 0600, atomically (temp file in the same
  directory, fsync, rename);
* parsed without ever echoing file contents in an error (YAML parser errors
  quote the offending line, which may be a password);
* split into ``credentials`` (operator-supplied) and ``cache`` (runtime state
  the worker may rewrite); a worker never rewrites the ``credentials`` section;
* overridable by environment variables, which are never written back;
* never published to Synapse and redacted from every log line.

This module only holds the mechanics shared by such files. Each component
owns its own schema and precedence rules (see ``plex_auth.py``).
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

PRIVATE_MODE = 0o600
REDACTED = "<redacted>"


class SecretFileError(ValueError):
    """A secret file exists but cannot be used. Messages never contain its contents."""


def read_private_yaml(path: Path) -> Any:
    """Return parsed YAML, ``None`` for a missing file, or raise SecretFileError."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        raise SecretFileError(f"cannot read {path}: {reason}") from None
    try:
        import yaml
        return yaml.safe_load(text)
    except Exception:
        # Deliberately drop the parser message: it quotes file content.
        raise SecretFileError(f"{path} is not valid YAML") from None


def write_private_yaml(path: Path, data: Any) -> None:
    """Atomically replace ``path`` with ``data`` as YAML, readable by the owner only."""
    import yaml

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates the file 0600 on POSIX, so the secret is never briefly public.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, PRIVATE_MODE)
        os.replace(tmp_name, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def permission_problem(path: Path) -> Optional[str]:
    """Describe group/other access to a secret file on POSIX, else ``None``."""
    if os.name != "posix":
        return None
    try:
        mode = stat.S_IMODE(Path(path).stat().st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return f"{path} is accessible to other users (mode {mode:04o}); run: chmod 600 {path}"
    return None


class Redactor:
    """Replace known secret values (and configured patterns) in log text."""

    def __init__(self, patterns: Iterable[re.Pattern] = ()):
        self._values: set[str] = set()
        self._patterns = list(patterns)

    def add(self, *values: Optional[str]) -> None:
        for value in values:
            # Very short values would redact ordinary words; real secrets are longer.
            if isinstance(value, str) and len(value.strip()) >= 4:
                self._values.add(value.strip())

    def __call__(self, text: object) -> str:
        result = str(text)
        for pattern in self._patterns:
            result = pattern.sub(lambda match: match.group(1) + REDACTED, result)
        for value in sorted(self._values, key=len, reverse=True):
            result = result.replace(value, REDACTED)
        return result
