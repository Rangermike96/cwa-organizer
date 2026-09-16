"""calibre's fetch-ebook-metadata, with a real XML parser for the returned OPF."""
from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from ..textutil import clean_text

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
OPF_NS = "{http://www.idpf.org/2007/opf}"
_SOURCE_ERROR = re.compile(r"HTTP Error (?:429|5\d\d)|Traceback \(most recent call last\)|timed out|URLError|"
                           r"Connection (?:reset|refused)|Too Many Requests", re.I)


@dataclass
class FetchedMeta:
    title: str = ""
    authors: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    publisher: str = ""
    pubdate: str = ""
    comments: str = ""
    languages: list = field(default_factory=list)
    identifiers: dict = field(default_factory=dict)
    series: str = ""
    series_index: float | None = None


def parse_opf(xml_text: str) -> FetchedMeta:
    """Parse an OPF document. ElementTree decodes &amp; and friends; clean_text catches double encoding."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    md = root.find("opf:metadata", NS)
    if md is None:
        md = root.find(".//{*}metadata")
    if md is None:
        raise ValueError("OPF has no <metadata>")
    m = FetchedMeta()

    def texts(tag):
        return [clean_text(e.text) for e in md.findall(f"dc:{tag}", NS) if e.text and clean_text(e.text)]

    t = texts("title")
    m.title = t[0] if t else ""
    m.authors = []
    for e in md.findall("dc:creator", NS):
        role = e.get(f"{OPF_NS}role") or e.get("role") or "aut"
        if role == "aut" and e.text and clean_text(e.text):
            m.authors.append(clean_text(e.text))
    m.tags = texts("subject")
    pubs = texts("publisher")
    m.publisher = pubs[0] if pubs else ""
    dates = texts("date")
    m.pubdate = dates[0] if dates else ""
    desc = [e.text for e in md.findall("dc:description", NS) if e.text and e.text.strip()]
    m.comments = desc[0].strip() if desc else ""  # HTML is kept as-is, like calibre does
    m.languages = [x.lower() for x in texts("language")]
    for e in md.findall("dc:identifier", NS):
        scheme = e.get(f"{OPF_NS}scheme") or e.get("scheme") or ""
        val = clean_text(e.text)
        if not val:
            continue
        if not scheme and ":" in val:
            scheme, val = val.split(":", 1)
        scheme = scheme.strip().lower()
        if scheme and scheme not in ("calibre", "uuid", "uuid_id") and val:
            m.identifiers[scheme] = val.strip()
    for e in md.findall("opf:meta", NS) + md.findall("meta"):
        name, content = e.get("name"), e.get("content")
        if name == "calibre:series" and content:
            m.series = clean_text(content)
        elif name == "calibre:series_index" and content:
            try:
                m.series_index = float(content)
            except ValueError:
                pass
    return m


class FetchResult:
    MATCH, NO_MATCH, ERROR = "match", "no_match", "error"

    def __init__(self, kind, meta=None, detail=""):
        self.kind, self.meta, self.detail = kind, meta, detail


class CalibreFetcher:
    def __init__(self, exe: str, plugins, timeout: int = 45):
        self.exe = exe
        self.plugins = list(plugins)
        self.timeout = timeout

    def available(self) -> bool:
        return shutil.which(self.exe) is not None

    def fetch(self, title: str, author: str | None, cover_path: Path | None = None) -> FetchResult:
        cmd = [shutil.which(self.exe) or self.exe]
        for p in self.plugins:
            cmd += ["-p", p]
        cmd += ["--title", title, "--opf", "--timeout", str(self.timeout)]
        if author:
            cmd += ["--authors", author]
        if cover_path:
            cmd += ["--cover", str(cover_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout * 3 + 30)
        except subprocess.TimeoutExpired:
            return FetchResult(FetchResult.ERROR, detail="timed out")
        err_tail = " ".join((proc.stderr or "").split())[-300:]
        if "No results found" in (proc.stderr or "") or "No results found" in (proc.stdout or ""):
            # calibre also prints "No results found" when a source crashed or was blocked;
            # that is a temporary error, not proof the book doesn't exist.
            if _SOURCE_ERROR.search(proc.stderr or ""):
                return FetchResult(FetchResult.ERROR, detail="source error: " + err_tail)
            return FetchResult(FetchResult.NO_MATCH, detail=err_tail)
        out = proc.stdout or ""
        start = out.find("<?xml")
        if start < 0:
            start = out.find("<package")
        if proc.returncode != 0 or start < 0:
            return FetchResult(FetchResult.ERROR, detail=f"exit {proc.returncode}: {err_tail}")
        try:
            meta = parse_opf(out[start:])
        except (ET.ParseError, ValueError) as e:
            return FetchResult(FetchResult.ERROR, detail=f"unreadable OPF: {e}")
        if not meta.title:
            return FetchResult(FetchResult.ERROR, detail="OPF without a title")
        return FetchResult(FetchResult.MATCH, meta)
