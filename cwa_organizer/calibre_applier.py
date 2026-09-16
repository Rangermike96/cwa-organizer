"""Runs INSIDE calibre's Python via `calibre-debug -e calibre_applier.py -- <mode> <args>`.

This file must not import anything from the cwa_organizer package: calibre
may use its own bundled interpreter. It is the ONLY code that writes to the
library, and it always goes through calibre's own database API, so folder
renames, metadata.opf files, author sort and title sort stay consistent.

Modes
  info <library>                         print JSON: calibre version, schema version, custom columns
  ensure-columns <library> <spec.json>   create missing custom columns
  apply <library> <plan.json> <journal.jsonl>
      For each op: compare the live values with op["expect"]; if anything
      differs, skip the whole book. Otherwise write each field and append one
      JSON line to the journal (flushed per book) with before/after values.
"""
import faulthandler
import json
import os
import sys
import traceback

# If calibre's native code crashes ("free(): invalid pointer"), print where it happened.
faulthandler.enable(file=sys.stderr, all_threads=True)


def _out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _open(library):
    from calibre.library import db as open_db
    return open_db(library)


def _fold(s):
    import unicodedata
    return unicodedata.normalize("NFKC", s or "").casefold().strip()


def _read(cache, book_id, fld):
    if fld == "cover":
        return None if not cache.field_for("cover", book_id) else True
    v = cache.field_for(fld, book_id)
    if fld in ("tags", "authors", "languages"):
        return list(v or ())
    if fld == "identifiers":
        return dict(v or {})
    if fld == "series_index":
        return float(v if v is not None else 1.0)
    if fld == "pubdate":
        if v is None:
            return None
        try:
            from calibre.utils.date import is_date_undefined
            if is_date_undefined(v):
                return None
        except Exception:
            pass
        return v.isoformat()
    if fld == "comments":
        return v or ""
    return v


def _same(fld, a, b):
    if fld == "tags":
        return sorted(_fold(x) for x in (a or [])) == sorted(_fold(x) for x in (b or []))
    if fld == "series_index":
        return abs(float(a or 0) - float(b or 0)) < 1e-9
    if fld == "identifiers":
        return {k.lower(): str(v) for k, v in (a or {}).items()} == {k.lower(): str(v) for k, v in (b or {}).items()}
    if fld == "pubdate":
        und = lambda x: (not x) or str(x).startswith(("0101-01-01", "0100-12-31", "0000"))  # noqa: E731
        if und(a) or und(b):
            return und(a) and und(b)
        return str(a)[:10] == str(b)[:10]
    if fld == "authors":
        # calibre matches authors case-insensitively and may re-case one for every book at once
        return [_fold(x) for x in (a or [])] == [_fold(x) for x in (b or [])]
    if fld in ("series", "publisher"):
        return _fold(a) == _fold(b)
    if fld in ("comments", "title") or fld.startswith("#"):
        return (a or "") == (b or "")
    return a == b


# Write order matters: series before series_index, title/authors last-but-cover
# so a failure in a cheap field happens before any folder rename.
ORDER = ["tags", "publisher", "identifiers", "languages", "comments", "pubdate",
         "series", "series_index", "title", "authors", "cover"]


def _write(cache, book_id, fld, value):
    if fld == "cover":
        if value is None:
            cache.set_cover({book_id: None})
        else:
            with open(value, "rb") as fh:
                cache.set_cover({book_id: fh.read()})
        return
    if fld == "pubdate":
        if value:
            from calibre.utils.date import parse_date
            value = parse_date(value, assume_utc=True, as_utc=True)
        else:
            from calibre.utils.date import UNDEFINED_DATE
            value = UNDEFINED_DATE
    elif fld in ("tags", "authors", "languages"):
        value = list(value or [])
        if fld == "authors" and not value:
            raise ValueError("refusing to set an empty author list")
    elif fld == "identifiers":
        value = dict(value or {})
    elif fld == "series_index":
        value = float(value)
    elif fld == "title" and not (value or "").strip():
        raise ValueError("refusing to set an empty title")
    cache.set_field(fld, {book_id: value})


def mode_info(library):
    from calibre.constants import numeric_version
    db = _open(library)
    try:
        cache = db.new_api
        cols = {k: {"datatype": v["datatype"], "is_multiple": bool(v["is_multiple"])}
                for k, v in cache.field_metadata.custom_field_metadata().items()}
        uv = cache.backend.conn.get("PRAGMA user_version", all=False)
        _out({"calibre_version": list(numeric_version), "user_version": uv,
              "custom_columns": cols, "book_count": len(cache.all_book_ids())})
    finally:
        db.close()


def mode_ensure_columns(library, spec_file):
    with open(spec_file) as fh:
        spec = json.load(fh)
    created = []
    db = _open(library)
    try:
        existing = {k.lstrip("#"): v for k, v in db.new_api.field_metadata.custom_field_metadata().items()}
        for col in spec:
            label = col["label"]
            if label in existing:
                ex = existing[label]
                if ex["datatype"] not in (col["datatype"], "text") or bool(ex["is_multiple"]):
                    _out({"error": f"column #{label} exists with datatype {ex['datatype']} "
                                   f"(multiple={bool(ex['is_multiple'])}); expected {col['datatype']}, single value"})
                    sys.exit(3)
                continue
            db.create_custom_column(label, col["name"], col["datatype"], False, display=col.get("display", {}))
            created.append(label)
    finally:
        db.close()
    _out({"created": created})


