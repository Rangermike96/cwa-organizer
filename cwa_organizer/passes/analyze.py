"""Always-run first pass: parse every title and author into hints. Changes nothing."""
from __future__ import annotations

from ..titles import parse_title
from .common import junk_author_info


def run(ctx) -> None:
    cfg = ctx.cfg
    publishers = set()
    for b in ctx.lib.books.values():
        if b.publisher:
            publishers.add(b.publisher)
    for key in ("light_novel", "manga", "mixed"):
        publishers.update(cfg["publishers"].get(key, []))
    junk_words = cfg["titles"]["bracket_junk_words"]
    junk_names = cfg["authors"]["junk_names"]
    ctx.known_publishers = sorted(publishers)

    n_junk = n_marker = 0
    for b in ctx.lib.books.values():
        p = parse_title(b.title, publishers=publishers, junk_words=junk_words)
        b.hints["parsed"] = p
        if p.marker:
            b.hints["title_marker"] = p.marker
            n_marker += 1
        junk = {}
        for a in b.authors:
            info = junk_author_info(a, junk_names)
            if info is not None:
                junk[a] = info
        if junk:
            b.hints["junk_author"] = junk
            n_junk += 1
            for info in junk.values():
                if info.get("volume") is not None and p.volume is None:
                    b.hints["volume_hint"] = info["volume"]
                if info.get("publisher"):
                    b.hints["publisher_hint"] = info["publisher"]
    ctx.log.info(f"Analyzed {len(ctx.lib.books)} books: {n_marker} with an edition marker in the title, "
                 f"{n_junk} with a filename-junk or placeholder author.")
