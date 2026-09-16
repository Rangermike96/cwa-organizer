"""In-memory model of the library, loaded read-only from a local snapshot of metadata.db.

Passes change Book objects through Book.set(); every change is recorded with
the pass name and a reason. `pending_ops()` turns the difference between a
book's last-written state and its current state into write operations for
the calibre applier, each carrying the expected "before" values so the
applier can refuse to write over anything that changed in the meantime.
"""
from __future__ import annotations

import copy
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


WRITABLE = (
    "title", "authors", "tags", "series", "series_index", "publisher",
    "identifiers", "languages", "comments", "pubdate",
)
UNDEFINED_DATE_PREFIX = ("0101-01-01", "0100-12-31", "0000")


def is_undefined_date(v: str | None) -> bool:
    return not v or str(v).startswith(UNDEFINED_DATE_PREFIX)


@dataclass
class Format:
    fmt: str
    name: str
    size: int


@dataclass
class Change:
    book_id: int
    field: str
    before: object
    after: object
    pass_name: str
    reason: str
    status: str = "planned"  # planned / applied / skipped / failed


@dataclass
class Book:
    id: int
    path: str
    has_cover: bool
    title: str
    authors: list
    tags: list
    series: str | None
    series_index: float
    publisher: str | None
    identifiers: dict
    languages: list
    comments: str
    pubdate: str | None
    formats: list
    custom: dict  # '#label' -> value (str or None)
    hints: dict = field(default_factory=dict)  # notes shared between passes, never written
    _written: dict = field(default_factory=dict, repr=False)

    def snapshot_fields(self) -> dict:
        d = {f: copy.deepcopy(getattr(self, f)) for f in WRITABLE}
        d.update({k: v for k, v in self.custom.items()})
        d["cover"] = None
        return d

    def get(self, fld: str):
        if fld.startswith("#"):
            return self.custom.get(fld)
        if fld == "cover":
            return self.hints.get("new_cover")
        return getattr(self, fld)

    def fmt_names(self) -> set:
        return {f.fmt.upper() for f in self.formats}


def same_value(fld: str, a, b) -> bool:
    """Exact comparison: a case or &amp; fix IS a change worth writing."""
    if fld == "tags":
        return sorted((x or "").strip() for x in (a or [])) == sorted((x or "").strip() for x in (b or []))
    if fld == "authors":
        return [(x or "").strip() for x in (a or [])] == [(x or "").strip() for x in (b or [])]
    if fld == "series_index":
        return abs(float(a or 0) - float(b or 0)) < 1e-9
    if fld == "identifiers":
        return {k.lower(): str(v) for k, v in (a or {}).items()} == {k.lower(): str(v) for k, v in (b or {}).items()}
    if fld == "pubdate":
        ua, ub = is_undefined_date(a), is_undefined_date(b)
        if ua or ub:
            return ua and ub
        return str(a)[:10] == str(b)[:10]
    if fld in ("series", "publisher", "comments") or fld.startswith("#"):
        return (a or "") == (b or "")
    return a == b


class Library:
    def __init__(self, books: dict, custom_columns: dict, path: Path, user_version: int):
        self.books: dict[int, Book] = books
        self.custom_columns = custom_columns  # label -> {id, datatype, is_multiple, display}
        self.path = path
        self.user_version = user_version
        self.changes: list[Change] = []
        self.review: list[dict] = []  # items for a human: category, book_ids, detail
        for b in self.books.values():
            b._written = b.snapshot_fields()

    # ---------------------------------------------------------------- edits --
    def set(self, book: Book, fld: str, value, pass_name: str, reason: str) -> bool:
        """Change a field in memory. Returns True if the value actually changed."""
        cur = book.get(fld)
        if fld != "cover" and same_value(fld, cur, value):
            return False
        if fld.startswith("#"):
            book.custom[fld] = value
        elif fld == "cover":
            book.hints["new_cover"] = value
        else:
            setattr(book, fld, copy.deepcopy(value))
        self.changes.append(Change(book.id, fld, cur, copy.deepcopy(value), pass_name, reason))
        return True

    def add_review(self, category: str, book_ids, detail: str, **extra) -> None:
        item = {"category": category, "book_ids": sorted(set(int(i) for i in book_ids)), "detail": detail}
        item.update(extra)
        self.review.append(item)

    # ------------------------------------------------------------ writing ---
    def pending_ops(self) -> list[dict]:
        ops = []
        for b in self.books.values():
            fields, expect = {}, {}
            for fld, old in b._written.items():
                if fld == "cover":
                    if b.hints.get("new_cover"):
                        fields["cover"] = b.hints["new_cover"]
                        expect["cover"] = None
                    continue
                new = b.get(fld)
                if not same_value(fld, old, new):
                    fields[fld] = _jsonable(fld, new)
                    expect[fld] = _jsonable(fld, old)
            if fields:
                ops.append({"book_id": b.id, "title": b.title, "fields": fields, "expect": expect})
        return ops

    def mark_written(self, results: list[dict]) -> None:
        """Update the last-written state from applier results.

        Fields that failed or were skipped are rolled back in memory to what
        is really in the database, so later passes do not build on them.
        """
        by_book = {r["book_id"]: r for r in results}
        for bid, r in by_book.items():
            b = self.books.get(bid)
            if b is None:
                continue
            if r.get("path"):
                b.path = r["path"]
                for f in b.formats:
                    if f.fmt.upper() in (r.get("formats") or {}):
                        f.name = r["formats"][f.fmt.upper()]
            if r["status"] == "applied":
                for fld in (r.get("withheld") or {}):
                    # calibre refused this field (unsafe rename): memory goes back to what is in the database
                    val = (r.get("actual") or {}).get(fld, b._written.get(fld))
                    b._written[fld] = copy.deepcopy(val)
                    setattr(b, fld, copy.deepcopy(val))
                for fld, val in r["after"].items():
                    if fld == "cover":
                        b.hints.pop("new_cover", None)
                        b.has_cover = True
                        continue
                    b._written[fld] = copy.deepcopy(val)
            else:
                for fld, val in (r.get("actual") or b._written).items():
                    if fld == "cover":
                        b.hints.pop("new_cover", None)
                        continue
                    if fld in b._written:
                        b._written[fld] = copy.deepcopy(val)
                        if fld.startswith("#"):
                            b.custom[fld] = copy.deepcopy(val)
                        else:
                            setattr(b, fld, copy.deepcopy(val))
        status_by_book = {bid: r["status"] for bid, r in by_book.items()}
        for c in self.changes:
            if c.status == "planned" and c.book_id in status_by_book:
                c.status = status_by_book[c.book_id]