def _author_case_map(cache):
    out = {}
    try:
        for name in cache.get_id_map("authors").values():
            out.setdefault(_fold(name), set()).add(name)
    except Exception:
        pass
    return out


def _path_guard(cache, bid, fields, author_case):
    """Reason to withhold a title/author change, or None.

    calibre renames a book's folder when its title or first author changes.
    On shares where the same folder can be reached under different
    capitalisations (for example Unraid user shares over NFS), calibre can
    mistake a case-only rename for a move to a different folder and delete
    the book's files. So case-only folder renames are never attempted, and
    neither is changing the capitalisation of an existing author (calibre
    applies that to every book by the author at once).
    """
    if "title" not in fields and "authors" not in fields:
        return None
    for a in fields.get("authors") or []:
        names = author_case.get(_fold(a))
        if names and a not in names:
            return ("would change the capitalisation of existing author '%s' to '%s', which renames folders "
                    "case-only (unsafe on this library's share)" % (sorted(names)[0], a))
    title = fields.get("title") or cache.field_for("title", bid)
    authors = list(fields.get("authors") or cache.field_for("authors", bid) or ())
    if not authors:
        return None
    cur = cache.field_for("path", bid) or ""
    try:
        new = cache.backend.construct_path_name(bid, title, authors[0])
    except Exception:
        return None
    if new == cur or not cur:
        return None
    if new.lower() == cur.lower():
        return "would rename the book folder only by capitalisation ('%s' -> '%s'), which is unsafe on this share" % (cur, new)
    target = os.path.join(cache.backend.library_path, *new.split("/"))
    try:
        if os.path.isdir(target) and any(n != "metadata.opf" for n in os.listdir(target)):
            return "the new folder '%s' already exists and contains files" % new
    except OSError:
        pass
    return None


def mode_apply(library, plan_file, journal_file, stop_file=None):
    with open(plan_file) as fh:
        plan = json.load(fh)
    db = _open(library)
    cache = db.new_api
    counts = {"applied": 0, "skipped": 0, "failed": 0, "partial": 0}
    stopped = False
    try:
        all_ids = set(cache.all_book_ids())
        author_case = _author_case_map(cache)
        with open(journal_file, "a", encoding="utf-8") as jf:
            for op in plan["ops"]:
                if stop_file and os.path.exists(stop_file):
                    stopped = True  # asked to stop between books: finish cleanly
                    break
                bid = int(op["book_id"])
                fields, expect = dict(op["fields"]), op.get("expect", {})
                rec = {"book_id": bid, "title": op.get("title"), "status": None,
                       "before": {}, "after": {}, "actual": {}, "error": None}
                if bid not in all_ids:
                    rec["status"], rec["error"] = "skipped", "book no longer exists"
                else:
                    try:
                        live = {f: _read(cache, bid, f) for f in fields}
                        diffs = [f for f in fields if f in expect and not _same(f, live[f], expect[f])]
                        if diffs:
                            rec["status"] = "skipped"
                            rec["error"] = "changed since planning: " + ", ".join(diffs)
                            rec["actual"] = live
                        else:
                            reason = _path_guard(cache, bid, fields, author_case)
                            if reason:
                                rec["withheld"] = {f: reason for f in ("title", "authors") if f in fields}
                                rec["actual"] = {f: live[f] for f in rec["withheld"]}
                                for f in rec["withheld"]:
                                    fields.pop(f)
                            rec["before"] = live
                            for f in sorted(fields, key=lambda x: ORDER.index(x) if x in ORDER else 50):
                                _write(cache, bid, f, fields[f])
                                rec["after"][f] = fields[f]
                                if f == "authors":
                                    for a in fields[f]:
                                        author_case.setdefault(_fold(a), set()).add(a)
                            rec["status"] = "applied"
                    except Exception as e:  # keep going with the next book
                        rec["error"] = f"{type(e).__name__}: {e}"
                        rec["trace"] = traceback.format_exc(limit=3)
                        rec["status"] = "partial" if rec["after"] else "failed"
                        try:
                            rec["actual"] = {f: _read(cache, bid, f) for f in fields}
                        except Exception:
                            pass
                if bid in all_ids:
                    try:  # calibre renames folders/files when title or author change
                        rec["path"] = cache.field_for("path", bid)
                        rec["formats"] = {}
                        for fmt in cache.formats(bid) or ():
                            fm = cache.format_metadata(bid, fmt) or {}
                            if fm.get("path"):
                                rec["formats"][fmt] = os.path.splitext(os.path.basename(fm["path"]))[0]
                    except Exception:
                        pass
                counts[rec["status"]] += 1
                jf.write(json.dumps(rec, default=str) + "\n")
                jf.flush()
                os.fsync(jf.fileno())
        # Write metadata.opf backups for every changed book before closing.
        cache.dump_metadata()
    finally:
        db.close()
    _out({"done": True, "counts": counts, "stopped": stopped})


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "info":
        mode_info(rest[0])
    elif mode == "ensure-columns":
        mode_ensure_columns(rest[0], rest[1])
    elif mode == "apply":
        mode_apply(rest[0], rest[1], rest[2], rest[3] if len(rest) > 3 else None)
    else:
        print(f"unknown mode {mode}")
        return 2
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--":
        args = args[1:]
    sys.exit(main(args))
