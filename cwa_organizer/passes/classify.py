"""Book type: Light Novel / Manga / Other Books, written to #booktype and mirrored to tags.

Two questions, answered separately:
  1. Medium: is the book prose or comics? Decided by the files themselves
     (CBZ, or an EPUB that is mostly images) and by title markers such as
     "(Manga)". Tags and publishers only help when the files can't tell.
  2. Origin: is it a Japanese/Asian light novel or manga, or general fiction?
     Decided by MangaUpdates matches, LN/manga publisher lists and tags.

MangaUpdates often lists a manga AND its novel under the same English name,
so a match alone never decides the medium. "Other Books" needs positive
evidence: prose, not found on MangaUpdates, and no LN/manga signal at all.
When signals conflict nothing is written and the book goes to review.
"""
from __future__ import annotations

from ..providers.http import HttpError
from ..textutil import fold
from .common import ProbeCache, book_medium, publisher_class

PASS = "classify"


def decide(*, file_medium, title_marker, tag_medium, pub_class, mu_media, mu_searched) -> tuple[str | None, str]:
    """Pure decision table. Returns ('novel'|'manga'|'other'|None, reason).

    file_medium: 'comic' | 'novel' | None   (from the files)
    title_marker: 'manga' | 'novel' | None  (from "(Manga)" etc.)
    tag_medium:  'manga' | 'novel' | 'conflict' | None (existing tags)
    pub_class:   'ln' | 'manga' | 'mixed' | 'general' | None
    mu_media:    set of 'novel'/'comic' from confident MangaUpdates matches
    mu_searched: whether a MangaUpdates lookup was possible
    """
    reasons = []
    marker_med = {"manga": "comic", "novel": "novel"}.get(title_marker)
    tag_med = {"manga": "comic", "novel": "novel"}.get(tag_medium)
    pub_med = {"ln": "novel", "manga": "comic"}.get(pub_class)

    # ---- 1. medium --------------------------------------------------------
    medium = None
    if file_medium:
        if marker_med and marker_med != file_medium:
            return None, f"conflict: files look like {file_medium} but the title says {title_marker}"
        medium = file_medium
        reasons.append(f"files: {file_medium}")
    elif marker_med:
        medium = marker_med
        reasons.append(f"title marker: {title_marker}")
    else:
        weak = {m for m in (tag_med, pub_med) if m}
        if len(weak) == 1:
            medium = weak.pop()
            reasons.append("tags/publisher: " + medium)
        elif len(weak) > 1:
            return None, "conflict: tags and publisher disagree about prose vs comics"
        elif len(mu_media) == 1:
            medium = next(iter(mu_media))
            reasons.append("MangaUpdates: only a " + medium + " entry matches")
    if medium is None:
        return None, "medium unknown (no file, title, tag, publisher or MangaUpdates evidence)"

    # ---- 2. origin --------------------------------------------------------
    if medium == "comic":
        signals = []
        if mu_media:
            signals.append("MangaUpdates match")
        if pub_class in ("manga", "mixed", "ln"):
            signals.append(f"publisher ({pub_class})")
        if tag_medium == "manga" or title_marker == "manga":
            signals.append("manga tag/marker")
        if signals:
            return "manga", "; ".join(reasons + signals)
        if pub_class == "general":
            return None, "comic-format book from a general publisher (Western comic?)"
        if mu_searched:
            return None, "comic-format book not found on MangaUpdates"
        return None, "comic-format book; origin unknown offline"

    # prose
    signals = []
    if "novel" in mu_media:
        signals.append("MangaUpdates novel entry")
    if pub_class == "ln":
        signals.append("light novel publisher")
    if tag_medium == "novel":
        signals.append("light novel tag")
    if title_marker == "novel":
        signals.append("light novel marker")
    if signals:
        if pub_class == "general" and "novel" not in mu_media:
            return None, "conflict: light novel signal but a general-fiction publisher"
        return "novel", "; ".join(reasons + signals)
    if "comic" in mu_media:
        # A prose book sharing a manga's name: a spin-off novel.
        if pub_class == "general":
            return None, "prose book sharing a manga's name, from a general publisher"
        return "novel", "; ".join(reasons + ["novelization of a MangaUpdates manga entry"])
    if pub_class == "mixed":
        return None, "prose from a mixed LN/manga publisher, not found on MangaUpdates"
    if mu_searched:
        return "other", "; ".join(reasons + ["not on MangaUpdates", f"publisher: {pub_class or 'unknown'}"])
    return None, "prose book; origin unknown offline"


def _tag_medium(tags, labels) -> str | None:
    f = {fold(t) for t in tags}
    novel = fold(labels["light_novel"]) in f or "light novel" in f or "light novels" in f
    manga = fold(labels["manga"]) in f or "manga" in f
    if novel and manga:
        return "conflict"
    return "novel" if novel else "manga" if manga else None


