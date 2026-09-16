"""MangaUpdates public API (no key). Used to tell Novel from Manga, and to find authors.

Credit: series data from MangaUpdates (https://www.mangaupdates.com).
"""
from __future__ import annotations

import re

from ..textutil import clean_text, similarity, words_key
from .http import HttpError, JsonClient

API = "https://api.mangaupdates.com/v1"
NOVEL_TYPES = {"novel"}
COMIC_TYPES = {"manga", "manhwa", "manhua", "doujinshi", "oel", "filipino", "indonesian", "thai",
               "vietnamese", "malaysian", "french", "spanish", "nordic"}
_SUFFIX = re.compile(r"\s*\((?:novel|light novel|manga|manhwa|manhua|webtoon|web novel)\)\s*$", re.I)


def medium_of(mu_type: str | None) -> str | None:
    t = (mu_type or "").strip().lower()
    if t in NOVEL_TYPES:
        return "novel"
    if t in COMIC_TYPES:
        return "comic"
    return None


def mu_author_name(name: str) -> str:
    """'AINANA Hiro' (family name in caps first) -> 'Hiro Ainana'. Other forms unchanged."""
    name = clean_text(name)
    parts = name.split()
    if len(parts) >= 2 and parts[0].isupper() and len(parts[0]) > 1 and not all(p.isupper() for p in parts[1:]):
        return " ".join(parts[1:] + [parts[0].capitalize()])
    return name


class MangaUpdates:
    def __init__(self, client: JsonClient, min_similarity: float = 0.93):
        self.client = client
        self.min_similarity = min_similarity

    def search(self, query: str) -> list[dict]:
        query = clean_text(query)
        if not query:
            return []
        data = self.client.request("POST", f"{API}/series/search", {"search": query, "perpage": 25})
        out = []
        for r in data.get("results", []) or []:
            rec = r.get("record") or {}
            out.append({
                "id": rec.get("series_id"),
                "title": clean_text(rec.get("title")),
                "hit_title": clean_text(r.get("hit_title") or rec.get("title")),
                "type": rec.get("type"),
                "year": rec.get("year"),
                "url": rec.get("url"),
            })
        return out

    def series(self, series_id) -> dict:
        return self.client.request("GET", f"{API}/series/{series_id}")

    def confident_matches(self, query: str) -> list[dict]:
        """Search results whose title (minus a '(Novel)' style suffix) matches the query."""
        qk = words_key(_SUFFIX.sub("", query))
        if not qk:
            return []
        matches = []
        for r in self.search(query):
            best = 0.0
            for t in (r["hit_title"], r["title"]):
                t2 = _SUFFIX.sub("", t or "")
                best = max(best, similarity(t2, query))
            if best >= self.min_similarity:
                r = dict(r, score=round(best, 3), medium=medium_of(r["type"]))
                matches.append(r)
        return matches

    def authors_for(self, series_id) -> list[str]:
        try:
            d = self.series(series_id)
        except HttpError:
            return []
        return [mu_author_name(a.get("name", "")) for a in d.get("authors", []) or []
                if (a.get("type") or "").lower() == "author" and a.get("name")]
