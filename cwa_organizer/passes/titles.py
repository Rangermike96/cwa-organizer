"""Title cleanup: strip release junk and edition markers, standardize volume formatting.

Runs after the series pass so bare-number titles are only rewritten when the
series pass accepted them. Titles without a recognisable volume are only
cleaned (junk brackets, file extensions, markers), never restructured.
"""
from __future__ import annotations

from ..textutil import clean_text
from .series import skey
from ..titles import extract_marker, parse_title, standard_title, strip_junk

PASS = "titles"


def new_title_for(book, cfg, publishers) -> tuple[str, str]:
    tcfg = cfg["titles"]
    p = book.hints.get("parsed") or parse_title(book.title, publishers, tcfg["bracket_junk_words"])
    reasons = []
    if tcfg["strip_edition_markers"]:
        base = p.cleaned
        if p.marker:
            reasons.append("removed edition marker")
    else:
        base = strip_junk(book.title, publishers, tcfg["bracket_junk_words"])
    if base != clean_text(book.title) and strip_junk(book.title, publishers, tcfg["bracket_junk_words"]) != clean_text(book.title):
        reasons.append("removed release junk")
    new = base
    if tcfg["standardize"]:
        ok = p.kind == "explicit" or (
            p.kind == "bare" and book.hints.get("series_accepted") and book.series and skey(book.series) == skey(p.series or "")
        )
        if ok:
            std = standard_title(p, tcfg["vol_label"])
            if std:
                if not tcfg["strip_edition_markers"]:
                    _, marker = extract_marker(book.title)
                    if marker:
                        std += " (Light Novel)" if marker == "novel" else " (Manga)"
                if std != base:
                    reasons.append("standardized volume format")
                new = std
    return clean_text(new), "; ".join(dict.fromkeys(reasons))


def run(ctx) -> None:
    lib = ctx.lib
    pubs = getattr(ctx, "known_publishers", [])
    changed = 0
    for b in sorted(lib.books.values(), key=lambda x: x.id):
        new, why = new_title_for(b, ctx.cfg, pubs)
        if not new or new == b.title:
            continue
        if not ctx.within_limit(PASS):
            break
        if lib.set(b, "title", new, PASS, why or "cleaned"):
            changed += 1
            ctx.stat(PASS, "books_changed")
    ctx.log.info(f"Titles: {changed} title(s) cleaned or standardized.")
