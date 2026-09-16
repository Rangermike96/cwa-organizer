"""Talks to calibre's command-line tools: the applier (writes), ebook-meta and fetch-ebook-metadata."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import PACKAGE_DIR, Config

APPLIER = PACKAGE_DIR / "calibre_applier.py"


class CalibreError(Exception):
    pass


def _require(cmd: str) -> str:
    path = shutil.which(cmd)
    if not path:
        raise CalibreError(
            f"'{cmd}' was not found. Install calibre on this computer (CachyOS/Arch: "
            f"sudo pacman -S calibre) or set its path under [calibre] in config.toml."
        )
    return path


_last_exit = 0.0  # when the previous calibre-debug process ended
MIN_GAP_SECONDS = 3.0


class ApplierInterrupted(Exception):
    """Ctrl+C reached us; calibre was told to stop after the current book and has exited."""


def _run_applier(cfg: Config, args: list[str], timeout: int | None = None, poll=None,
                 stderr_log: Path | None = None, stop_file: Path | None = None) -> list[dict]:
    """Run the applier in its own session so Ctrl+C can never kill calibre mid-write.

    `poll` is called about twice a second. On Ctrl+C the stop file is created;
    the applier finishes the book it is on, closes the database and exits, and
    ApplierInterrupted is raised. calibre's full stderr goes to `stderr_log`.
    """
    global _last_exit
    exe = _require(cfg["calibre"]["calibre_debug"])
    gap = time.monotonic() - _last_exit
    if gap < MIN_GAP_SECONDS:
        time.sleep(MIN_GAP_SECONDS - gap)  # let the previous process fully release the database
    env = dict(os.environ, PYTHONFAULTHANDLER="1")
    interrupted = False
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        p = subprocess.Popen([exe, "-e", str(APPLIER), "--", *args], stdout=out, stderr=err, text=True,
                             env=env, start_new_session=True)
        started = time.monotonic()
        try:
            while True:
                try:
                    p.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if poll:
                        poll()
                    if timeout and time.monotonic() - started > timeout:
                        p.kill()
                        p.wait()
                        raise
                except KeyboardInterrupt:
                    if stop_file is None:
                        continue  # nothing safe to do but wait for calibre to finish
                    if not interrupted:
                        interrupted = True
                        stop_file.touch()
                        print("\nStopping after the book calibre is writing now (this can take a few seconds)...",
                              file=sys.stderr, flush=True)
        finally:
            _last_exit = time.monotonic()
        out.seek(0)
        err.seek(0)
        proc = subprocess.CompletedProcess(p.args, p.returncode, out.read(), err.read())
    if stderr_log is not None and (proc.stderr or proc.returncode):
        stderr_log.write_text(f"exit code: {proc.returncode}\n\n{proc.stderr}")
    lines = []
    for ln in proc.stdout.splitlines():
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                lines.append(json.loads(ln))
            except ValueError:
                pass
    if proc.returncode != 0:
        err = next((x.get("error") for x in lines if x.get("error")), None)
        tail = [t for t in (proc.stderr or "").strip().splitlines() if t.strip()][-6:]
        where = f" (full output: {stderr_log})" if stderr_log else ""
        raise CalibreError(err or ("calibre-debug failed (exit %d)%s:\n%s" % (proc.returncode, where, "\n".join(tail))))
    if interrupted:
        raise ApplierInterrupted()
    return lines


def require_calibre(cfg: Config) -> None:
    _require(cfg["calibre"]["calibre_debug"])


def library_info(cfg: Config, library_path: Path | None = None) -> dict:
    out = _run_applier(cfg, ["info", str(library_path or cfg.library)], timeout=600)
    if not out:
        raise CalibreError("calibre-debug returned no library info")
    return out[-1]


def ensure_columns(cfg: Config) -> list[str]:
    lab = cfg["labels"]
    spec = [
        {"label": cfg.column("booktype")[1:], "name": "Book Type", "datatype": "enumeration",
         "display": {"enum_values": [lab["light_novel"], lab["manga"], lab["other"]], "enum_colors": []}},
        {"label": cfg.column("status")[1:], "name": "Metadata Status", "datatype": "enumeration",
         "display": {"enum_values": ["Fetched", "Failed"], "enum_colors": []}},
    ]
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    spec_file = cfg.state_dir / "columns_spec.json"
    spec_file.write_text(json.dumps(spec))
    out = _run_applier(cfg, ["ensure-columns", str(cfg.library), str(spec_file)], timeout=600)
    return out[-1].get("created", []) if out else []


class ApplyError(CalibreError):
    def __init__(self, msg, records):
        super().__init__(msg)
        self.records = records


def _read_journal(journal_file: Path, start: int) -> list[dict]:
    records = []
    if journal_file.exists():
        with open(journal_file, encoding="utf-8") as fh:
            fh.seek(start)
            for ln in fh:
                ln = ln.strip()
                if ln:
                    try:
                        records.append(json.loads(ln))
                    except ValueError:
                        pass
    return records


def apply_plan(cfg: Config, ops: list[dict], plan_file: Path, journal_file: Path, library_path: Path | None = None,
               on_progress=None, retries: int = 2, log=None) -> list[dict]:
    """Write ops through calibre and return the journal records for them.

    If calibre crashes, the books it had not reached are retried (up to
    `retries` more times). That is safe because every op checks the live
    values first, so a book that was written is never written twice.
    Ctrl+C stops cleanly after the current book (ApplierInterrupted).
    If it still fails, ApplyError carries the records that were written.
    """
    start = journal_file.stat().st_size if journal_file.exists() else 0
    stop_file = plan_file.with_suffix(".stop")
    stop_file.unlink(missing_ok=True)
    remaining = list(ops)
    attempt = 0
    while True:
        attempt += 1
        suffix = "" if attempt == 1 else f"-retry{attempt - 1}"
        pf = plan_file.with_name(plan_file.stem + suffix + plan_file.suffix)
        pf.write_text(json.dumps({"ops": remaining}, indent=1, default=str))
        done_before = len(ops) - len(remaining)

        def poll():
            if on_progress and journal_file.exists():
                with open(journal_file, "rb") as fh:
                    fh.seek(start)
                    on_progress(n=fh.read().count(b"\n"))

        error = None
        try:
            _run_applier(cfg, ["apply", str(library_path or cfg.library), str(pf), str(journal_file), str(stop_file)],
                         poll=poll, stderr_log=pf.with_suffix(".stderr.log"), stop_file=stop_file)
        except ApplierInterrupted:
            stop_file.unlink(missing_ok=True)
            raise
        except (CalibreError, subprocess.TimeoutExpired, OSError) as e:
            error = e
        records = _read_journal(journal_file, start)
        if error is None:
            return records
        reached = {r["book_id"] for r in records}
        remaining = [op for op in remaining if op["book_id"] not in reached]
        progressed = len(ops) - len(remaining) > done_before
        if not remaining:
            return records  # it crashed while closing, after every book was written
        if attempt > retries:
            raise ApplyError(f"calibre failed while writing: {error}", records)
        if log:
            log(f"calibre crashed ({str(error).splitlines()[0]}); {len(remaining)} book(s) not written yet"
                f"{'' if progressed else ' (none were written in that attempt)'}. Retrying in 5 seconds...")
        time.sleep(5)


def extract_cover(cfg: Config, book_file: Path, out_file: Path, timeout: int = 120) -> bool:
    exe = shutil.which(cfg["calibre"]["ebook_meta"])
    if not exe:
        return False
    try:
        subprocess.run([exe, str(book_file), f"--get-cover={out_file}"],
                       capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return out_file.exists() and out_file.stat().st_size > 1024
