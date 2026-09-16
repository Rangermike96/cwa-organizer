"""Missing covers: first extract the cover from the book file itself, then try calibre's sources."""
from __future__ import annotations

from ..calibre_bridge import extract_cover
from ..providers.calibre_fetch import CalibreFetcher, FetchResult
from ..titles import parse_title, title_variants
from .common import format_path
from .fetch import validate

PASS = "covers"


def run(ctx) -> None:
    lib, cfg = ctx.lib, ctx.cfg
    missing = []
    for b in sorted(lib.books.values(), key=lambda x: x.id):
        on_disk = (cfg.library / b.path / "cover.jpg").exists()
        if not b.has_cover or not on_disk:
            missing.append(b)
    if not missing:
        ctx.log.info("Covers: every book has a cover.")
        return
    out_dir = ctx.run_dir / "covers"
    out_dir.mkdir(parents=True, exist_ok=True)
    fc = cfg["fetch"]
    fetcher = None if ctx.offline else CalibreFetcher(cfg["calibre"]["fetch_ebook_metadata"], fc["plugins"] + ["Big Book Search"], fc["timeout_seconds"])
    fixed = 0
    for b in missing:
        if not ctx.within_limit(PASS):
            break
        target = out_dir / f"{b.id}.jpg"
        source = None
        for f in b.formats:
            if f.fmt.upper() in ("EPUB", "KEPUB", "AZW3", "MOBI", "PDF", "CBZ", "CBR"):
                p = format_path(cfg.library, b, f)
                if p.exists() and extract_cover(cfg, p, target):
                    source = f"extracted from the {f.fmt} file"
                    break
        if not source and fetcher and fetcher.available():
            parsed = b.hints.get("parsed") or parse_title(b.title)
            author = None if b.hints.get("junk_author") else (b.authors[0] if b.authors else None)
            for v in title_variants(parsed, 2):
                res = fetcher.fetch(v, author, cover_path=target)
                if res.kind == FetchResult.MATCH and not validate(res.meta, b, parsed, ctx.book_medium(b), cfg):
                    if target.exists() and target.stat().st_size > 1024:
                        source = f"downloaded for '{res.meta.title}'"
                        break
                if target.exists():
                    target.unlink()
        if source:
            if ctx.dry_run:
                lib.add_review("cover-preview", [b.id], f"'{b.title}': a cover would be added ({source}).")
            if lib.set(b, "cover", str(target), PASS, source):
                fixed += 1
                ctx.stat(PASS, "books_changed")
        else:
            lib.add_review("missing-cover", [b.id], f"'{b.title}' has no cover and none could be found.")
    ctx.log.info(f"Covers: {len(missing)} missing, {fixed} found.")