def _jsonable(fld, v):
    if fld in ("tags", "authors", "languages"):
        return list(v or [])
    if fld == "identifiers":
        return dict(v or {})
    if fld == "series_index":
        return float(v if v is not None else 1.0)
    return v


# ---------------------------------------------------------------- loading ---

def load_library(snapshot_db: Path, library_path: Path, custom_labels=()) -> Library:
    con = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return _load(con, library_path, custom_labels)
    finally:
        con.close()


def _load(con, library_path, custom_labels) -> Library:
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    user_version = q("PRAGMA user_version")[0][0]

    books = {}
    for r in q("SELECT id, title, path, has_cover, series_index, pubdate FROM books"):
        books[r["id"]] = Book(
            id=r["id"], path=r["path"], has_cover=bool(r["has_cover"]), title=r["title"] or "",
            authors=[], tags=[], series=None, series_index=float(r["series_index"] or 1.0),
            publisher=None, identifiers={}, languages=[], comments="", pubdate=r["pubdate"],
            formats=[], custom={},
        )

    for r in q("SELECT l.book, a.name FROM books_authors_link l JOIN authors a ON a.id=l.author ORDER BY l.book, l.id"):
        if r[0] in books:
            books[r[0]].authors.append((r[1] or "").replace("|", ","))  # calibre stores "," as "|"
    for r in q("SELECT l.book, t.name FROM books_tags_link l JOIN tags t ON t.id=l.tag ORDER BY l.book, l.id"):
        if r[0] in books:
            books[r[0]].tags.append(r[1])
    for r in q("SELECT l.book, s.name FROM books_series_link l JOIN series s ON s.id=l.series"):
        if r[0] in books:
            books[r[0]].series = r[1]
    for r in q("SELECT l.book, p.name FROM books_publishers_link l JOIN publishers p ON p.id=l.publisher"):
        if r[0] in books:
            books[r[0]].publisher = r[1]
    for r in q("SELECT book, type, val FROM identifiers"):
        if r[0] in books:
            books[r[0]].identifiers[r[1]] = r[2]
    for r in q("SELECT l.book, g.lang_code FROM books_languages_link l JOIN languages g ON g.id=l.lang_code ORDER BY l.book, l.item_order"):
        if r[0] in books:
            books[r[0]].languages.append(r[1])
    for r in q("SELECT book, text FROM comments"):
        if r[0] in books:
            books[r[0]].comments = r[1] or ""
    for r in q("SELECT book, format, name, uncompressed_size FROM data"):
        if r[0] in books:
            books[r[0]].formats.append(Format(r[1], r[2], int(r[3] or 0)))

    import json

    custom_columns = {}
    for r in q("SELECT id, label, name, datatype, is_multiple, display FROM custom_columns"):
        try:
            display = json.loads(r["display"] or "{}")
        except ValueError:
            display = {}
        custom_columns[r["label"]] = {
            "id": r["id"], "name": r["name"], "datatype": r["datatype"],
            "is_multiple": bool(r["is_multiple"]), "display": display,
        }
    for label in custom_labels:
        label = label.lstrip("#")
        key = "#" + label
        for b in books.values():
            b.custom[key] = None
        col = custom_columns.get(label)
        if not col or col["is_multiple"]:
            continue
        n = col["id"]
        sql = (f"SELECT l.book, c.value FROM books_custom_column_{n}_link l "
               f"JOIN custom_column_{n} c ON c.id=l.value")
        for r in q(sql):
            if r[0] in books:
                books[r[0]].custom[key] = r[1]
    return Library(books, custom_columns, Path(library_path), user_version)
