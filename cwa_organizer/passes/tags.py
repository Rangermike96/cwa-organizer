"""Tag cleanup: decode entities, split BISAC / library-catalog strings, synonyms, case duplicates.

Tags are always merged: the pass only rewrites or drops tags that match a
cleanup rule, and never replaces a book's tag list wholesale.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..textutil import clean_text, fold

PASS = "tags"
_LOC_PREFIX = re.compile(r"^(?:cyac|lcgft|lcsh|fast|bisac)\s*:\s*", re.I)
_LOC_SUFFIX = re.compile(r"\s*--\s*(?:fiction|juvenile fiction|comic books, strips, etc|novels|translations into english)\.?\s*$", re.I)


class TagNormalizer:
    def __init__(self, tag_cfg: dict, preferred_case: dict | None = None):
        self.split_bisac = tag_cfg.get("split_bisac", True)
        self.junk = {fold(t) for t in tag_cfg.get("junk", [])}
        self.drop_author_tags = tag_cfg.get("drop_author_name_tags", True)
        self.synonyms = {fold(k): clean_text(v) for k, v in tag_cfg.get("synonyms", {}).items()}
        self.preferred_case = preferred_case or {}

    def expand(self, raw: str) -> list[str]:
        """One stored tag -> zero or more clean tags (before synonym/case handling)."""
        t = clean_text(raw)
        if not t:
            return []
        parts = [p for p in t.split("|")] if "|" in t else [t]
        out = []
        for p in parts:
            p = clean_text(p)
            if not p or re.match(r"^https?://", p, re.I):
                continue
            p = _LOC_PREFIX.sub("", p)
            p = _LOC_SUFFIX.sub("", p).rstrip(".").strip()
            if "--" in p:
                p = p.split("--", 1)[0].strip()
            if self.split_bisac and " / " in p:
                for seg in p.split(" / "):
                    seg = re.sub(r"\s+-\s+general$", "", clean_text(seg), flags=re.I)
                    if seg:
                        out.append(seg)
            else:
                out.append(re.sub(r"\s+-\s+general$", "", p, flags=re.I))
        return [x for x in out if x]

    def canonical(self, tag: str) -> str:
        f = fold(tag)
        if f in self.synonyms:
            return self.synonyms[f]
        return self.preferred_case.get(f, tag)

    def normalize(self, tags, authors=()) -> list[str]:
        author_folds = {fold(a) for a in authors} if self.drop_author_tags else set()
        out, seen = [], set()
        for raw in tags:
            for t in self.expand(raw):
                f = fold(t)
                if f in self.junk or f in author_folds:
                    continue
                c = self.canonical(t)
                cf = fold(c)
                if cf in self.junk or cf in seen:
                    continue
                seen.add(cf)
                out.append(c)
        return out


def preferred_casing(books) -> dict:
    """Most common spelling of each tag, case-insensitively (ties: the one with more capitals)."""
    counts = defaultdict(Counter)
    for b in books:
        for t in b.tags:
            ct = clean_text(t)
            counts[fold(ct)][ct] += 1
    pref = {}
    for f, c in counts.items():
        pref[f] = sorted(c.items(), key=lambda kv: (-kv[1], -sum(ch.isupper() for ch in kv[0]), kv[0]))[0][0]
    return pref


def run(ctx) -> None:
    lib = ctx.lib
    norm = TagNormalizer(ctx.cfg["tags"], preferred_casing(lib.books.values()))
    ctx._tag_norm = norm
    changed = 0
    for b in sorted(lib.books.values(), key=lambda x: x.id):
        if not b.tags:
            continue
        new = norm.normalize(b.tags, b.authors)
        if [clean_text(x) for x in new] == list(b.tags):
            continue
        if not ctx.within_limit(PASS):
            break
        removed = [t for t in b.tags if fold(t) not in {fold(x) for x in new}]
        added = [t for t in new if fold(t) not in {fold(x) for x in b.tags}]
        recased = [t for t in new if t not in b.tags and fold(t) in {fold(x) for x in b.tags}]
        why = []
        if removed:
            why.append("removed/split " + ", ".join(removed))
        if added:
            why.append("added " + ", ".join(added))
        if recased:
            why.append("fixed case/encoding: " + ", ".join(recased))
        if lib.set(b, "tags", new, PASS, "; ".join(why) or "reordered/decoded"):
            changed += 1
            ctx.stat(PASS, "books_changed")
    ctx.log.info(f"Tags: {changed} book(s) with cleaned tags.")
