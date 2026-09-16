"""Talks to calibre's command-line tools: the applier (writes), ebook-meta and fetch-ebook-metadata."""
from __future__ import annotations

import json
import shutil
import subprocess
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


def _run_applier(cfg: Config, args: list[str], timeout: int | None = None, poll=None) -> list[dict]:
    """Run the applier. `poll` is called about twice a second while it runs."""
    exe = _require(cfg["calibre"]["calibre_debug"])
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        p = subprocess.Popen([exe, "-e", str(APPLIER), "--", *args], stdout=out, stderr=err, text=True)
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
            # Let calibre finish the book it is on and close the database cleanly.
            p.wait()
            raise
        out.seek(0)
        err.seek(0)
        proc = subprocess.CompletedProcess(p.args, p.returncode, out.read(), err.read())
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
        tail = (proc.stderr or "").strip().splitlines()[-15:]
        raise CalibreError(err or ("calibre-debug failed (exit %d):\n%s" % (proc.returncode, "\n".join(tail))))
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


def apply_plan(cfg: Config, ops: list[dict], plan_file: Path, journal_file: Path, library_path: Path | None = None,
               on_progress=None) -> list[dict]:
    """Write ops through calibre. Returns this batch's journal records.

    If calibre fails part-way, ApplyError carries the records that did reach
    the journal, so the caller can still account for them.
    """
    plan_file.write_text(json.dumps({"ops": ops}, indent=1, default=str))
    start = journal_file.stat().st_size if journal_file.exists() else 0
    error = None

    def poll():
        if on_progress and journal_file.exists():
            with open(journal_file, "rb") as fh:
                fh.seek(start)
                on_progress(n=fh.read().count(b"\n"))

    try:
        _run_applier(cfg, ["apply", str(library_path or cfg.library), str(plan_file), str(journal_file)], poll=poll)
    except (CalibreError, subprocess.TimeoutExpired, OSError) as e:
        error = e
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
    if error is not None:
        raise ApplyError(f"calibre failed while writing: {error}", records)
    return records


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
