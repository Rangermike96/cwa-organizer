"""Metadata fetch through calibre's sources (Google, Open Library, Edelweiss).

A provider result is only used when it passes every gate:
  * English (dc:language eng/en) and at least one author
  * author matches ours (unless ours is a placeholder)
  * title matches ours, and the volume number matches when ours has one
  * edition guard: a "(Manga)" result is never applied to prose, and a
    "(Light Novel)"/"(Novel)" result never to a comic
Then it is MERGED: tags are added, empty fields are filled, identifiers are
added. The title is replaced only when the volume numbers match. Series,
authors and a non-empty publisher/description/date are never overwritten.
"""
from __future__ import annotations

import random
import time

from ..library import is_undefined_date
from ..providers.calibre_fetch import CalibreFetcher, FetchedMeta, FetchResult
from ..providers.http import HttpError
from ..textutil import clean_text, fold, similarity, words_key
from .series import skey
from ..titles import parse_title, standard_title, title_variants

PASS = "fetch"
STATUS_FETCHED, STATUS_FAILED = "Fetched", "Failed"


class Pacer:
    """Keeps a minimum gap between provider calls, and widens it when a source rate-limits us.

    Google answers bursts of requests with HTTP 429, which used to cost minutes
    of retries per book. Spacing the calls out costs seconds and avoids most of them.
    """

    def __init__(self, base: float, cap: float):
        self.base = max(0.0, float(base))
        self.cap = max(float(cap), self.base)
        self.gap = self.base
        self.last = 0.0

    def wait(self) -> None:
        left = self.gap - (time.monotonic() - self.last)
        if left > 0:
            time.sleep(left)
        self.last = time.monotonic()

    def penalize(self) -> None:
        self.gap = min(self.cap, max(self.base, self.gap * 2) if self.gap else max(self.base, 2.0))

    def relax(self) -> None:
        self.gap = max(self.base, self.gap / 2)


