"""Helpers shared by several passes."""
from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from pathlib import Path

from ..library import Book
from ..textutil import clean_text, fold, words_key

COMIC_FORMATS = {"CBZ", "CBR", "CB7", "CBT", "CBC"}
TEXT_FORMATS = {"EPUB", "KEPUB", "AZW3", "AZW", "MOBI", "FB2", "TXT", "DOCX", "RTF", "KFX"}
_IMG = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif")
_HTML = (".xhtml", ".html", ".htm")


# ------------------------------------------------------------ publishers ---

def _contains_phrase(haystack_words: str, phrase: str) -> bool:
    p = words_key(phrase)
    if not p:
        return False
    return re.search(r"(?:^|\s)" + re.escape(p) + r"(?:\s|$)", haystack_words) is not None


def publisher_class(publisher: str | None, pub_cfg: dict) -> str | None:
    """'ln', 'manga', 'mixed', 'general' or None. Specific lists win over 'mixed'."""
    if not publisher:
        return None
    w = words_key(publisher)
    hits = {name for name, key in (("ln", "light_novel"), ("manga", "manga"), ("general", "general"))
            if any(_contains_phrase(w, p) for p in pub_cfg.get(key, []))}
    if "ln" in hits and "manga" in hits:
        return "mixed"
    if "ln" in hits:
        return "ln"
    if "manga" in hits:
        return "manga"
    if any(_contains_phrase(w, p) for p in pub_cfg.get("mixed", [])):
        return "mixed"
    if "general" in hits:
        return "general"
    return None


# ---------------------------------------------------------------- files ---

def format_path(library: Path, book: Book, fmt) -> Path:
    return library / book.path / f"{fmt.name}.{fmt.fmt.lower()}"


class ProbeCache:
    """Caches EPUB content probes keyed by path+size+mtime so reruns don't re-read NFS."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.execute("CREATE TABLE IF NOT EXISTS probe (k TEXT PRIMARY KEY, v TEXT)")

    def get(self, k):
        r = self.con.execute("SELECT v FROM probe WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else None

    def put(self, k, v):
        self.con.execute("INSERT OR REPLACE INTO probe VALUES (?,?)", (k, json.dumps(v)))

    def commit(self):
        self.con.commit()

    def close(self):
        self.con.commit()
        self.con.close()


def probe_zip_book(path: Path) -> dict:
    """Read only the zip directory and summarise it: image count and text bytes.

    A manga EPUB is mostly images wrapped in tiny XHTML pages; a novel has a
    lot of text and a handful of illustrations.
    """
    info = {"images": 0, "html_files": 0, "text_bytes": 0, "ok": True, "error": None}
    try:
        with zipfile.ZipFile(path) as z:
            for zi in z.infolist():
                n = zi.filename.lower()
                if n.endswith(_IMG):
                    info["images"] += 1
                elif n.endswith(_HTML):
                    info["html_files"] += 1
                    info["text_bytes"] += zi.file_size
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        info["ok"], info["error"] = False, f"{type(e).__name__}: {e}"
    return info


def medium_from_probe(p: dict) -> str | None:
    if not p or not p.get("ok"):
        return None
    imgs, text = p["images"], p["text_bytes"]
    if imgs >= 40 and text < imgs * 4000:
        return "comic"
    if text >= 120_000:
        return "novel"
    if imgs <= 30 and text >= 40_000:
        return "novel"
    return None


def book_medium(book: Book, library: Path, cache: ProbeCache | None) -> tuple[str | None, str]:
    """('comic'|'novel'|None, reason) from the book's files."""
    fmts = book.fmt_names()
    if fmts and fmts <= COMIC_FORMATS:
        return "comic", "comic archive format (" + ", ".join(sorted(fmts)) + ")"
    for f in book.formats:
        if f.fmt.upper() in ("EPUB", "KEPUB"):
            p = format_path(library, book, f)
            try:
                st = p.stat()
            except OSError:
                return None, "book file missing"
            k = f"{p}|{st.st_size}|{int(st.st_mtime)}"
            probe = cache.get(k) if cache else None
            if probe is None:
                probe = probe_zip_book(p)
                if cache:
                    cache.put(k, probe)
            m = medium_from_probe(probe)
            if m:
                return m, f"EPUB content: {probe['images']} images, {probe['text_bytes'] // 1024} KB of text"
            return None, "EPUB content inconclusive"
    if fmts & TEXT_FORMATS:
        return "novel", "text ebook format (" + ", ".join(sorted(fmts & TEXT_FORMATS)) + ")"
    return None, "no deciding format (" + (", ".join(sorted(fmts)) or "no files") + ")"


# -------------------------------------------------------------- authors ---

_JUNK_AUTHOR = re.compile(
    r"^(?P<lead>.*?)\s*(?P<brackets>(?:\[[^\]]*\]\s*){2,})$"
)
_VOL_IN_JUNK = re.compile(r"^vol(?:ume)?\.?\s*(?P<a>\d{1,3})(?:\s*-\s*(?P<b>\d{1,3}))?(?:\s+chapter\s+(?P<ch>\d+))?\s*$", re.I)
_HEX8 = re.compile(r"^[0-9A-Fa-f]{8}$")


def junk_author_info(name: str, junk_names) -> dict | None:
    """Recognise filename-junk authors like 'Vol 02 [June][Scans][4FF7E520]'.

    Returns hints {'volume', 'volume_end', 'publisher', 'source', 'complete'} or
    None if the name looks like a real person.
    """
    n = clean_text(name)
    if fold(n) in {fold(j) for j in junk_names}:
        return {"reason": "known non-author name"}
    if re.match(r"^(?:\u00a9|\(c\)|copyright\b)", n, re.I):
        return {"reason": "copyright line instead of an author"}
    m = _JUNK_AUTHOR.match(n)
    if not m:
        return None
    groups = re.findall(r"\[([^\]]*)\]", m.group("brackets"))
    if not groups or not _HEX8.match(groups[-1].strip()):
        return None
    hints = {"reason": "release-filename author", "publisher": clean_text(groups[0]) or None,
             "source": ", ".join(clean_text(g) for g in groups[1:-1]) or None}
    lead = m.group("lead").strip()
    vm = _VOL_IN_JUNK.match(lead)
    if vm:
        hints["volume"] = float(vm.group("a"))
        if vm.group("b"):
            hints["volume_end"] = float(vm.group("b"))
    elif fold(lead) == "complete":
        hints["complete"] = True
    elif lead:
        hints["subtitle"] = lead
    return hints


def is_junk_book(book: Book) -> bool:
    return bool(book.hints.get("junk_author"))
