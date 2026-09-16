"""Text helpers: entity decoding, normalization keys, similarity.

Everything that compares two strings for "sameness" goes through the key
functions here, so matching rules live in one place.
"""
from __future__ import annotations

import difflib
import html
import re
import unicodedata

_WS = re.compile(r"\s+")
_QUOTES = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "`": "'", "´": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "―": "-", "‒": "-",
    " ": " ", "　": " ",
})


def decode_entities(text: str | None) -> str:
    """Decode HTML/XML entities, including double-encoded ones (&amp;amp;)."""
    if not text:
        return ""
    prev = None
    cur = text
    for _ in range(4):  # bounded: &amp;amp;amp; is the worst seen in the wild
        if cur == prev:
            break
        prev, cur = cur, html.unescape(cur)
    return cur


def clean_text(text: str | None) -> str:
    """Decode entities, NFC-normalize, collapse whitespace, strip."""
    t = decode_entities(text)
    t = unicodedata.normalize("NFC", t)
    return _WS.sub(" ", t).strip()


def fold(text: str | None) -> str:
    """Case/quote/dash/width-insensitive form that still keeps word breaks."""
    t = unicodedata.normalize("NFKC", clean_text(text)).translate(_QUOTES)
    return _WS.sub(" ", t.casefold()).strip()


def words_key(text: str | None) -> str:
    """fold() with punctuation turned into spaces: 'Re:ZERO -Starting Life-' -> 're zero starting life'."""
    t = fold(text).replace("&", " and ")
    t = "".join(ch if ch.isalnum() else " " for ch in t)
    return _WS.sub(" ", t).strip()


def key(text: str | None, drop_leading_the: bool = True) -> str:
    """Compact identity key: letters/digits only. 'VIZMedia' == 'VIZ Media, ' etc."""
    w = words_key(text)
    if drop_leading_the and w.startswith("the "):
        w = w[4:]
    return w.replace(" ", "")


_ROMAJI = [
    (re.compile(r"ou"), "o"), (re.compile(r"oo"), "o"), (re.compile(r"uu"), "u"),
    (re.compile(r"aa"), "a"), (re.compile(r"ii"), "i"), (re.compile(r"ee"), "e"),
    (re.compile(r"jy"), "j"), (re.compile(r"zi"), "ji"), (re.compile(r"si"), "shi"),
    (re.compile(r"ti"), "chi"), (re.compile(r"tu"), "tsu"), (re.compile(r"hu"), "fu"),
    (re.compile(r"sy"), "sh"), (re.compile(r"ty"), "ch"), (re.compile(r"shh"), "sh"),
]


def romaji_key(text: str | None) -> str:
    """Looser key for Japanese romanization variants (Juumonji / Jyumonji)."""
    k = key(text, drop_leading_the=False)
    for pat, rep in _ROMAJI:
        k = pat.sub(rep, k)
    return k


def similarity(a: str | None, b: str | None) -> float:
    """0..1 similarity on words_key forms."""
    wa, wb = words_key(a), words_key(b)
    if not wa or not wb:
        return 0.0
    if wa == wb:
        return 1.0
    return difflib.SequenceMatcher(None, wa, wb).ratio()


def first_non_empty(*vals):
    for v in vals:
        if v:
            return v
    return None


def ci_unique(items, prefer=None):
    """Case-insensitive de-duplication that keeps first-seen order.

    `prefer` maps fold(item) -> preferred display form.
    """
    out, seen = [], set()
    prefer = prefer or {}
    for it in items:
        it = clean_text(it)
        if not it:
            continue
        f = fold(it)
        if f in seen:
            continue
        seen.add(f)
        out.append(prefer.get(f, it))
    return out
