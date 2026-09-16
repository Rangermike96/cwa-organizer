"""Orchestrates a run: lock, safety checks, snapshot, backup, passes, batched writes, verification, reports."""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import calibre_bridge, safety
from .config import Config
from .context import Log, RunContext
from .library import load_library
from .progress import ProgressBar
from .passes import (analyze, authors, checks, classify, covers, fetch, junk_authors, publishers, series,
                     series_lookup, tag_backfill, tags, titles)
from .report import write_reports

PASSES = {
    "tags": (tags.run, "Clean tags: decode &amp;, split BISAC, synonyms, case duplicates"),
    "publishers": (publishers.run, "Normalize publisher names"),
    "junk_authors": (junk_authors.run, "Replace filename-junk authors (online lookups)"),
    "authors": (authors.run, "Normalize author names; write spelling suggestions"),
    "series": (series.run, "Series and volume numbers from titles; collisions; gaps"),
    "titles": (titles.run, "Strip release junk and standardize volume titles"),
    "classify": (classify.run, "Light Novel / Manga / Other Books (MangaUpdates + file contents)"),
    "series_lookup": (series_lookup.run, "Series for books without volume numbers (Hardcover)"),
    "fetch": (fetch.run, "Fill missing metadata from Google / Open Library / Edelweiss"),
    "covers": (covers.run, "Find missing covers"),
    "tag_backfill": (tag_backfill.run, "Copy genre tags shared by a whole series"),
    "files": (checks.run_files, "Report missing or damaged book files"),
    "duplicates": (checks.run_duplicates, "Report possible duplicate books"),
}
ORDER = list(PASSES)
FLUSH_AFTER = {"titles", "classify", "series_lookup", "fetch", "covers", "tag_backfill", "junk_authors"}
PRESETS = {
    "all": ORDER,
    "offline": ["tags", "publishers", "authors", "series", "titles", "tag_backfill", "files", "duplicates"],
    "classify": ["tags", "classify"],
    "fetch": ["tags", "publishers", "fetch"],
    "report": ["files", "duplicates"],
}
ONLINE = {"junk_authors", "classify", "series_lookup", "fetch", "covers"}


def resolve_passes(spec: str | None, skip: str | None = None) -> list[str]:
    names: list[str] = []
    for token in (spec or "all").split(","):
        token = token.strip().lower().replace("-", "_")
        if not token:
            continue
        if token in PRESETS:
            names += PRESETS[token]
        elif token in PASSES:
            names.append(token)
        else:
            raise ValueError(f"Unknown pass or preset '{token}'. Passes: {', '.join(ORDER)}; presets: {', '.join(PRESETS)}")
    skipped = {s.strip().lower().replace("-", "_") for s in (skip or "").split(",") if s.strip()}
    for s in skipped:
        if s not in PASSES:
            raise ValueError(f"Unknown pass '{s}' in --skip")
    chosen = set(names) - skipped
    return [p for p in ORDER if p in chosen]


@dataclass
class RunOptions:
    passes: list = field(default_factory=lambda: list(ORDER))
    dry_run: bool = True
    limit: int = 0
    force_rescan: bool = False
    offline: bool = False
    reclassify: bool = False
    deep_files: bool = False
    yes: bool = False
    allow_schema_upgrade: bool = False
    verbose: bool = False
    full_preview: bool = False


