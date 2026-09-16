"""Author normalization.

Automatic (safe) changes:
  * split "A; B" and "First Last, First Last" into separate authors
  * drop credits explicitly marked as illustrator/translator ("Illustrated by X")
  * strip catalog noise: "880-01 " prefixes, ", 1965-" life dates, trailing "," or "_"
  * merge spellings that differ only in case, spaces or punctuation (NISIOISIN / Nisio Isin)
  * flip "Last, First" when "First Last" already exists in the library
  * apply your approved [authors] entries from aliases.toml

Everything fuzzier (Ao Jyumonji / Ao Juumonji, name-order swaps) becomes a
suggestion in review/aliases_suggested.toml for you to approve.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..textutil import clean_text, fold, key, romaji_key, similarity

PASS = "authors"
_MARC = re.compile(r"^\d{3}-\d{2}\s+")
_DATES = re.compile(r",?\s*(?:\d{4}\s*-\s*(?:\d{4})?|\d{4}\s*-|b\.\s*\d{4}|d\.\s*\d{4})\.?\s*$")
_ROLE_DROP = re.compile(r"^(?:illustrated\s+by|illustrations?\s+by|illustrator|cover\s+art\s+by|translated\s+by|translator|translation\s+by|edited\s+by|editor|narrated\s+by)\s*[:_\-]?\s+", re.I)
_ROLE_KEEP = re.compile(r"^(?:author|written\s+by|story\s+by|original\s+story\s+by|by)\s*[:_\-]?\s+", re.I)
_NOT_PERSON = re.compile(r"\b(?:editorial|department|publishing|publishers|staff|bunko)\b", re.I)
_TRAIL = re.compile(r"[\s,;_]+$")
_LEAD = re.compile(r"^[\s,;_]+")


def _tidy(n: str) -> str:
    n = clean_text(n)
    n = _MARC.sub("", n)
    n = _DATES.sub("", n)
    n = _TRAIL.sub("", _LEAD.sub("", n))
    return clean_text(n)


def split_author(name: str) -> tuple[list[str], list[str]]:
    """Return (authors, dropped_credits) for one stored author string."""
    raw = clean_text(name)
    pieces = [p for p in raw.split(";")] if ";" in raw else [raw]
    catalog = ";" in raw or bool(_MARC.match(raw)) or bool(_DATES.search(raw))
    authors, dropped = [], []
    for piece in pieces:
        piece = _tidy(piece)
        if not piece:
            continue
        parts = [clean_text(x) for x in piece.split(",")]
        has_role = any(_ROLE_DROP.match(p) or _ROLE_KEEP.match(p) for p in parts)
        multi_word = len(parts) >= 2 and all(len(p.split()) >= 2 for p in parts if p)
        chunks = parts if (has_role or multi_word) else [piece]
        for c in chunks:
            c = _tidy(c)
            if not c:
                continue
            if _ROLE_DROP.match(c) or _NOT_PERSON.search(c):
                dropped.append(_tidy(_ROLE_DROP.sub("", c)))
                continue
            c = _tidy(_ROLE_KEEP.sub("", c))
            if c and catalog:
                c = flipped(c) or c  # library-catalog data is reliably "Last, First"
            if c:
                authors.append(c)
    return authors, dropped


_LAST_FIRST = re.compile(r"^(?P<last>[^,\d]+),\s*(?P<first>[^,\d]+)$")


def flipped(name: str) -> str | None:
    m = _LAST_FIRST.match(clean_text(name))
    if not m:
        return None
    return clean_text(f"{m.group('first')} {m.group('last')}")


def _display_rank(name: str, count: int):
    mixed = not name.isupper() and not name.islower()
    return (-count, " " not in name, not mixed, "," in name, name)


class AuthorNormalizer:
    def __init__(self, name_counts: Counter, user_aliases: dict, flip_only_when_counterpart_exists=True):
        self.user_alias = {fold(k): clean_text(v) for k, v in user_aliases.items()}
        self.flip_strict = flip_only_when_counterpart_exists
        groups = defaultdict(Counter)
        for n, c in name_counts.items():
            groups[key(n, False)][n] += c
        self.key_canon = {k: sorted(c, key=lambda n: _display_rank(n, c[n]))[0] for k, c in groups.items()}
        self.groups = groups
        self.flip_suggestions = {}

    def canonical(self, name: str) -> tuple[str, str | None]:
        """(canonical name, reason or None)."""
        n = clean_text(name)
        if fold(n) in self.user_alias:
            return self.user_alias[fold(n)], "aliases.toml"
        fl = flipped(n)
        if fl:
            fk = key(fl, False)
            if fk in self.key_canon and fk != key(n, False):
                return self.key_canon[fk], "'Last, First' form of existing author"
            if not self.flip_strict:
                return fl, "'Last, First' flipped"
            self.flip_suggestions[n] = fl
        k = key(n, False)
        canon = self.key_canon.get(k, n)
        if fold(canon) in self.user_alias:
            return self.user_alias[fold(canon)], "aliases.toml"
        return canon, ("same name, different spelling/case" if canon != n else None)


def suggest_variants(names: Counter, threshold: float) -> list[tuple[str, str, str]]:
    """Pairs of distinct names that may be the same person. Returns (a, b, why)."""
    out, seen = [], set()
    by_romaji = defaultdict(list)
    by_tokens = defaultdict(list)
    for n in names:
        by_romaji[romaji_key(n)].append(n)
        by_tokens[" ".join(sorted(fold(n).replace(",", " ").split()))].append(n)

    def add(a, b, why):
        if key(a, False) == key(b, False):
            return
        pair = tuple(sorted((a, b)))
        if pair not in seen:
            seen.add(pair)
            out.append((pair[0], pair[1], why))

    for group in by_romaji.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                add(a, b, "romanization variant")
    for group in by_tokens.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                add(a, b, "same words, different order")
    # Fuzzy pass within buckets that share the first 3 letters of the romaji key.
    buckets = defaultdict(list)
    for n in names:
        rk = romaji_key(n)
        if len(rk) >= 5:
            buckets[rk[:3]].append(n)
    for group in buckets.values():
        if len(group) > 300:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if not _tokens_align(a, b):
                    continue
                if similarity(romaji_key(a), romaji_key(b)) >= threshold or similarity(a, b) >= threshold:
                    add(a, b, "similar spelling")
    return out


def _tokens_align(a: str, b: str) -> bool:
    """Same number of name parts, each part similar and starting with the same letter.

    Stops 'Hajime Tanaka' / 'Hajime Kanzaka' (different people) from being suggested.
    """
    ta, tb = fold(a).replace(",", " ").split(), fold(b).replace(",", " ").split()
    if len(ta) != len(tb):
        return False
    return all(x[:1] == y[:1] and similarity(x, y) >= 0.7 for x, y in zip(ta, tb))


def run(ctx) -> None:
    lib = ctx.lib
    acfg = ctx.cfg["authors"]
    junk_names = {fold(j) for j in acfg["junk_names"]}

    # 1) split and tidy every author string, counting books per resulting name
    split_cache = {}
    counts = Counter()
    for b in lib.books.values():
        for a in b.authors:
            if a in (b.hints.get("junk_author") or {}):
                continue
            if a not in split_cache:
                split_cache[a] = split_author(a)
            for n in split_cache[a][0]:
                counts[n] += 1

    norm = AuthorNormalizer(counts, ctx.aliases["authors"], acfg["flip_only_when_counterpart_exists"])
    # calibre treats author names case-insensitively and re-cases an existing author for ALL their
    # books at once, renaming folders case-only. That is unsafe on some shares (it deleted files on
    # an Unraid NFS share), so a new name that differs from an existing author only by capitalisation
    # always takes the existing spelling.
    existing_case = {}
    for b in lib.books.values():
        for a in b.authors:
            existing_case.setdefault(fold(a), a)
    changed = 0
    for b in sorted(lib.books.values(), key=lambda x: x.id):
        new, reasons = [], []
        junk = b.hints.get("junk_author") or {}
        for a in b.authors:
            if a in junk or fold(a) in junk_names:
                new.append(a)  # handled by the junk-author pass / left for review
                continue
            names, dropped = split_cache.get(a) or split_author(a)
            if not names:
                new.append(a)
                continue
            if dropped:
                reasons.append(f"dropped non-author credit(s) {', '.join(dropped)} from '{a}'")
            if len(names) > 1:
                reasons.append(f"split '{a}'")
            for n in names:
                c, why = norm.canonical(n)
                if fold(c) in existing_case and existing_case[fold(c)] != c:
                    kept = existing_case[fold(c)]
                    if c != n or kept != a:
                        why = f"kept existing capitalisation '{kept}'"
                    c = kept
                if why and c != a:
                    reasons.append(f"'{n}' -> '{c}' ({why})")
                elif n != a and len(names) == 1:
                    reasons.append(f"tidied '{a}' -> '{n}'")
                new.append(c)
        dedup, seen = [], set()
        for n in new:
            k = key(n, False)
            if k not in seen:
                seen.add(k)
                dedup.append(n)
        if not dedup or dedup == b.authors:
            continue
        if not ctx.within_limit(PASS):
            break
        if lib.set(b, "authors", dedup, PASS, "; ".join(reasons) or "normalized"):
            changed += 1
            ctx.stat(PASS, "books_changed")

    # 2) suggestions for a human
    final = Counter()
    for b in lib.books.values():
        for a in b.authors:
            final[a] += 1
    ctx.author_suggestions = []
    for a, bname, why in suggest_variants(final, acfg["suggest_similarity"]):
        ctx.author_suggestions.append((a, bname, why, final[a], final[bname]))
    for n, fl in sorted(norm.flip_suggestions.items()):
        last, _, first = n.partition(",")
        # "Kei Uekawa, TEDDY" is author + illustrator, not "Last, First".
        if n in final and len(last.split()) == 1 and 1 <= len(first.split()) <= 2:
            ctx.author_suggestions.append((n, fl, "'Last, First' order", final[n], 0))
    ctx.log.info(f"Authors: {changed} book(s) normalized; {len(ctx.author_suggestions)} spelling suggestion(s) "
                 f"written for review.")