def _query(b) -> str | None:
    p = b.hints.get("parsed")
    if b.series:
        return b.series
    if p and p.series:
        return p.series
    if p and p.cleaned:
        return p.cleaned
    return b.title


def run(ctx) -> None:
    lib, cfg = ctx.lib, ctx.cfg
    labels = cfg["labels"]
    col = cfg.column("booktype")
    label_of = {"novel": labels["light_novel"], "manga": labels["manga"], "other": labels["other"]}
    all_labels = {fold(v) for v in label_of.values()} | {"light novel", "light novels"}
    mu = ctx.mangaupdates()
    probe = ProbeCache(cfg.state_dir / "probe_cache.sqlite")
    mu_cache = {}
    changed = counts = 0
    tally = {"novel": 0, "manga": 0, "other": 0, None: 0}
    if mu:
        ctx.log.info(f"Classify: checking {len(lib.books)} books. MangaUpdates lookups are about 1.5 s each and "
                     "cached, so the first run takes a while (roughly 20-40 min for this library); later runs are fast.")
    bar = ctx.progress("Classify", len(lib.books))
    try:
        bar.__enter__()
        for i, b in enumerate(sorted(lib.books.values(), key=lambda x: x.id)):
            bar.update(n=i, item=f"LN {tally['novel']} · manga {tally['manga']} · other {tally['other']} · unsure {tally[None]}")
            if i and i % 100 == 0:
                probe.commit()
            current = b.custom.get(col)
            tag_types = [t for t in b.tags if fold(t) in {fold(v) for v in label_of.values()}]
            if current and not ctx.reclassify and not ctx.force_rescan:
                _mirror(ctx, b, current, label_of, all_labels)
                continue
            if not ctx.within_limit(PASS):
                break
            fmed, freason = book_medium(b, cfg.library, probe)
            b.hints["file_medium"] = fmed
            query = _query(b)
            mu_media, mu_searched = set(), False
            if mu and query and "mangaupdates" not in ctx.provider_errors_given_up():
                qk = fold(query)
                if qk not in mu_cache:
                    try:
                        mu_cache[qk] = mu.confident_matches(query)
                    except HttpError as e:
                        ctx.provider_failed("mangaupdates", e)
                        mu_cache[qk] = None
                matches = mu_cache[qk]
                if matches is not None:
                    mu_searched = True
                    mu_media = {m["medium"] for m in matches if m.get("medium")}
                    if matches:
                        b.hints["mu_matches"] = matches
            decision, reason = decide(
                file_medium=fmed, title_marker=b.hints.get("title_marker"), tag_medium=_tag_medium(b.tags, labels),
                pub_class=publisher_class(b.publisher, cfg["publishers"]), mu_media=mu_media, mu_searched=mu_searched,
            )
            counts += 1
            tally[decision] += 1
            b.hints["booktype_reason"] = f"{reason} ({freason})"
            if decision is None:
                lib.add_review("unclassified", [b.id], f"'{b.title}': {reason} ({freason})")
                continue
            value = label_of[decision]
            if current and fold(current) != fold(value):
                if not ctx.reclassify:
                    lib.add_review("classification-conflict", [b.id],
                                   f"'{b.title}' is '{current}' but evidence says '{value}': {reason}. "
                                   "Not changed (use --reclassify to overwrite).")
                    _mirror(ctx, b, current, label_of, all_labels)
                    continue
            if len(tag_types) > 1 or (tag_types and fold(tag_types[0]) != fold(value)):
                ctx.log.detail(f"  [{b.id}] booktype tags {tag_types} -> {value}")
            ch = lib.set(b, col, value, PASS, reason)
            ch = _mirror(ctx, b, value, label_of, all_labels) or ch
            if ch:
                changed += 1
                ctx.stat(PASS, "books_changed")
        bar.update(n=len(lib.books))
    finally:
        bar.__exit__(None, None, None)
        probe.close()
    ctx.log.info(f"Classify: examined {counts} book(s): {tally['novel']} light novel, {tally['manga']} manga, "
                 f"{tally['other']} other, {tally[None]} left for review; {changed} book(s) changed.")


def _mirror(ctx, b, value, label_of, all_labels) -> bool:
    """Make the tags agree with the column: exactly one booktype tag."""
    if not ctx.cfg["labels"]["mirror_to_tags"]:
        return False
    keep = [t for t in b.tags if fold(t) not in all_labels]
    new = keep + [value]
    if sorted(fold(t) for t in new) == sorted(fold(t) for t in b.tags):
        return False
    removed = [t for t in b.tags if fold(t) in all_labels and fold(t) != fold(value)]
    why = f"booktype tag set to '{value}'" + (f", removed {removed}" if removed else "")
    return ctx.lib.set(b, "tags", new, PASS, why)
