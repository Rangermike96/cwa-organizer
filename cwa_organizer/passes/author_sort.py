"""Rebuild each book's author_sort from its own author records.

Books imported by other tools often carry an author_sort that calibre did not
build: two authors joined with "and" instead of "&", or a spelling that no
longer matches the author record ("FUJIKI, WASHIRO" after the author was
renamed to "Fujiki, Washiro"). Calibre-Web splits author_sort on "&" to show
names in reading order, logs an error for every book it cannot split, and
sorts those books in the wrong place.

Only the author_sort field is written. It is not part of a book's folder path,
so nothing is renamed or moved. Books whose authors changed earlier in the same
run are left alone: calibre recomputes their author_sort itself.
"""
from __future__ import annotations

PASS = "author_sort"


def desired(book) -> str | None:
    """What calibre would store: each author's own sort value, joined with ' & '."""
    if not book.authors:
        return None
    parts = []
    for name in book.authors:
        srt = (book.author_sorts.get(name) or "").strip()
        if not srt:
            return None  # an author this run added or renamed; leave it to calibre
        parts.append(srt)
    return " & ".join(parts)


def run(ctx) -> None:
    lib = ctx.lib
    changed = joined = 0
    for b in sorted(lib.books.values(), key=lambda x: x.id):
        want = desired(b)
        if not want or want == (b.author_sort or "").strip():
            continue
        if not ctx.within_limit(PASS):
            break
        why = "author sort rebuilt from the author records"
        if " and " in (b.author_sort or ""):
            why += " (was joined with 'and', which Calibre-Web cannot read)"
            joined += 1
        if lib.set(b, "author_sort", want, PASS, why):
            changed += 1
            ctx.stat(PASS, "books_changed")
    extra = f", {joined} of them joined with 'and'" if joined else ""
    ctx.log.info(f"Author sort: {changed} book(s) rebuilt from their author records{extra}.")
