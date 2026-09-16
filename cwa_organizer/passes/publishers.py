"""Publisher normalization: aliases plus automatic merging of trivially different spellings."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..textutil import clean_text, fold, key, similarity

PASS = "publishers"
_SUFFIX = re.compile(
    r"(?:[\s,]+(?:llc|l\.l\.c\.|inc\.?|incorporated|ltd\.?|limited|co\.|corp\.?|corporation|gmbh|pty|plc))+\.?\s*$",
    re.I,
)


def pub_key(name: str) -> str:
    n = clean_text(name).replace("_", " ")
    prev = None
    while prev != n:
        prev, n = n, _SUFFIX.sub("", n).strip(" ,")
    return key(n, drop_leading_the=True)


class PublisherNormalizer:
    def __init__(self, cfg_aliases: dict, user_aliases: dict, library_names=()):
        self.alias = {}
        for src in (cfg_aliases, user_aliases):
            for k, v in src.items():
                self.alias[fold(k)] = clean_text(v)
        counts = Counter(clean_text(n) for n in library_names if n)
        groups = defaultdict(Counter)
        for n, c in counts.items():
            groups[pub_key(n)][n] += c
        self.by_key = {}
        for k, c in groups.items():
            # Prefer an alias target if one is in the group, else the most used spelling,
            # else the spelling without a corporate suffix.
            names = list(c)
            targets = [self.alias[fold(n)] for n in names if fold(n) in self.alias]
            if targets:
                self.by_key[k] = targets[0]
            else:
                self.by_key[k] = sorted(names, key=lambda n: (-c[n], bool(_SUFFIX.search(n)), len(n), n))[0]

    def canonical(self, name: str | None) -> str | None:
        n = clean_text(name)
        if not n:
            return None
        f = fold(n)
        if f in self.alias:
            return self.alias[f]
        k = pub_key(n)
        if k in self.by_key:
            target = self.by_key[k]
            return self.alias.get(fold(target), target)
        return n


def run(ctx) -> None:
    lib = ctx.lib
    names = [b.publisher for b in lib.books.values() if b.publisher]
    norm = PublisherNormalizer(ctx.cfg["publishers"].get("aliases", {}), ctx.aliases["publishers"], names)
    ctx._pub_norm = norm
    changed = 0
    mapping = Counter()
    for b in sorted(lib.books.values(), key=lambda x: x.id):
        if not b.publisher:
            continue
        new = norm.canonical(b.publisher)
        if new == b.publisher:
            continue
        if not ctx.within_limit(PASS):
            break
        old = b.publisher
        if lib.set(b, "publisher", new, PASS, f"'{old}' -> '{new}'"):
            mapping[(old, new)] += 1
            changed += 1
            ctx.stat(PASS, "books_changed")
    for (old, new), n in sorted(mapping.items()):
        ctx.log.detail(f"  publisher '{old}' -> '{new}' ({n})")

    # Suggest near-duplicates that were not merged automatically.
    distinct = sorted({b.publisher for b in lib.books.values() if b.publisher})
    seen = set()
    for i, a in enumerate(distinct):
        for bname in distinct[i + 1:]:
            if pub_key(a) == pub_key(bname) or norm.canonical(a) == norm.canonical(bname):
                continue  # already merged automatically
            ka, kb = pub_key(a), pub_key(bname)
            if (ka.startswith(kb) or kb.startswith(ka)) and min(len(ka), len(kb)) >= 5 or similarity(a, bname) >= 0.9:
                pair = tuple(sorted((a, bname)))
                if pair not in seen:
                    seen.add(pair)
                    lib.add_review("publisher-suggestion", [], f"'{pair[0]}' and '{pair[1]}' may be the same publisher. "
                                   f"If so, add a line under [publishers] in aliases.toml.")
    ctx.log.info(f"Publishers: {changed} book(s) normalized.")
