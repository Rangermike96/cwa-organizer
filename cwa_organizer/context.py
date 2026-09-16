"""Run context shared by all passes: config, library model, logging, providers, write batching."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from . import calibre_bridge
from .config import Config
from .library import Library
from .progress import NullBar, ProgressBar
from .providers.http import JsonCache, JsonClient


class Log:
    def __init__(self, path: Path | None, verbose: bool = False, quiet: bool = False):
        self.fh = open(path, "a", encoding="utf-8") if path else None
        self.verbose = verbose
        self.quiet = quiet

    def _emit(self, level, msg, to_console):
        line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {level:<5} {msg}"
        if self.fh:
            self.fh.write(line + "\n")
            self.fh.flush()
        if to_console:
            ProgressBar.print_above(msg if level == "INFO" else f"{level}: {msg}",
                                    sys.stderr if level in ("WARN", "ERROR") else sys.stdout)

    def info(self, msg):
        self._emit("INFO", msg, not self.quiet)

    def detail(self, msg):
        self._emit("DEBUG", msg, self.verbose)

    def warn(self, msg):
        self._emit("WARN", msg, True)

    def error(self, msg):
        self._emit("ERROR", msg, True)

    def close(self):
        if self.fh:
            self.fh.close()


class RunContext:
    def __init__(self, cfg: Config, lib: Library, run_id: str, run_dir: Path, log: Log, *,
                 dry_run: bool, limit: int, force_rescan: bool, offline: bool,
                 reclassify: bool = False, deep_files: bool = False, full_preview: bool = False):
        self.cfg = cfg
        self.lib = lib
        self.run_id = run_id
        self.run_dir = run_dir
        self.log = log
        self.dry_run = dry_run
        self.limit = max(0, int(limit or 0))
        self.force_rescan = force_rescan
        self.offline = offline
        self.reclassify = reclassify
        self.deep_files = deep_files
        self.full_preview = full_preview
        self.aliases = cfg.aliases()
        self.batch_no = 0
        self.write_counts = {"applied": 0, "skipped": 0, "failed": 0, "partial": 0}
        self.columns_ready = False
        self.writing = False
        self._cache = None
        self._mu = None
        self._hc = None
        self.provider_errors: dict[str, int] = {}
        self.pass_stats: dict[str, dict] = {}

    # ------------------------------------------------------------ helpers --
    def stat(self, pass_name: str, key: str, n: int = 1):
        d = self.pass_stats.setdefault(pass_name, {})
        d[key] = d.get(key, 0) + n

    def within_limit(self, pass_name: str) -> bool:
        """True while this pass may still change another book (--limit)."""
        if not self.limit:
            return True
        return self.pass_stats.get(pass_name, {}).get("books_changed", 0) < self.limit

    def progress(self, label: str, total: int):
        """A progress bar for a pass (a plain line every ~10% when not on a terminal)."""
        if self.log.quiet or total <= 0:
            return NullBar()
        return ProgressBar(label, total, plain_log=self.log.info)

    def lookup_cap(self, pass_name: str, total: int) -> int:
        """How many books a slow per-book online pass may look up (0 = all).

        --limit wins. A dry run without --limit previews 10 books unless --full-preview is given.
        """
        if self.limit:
            return self.limit
        if self.dry_run and not self.full_preview and total > 10:
            self.log.info(f"{pass_name}: dry run, so only previewing 10 of {total} book(s) "
                          "(use --full-preview or --limit N to look up more).")
            return 10
        return 0

    @property
    def cache(self) -> JsonCache:
        if self._cache is None:
            self._cache = JsonCache(self.cfg.state_dir / "http_cache.sqlite")
        return self._cache

    def mangaupdates(self):
        if self.offline or not self.cfg["mangaupdates"]["enabled"]:
            return None
        if self._mu is None:
            from .providers.mangaupdates import MangaUpdates
            c = self.cfg["mangaupdates"]
            client = JsonClient(self.cache, c["min_interval_seconds"], c["cache_days"], log=self.log.detail)
            self._mu = MangaUpdates(client, c["min_similarity"])
        return self._mu

    def hardcover(self):
        if self.offline or not self.cfg["hardcover"]["enabled"]:
            return None
        token = self.cfg.hardcover_token
        if not token:
            return None
        if self._hc is None:
            from .providers.hardcover import Hardcover
            c = self.cfg["hardcover"]
            client = JsonClient(self.cache, c["min_interval_seconds"], c["cache_days"], log=self.log.detail)
            self._hc = Hardcover(client, token)
        return self._hc

    def provider_errors_given_up(self) -> set:
        return {name for name, n in self.provider_errors.items() if n >= 10}

    def provider_failed(self, name: str, err: Exception) -> bool:
        """Count a provider error. Returns True when the provider should be given up for this run."""
        n = self.provider_errors.get(name, 0) + 1
        self.provider_errors[name] = n
        self.log.warn(f"{name}: {err}")
        if n == 10:
            self.log.warn(f"{name}: 10 errors this run, skipping it for the rest of the run.")
        return n >= 10

    def tag_normalizer(self):
        if getattr(self, "_tag_norm", None) is None:
            from .passes.tags import TagNormalizer, preferred_casing
            self._tag_norm = TagNormalizer(self.cfg["tags"], preferred_casing(self.lib.books.values()))
        return self._tag_norm

    def publisher_normalizer(self):
        if getattr(self, "_pub_norm", None) is None:
            from .passes.publishers import PublisherNormalizer
            names = [b.publisher for b in self.lib.books.values() if b.publisher]
            self._pub_norm = PublisherNormalizer(self.cfg["publishers"].get("aliases", {}), self.aliases["publishers"], names)
        return self._pub_norm

    def book_medium(self, book):
        """'novel' / 'comic' / None for a book, from its Book Type, title marker or files (cached)."""
        if "file_medium" in book.hints:
            return book.hints["file_medium"]
        labels = self.cfg["labels"]
        bt = (book.custom.get(self.cfg.column("booktype")) or "").casefold()
        medium = None
        if bt == labels["manga"].casefold():
            medium = "comic"
        elif bt in (labels["light_novel"].casefold(), labels["other"].casefold()):
            medium = "novel"
        if medium is None:
            from .passes.common import ProbeCache, book_medium
            if getattr(self, "_probe", None) is None:
                self._probe = ProbeCache(self.cfg.state_dir / "probe_cache.sqlite")
            medium, _ = book_medium(book, self.cfg.library, self._probe)
        if medium is None and book.hints.get("title_marker"):
            medium = {"manga": "comic", "novel": "novel"}[book.hints["title_marker"]]
        book.hints["file_medium"] = medium
        return medium

    # ------------------------------------------------------------- writes --
    def flush(self, reason: str = "") -> None:
        ops = self.lib.pending_ops()
        if not ops:
            return
        if self.dry_run:
            return
        if not self.columns_ready and any(f.startswith("#") for op in ops for f in op["fields"]):
            created = calibre_bridge.ensure_columns(self.cfg)
            if created:
                self.log.info("Created custom columns: " + ", ".join("#" + c for c in created))
            self.columns_ready = True
        self.batch_no += 1
        journal = self.run_dir / "journal.jsonl"
        chunk = max(1, int(self.cfg["calibre"].get("write_chunk", 500)))
        self.log.info(f"Writing batch {self.batch_no} ({len(ops)} book(s)){' - ' + reason if reason else ''}...")
        batch = {"applied": 0, "skipped": 0, "failed": 0, "partial": 0}
        self.writing = True
        try:
            with self.progress(f"Writing batch {self.batch_no}", len(ops)) as bar:
                for ci in range(0, len(ops), chunk):
                    part = ops[ci:ci + chunk]
                    plan = self.run_dir / f"plan-{self.batch_no:04d}-{ci // chunk + 1:03d}.json"
                    base = ci

                    def progress(n, _base=base):
                        bar.update(n=_base + n)

                    jstart = journal.stat().st_size if journal.exists() else 0
                    try:
                        records = calibre_bridge.apply_plan(self.cfg, part, plan, journal, on_progress=progress,
                                                            log=self.log.warn)
                    except calibre_bridge.ApplierInterrupted:
                        self._account(calibre_bridge._read_journal(journal, jstart), part, batch)
                        self.log.info("  applied {applied}, skipped {skipped}, failed {failed}, partial {partial}"
                                      " before stopping".format(**batch))
                        raise
                    except calibre_bridge.ApplyError as e:
                        self._account(e.records, part, batch)
                        raise
                    self._account(records, part, batch)
        finally:
            self.writing = False
        self.log.info("  applied {applied}, skipped {skipped}, failed {failed}, partial {partial}".format(**batch))

    def _account(self, records, part, batch):
        """Fold journal records into the model, counters and review list (once per record)."""
        seen = getattr(self, "_accounted", None)
        if seen is None:
            seen = self._accounted = set()
        ids = {op["book_id"] for op in part}
        fresh = []
        for r in records:
            key = (self.batch_no, r["book_id"])
            if r["book_id"] not in ids or key in seen:
                continue
            seen.add(key)
            fresh.append(r)
        self.lib.mark_written(fresh)
        for r in fresh:
            batch[r["status"]] = batch.get(r["status"], 0) + 1
            self.write_counts[r["status"]] = self.write_counts.get(r["status"], 0) + 1
            if r["status"] != "applied":
                self.log.warn(f"[{r['book_id']}] {r['status']}: {r.get('error')}")
                self.lib.add_review("write-problem", [r["book_id"]], f"{r['status']}: {r.get('error')}")
            for fld, why in (r.get("withheld") or {}).items():
                self.lib.add_review("withheld-rename", [r["book_id"]],
                                    f"'{r.get('title')}': {fld} not changed: {why}. Change it by hand if you need to.")

    @property
    def wrote_anything(self) -> bool:
        return (self.write_counts.get("applied", 0) + self.write_counts.get("partial", 0)) > 0

    def save_summary(self, extra: dict) -> None:
        (self.run_dir / "summary.json").write_text(json.dumps(extra, indent=2, default=str))

    def close(self):
        if self._cache:
            self._cache.close()
        if getattr(self, "_probe", None) is not None:
            self._probe.close()
