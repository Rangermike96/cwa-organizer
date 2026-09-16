"""Series from Hardcover for books whose titles carry no volume number.

Only written when Hardcover's book title matches ours, the author matches,
the book has a series position, and the series + position slot is free.
Needs a Hardcover API key in secrets.toml; skipped otherwise.
"""
from __future__ import annotations

from ..providers.http import HttpError
from ..textutil import similarity
from .series import skey
from ..titles import fmt_num

PASS = "series_lookup"


def _author_ok(ours, theirs, threshold) -> bool:
    if not ours or not theirs:
        return False
    for a in ours:
        for t in theirs:
            if similarity(a, t) >= threshold or similarity(" ".join(reversed(a.split())), t) >= threshold:
                return True
    return False


def run(ctx) -> None:
    lib = ctx.lib
    hc = ctx.hardcover()
    if hc is None:
        why = "offline" if ctx.offline else "no Hardcover API key in secrets.toml (or [hardcover] disabled)"
        ctx.log.info(f"Series lookup: skipped ({why}).")
        return
    hcfg, fcfg = ctx.cfg["hardcover"], ctx.cfg["fetch"]
    taken = {(skey(b.series), f"{b.series_index:.4f}") for b in lib.books.values() if b.series}
    # Books whose titles carry a volume number belong to the series pass (collision checks, --limit).
    numbered = ("explicit", "bare", "range", "complex", "explicit-unsafe")
    todo = [b for b in lib.books.values()
            if not (b.hints.get("parsed") and b.hints["parsed"].kind in numbered)
            and not b.series and not b.hints.get("junk_author") and not b.hints.get("series_withheld")]
    found = 0
    cap = ctx.lookup_cap("Series lookup", len(todo))
    total = min(cap or len(todo), len(todo))
    ctx.log.info(f"Series lookup: checking {total} book(s) on Hardcover (about {total * 2.5 / 60:.0f} min).")
    bar = ctx.progress("Series lookup", total)
    bar.__enter__()
    for n, b in enumerate(sorted(todo, key=lambda x: x.id), 1):
        if not ctx.within_limit(PASS) or "hardcover" in ctx.provider_errors_given_up() or (cap and n > cap):
            break
        bar.update(n=n - 1, item=f"{found} found · {b.title}")
        p = b.hints.get("parsed")
        q = p.cleaned if p else b.title
        try:
            hits = hc.search_books(f"{q} {b.authors[0]}" if b.authors else q)
            if not hits:
                hits = hc.search_books(q)
        except (HttpError, RuntimeError) as e:
            ctx.provider_failed("hardcover", e)
            continue
        for h in hits:
            full = f"{h['title']}: {h['subtitle']}" if h.get("subtitle") else h["title"]
            tsim = max(similarity(h["title"], q), similarity(full, q))
            if tsim < hcfg["min_similarity"] or not h["series"] or h["position"] is None:
                continue
            if not _author_ok(b.authors, h["authors"], fcfg["min_author_similarity"]):
                continue
            slot = (skey(h["series"]), f"{float(h['position']):.4f}")
            if slot in taken:
                lib.add_review("series-lookup-collision", [b.id],
                               f"Hardcover puts '{b.title}' at '{h['series']}' #{fmt_num(h['position'])}, "
                               "but that slot is already used in the library. Not assigned.")
                break
            # Reuse an existing spelling of the series name if there is one.
            existing = next((x.series for x in lib.books.values() if x.series and skey(x.series) == slot[0]), None)
            name = existing or h["series"]
            why = f"Hardcover: '{full}' is #{fmt_num(h['position'])} in '{h['series']}' (title match {tsim:.2f})"
            lib.set(b, "series", name, PASS, why)
            lib.set(b, "series_index", float(h["position"]), PASS, why)
            if h.get("slug"):
                ids = dict(b.identifiers)
                ids.setdefault("hardcover", h["slug"])
                lib.set(b, "identifiers", ids, PASS, "Hardcover identifier")
            taken.add(slot)
            found += 1
            ctx.stat(PASS, "books_changed")
            break
    bar.__exit__(None, None, None)
    checked = min(total, n) if todo else 0
    ctx.log.info(f"Series lookup: {checked} of {len(todo)} book(s) without a series checked on Hardcover, {found} assigned.")
