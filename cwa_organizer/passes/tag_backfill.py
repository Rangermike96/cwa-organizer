"""Series tag backfill: a genre tag present on EVERY tagged volume of a series is added to the rest.

Case-insensitive and entity-decoded (fixes the old "Fantasy" vs "fantasy" and
"&amp;" leaks). Format, status and personal tags are never propagated.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from ..textutil import fold

PASS = "tag_backfill"


def run(ctx) -> None:
    lib, cfg = ctx.lib, ctx.cfg
    never = {fold(t) for t in cfg["tags"]["never_propagate"]}
    never |= {fold(cfg.label(x)) for x in ("light_novel", "manga", "other", "fetched", "failed")}
    by_series = defaultdict(list)
    for b in lib.books.values():
        if b.series:
            by_series[b.series].append(b)
    changed = 0
    for name, books in sorted(by_series.items()):
        if len(books) < 2:
            continue
        voters = []
        for b in books:
            content = {fold(t): t for t in b.tags if fold(t) not in never}
            if content:
                voters.append(content)
        if len(voters) < 2:
            continue
        counts = Counter(f for v in voters for f in v)
        consensus = [f for f, c in counts.items() if c == len(voters)]
        if not consensus:
            continue
        display = {}
        for v in voters:
            for f, t in v.items():
                display.setdefault(f, t)
        for b in sorted(books, key=lambda x: x.id):
            have = {fold(t) for t in b.tags}
            add = [display[f] for f in sorted(consensus) if f not in have]
            if not add:
                continue
            if not ctx.within_limit(PASS):
                return
            if lib.set(b, "tags", list(b.tags) + add, PASS,
                       f"series '{name}': every tagged volume ({len(voters)}) has {', '.join(add)}"):
                changed += 1
                ctx.stat(PASS, "books_changed")
    ctx.log.info(f"Tag backfill: {changed} book(s) received tags shared by their whole series.")
