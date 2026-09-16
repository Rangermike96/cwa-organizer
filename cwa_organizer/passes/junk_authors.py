"""Replace filename-junk authors ("Vol 02 [June][Scans][4FF7E520]", "VeryPDF") with real ones.

A real author is only written when the lookup is confident:
  * two different sources (Hardcover, MangaUpdates, calibre sources) name the
    same author, or
  * one source has an exact title match and names exactly one author.
Anything else is listed for review and the book is left unchanged.
"""
from __future__ import annotations

import random
import time
from collections import defaultdict

from ..providers.calibre_fetch import CalibreFetcher, FetchResult
from ..providers.http import HttpError
from ..textutil import clean_text, similarity, words_key

PASS = "junk_authors"


def _person_key(name: str) -> str:
    """Order-insensitive name key, so 'Ohba Tsugumi' and 'Tsugumi Ohba' vote together."""
    return "".join(sorted(words_key(name).split()))


def _query_for(book) -> str:
    p = book.hints.get("parsed")
    if p and p.series:
        return p.series
    return p.cleaned if p else book.title


def run(ctx) -> None:
    lib = ctx.lib
    books = [b for b in lib.books.values() if b.hints.get("junk_author")]
    if not books:
        ctx.log.info("Junk authors: none found.")
        return
    mu, hc = ctx.mangaupdates(), ctx.hardcover()
    fetcher = None
    if not ctx.offline:
        c = ctx.cfg["fetch"]
        fetcher = CalibreFetcher(ctx.cfg["calibre"]["fetch_ebook_metadata"], c["plugins"], c["timeout_seconds"])
        if not fetcher.available():
            fetcher = None
    pubnorm = ctx.publisher_normalizer()
    fixed = 0
    looked = 0
    online = not ctx.offline and bool(mu or hc or fetcher)
    cap = ctx.lookup_cap("Junk authors", len(books)) if online else 0
    if online:
        ctx.log.info(f"Junk authors: looking up {min(cap or len(books), len(books))} of {len(books)} book(s); "
                     "each can take 10-60 seconds.")
    bar = ctx.progress("Junk authors", min(cap or len(books), len(books)) if online else 0)
    bar.__enter__()
    for n, b in enumerate(sorted(books, key=lambda x: x.id), 1):
        junk = b.hints["junk_author"]
        # A publisher hint from the brackets can be used even offline.
        hint_pub = b.hints.get("publisher_hint")
        if hint_pub and not b.publisher:
            pub = pubnorm.canonical(hint_pub) if pubnorm else hint_pub
            lib.set(b, "publisher", pub, PASS, f"publisher taken from release filename '{list(junk)[0]}'")

        if ctx.offline or not (mu or hc or fetcher):
            lib.add_review("junk-author", [b.id], f"'{b.title}' has placeholder author(s) {list(junk)}; "
                           "no online lookup was possible (offline or no providers).")
            continue
        if not ctx.within_limit(PASS) or (cap and looked >= cap):
            break
        looked += 1
        q = _query_for(b)
        bar.update(n=looked - 1, item=b.title)
        ctx.log.info(f"  [{looked}/{min(cap or len(books), len(books))}] [{b.id}] {b.title} (searching '{q}')")
        votes = defaultdict(set)      # author key -> sources
        display = {}
        exact_single = []
        evidence = []

        def vote(source, names, title_found):
            names = [clean_text(n) for n in names if clean_text(n)]
            if not names:
                return
            sim = similarity(title_found, q)
            evidence.append(f"{source}: '{title_found}' -> {', '.join(names)} (title match {sim:.2f})")
            for n in names[:1]:  # first credited author only
                k = _person_key(n)
                votes[k].add(source)
                display.setdefault(k, n)
            if words_key(title_found) == words_key(q) and len(names) >= 1:
                exact_single.append(_person_key(names[0]))

        if hc and "hardcover" not in ctx.provider_errors_given_up():
            try:
                for hit in hc.search_books(q):
                    cand_title = hit["series"] if hit["series"] and similarity(hit["series"], q) > similarity(hit["title"], q) else hit["title"]
                    if similarity(cand_title, q) >= ctx.cfg["hardcover"]["min_similarity"] and hit["authors"]:
                        vote("Hardcover", hit["authors"], cand_title)
                        break
            except (HttpError, RuntimeError) as e:
                ctx.provider_failed("hardcover", e)
        if mu and "mangaupdates" not in ctx.provider_errors_given_up():
            try:
                for m in mu.confident_matches(q)[:3]:
                    names = mu.authors_for(m["id"])
                    if names:
                        vote("MangaUpdates", names, m["hit_title"])
                        break
            except HttpError as e:
                ctx.provider_failed("mangaupdates", e)
        if fetcher and not votes:
            res = fetcher.fetch(q, None)
            fc = ctx.cfg["fetch"]
            time.sleep(fc["sleep_base"] + random.uniform(0, fc["sleep_jitter"]))
            if res.kind == FetchResult.MATCH and res.meta.authors:
                if similarity(res.meta.title, q) >= ctx.cfg["fetch"]["min_title_similarity"]:
                    vote("calibre", res.meta.authors, res.meta.title)

        chosen = None
        multi = [k for k, s in votes.items() if len(s) >= 2]
        if len(multi) == 1:
            chosen = multi[0]
        elif not multi and len(votes) == 1 and exact_single and exact_single[0] in votes:
            chosen = exact_single[0]
        if chosen:
            new_authors, replaced = [], False
            for a in b.authors:
                if a in junk:
                    if not replaced:
                        new_authors.append(display[chosen])
                        replaced = True
                else:
                    new_authors.append(a)
            if lib.set(b, "authors", new_authors, PASS, "placeholder author replaced; " + " | ".join(evidence)):
                fixed += 1
                ctx.stat(PASS, "books_changed")
                b.hints.pop("junk_author", None)
                ctx.log.info(f"      -> author: {display[chosen]}")
        else:
            ctx.log.info("      -> no confident match, left for review")
            lib.add_review("junk-author", [b.id], f"'{b.title}' has placeholder author(s) {list(junk)}; "
                           f"no confident match. Evidence: " + (" | ".join(evidence) or "nothing found"))
    bar.update(n=looked)
    bar.__exit__(None, None, None)
    ctx.log.info(f"Junk authors: {len(books)} book(s) with placeholder authors, {looked} looked up, {fixed} fixed.")
