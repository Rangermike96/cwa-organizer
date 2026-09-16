"""Run safety: single-instance lock, "only writer" checks, snapshots, backups, verification.

metadata.db lives on NFS. SQLite's locking is unreliable over NFS, so this
tool never opens the NFS database with SQLite directly. Reads use a byte copy
taken to the local disk; writes happen only through calibre, only after these
checks pass, and only while CWA is stopped.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config


class SafetyError(Exception):
    pass


# ------------------------------------------------------------------ lock ---

class RunLock:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close()
            raise SafetyError(f"Another cwa-organizer run is already in progress (lock: {self.path}).")
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()


# ------------------------------------------------------------- preflight ---

@dataclass
class Preflight:
    problems: list = field(default_factory=list)   # block writing
    warnings: list = field(default_factory=list)   # shown, do not block
    info: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _cwa_answers(url: str, timeout: float = 4.0) -> str | None:
    """Return a description if something answers at url, None if nothing does."""
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "cwa-organizer"})
        # Never go through a proxy for a LAN address check.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            return f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError):
        return None


def blocked_processes(names) -> list[str]:
    """Local processes whose executable or script name matches a blocked name."""
    names = [n.lower() for n in names]
    me = {os.getpid(), os.getppid()}
    found = []
    proc = Path("/proc")
    if not proc.exists():
        return found
    for d in proc.iterdir():
        if not d.name.isdigit() or int(d.name) in me:
            continue
        try:
            raw = (d / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a for a in raw.decode("utf-8", "replace").split("\0") if a]
        for tok in argv[:3]:  # executable, interpreter script, module name
            base = os.path.basename(tok).lower()
            for suffix in (".py", ".exe"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
            for n in names:
                if base == n or base.startswith(n + "-") or base.startswith(n + "_"):
                    found.append(f"pid {d.name}: {' '.join(argv)[:120]}")
                    break
            else:
                continue
            break
    return found


def preflight(cfg: Config, for_write: bool) -> Preflight:
    pf = Preflight()
    lib, db = cfg.library, cfg.metadata_db
    if not lib.is_dir():
        pf.problems.append(f"Library folder not found: {lib} (is the NFS share mounted?)")
        return pf
    if not db.is_file():
        pf.problems.append(f"metadata.db not found in {lib}")
        return pf
    if not os.access(db, os.R_OK):
        pf.problems.append(f"metadata.db is not readable: {db}")
    if for_write and not os.access(lib, os.W_OK):
        pf.problems.append(f"The library folder is not writable from this computer: {lib}")

    s = cfg["safety"]
    busy = pf.problems if for_write else pf.warnings
    url = (s.get("cwa_url") or "").strip()
    if url:
        ans = _cwa_answers(url)
        if ans:
            busy.append(f"CWA is still answering at {url} ({ans}). Stop the calibre-web-automated container first.")
        else:
            pf.info.append(f"CWA is not answering at {url}.")
    else:
        pf.warnings.append("safety.cwa_url is not set, so the tool cannot check that CWA is stopped.")

    procs = blocked_processes(s.get("blocked_processes", []))
    if procs:
        busy.append("Other calibre programs are running on this computer:\n    " + "\n    ".join(procs))

    wal = lib / "metadata.db-wal"
    if wal.exists() and wal.stat().st_size > 0:
        msg = (f"metadata.db-wal is not empty ({wal.stat().st_size} bytes). Another program may still have "
               "the database open, or CWA did not shut down cleanly. Start and cleanly stop CWA, then retry.")
        if for_write and s.get("refuse_if_wal_not_empty", True):
            pf.problems.append(msg)
        else:
            pf.warnings.append(msg)
    return pf


# --------------------------------------------------------------- snapshot ---

def take_snapshot(cfg: Config, dest: Path, include_wal: bool) -> Path:
    """Byte-copy metadata.db (and a non-empty WAL for read-only runs) to local disk."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".partial")
    shutil.copyfile(cfg.metadata_db, tmp)
    wal = cfg.library / "metadata.db-wal"
    if include_wal and wal.exists() and wal.stat().st_size > 0:
        shutil.copyfile(wal, Path(str(tmp) + "-wal"))
    # Opening the local copy replays any copied WAL into it.
    con = sqlite3.connect(tmp)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    for extra in (Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        if extra.exists():
            extra.unlink()
    os.replace(tmp, dest)
    return dest


def integrity(db_path: Path) -> str:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute("PRAGMA integrity_check").fetchall()
    finally:
        con.close()
    return "ok" if rows == [("ok",)] else "; ".join(r[0] for r in rows[:10])


def book_count(db_path: Path) -> int:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT count(*) FROM books").fetchone()[0]
    finally:
        con.close()


def user_version(db_path: Path) -> int:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def make_backup(cfg: Config, snapshot: Path, run_id: str) -> Path:
    cfg.backups_dir.mkdir(parents=True, exist_ok=True)
    dest = cfg.backups_dir / f"metadata-{run_id}.db"
    shutil.copyfile(snapshot, dest)
    if integrity(dest) != "ok":
        raise SafetyError(f"The backup copy failed its integrity check: {dest}")
    keep = int(cfg["safety"].get("backup_keep", 20))
    backups = sorted(cfg.backups_dir.glob("metadata-*.db"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()
    return dest