def _latin(text: str) -> bool:
    """True when a title is written in Latin script (a stand-in language check)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if c.isascii()) / len(letters) >= 0.8


def from_hardcover(hit: dict) -> FetchedMeta:
    """Turn a Hardcover search hit into the same shape a calibre source returns."""
    m = FetchedMeta()
    m.title = f"{hit['title']}: {hit['subtitle']}" if hit.get("subtitle") else (hit.get("title") or "")
    m.authors = [a for a in (hit.get("authors") or []) if a]
    m.comments = hit.get("description") or ""
    # Hardcover only gives a release year, and a made-up January 1st is worse
    # than an empty date, so the publication date is left alone.
    m.pubdate = ""
    ids = {}
    if hit.get("slug"):
        ids["hardcover"] = str(hit["slug"])
    isbn13 = next((str(i) for i in (hit.get("isbns") or []) if len(str(i).replace("-", "")) == 13), "")
    if isbn13:
        ids["isbn"] = isbn13.replace("-", "")
    m.identifiers = ids
    m.series = hit.get("series") or ""
    m.series_index = hit.get("position")
    return m


def needs_fetch(b, cfg, status_col, force: bool) -> bool:
    status = b.custom.get(status_col)
    if status and not force:
        return False
    if not cfg["fetch"]["only_incomplete"] or force:
        return True
    for f in cfg["fetch"]["required_fields"]:
        v = b.get(f)
        if f == "pubdate":
            if is_undefined_date(v):
                return True
        elif not v:
            return True
    return False


def normalize_pubdate(v: str) -> str | None:
    v = clean_text(v)
    if len(v) < 4 or not v[:4].isdigit() or v.startswith(("0101", "0100", "0000")):
        return None
    y = v[:4]
    m = v[5:7] if len(v) >= 7 and v[5:7].isdigit() else "01"
    d = v[8:10] if len(v) >= 10 and v[8:10].isdigit() else "01"
    return f"{y}-{m}-{d}T12:00:00+00:00"


def our_volume(book, parsed):
    """Volume number we trust for matching: explicit, or a bare number the series pass accepted."""
    if not parsed or parsed.volume is None:
        return None
    if parsed.kind == "explicit" or (parsed.kind == "bare" and (
            book.hints.get("series_accepted") or (book.series and skey(book.series) == skey(parsed.series or "")))):
        return float(parsed.volume)
    return None


def validate(meta, book, parsed, file_medium, cfg, assume_english: bool = False) -> str | None:
    """Return a rejection reason, or None if the match is acceptable.

    assume_english is for sources that carry no language field (Hardcover); the
    caller checks the script of the title instead.
    """
    fc = cfg["fetch"]
    langs = [x.lower() for x in meta.languages]
    if not assume_english and (not langs or langs[0] not in ("eng", "en", "en-us", "en-gb")):
        return f"language {langs[0] if langs else 'missing'}, not English"
    if not meta.authors:
        return "no author in the result"
    if not book.hints.get("junk_author"):
        best = max((similarity(a, t) for a in book.authors for t in meta.authors), default=0.0)
        best = max(best, max((similarity(" ".join(reversed(a.split())), t) for a in book.authors for t in meta.authors), default=0.0))
        if best < fc["min_author_similarity"]:
            return f"author mismatch ({', '.join(meta.authors)} vs {', '.join(book.authors)})"
    rp = parse_title(meta.title)
    if rp.marker == "manga" and file_medium != "comic":
        return f"wrong edition: '{meta.title}' is manga, this book is {'prose' if file_medium else 'not known to be a comic'}"
    if rp.marker == "novel" and file_medium == "comic":
        return f"wrong edition: '{meta.title}' is a novel, this book is a comic"
    if file_medium == "novel" and any(fold(t) in ("manga", "comics & graphic novels / manga") for t in meta.tags) \
            and not any("light novel" in fold(t) for t in meta.tags):
        return f"wrong edition: '{meta.title}' is tagged Manga, this book is prose"
    ours_series = parsed.series if parsed and parsed.series else (parsed.cleaned if parsed else book.title)
    theirs_series = rp.series or rp.cleaned
    tsim = similarity(ours_series, theirs_series)
    if tsim < fc["min_title_similarity"]:
        tsim_full = similarity(parsed.cleaned if parsed else book.title, rp.cleaned)
        if tsim_full < fc["min_title_similarity"]:
            return f"title mismatch ('{meta.title}', similarity {max(tsim, tsim_full):.2f})"
    vol = our_volume(book, parsed)
    if vol is not None:
        their_vol = rp.volume if rp.volume is not None else meta.series_index
        if their_vol is None:
            return f"result '{meta.title}' has no volume number; ours is {vol:g}"
        if abs(float(their_vol) - vol) > 1e-9:
            return f"volume mismatch (result {float(their_vol):g}, ours {vol:g})"
        if (rp.part or None) != (parsed.part or None):
            return f"part mismatch (result part {rp.part}, ours part {parsed.part})"
    elif rp.kind == "explicit" and rp.volume is not None:
        return f"result '{meta.title}' is a numbered volume but ours has no volume number"
    return None


def merge(ctx, b, meta, parsed) -> list[str]:
    lib, cfg = ctx.lib, ctx.cfg
    applied = []
    why = f"fetched: '{meta.title}'"
    norm = ctx.tag_normalizer()
    if meta.tags:
        incoming = norm.normalize(meta.tags, b.authors) if norm else [clean_text(t) for t in meta.tags]
        # Booktype labels are decided by the classify pass, never by a provider's subjects.
        labels = {fold(cfg.label(x)) for x in ("light_novel", "manga", "other")} | {"light novel", "manga"}
        incoming = [t for t in incoming if fold(t) not in labels]
        have = {fold(t) for t in b.tags}
        add = [t for t in incoming if fold(t) not in have]
        if add and lib.set(b, "tags", list(b.tags) + add, PASS, why + "; added tags " + ", ".join(add)):
            applied.append("tags")
    if meta.publisher and not b.publisher:
        pub = ctx.publisher_normalizer().canonical(meta.publisher)
        if lib.set(b, "publisher", pub, PASS, why):
            applied.append("publisher")
    if meta.comments and not (b.comments or "").strip():
        if lib.set(b, "comments", meta.comments, PASS, why):
            applied.append("description")
    pd = normalize_pubdate(meta.pubdate)
    if pd and is_undefined_date(b.pubdate):
        if lib.set(b, "pubdate", pd, PASS, why):
            applied.append("date")
    if meta.languages and not b.languages:
        if lib.set(b, "languages", ["eng"], PASS, why):
            applied.append("language")
    if meta.identifiers:
        ids = dict(b.identifiers)
        added = [k for k in meta.identifiers if k not in {x.lower() for x in ids}]
        for k in added:
            ids[k] = meta.identifiers[k]
        if added and lib.set(b, "identifiers", ids, PASS, why + "; identifiers " + ", ".join(added)):
            applied.append("identifiers")
    # Title policy: replace only when volume numbers match (validate() guarantees it for numbered books).
    rp = parse_title(meta.title)
    vol = our_volume(b, parsed)
    if vol is not None and rp.volume is not None and abs(rp.volume - vol) < 1e-9:
        if skey(rp.series or "") == skey(parsed.series or "") or similarity(rp.series, parsed.series) >= 0.9:
            rp.series = b.series or parsed.series  # always keep our series spelling in the title
            new_title = standard_title(rp, cfg["titles"]["vol_label"]) if cfg["titles"]["standardize"] else rp.cleaned
            # Only take the provider title if it adds information (a subtitle) and keeps ours intact.
            if new_title and rp.subtitle and not (parsed.subtitle) and words_key(new_title) != words_key(b.title):
                if lib.set(b, "title", new_title, PASS, why + "; title gained subtitle, volume numbers match"):
                    applied.append("title")
    if not b.series and not b.hints.get("series_withheld") and meta.series and meta.series_index is not None and vol is not None \
            and abs(meta.series_index - vol) < 1e-9 and similarity(meta.series, parsed.series) >= 0.9:
        taken = any(x.series and skey(x.series) == skey(meta.series) and abs(x.series_index - meta.series_index) < 1e-9
                    for x in lib.books.values())
        if not taken:
            lib.set(b, "series", meta.series, PASS, why)
            lib.set(b, "series_index", float(meta.series_index), PASS, why)
            applied.append("series")
    return applied


def set_status(ctx, b, status: str, reason: str) -> None:
    cfg, lib = ctx.cfg, ctx.lib
    lib.set(b, cfg.column("status"), status, PASS, reason)
    if cfg["labels"]["mirror_to_tags"]:
        fetched, failed = cfg.label("fetched"), cfg.label("failed")
        want = fetched if status == STATUS_FETCHED else failed
        new = [t for t in b.tags if fold(t) not in (fold(fetched), fold(failed))] + [want]
        lib.set(b, "tags", new, PASS, f"status tag '{want}'")


def run(ctx) -> None:
    lib, cfg = ctx.lib, ctx.cfg
    if ctx.offline:
        ctx.log.info("Fetch: skipped (offline).")
        return
    fc = cfg["fetch"]
    fetcher = CalibreFetcher(cfg["calibre"]["fetch_ebook_metadata"], fc["plugins"], fc["timeout_seconds"])
    if not fetcher.available():
        ctx.log.warn(f"Fetch: '{cfg['calibre']['fetch_ebook_metadata']}' not found; install calibre. Skipping.")
        return
    status_col = cfg.column("status")
    todo = [b for b in sorted(lib.books.values(), key=lambda x: x.id) if needs_fetch(b, cfg, status_col, ctx.force_rescan)]
    limit = ctx.lookup_cap("Fetch", len(todo))
    if limit:
        todo = todo[:limit]
    ctx.log.info(f"Fetch: {len(todo)} book(s) to look up"
                 f" (about {len(todo) * (fc['sleep_base'] + fc['sleep_jitter'] / 2 + 6) / 3600:.1f} h at the configured pace).")
    flush_every = cfg["calibre"]["flush_every"]
    bar = ctx.progress("Fetch", len(todo))
    bar.__enter__()
    try:
        _fetch_loop(ctx, todo, fetcher, fc, flush_every, bar)
    finally:
        bar.__exit__(None, None, None)


def _short(detail: str, limit: int = 140) -> str:
    """calibre prints its whole session log on an error; keep the useful head of it."""
    return " ".join((detail or "").split())[:limit]


def _try_hardcover(ctx, hc, book, parsed, medium, author, rejected) -> tuple:
    """Last resort when the calibre sources have nothing acceptable."""
    cfg = ctx.cfg
    query = standard_title(parsed, "Vol.") or (parsed.cleaned if parsed else book.title)
    try:
        hits = hc.search_books(f"{query} {author}" if author else query)
        if not hits:
            hits = hc.search_books(query)
    except (HttpError, RuntimeError) as e:
        ctx.provider_failed("hardcover", e)
        return None, ""
    for hit in hits:
        meta = from_hardcover(hit)
        if not meta.title or not _latin(meta.title):
            continue
        if not (meta.comments or meta.identifiers):
            continue  # nothing this source could add
        reason = validate(meta, book, parsed, medium, cfg, assume_english=True)
        if reason:
            rejected.append(f"Hardcover '{meta.title}' -> {reason}")
            ctx.log.detail(f"    Hardcover '{meta.title}': rejected, {reason}")
            continue
        return meta, meta.title
    return None, ""


def _fetch_loop(ctx, todo, fetcher, fc, flush_every, bar) -> None:
    lib, cfg = ctx.lib, ctx.cfg
    streak = done = ok = failed = errors = hc_ok = 0
    pacer = Pacer(fc["min_request_gap"], fc["max_request_gap"])
    hc = ctx.hardcover() if fc["hardcover_fallback"] else None
    for n, b in enumerate(todo, 1):
        bar.update(n=n - 1, item=f"matched {ok} · no match {failed} · {b.title}")
        parsed = b.hints.get("parsed")
        if parsed is None or parsed.original != clean_text(b.title):
            parsed = parse_title(b.title, getattr(ctx, "known_publishers", []), cfg["titles"]["bracket_junk_words"])
        author = None if b.hints.get("junk_author") else (b.authors[0] if b.authors else None)
        ctx.log.info(f"[{n}/{len(todo)}] [{b.id}] {b.title} - {author or '(no usable author)'}")
        matched, rejected, had_error, no_match_seen, from_hc = None, [], False, False, False
        variants = title_variants(parsed, fc["max_title_variants"])
        medium = ctx.book_medium(b)
        std = standard_title(parsed, "Vol.") if our_volume(b, parsed) is not None else None
        if std and medium in ("novel", "comic"):
            # Providers often list the manga and the light novel under one title; say which one we want.
            variants.insert(1, f"{std} ({'Light Novel' if medium == 'novel' else 'Manga'})")
            variants = variants[: max(fc["max_title_variants"], 2)]
        # Source errors (mostly Google's 429) share one retry budget for the whole book:
        # retrying every phrasing separately used to cost minutes and made the 429s worse.
        err_budget = fc["max_retries"]
        for variant in variants:
            res = None
            for attempt in range(fc["max_retries"] + 1):
                pacer.wait()
                res = fetcher.fetch(variant, author)
                if res.kind != FetchResult.ERROR:
                    pacer.relax()
                    break
                pacer.penalize()
                if err_budget <= 0:
                    break
                err_budget -= 1
                wait = min(fc["retry_backoff_base"] * 2 ** attempt, fc["retry_backoff_cap"])
                ctx.log.detail(f"    '{variant}': {_short(res.detail)}; retry in {wait}s")
                time.sleep(wait)
            if res.kind == FetchResult.ERROR:
                had_error = True
                ctx.log.detail(f"    '{variant}': gave up ({_short(res.detail)})")
                continue
            if res.kind == FetchResult.NO_MATCH:
                no_match_seen = True
                ctx.log.detail(f"    '{variant}': no match")
                time.sleep(1)
                continue
            reason = validate(res.meta, b, parsed, ctx.book_medium(b), cfg)
            if reason:
                rejected.append(f"'{variant}' -> {reason}")
                ctx.log.detail(f"    '{variant}': rejected, {reason}")
                time.sleep(1)
                continue
            matched = res.meta
            if variant != parsed.cleaned:
                ctx.log.detail(f"    matched with phrasing '{variant}'")
            break

        if matched is None and hc is not None and "hardcover" not in ctx.provider_errors_given_up():
            # The calibre sources miss volumes Hardcover has (Google often lists only
            # omnibus or manga editions of a light novel).
            found, why = _try_hardcover(ctx, hc, b, parsed, medium, author, rejected)
            if found:
                matched, from_hc = found, True
                ctx.log.detail(f"    matched on Hardcover as '{why}'")

        if matched:
            src = " on Hardcover" if from_hc else ""
            fields = merge(ctx, b, matched, parsed)
            still_missing = [f for f in fc["required_fields"]
                             if (is_undefined_date(b.pubdate) if f == "pubdate" else not b.get(f))]
            if from_hc and still_missing:
                # Hardcover has no publisher and no full date. Leaving the status unset
                # means a later run tries the other sources again for what is missing.
                ctx.log.info(f"    matched{src} '{matched.title}'; filled {', '.join(fields) or 'nothing'}"
                             f"; still missing {', '.join(still_missing)}, so it stays on the list for next time")
            else:
                set_status(ctx, b, STATUS_FETCHED,
                           f"metadata found{src}: '{matched.title}'" + (f" ({', '.join(fields)})" if fields else ""))
                ctx.log.info(f"    matched{src} '{matched.title}'" + (f"; filled {', '.join(fields)}" if fields else "; nothing new"))
            ok += 1
            hc_ok += 1 if from_hc else 0
            streak = 0
        elif had_error and not no_match_seen and not rejected:
            errors += 1
            ctx.log.warn(f"    [{b.id}] provider errors only; left unmarked so the next run retries it.")
            streak += 1
        else:
            detail = "; ".join(rejected) if rejected else "no source had a match"
            set_status(ctx, b, STATUS_FAILED, detail)
            lib.add_review("fetch-failed", [b.id], f"'{b.title}': {detail}")
            ctx.log.info(f"    no acceptable match ({detail[:200]})")
            failed += 1
            streak = streak + 1 if not rejected else 0
        done += 1
        ctx.stat(PASS, "books_changed")
        if done % flush_every == 0:
            ctx.flush(f"fetch progress {done}/{len(todo)}")
        if n < len(todo):
            if streak >= fc["empty_streak_threshold"]:
                ctx.log.warn(f"{streak} books in a row without results; a source may be blocking us. "
                             f"Cooling down {fc['cooldown_seconds']}s.")
                ctx.flush("before cooldown")
                time.sleep(fc["cooldown_seconds"])
                streak = 0
            else:
                time.sleep(fc["sleep_base"] + random.uniform(0, fc["sleep_jitter"]))
    ctx.log.info(f"Fetch: {ok} matched{f' ({hc_ok} of them on Hardcover)' if hc_ok else ''}, "
                 f"{failed} without an acceptable match, {errors} left for retry.")
