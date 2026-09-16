"""Hardcover GraphQL API (needs a personal API key). Used for series names/positions and authors."""
from __future__ import annotations

import json

from ..textutil import clean_text
from .http import JsonClient

API = "https://api.hardcover.app/v1/graphql"

SEARCH_QUERY = """
query Search($q: String!, $perPage: Int!) {
  search(query: $q, query_type: "Book", per_page: $perPage, page: 1) { results }
}
"""


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


class Hardcover:
    def __init__(self, client: JsonClient, token: str):
        self.client = client
        self.token = token

    def search_books(self, query: str, per_page: int = 8) -> list[dict]:
        query = clean_text(query)
        if not query:
            return []
        data = self.client.request(
            "POST", API,
            {"query": SEARCH_QUERY, "variables": {"q": query, "perPage": per_page}},
            headers={"authorization": f"Bearer {self.token}"},
            cache_key_extra="hardcover-search-v1",
        )
        if data.get("errors"):
            raise RuntimeError("Hardcover error: " + json.dumps(data["errors"])[:300])
        results = ((data.get("data") or {}).get("search") or {}).get("results") or {}
        if isinstance(results, str):
            try:
                results = json.loads(results)
            except ValueError:
                results = {}
        out = []
        for hit in results.get("hits", []) or []:
            doc = hit.get("document") or {}
            series_name, position = None, None
            fs = doc.get("featured_series")
            if isinstance(fs, dict):
                s = fs.get("series") or {}
                series_name = clean_text(s.get("name")) or None
                position = fs.get("position")
            if series_name is None and doc.get("series_names"):
                names = _as_list(doc.get("series_names"))
                series_name = clean_text(names[0]) if names else None
            if position is None:
                position = doc.get("featured_series_position")
            try:
                position = float(position) if position is not None else None
            except (TypeError, ValueError):
                position = None
            out.append({
                "id": doc.get("id"),
                "slug": doc.get("slug"),
                "title": clean_text(doc.get("title")),
                "subtitle": clean_text(doc.get("subtitle")),
                "authors": [clean_text(a) for a in _as_list(doc.get("author_names")) if a],
                "series": series_name,
                "position": position,
                "isbns": [str(i) for i in _as_list(doc.get("isbns")) if i],
                "year": doc.get("release_year"),
            })
        return out
