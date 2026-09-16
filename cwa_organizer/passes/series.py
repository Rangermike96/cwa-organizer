"""Series from titles: assign series + index, merge spelling variants, detect collisions and gaps.

Rules
  * Explicit volume titles ("X, Vol. 3", "X Part 2", "X Book 1") are trusted.
  * A bare trailing number ("X 3") counts only when sibling books prove the
    pattern (at least `bare_number_min_siblings` distinct numbers for the
    same name, or an explicit/existing series with that name).
  * A book that already has a series is never moved to a different series;
    disagreements are reported. Spelling-only variants of the same series
    name are merged.
  * If two books would land on the same series + index, NEITHER new
    assignment is written. Identical titles are reported as DUPLICATE,
    different titles as AMBIGUOUS. Nothing is auto-resolved.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..textutil import fold, romaji_key, similarity, words_key
from ..titles import fmt_num

PASS = "series"


def skey(name: str) -> str:
    """Series identity key: letters/digits only, leading 'The'/'A'/'An' ignored."""
    w = words_key(name)
    for art in ("the ", "a ", "an "):
        if w.startswith(art) and len(w) > len(art) + 3:
            w = w[len(art):]
            break
    return w.replace(" ", "")


def _slot(idx: float) -> str:
    return f"{idx:.4f}"


def run(ctx) -> None:
    lib = ctx.lib
    scfg = ctx.cfg["series"]
    part_style = ctx.cfg["titles"]["part_index_style"]
    min_len = int(scfg["min_series_name_len"])
    blocked = {skey(x) for x in scfg["blocklist"]}
    user_alias = {fold(k): v for k, v in ctx.aliases["series"].items()}

    # ---- candidates from titles -------------------------------------------
    cands = {}  # book_id -> dict(name, key, index, kind)
    for b in lib.books.values():
        p = b.hints.get("parsed")
        if not p:
            continue
        hint = b.hints.get("volume_hint")
        name, kind, idx = p.series, p.kind, None
        if kind == "range":
            lib.add_review("omnibus", [b.id], f"'{b.title}' covers a range of volumes; no series index assigned.")
            continue
        if kind in ("complex", "explicit-unsafe"):
            lib.add_review("unusual-numbering", [b.id], f"'{b.title}': {'; '.join(p.notes) or kind}. "
                           "No series index assigned automatically.")
            continue
        if kind in ("explicit", "bare"):
            idx = p.index(part_style)
            if p.part is not None and idx is None:
                lib.add_review("part-volume", [b.id], f"'{b.title}': Vol {fmt_num(p.volume)} Part {p.part} "
                               f"has no series index under part_index_style='{part_style}'.")
                continue
            if kind == "bare" and hint is not None and abs(hint - (p.volume or -1)) < 1e-9:
                kind = "explicit"
        elif hint is not None and p.cleaned:
            # Volume number only known from a release-filename author ("Vol 07 [Tokyopop]...").
            name, kind, idx = p.cleaned, "hint", float(hint)
        if not name or idx is None:
            continue
        k = skey(name)
        if len(k) < min_len or k in blocked:
            continue
        cands[b.id] = {"name": name, "key": k, "index": idx, "kind": kind}

    # ---- accept bare numbers only with sibling evidence --------------------
    existing_keys = Counter(skey(b.series) for b in lib.books.values() if b.series)
    numbers_by_key = defaultdict(set)
    explicit_keys = set()
    for c in cands.values():
        numbers_by_key[c["key"]].add(c["index"])
        if c["kind"] != "bare":
            explicit_keys.add(c["key"])
    need = int(scfg["bare_number_min_siblings"])
    for bid in list(cands):
        c = cands[bid]
        if c["kind"] == "bare":
            if not (c["key"] in explicit_keys or c["key"] in existing_keys or len(numbers_by_key[c["key"]]) >= need):
                del cands[bid]

    # ---- canonical display name per key -----------------------------------
    # Spellings seen in titles are the publisher's own, so they outvote older series fields;
    # an existing series name counts once per book so it still wins when titles don't say.
    names_by_key = defaultdict(Counter)
    for b in lib.books.values():
        if b.series:
            names_by_key[skey(b.series)][b.series] += 1
    for c in cands.values():
        names_by_key[c["key"]][c["name"]] += 2

    def canonical(k: str) -> str:
        names = names_by_key[k]
        for n in names:
            if fold(n) in user_alias:
                return user_alias[fold(n)]
        best = sorted(names.items(), key=lambda kv: (-kv[1], -sum(ch.isupper() for ch in kv[0]), kv[0]))[0][0]
        return user_alias.get(fold(best), best)

    canon = {k: canonical(k) for k in names_by_key}

    # ---- plan -------------------------------------------------------------
    planned = {}  # book_id -> (name, index, reason)
    conflict_pairs = Counter()
    for b in lib.books.values():
        c = cands.get(b.id)
        if b.series:
            ek = skey(b.series)
            target = canon.get(ek, b.series)
            if c and c["key"] != ek and c["kind"] != "bare" and skey(target) != c["key"]:
                lib.add_review("series-conflict", [b.id],
                               f"'{b.title}' is in series '{b.series}' but its title suggests '{c['name']}'. Not changed.")
                conflict_pairs[(b.series, c["name"])] += 1
            new_index = b.series_index
            if c and c["key"] == ek and abs(c["index"] - b.series_index) > 1e-9:
                if scfg["fix_existing_index"]:
                    new_index = c["index"]
                else:
                    lib.add_review("index-mismatch", [b.id],
                                   f"'{b.title}' has index {fmt_num(b.series_index)} in '{b.series}' but the title says "
                                   f"{fmt_num(c['index'])}. Not changed (series.fix_existing_index = false).")
            if target != b.series or abs(new_index - b.series_index) > 1e-9:
                why = []
                if target != b.series:
                    why.append(f"series name '{b.series}' merged into '{target}'")
                if abs(new_index - b.series_index) > 1e-9:
                    why.append(f"index {fmt_num(b.series_index)} -> {fmt_num(new_index)} from title")
                planned[b.id] = (target, new_index, "; ".join(why))
            b.hints["series_accepted"] = True if (c and c["key"] == ek) else b.hints.get("series_accepted", False)
        elif c:
            planned[b.id] = (canon[c["key"]], c["index"], f"from title ({c['kind']} volume number)")

    # ---- collisions -------------------------------------------------------
    slots = defaultdict(list)
    for b in lib.books.values():
        name, idx = (planned[b.id][0], planned[b.id][1]) if b.id in planned else (b.series, b.series_index)
        if name:
            slots[(skey(name), _slot(idx))].append(b)
    withheld = set()
    for (k, s), group in slots.items():
        if len(group) < 2:
            continue
        new_ids = [g.id for g in group if not g.series]
        label = "DUPLICATE" if _looks_duplicate(group, k) else "AMBIGUOUS"
        detail = "; ".join(f"[{g.id}] '{g.title}'{' (already assigned)' if g.series else ''}" for g in group)
        name = canon.get(k, group[0].series or "")
        if label == "DUPLICATE":
            msg = f"{label}: same title in '{name}' #{fmt_num(float(s))}: {detail}. Probably a duplicate import; nothing assigned."
        else:
            msg = f"{label}: different books map to '{name}' #{fmt_num(float(s))}: {detail}. Needs a human decision; nothing assigned."
        lib.add_review("series-" + label.lower(), [g.id for g in group], msg)
        withheld.update(new_ids)
        for g in group:
            g.hints["series_withheld"] = True

    changed = 0
    for bid, (name, idx, why) in sorted(planned.items()):
        if bid in withheld:
            continue
        b = lib.books[bid]
        if not ctx.within_limit(PASS):
            break
        ch = lib.set(b, "series", name, PASS, why)
        ch = lib.set(b, "series_index", float(idx), PASS, why) or ch
        b.hints["series_accepted"] = True
        if ch:
            changed += 1
            ctx.stat(PASS, "books_changed")
    for bid in cands:
        if bid not in withheld and bid not in planned and lib.books[bid].series:
            lib.books[bid].hints["series_accepted"] = True

    _report_gaps(lib)
    _suggest_merges(ctx, lib)
    for (old_name, title_name), n in conflict_pairs.most_common():
        ctx.series_suggestions.append((title_name, old_name, f"series field differs from {n} title(s); approve to rename the series"))
    ctx.log.info(f"Series: {changed} book(s) assigned or corrected; {len(withheld)} withheld because of collisions.")


def _looks_duplicate(group, slot_key: str) -> bool:
    """Same title, or same series/volume where at most one distinct subtitle appears."""
    if len({words_key(b.title) for b in group}) == 1:
        return True
    subs = set()
    for b in group:
        p = b.hints.get("parsed")
        if not p or not p.series or skey(p.series) != slot_key:
            return False
        if p.subtitle:
            subs.add(words_key(p.subtitle))
    return len(subs) <= 1


_NUMBERING = re.compile(r"\b(?:\d+|[ivxlc]+|nt|ss|ex|zero|gaiden|side stor(?:y|ies)|short stor(?:y|ies))\b", re.I)


def _numbering(name: str) -> tuple:
    """Numbers, roman numerals and sub-series markers: 'Unital Ring II' vs 'Unital Ring I' differ here."""
    return tuple(sorted(m.group(0).lower() for m in _NUMBERING.finditer(name)))


def _report_gaps(lib) -> None:
    by_series = defaultdict(list)
    for b in lib.books.values():
        if b.series:
            by_series[b.series].append(b.series_index)
    for name, idxs in sorted(by_series.items()):
        ints = {int(i) for i in idxs if i >= 1}
        if len(ints) < 2:
            continue
        missing = sorted(set(range(1, max(ints) + 1)) - ints)
        if missing:
            shown = ", ".join(str(m) for m in missing[:30]) + (" ..." if len(missing) > 30 else "")
            lib.add_review("series-gap", [], f"'{name}': have {len(ints)} volume(s) up to {max(ints)}, missing {shown}",
                           series=name, missing=missing)


def _suggest_merges(ctx, lib) -> None:
    names = sorted({b.series for b in lib.books.values() if b.series})
    ctx.series_suggestions = []
    by_r = defaultdict(list)
    for n in names:
        by_r[(romaji_key(n), _numbering(n))].append(n)
    seen = set()
    for group in by_r.values():
        for i, a in enumerate(group):
            for bname in group[i + 1:]:
                seen.add((a, bname))
                ctx.series_suggestions.append((a, bname, "romanization variant"))
    buckets = defaultdict(list)
    for n in names:
        buckets[skey(n)[:4]].append(n)
    for group in buckets.values():
        for i, a in enumerate(group):
            for bname in group[i + 1:]:
                if (a, bname) not in seen and skey(a) != skey(bname) and similarity(a, bname) >= 0.93 \
                        and _numbering(a) == _numbering(bname):
                    seen.add((a, bname))
                    ctx.series_suggestions.append((a, bname, "similar name"))

