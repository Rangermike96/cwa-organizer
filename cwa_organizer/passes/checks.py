"""Report-only checks: missing/corrupt book files and duplicate books. Nothing is changed or deleted."""
from __future__ import annotations

import zipfile
from collections import defaultdict

from ..textutil import key, words_key
from .common import format_path

FILES_PASS = "files"
DUP_PASS = "duplicates"


def check_file(path, fmt: str, deep: bool) -> str | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return "file missing"
    except OSError as e:
        return f"cannot read ({e})"
    if st.st_size == 0:
        return "file is empty"
    fmt = fmt.upper()
    try:
        if fmt in ("EPUB", "KEPUB", "CBZ"):
            if not zipfile.is_zipfile(path):
                return "not a valid zip archive"
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if fmt in ("EPUB", "KEPUB"):
                    if "META-INF/container.xml" not in names:
                        return "EPUB has no META-INF/container.xml"
                elif not any(n.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")) for n in names):
                    return "CBZ contains no images"
                if deep:
                    bad = z.testzip()
                    if bad:
                        return f"corrupt member in archive: {bad}"
        elif fmt == "PDF":
            with open(path, "rb") as fh:
                head = fh.read(1024)
            if b"%PDF-" not in head:
                return "not a PDF (no %PDF header)"
            if deep:
                with open(path, "rb") as fh:
                    fh.seek(max(0, st.st_size - 2048))
                    if b"%%EOF" not in fh.read():
                        return "PDF is truncated (no %%EOF)"
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError) as e:
        return f"unreadable: {type(e).__name__}: {e}"
    return None


def run_files(ctx) -> None:
    lib, cfg = ctx.lib, ctx.cfg
    deep = ctx.deep_files or cfg["files"]["check_level"] == "deep"
    problems = 0
    no_formats = 0
    bar = ctx.progress("File check", len(lib.books))
    bar.__enter__()
    for i, b in enumerate(sorted(lib.books.values(), key=lambda x: x.id)):
        bar.update(n=i, item=f"{problems} problem(s)")
        if not b.formats:
            no_formats += 1
            lib.add_review("no-files", [b.id], f"'{b.title}' has no book files at all.")
            continue
        for f in b.formats:
            err = check_file(format_path(cfg.library, b, f), f.fmt, deep)
            if err:
                problems += 1
                lib.add_review("file-problem", [b.id], f"'{b.title}' {f.fmt}: {err}", path=str(format_path(cfg.library, b, f)))
    bar.update(n=len(lib.books))
    bar.__exit__(None, None, None)
    ctx.log.info(f"Files ({'deep' if deep else 'fast'} check): {problems} problem file(s), {no_formats} book(s) with no files.")


def run_duplicates(ctx) -> None:
    lib = ctx.lib
    groups = 0
    by_isbn = defaultdict(set)
    by_title_author = defaultdict(set)
    for b in lib.books.values():
        for k, v in b.identifiers.items():
            if k.lower() in ("isbn", "isbn13", "isbn10"):
                digits = "".join(ch for ch in str(v) if ch.isdigit() or ch in "Xx")
                if len(digits) in (10, 13):
                    by_isbn[digits].add(b.id)
        fmt_kind = "comic" if b.fmt_names() and b.fmt_names() <= {"CBZ", "CBR"} else "text"
        first_author = key(b.authors[0], False) if b.authors else ""
        by_title_author[(words_key(b.title), first_author, fmt_kind)].add(b.id)
    reported = set()
    for isbn, ids in sorted(by_isbn.items()):
        if len(ids) > 1 and frozenset(ids) not in reported:
            reported.add(frozenset(ids))
            groups += 1
            lib.add_review("duplicate-isbn", ids, f"ISBN {isbn} is on {len(ids)} books: " +
                           "; ".join(f"[{i}] '{lib.books[i].title}'" for i in sorted(ids)))
    for (title, author, _), ids in sorted(by_title_author.items()):
        if len(ids) > 1 and frozenset(ids) not in reported:
            reported.add(frozenset(ids))
            groups += 1
            fmts = "; ".join(f"[{i}] {', '.join(sorted(lib.books[i].fmt_names())) or 'no files'}" for i in sorted(ids))
            lib.add_review("duplicate-title", ids, f"Same title and author on {len(ids)} books: '{lib.books[min(ids)].title}' ({fmts})")
    ctx.log.info(f"Duplicates: {groups} possible duplicate group(s) reported (nothing merged or deleted).")
