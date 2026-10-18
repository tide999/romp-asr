"""Trusted local tool backends; no policy-specific decision branches."""

import os
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import stat
from dataclasses import dataclass
from typing import Mapping


class NoEffectError(Exception):
    """Backend guarantees this failure caused no external side effect."""


@dataclass(frozen=True)
class ToolResult:
    outcome: str
    reason: str


class LocalPaymentLedger:
    """Durable local payment demonstrator, not a bank connector."""

    def __init__(self, database: Path):
        self.database = str(database)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS payments "
                       "(id TEXT PRIMARY KEY, amount INTEGER NOT NULL)")

    def charge(self, identifier: str, amount: int) -> None:
        with self._connect() as db:
            prior = db.execute("SELECT amount FROM payments WHERE id=?", (identifier,)).fetchone()
            if prior and prior[0] != amount:
                raise NoEffectError("payment identifier conflict")
            db.execute("INSERT OR IGNORE INTO payments VALUES (?,?)", (identifier, amount))

    @property
    def total(self) -> int:
        with self._connect() as db:
            return db.execute("SELECT COALESCE(SUM(amount),0) FROM payments").fetchone()[0]

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database)
        try:
            db.execute("PRAGMA synchronous=FULL")
            if (db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete"
                    or db.execute("PRAGMA synchronous").fetchone()[0] != 2):
                raise ValueError("payment database requires DELETE journal and FULL synchronous")
            with db:
                yield db
        finally:
            db.close()


class PosixFileTools:
    """Descriptor-relative deletion; host must exclusively own the namespace."""

    def __init__(self, roots: Mapping[str, Path]):
        if os.name != "posix" or os.unlink not in os.supports_dir_fd:
            raise RuntimeError("safe file backend requires POSIX dir_fd")
        self._fds = {}
        self.roots = {}
        try:
            for name, root in roots.items():
                path = Path(os.path.abspath(root))
                if any(p.is_symlink() for p in (path, *path.parents)):
                    raise ValueError("root path contains a symbolic link")
                self.roots[name] = str(path)
            paths = list(self.roots.values())
            if any(os.path.commonpath((a, b)) in (a, b)
                   for i, a in enumerate(paths) for b in paths[i + 1:]):
                raise ValueError("root directories must not overlap")
            for name, path in self.roots.items():
                self._fds[name] = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except Exception:
            self.close()
            raise

    def close(self):
        for fd in self._fds.values():
            os.close(fd)
        self._fds.clear()

    def delete(self, root, relative, *, validate_only=False):
        parts = relative.split("/")
        if not relative or any(p in ("", ".", "..") for p in parts) or "\\" in relative:
            raise NoEffectError("non-canonical relative path")
        if root not in self._fds:
            raise NoEffectError("unknown root")
        fd = os.dup(self._fds[root])
        try:
            try:
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise NoEffectError("only single-link regular files may be deleted")
            except OSError as exc:
                raise NoEffectError("target validation failed") from exc
            if not validate_only:
                os.unlink(parts[-1], dir_fd=fd)
        finally:
            os.close(fd)