def new_run_id(prefix: str = "") -> str:
    return prefix + dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        return input(prompt + " [y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _schema_guard(cfg: Config, run_dir: Path, snapshot: Path, log: Log, allow: bool) -> dict:
    """Open a throwaway copy with this computer's calibre to make sure it won't upgrade the schema."""
    probe_dir = run_dir / "schema_probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snapshot, probe_dir / "metadata.db")
    before = safety.user_version(probe_dir / "metadata.db")
    try:
        info = calibre_bridge.library_info(cfg, probe_dir)
    except calibre_bridge.CalibreError as e:
        raise safety.SafetyError(
            "This computer's calibre could not open a copy of the library database. It may be OLDER than the "
            f"calibre inside CWA. Update calibre and retry.\n{e}")
    after = safety.user_version(probe_dir / "metadata.db")
    shutil.rmtree(probe_dir, ignore_errors=True)
    for name in ("booktype", "status"):
        col = cfg.column(name)
        existing = (info.get("custom_columns") or {}).get(col)
        if existing and (existing["datatype"] not in ("enumeration", "text") or existing["is_multiple"]):
            raise safety.SafetyError(
                f"The library already has a column {col} of type {existing['datatype']}"
                f"{' (multiple values)' if existing['is_multiple'] else ''}. Rename it in calibre or choose a "
                f"different name under [columns] in config.toml.")
    log.info(f"calibre {'.'.join(map(str, info['calibre_version']))} on this computer; database schema {before}.")
    if after != before:
        msg = (f"This computer's calibre would upgrade the database schema from {before} to {after}. CWA's calibre "
               "might not read the upgraded database. Update CWA first, or pass --allow-schema-upgrade if you are sure.")
        if not allow:
            raise safety.SafetyError(msg)
        log.warn(msg)
    return info


def run(cfg: Config, opt: RunOptions) -> int:
    run_id = new_run_id()
    run_dir = cfg.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = Log(run_dir / "run.log", verbose=opt.verbose)
    meta = {"run_id": run_id, "started": dt.datetime.now().isoformat(timespec="seconds"), "dry_run": opt.dry_run,
            "passes": opt.passes, "limit": opt.limit, "force_rescan": opt.force_rescan, "offline": opt.offline,
            "reclassify": opt.reclassify, "status": "started", "library": str(cfg.library)}
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    ctx = None
    try:
        with safety.RunLock(cfg.state_dir / "organizer.lock"):
            log.info(f"Run {run_id} ({'DRY RUN, nothing will be written' if opt.dry_run else 'WRITE MODE'})")
            log.info(f"Passes: {', '.join(opt.passes) or '(none)'}" + (f"; limit {opt.limit}" if opt.limit else ""))
            pf = safety.preflight(cfg, for_write=not opt.dry_run)
            for m in pf.info:
                log.info(m)
            for m in pf.warnings:
                log.warn(m)
            if not pf.ok:
                for m in pf.problems:
                    log.error(m)
                log.error("Stopping before touching anything.")
                meta["status"] = "refused"
                return 2
            if not opt.dry_run and not (cfg["safety"].get("cwa_url") or "").strip():
                if not _confirm("CWA can't be checked automatically. Is the calibre-web-automated container stopped?", opt.yes):
                    log.error("Not confirmed that CWA is stopped. Stopping. (Set safety.cwa_url or pass --yes.)")
                    meta["status"] = "refused"
                    return 2

            snap = safety.take_snapshot(cfg, run_dir / "snapshot.db", include_wal=opt.dry_run)
            integ = safety.integrity(snap)
            if integ != "ok":
                if not opt.dry_run:
                    log.error(f"metadata.db FAILED its integrity check: {integ}. Nothing will be written.")
                    meta["status"] = "refused"
                    return 3
                log.warn(f"The snapshot failed its integrity check ({integ}); CWA may have been writing. Results may be off.")
            backup = None
            if not opt.dry_run:
                calibre_bridge.require_calibre(cfg)
                backup = safety.make_backup(cfg, snap, run_id)
                log.info(f"Backup saved: {backup}")
                _schema_guard(cfg, run_dir, snap, log, opt.allow_schema_upgrade)
            meta["backup"] = str(backup) if backup else None

            lib = load_library(snap, cfg.library, custom_labels=[cfg.column("booktype"), cfg.column("status")])
            log.info(f"Loaded {len(lib.books)} books.")
            ctx = RunContext(cfg, lib, run_id, run_dir, log, dry_run=opt.dry_run, limit=opt.limit,
                             force_rescan=opt.force_rescan, offline=opt.offline, reclassify=opt.reclassify,
                             deep_files=opt.deep_files, full_preview=opt.full_preview)
            if not opt.offline and any(p in ONLINE for p in opt.passes) and not cfg.hardcover_token:
                log.info("No Hardcover API key found (secrets.toml); Hardcover lookups are skipped.")
            analyze.run(ctx)
            interrupted = False
            try:
                for name in opt.passes:
                    log.info(f"--- {name}: {PASSES[name][1]}")
                    PASSES[name][0](ctx)
                    if name in FLUSH_AFTER:
                        ctx.flush(f"after {name}")
                ctx.flush("end of run")
            except KeyboardInterrupt:
                ProgressBar.stop_all()
                interrupted = True
                log.warn("Interrupted. " + ("Writing the report for what was planned so far..." if opt.dry_run
                                            else "Saving the work planned so far..."))
                try:
                    ctx.flush("interrupted")
                except KeyboardInterrupt:
                    log.error("Interrupted again; unsaved changes were dropped. The journal has everything written.")

            if not opt.dry_run and ctx.batch_no:
                _verify(cfg, run_dir, snap, lib, log)
            meta["status"] = "interrupted" if interrupted else "finished"
            summary = write_reports(ctx, {"run_id": run_id, "dry_run": opt.dry_run, "passes": opt.passes,
                                          "backup": meta["backup"], "status": meta["status"]})
            log.info("")
            log.info(f"Report:  {run_dir / 'report.html'}")
            log.info(f"Changes: {run_dir / 'changes.csv'}  ({len(lib.changes)} field change(s) on "
                     f"{len({c.book_id for c in lib.changes})} book(s))")
            log.info(f"Review:  {run_dir / 'review.csv'}  ({len(lib.review)} item(s))")
            if summary.get("alias_suggestions_file"):
                log.info(f"Aliases: {summary['alias_suggestions_file']}")
            if not opt.dry_run:
                w = ctx.write_counts
                log.info(f"Written: {w['applied']} book(s); skipped {w['skipped']}, failed {w['failed']}, partial {w['partial']}.")
                if ctx.batch_no:
                    log.info(f"Undo this run with:  ./cwa-organizer undo {run_id}")
            return 130 if interrupted else 0
    except safety.SafetyError as e:
        log.error(str(e))
        meta["status"] = "refused"
        return 2
    except calibre_bridge.CalibreError as e:
        log.error(str(e))
        meta["status"] = "failed"
        if ctx and ctx.batch_no:
            log.error(f"Some changes were written before the failure. Undo them with: ./cwa-organizer undo {run_id}")
        return 4
    finally:
        ProgressBar.stop_all()
        meta["finished"] = dt.datetime.now().isoformat(timespec="seconds")
        if ctx:
            meta["batches_written"] = ctx.batch_no
            meta["writes"] = ctx.write_counts
            ctx.close()
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        snap_file = run_dir / "snapshot.db"
        if snap_file.exists():
            snap_file.unlink()  # the backup (write runs) is the durable copy
        log.close()


def _verify(cfg: Config, run_dir: Path, before_snap: Path, lib, log: Log) -> None:
    after = safety.take_snapshot(cfg, run_dir / "after.db", include_wal=False)
    try:
        integ = safety.integrity(after)
        n_before, n_after = safety.book_count(before_snap), safety.book_count(after)
        uv_before, uv_after = safety.user_version(before_snap), safety.user_version(after)
        if integ != "ok":
            log.error(f"POST-RUN INTEGRITY CHECK FAILED: {integ}. Do not start CWA. See README: 'If something goes wrong'.")
        elif n_before != n_after:
            log.error(f"Book count changed from {n_before} to {n_after}. This tool never adds or deletes books; investigate.")
        else:
            log.info(f"Verified: database integrity ok, {n_after} books.")
        if uv_before != uv_after:
            log.warn(f"Database schema changed from {uv_before} to {uv_after}.")
    finally:
        after.unlink(missing_ok=True)


# ------------------------------------------------------------------- undo ---

def undo(cfg: Config, target_run: str, dry_run: bool, yes: bool) -> int:
    src = cfg.runs_dir / target_run
    journal = src / "journal.jsonl"
    if not journal.exists():
        print(f"No journal for run '{target_run}' (dry runs write nothing and cannot be undone).", file=sys.stderr)
        return 1
    records = []
    with open(journal, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                records.append(json.loads(ln))
    ops, notes = build_undo_ops(records)
    run_id = new_run_id("undo-")
    run_dir = cfg.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = Log(run_dir / "run.log")
    try:
        log.info(f"Undo of run {target_run}: {len(ops)} write(s) to reverse.")
        for n in notes:
            log.warn(n)
        if dry_run or not ops:
            (run_dir / "undo-plan.json").write_text(json.dumps(ops, indent=1))
            log.info(f"Plan saved to {run_dir / 'undo-plan.json'}; nothing written.")
            return 0
        with safety.RunLock(cfg.state_dir / "organizer.lock"):
            pf = safety.preflight(cfg, for_write=True)
            for m in pf.warnings:
                log.warn(m)
            if not pf.ok:
                for m in pf.problems:
                    log.error(m)
                return 2
            if not (cfg["safety"].get("cwa_url") or "").strip() and not _confirm("Is CWA stopped?", yes):
                log.error("Not confirmed that CWA is stopped.")
                return 2
            if not _confirm(f"Reverse {len(ops)} write(s) from run {target_run}?", yes):
                log.info("Cancelled.")
                return 1
            calibre_bridge.require_calibre(cfg)
            snap = safety.take_snapshot(cfg, run_dir / "snapshot.db", include_wal=False)
            if safety.integrity(snap) != "ok":
                log.error("metadata.db failed its integrity check; not writing.")
                return 3
            backup = safety.make_backup(cfg, snap, run_id)
            log.info(f"Backup saved: {backup}")
            try:
                recs = calibre_bridge.apply_plan(cfg, ops, run_dir / "plan-undo.json", run_dir / "journal.jsonl")
            except calibre_bridge.ApplyError as e:
                recs = e.records
                log.error(str(e))
            counts = {}
            for r in recs:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
                if r["status"] != "applied":
                    log.warn(f"[{r['book_id']}] {r['status']}: {r.get('error')}")
            log.info(f"Undo finished: {counts}")
            _verify(cfg, run_dir, snap, None, log)
            snap.unlink(missing_ok=True)
            return 0 if counts.get("applied", 0) == len(ops) else 5
    except safety.SafetyError as e:
        log.error(str(e))
        return 2
    finally:
        log.close()


def build_undo_ops(records: list[dict]) -> tuple[list[dict], list[str]]:
    """Reverse journal records, newest first. Each op expects the value that run wrote."""
    ops, notes = [], []
    for r in reversed(records):
        if r.get("status") not in ("applied", "partial") or not r.get("after"):
            continue
        fields, expect = {}, {}
        for fld, new_val in r["after"].items():
            old_val = r.get("before", {}).get(fld)
            if fld == "cover":
                if old_val:
                    notes.append(f"[{r['book_id']}] had a cover before; the old image can't be restored, left as is.")
                    continue
                fields["cover"], expect["cover"] = None, True
                continue
            fields[fld], expect[fld] = old_val, new_val
        if fields:
            ops.append({"book_id": r["book_id"], "title": r.get("title"), "fields": fields, "expect": expect})
    return ops, notes


def list_runs(cfg: Config) -> int:
    if not cfg.runs_dir.exists():
        print("No runs yet.")
        return 0
    for d in sorted(cfg.runs_dir.iterdir()):
        mf = d / "meta.json"
        if not mf.exists():
            continue
        try:
            m = json.loads(mf.read_text())
        except ValueError:
            continue
        kind = "dry run" if m.get("dry_run") else "write"
        w = m.get("writes") or {}
        print(f"{m.get('run_id', d.name):<22} {kind:<8} {m.get('status', '?'):<11} "
              f"passes={','.join(m.get('passes', []))[:60]:<60} written={w.get('applied', 0)}")
    return 0
