"""Title parsing: series name, volume/part numbers, subtitle, edition markers.

Design rules (each one fixes a bug from the old bash tool):

* Keywords only count at a word boundary, so "Revolution 2" or "Evolution"
  never look like "Vol".
* Numbers are real numbers: "Vol. 03", "Vol.3", "Volume 3" all give 3, and
  nothing is ever rewritten by a regex substitution ("Volume. 3" bug).
* A bare trailing number ("Sword Art Online Progressive 6") is only a
  *candidate* (kind="bare"). The series pass accepts it only when sibling
  books prove the pattern, so "Fahrenheit 451" is never volume 451.
* Ranges ("Vol. 1-16") are omnibus markers, not a volume.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .textutil import clean_text, fold

# ---------------------------------------------------------------- cleanup ---

_EXT = re.compile(r"\.(?:epub|azw3?|mobi|pdf|cbz|cbr|cb7|kfx|txt|docx?)$", re.I)
_BRACKET = re.compile(r"\s*\[([^\[\]]*)\]\s*")
_PAREN = re.compile(r"\s*\(([^()]*)\)\s*")
_HEX8 = re.compile(r"^[0-9A-Fa-f]{8}$")
_RELEASE_RUN = re.compile(r"(?:\s*\[[^\[\]]*\]){2,}\s*$")
DEFAULT_BRACKET_JUNK_WORDS = (
    "scan", "ocr", "kindle", "kobo", "webnovel", "retail", "digital", "compressed",
    "epub", "calibre", "converted", "steam", "lnwncentral", "dale", "zaphkiel", "premium",
)

_MARKER = re.compile(
    r"\s*[\(\[]\s*(?:the\s+)?((?:(?:yaoi|bl|boys'?\s*love|web|yuri)\s+)?light[\s-]*novels?|(?:(?:yaoi|bl|boys'?\s*love|web|yuri)\s+)?novel|ln|manga|comics?|graphic\s+novel)\s*[\)\]]",
    re.I,
)


# "Toradora! Light Novel: Volume 3" - unbracketed marker right before a separator or the end.
_BARE_LN = re.compile(r"\s+light\s+novel(?=\s*(?:[:,]|-\s|vol(?:ume)?\.?\s*\d))", re.I)


def _is_junk_bracket(content: str, junk_words, publishers_folded) -> bool:
    c = content.strip()
    if not c:
        return True
    if _HEX8.match(c):
        return True
    f = fold(c)
    if f in publishers_folded:
        return True
    return any(w in f for w in junk_words)


def strip_junk(title: str, publishers=(), junk_words=DEFAULT_BRACKET_JUNK_WORDS) -> str:
    """Remove file extensions and bracketed release junk like [Yen Press][Scans][0EDF6669].

    Brackets that look like real title text (e.g. "[Oshi no Ko]") are kept:
    only hex hashes, known publishers and release-group words are removed.
    Parenthesised publishers "(Yen Press)" are removed too.
    """
    t = clean_text(title)
    t = _EXT.sub("", t).strip()
    pubs = {fold(p) for p in publishers if p}
    run = _RELEASE_RUN.search(t)
    if run and _HEX8.match(re.findall(r"\[([^\]]*)\]", run.group(0))[-1].strip()):
        t = t[: run.start()].strip()  # "[Yen Press][Kobo][0EDF6669]" release tag run

    def br(m):
        return " " if _is_junk_bracket(m.group(1), junk_words, pubs) else m.group(0)

    t = _BRACKET.sub(br, t)

    def pr(m):
        return " " if fold(m.group(1)) in pubs else m.group(0)

    t = _PAREN.sub(pr, t)
    return clean_text(t)


def extract_marker(title: str) -> tuple[str, str | None]:
    """Return (title_without_marker, 'novel'|'manga'|None)."""
    marker = None
    m = _BARE_LN.search(title)
    if m:
        marker = "novel"
        title = clean_text(title[: m.start()] + title[m.end():])
    for _ in range(3):
        m = _MARKER.search(title)
        if not m:
            break
        word = m.group(1).lower()
        marker = marker or ("manga" if ("manga" in word or "comic" in word or "graphic" in word) else "novel")
        title = clean_text(title[: m.start()] + " " + title[m.end():])
    return title, marker


# ---------------------------------------------------------------- parsing ---

_SEP_TRAIL = re.compile(r"(?:\s|[,:;(\[]|(?<=\s)[-\u2013\u2014~]+)+$")
_SEP_LEAD = re.compile(r"^(?:\s|[,:;)\]]|[-\u2013\u2014~]+(?=\s|$))+")
_VOL_IN_BRACKETS = re.compile(r"[\[(]\s*((?:vol(?:ume)?\.?|v\.?)\s*\d{1,3})\s*[\])]", re.I)
_KW = re.compile(
    r"(?<![\w])(?P<kw>vol(?:ume)?s?\.?|tome|book|part|#)\s*(?P<num>\d{1,4}(?:\.\d{1,2})?)(?![\d])",
    re.I,
)
_RANGE_AFTER = re.compile(r"^\s*[-~]\s*\d")
_PART_LEAD = re.compile(r"^\s*[,:\-]?\s*(?P<label>part|act)\s*(?P<part>\d{1,2})(?!\d)", re.I)
_PART_TRAIL = re.compile(r"[\s,:\-]*\b(?P<label>part|act)\s*(?P<part>\d{1,2})\s*$", re.I)
_LATER_VOL = re.compile(r"(?<![\w])vol(?:ume)?\.?\s*\d", re.I)
_BARE = re.compile(
    r"^(?P<series>.*\S)\s+(?P<num>\d{1,3}(?:\.\d)?)(?:\s*(?::|\s-\s)\s*(?P<sub>.+))?$"
)
# Words that make a trailing number NOT a volume ("Level 99", "Disc 2", "Year 2").
_BARE_BLOCK_WORDS = {
    "no", "no.", "chapter", "ch", "ch.", "episode", "ep", "season", "act", "disc", "year",
    "level", "lv", "lv.", "class", "day", "days", "c", "number", "stage", "phase", "grade",
    "route", "floor", "round", "file", "case", "report", "side", "the",
}


@dataclass
class ParsedTitle:
    original: str
    cleaned: str
    series: str | None = None
    volume: float | None = None
    part: int | None = None
    subtitle: str | None = None
    kind: str | None = None  # "explicit", "bare", "range" or None
    keyword: str | None = None  # "vol", "book", "part", "#"
    part_label: str = "Part"
    marker: str | None = None  # "novel" / "manga"
    notes: list = field(default_factory=list)

    def index(self, part_style: str = "decimal") -> float | None:
        if self.volume is None:
            return None
        if self.part is None:
            return float(self.volume)
        if part_style != "decimal" or self.part >= 10 or float(self.volume) != int(self.volume):
            return None
        return float(self.volume) + self.part / 10.0


def _kwname(raw: str) -> str:
    r = raw.lower().rstrip(".")
    if r.startswith("vol") or r == "tome":
        return "vol"
    return r  # book / part / #


def _num(s: str) -> float:
    return float(s)


def parse_title(title: str, publishers=(), junk_words=DEFAULT_BRACKET_JUNK_WORDS) -> ParsedTitle:
    original = clean_text(title)
    cleaned = strip_junk(original, publishers, junk_words)
    cleaned, marker = extract_marker(cleaned)
    p = ParsedTitle(original=original, cleaned=cleaned, marker=marker)
    work = _VOL_IN_BRACKETS.sub(lambda m: " " + m.group(1) + " ", cleaned)
    work = clean_text(work)

    for m in _KW.finditer(work):
        rest = work[m.end():]
        series = _SEP_TRAIL.sub("", work[: m.start()]).strip()
        kw = _kwname(m.group("kw"))
        if kw in ("book", "#") and not series:
            continue
        if _RANGE_AFTER.match(rest):
            p.kind, p.series, p.keyword = "range", series or None, kw
            p.notes.append("volume range (omnibus)")
            return p
        vol = _num(m.group("num"))
        if vol > 500:
            continue
        if kw == "part" and _LATER_VOL.search(rest):
            # "Series: Part 2 Arc Name Volume 3": two numbering levels, no safe single index.
            p.kind, p.series, p.keyword = "complex", series or None, kw
            p.notes.append("part and volume numbering")
            return p
        part = None
        if kw != "part":
            pm = _PART_LEAD.match(rest) or _PART_TRAIL.search(rest)
            if pm:
                part = int(pm.group("part"))
                p.part_label = pm.group("label").capitalize()
                rest = rest[: pm.start()] + rest[pm.end():]
        subtitle = _SEP_TRAIL.sub("", _SEP_LEAD.sub("", rest)).strip() or None
        if subtitle and series and (fold(series).endswith(fold(subtitle)) or _wk(subtitle) == _wk(series)):
            subtitle = None  # "X: Foo - Volume 6: Foo" repeats the series; "(X)" too
        p.series = series or None
        p.volume, p.part, p.subtitle = vol, part, subtitle
        p.kind, p.keyword = "explicit", kw
        if not (_balanced(series) and _balanced(subtitle or "")):
            p.notes.append("unbalanced brackets; not restructured")
            p.kind = "explicit-unsafe"
        return p

    bm = _BARE.match(work)
    if bm:
        series = _SEP_TRAIL.sub("", bm.group("series")).strip()
        last_word = series.split()[-1].lower() if series.split() else ""
        if series and not series[-1].isdigit() and last_word not in _BARE_BLOCK_WORDS:
            p.series = series
            p.volume = _num(bm.group("num"))
            sub = bm.group("sub")
            p.subtitle = clean_text(sub) if sub else None
            p.kind = "bare"
    return p


def _wk(s: str) -> str:
    return "".join(ch for ch in fold(s) if ch.isalnum())


def _balanced(s: str) -> bool:
    for a, b in ("()", "[]", "{}"):
        if s.count(a) != s.count(b):
            return False
    return True


def fmt_num(n: float | int | None) -> str:
    if n is None:
        return ""
    f = float(n)
    return str(int(f)) if f.is_integer() else ("%g" % f)


def standard_title(p: ParsedTitle, vol_label: str = "Vol.") -> str | None:
    """Canonical "Series, Vol. N[, Part M][: Subtitle]" form, or None if not parseable."""
    if p.kind not in ("explicit", "bare") or not p.series or p.volume is None:
        return None
    if p.keyword == "part":
        core = f"{p.series}, Part {fmt_num(p.volume)}"
    else:
        label = "Book" if p.keyword == "book" else vol_label
        core = f"{p.series}, {label} {fmt_num(p.volume)}"
        if p.part is not None:
            core += f", {p.part_label} {p.part}"
    if p.subtitle:
        core += f": {p.subtitle}"
    return core


def title_variants(p: ParsedTitle, limit: int = 5) -> list[str]:
    """Search phrasings for metadata providers, most specific first, de-duplicated."""
    out: list[str] = []

    def add(s):
        s = clean_text(s)
        if s and fold(s) not in {fold(x) for x in out}:
            out.append(s)

    add(p.cleaned)
    if p.series and p.volume is not None and p.kind in ("explicit", "bare"):
        n = fmt_num(p.volume)
        if p.keyword == "part":
            add(f"{p.series}, Part {n}")
            add(f"{p.series} Part {n}")
        else:
            if p.subtitle:
                add(f"{p.series}, Vol. {n}: {p.subtitle}")
            add(f"{p.series}, Vol. {n}")
            add(f"{p.series} Vol. {n}")
            add(f"{p.series}, Volume {n}")
            add(f"{p.series} {n}")
    if p.original and p.original != p.cleaned:
        add(p.original)
    return out[:limit]
