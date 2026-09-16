"""End-to-end test against a throwaway calibre library (needs calibre installed; uses the network).

    python3 tests/e2e_calibre.py [--online]

Builds a small messy library in a temp folder, runs the organizer in write
mode, checks the results through calibredb, then undoes every run and checks
that the metadata is back to where it started.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cwa_organizer import runner  # noqa: E402
from cwa_organizer.config import load_config  # noqa: E402


def make_epub(path: Path, title: str, text_kb: int, images: int):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        items = []
        body = ("<p>" + "Lorem ipsum dolor sit amet. " * 40 + "</p>\n") * max(1, text_kb // 1)
        pages = max(1, images) if images else 3
        for i in range(pages):
            name = f"p{i}.xhtml"
            content = f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{i}</title></head><body>'
            if images:
                content += f'<img src="i{i}.jpg"/>'
                z.writestr(f"i{i}.jpg", b"\xff\xd8\xff" + os.urandom(2000))
            else:
                content += body
            content += "</body></html>"
            z.writestr(name, content)
            items.append(name)
        manifest = "".join(f'<item id="x{i}" href="{n}" media-type="application/xhtml+xml"/>' for i, n in enumerate(items))
        spine = "".join(f'<itemref idref="x{i}"/>' for i in range(len(items)))
        z.writestr("content.opf", f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title><dc:identifier id="id">x</dc:identifier><dc:language>en</dc:language></metadata><manifest>{manifest}</manifest><spine>{spine}</spine></package>')


def make_cbz(path: Path, pages: int = 30):
    with zipfile.ZipFile(path, "w") as z:
        for i in range(pages):
            z.writestr(f"{i:03d}.jpg", b"\xff\xd8\xff" + os.urandom(3000))


def calibredb(lib, *args):
    out = subprocess.run(["calibredb", "--with-library", str(lib), *args], capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(out.stderr)
    return out.stdout


BOOKS = [
    # title, authors, format, tags, publisher
    ("Death March to the Parallel World Rhapsody, Vol. 3", "Ainana, Hiro", "novel", "light novel,Comics &amp; Graphic Novels", "Yen Press, LLC"),
    ("Death March to the Parallel World Rhapsody Volume 4", "Hiro Ainana", "novel", "Fantasy", "Yen On"),
    ("Death March to the Parallel World Rhapsody Vol. 5 [Yen Press][Kobo][0EDF6669]", "Hiro Ainana", "novel", "fantasy", "Yen On"),
    ("BEASTARS, Vol. 2", "Paru Itagaki", "cbz", "", "VIZ Media LLC"),
    ("Kodansha Test Manga, Vol. 1 (Manga)", "Some Mangaka", "comic-epub", "", "Kodansha"),
    ("Assassin's Apprentice", "Robin Hobb", "novel", "Fiction / Fantasy / General,ebook", "Bantam"),
    ("Fahrenheit 451", "Ray Bradbury", "novel", "", ""),
    ("Sword Art Online Progressive 1", "Kawahara, Reki", "novel", "", "Yen On"),
    ("Sword Art Online Progressive 2", "Kawahara, Reki", "novel", "", "Yen On"),
    ("Revolution 2", "Some Writer", "novel", "", ""),
    ("BAKEMONOGATARI, Part 1: Monster Tale", "NISIOISIN", "novel", "", "Vertical"),
    ("BAKEMONOGATARI Part 1", "Nisio Isin", "novel", "", "Vertical"),
    ("Toradora! Light Novel: Volume 3", "Yuyuko Takemiya; Illustrator Yasu", "novel", "", "Seven Seas"),
]


def build(lib: Path, work: Path):
    lib.mkdir(parents=True)
    for i, (title, authors, kind, tags, pub) in enumerate(BOOKS):
        if kind == "cbz":
            f = work / f"b{i}.cbz"
            make_cbz(f)
        else:
            f = work / f"b{i}.epub"
            make_epub(f, title, text_kb=200, images=60 if kind == "comic-epub" else 0)
        out = calibredb(lib, "add", "--duplicates", "-t", title, "-a", authors, *(["-T", tags] if tags else []), str(f))
        bid = int(out.strip().split(":")[-1].split(",")[0])
        if pub:
            calibredb(lib, "set_metadata", "--field", f"publisher:{pub}", str(bid))


def dump(lib: Path) -> list:
    keep = {"id", "title", "authors", "tags", "series", "series_index", "publisher", "identifiers", "pubdate",
            "comments", "languages", "*booktype", "*metadata_status"}
    rows = json.loads(calibredb(lib, "list", "--for-machine", "-f", "all"))
    rows = [{k: r.get(k) for k in keep} for r in rows]
    for r in rows:
        for k in list(r):
            if isinstance(r[k], list):
                r[k] = sorted(r[k]) if k == "tags" else r[k]
            if k in ("*booktype", "*metadata_status") and r[k] is None:
                r.pop(k)
    return sorted(rows, key=lambda r: r["id"])


def main():
    online = "--online" in sys.argv
    tmp = Path(tempfile.mkdtemp(prefix="cwa-e2e-"))
    lib, work, proj = tmp / "Calibre Library", tmp / "work", tmp / "proj"
    work.mkdir()
    proj.mkdir()
    build(lib, work)
    (proj / "config.toml").write_text(f'[library]\npath = "{lib}"\n[fetch]\nsleep_base = 1\nsleep_jitter = 1\n[mangaupdates]\nmin_interval_seconds = 1.0\n')
    cfg = load_config(project_dir=proj)
    before = dump(lib)

    passes = runner.resolve_passes("all", skip="fetch,series_lookup,covers" if online else "fetch,series_lookup,covers,junk_authors")
    rc = runner.run(cfg, runner.RunOptions(passes=passes, dry_run=False, offline=not online, yes=True))
    assert rc == 0, f"run failed rc={rc}"
    after = {r["id"]: r for r in dump(lib)}
    by_title = {r["title"]: r for r in after.values()}
    print(json.dumps(list(after.values()), indent=1)[:6000])

    def t(title):
        assert title in by_title, f"missing title {title!r}; have {sorted(by_title)}"
        return by_title[title]

    dm3 = t("Death March to the Parallel World Rhapsody, Vol. 3")
    assert dm3["authors"] == "Hiro Ainana", dm3["authors"]
    assert dm3["series"] == "Death March to the Parallel World Rhapsody" and dm3["series_index"] == 3.0
    assert "Comics & Graphic Novels" in dm3["tags"] and "Light Novel" in dm3["tags"], dm3["tags"]
    assert dm3["publisher"] == "Yen Press"
    assert t("Death March to the Parallel World Rhapsody, Vol. 5")["series_index"] == 5.0
    assert "Fantasy" in t("Death March to the Parallel World Rhapsody, Vol. 5")["tags"]
    assert t("Sword Art Online Progressive, Vol. 2")["series_index"] == 2.0
    fah = t("Fahrenheit 451")
    assert not fah["series"], fah
    assert t("Revolution 2")["series"] in (None, "")
    tor = t("Toradora!, Vol. 3")
    assert tor["authors"] == "Yuyuko Takemiya", tor["authors"]
    aa = t("Assassin's Apprentice")
    assert sorted(aa["tags"]) == ["Fantasy", "Fiction"] or "Fantasy" in aa["tags"], aa["tags"]
    if online:
        assert t("BEASTARS, Vol. 2").get("*booktype") == "Manga"
        assert t("Kodansha Test Manga, Vol. 1").get("*booktype") in ("Manga", None)
        assert "Light Novel" not in t("Kodansha Test Manga, Vol. 1")["tags"]
        assert dm3.get("*booktype") == "Light Novel", dm3
        assert aa.get("*booktype") == "Other Books", aa
    # series collision: both BAKEMONOGATARI Part 1 books withheld
    bake = [r for r in after.values() if "BAKEMONOGATARI" in r["title"]]
    assert all(not r["series"] for r in bake), bake
    assert len({r["authors"] for r in bake}) == 1, bake
    # folders renamed by calibre
    assert (lib / "Hiro Ainana").exists() and not (lib / "Ainana, Hiro").exists()

    if online:
        rc = runner.run(cfg, runner.RunOptions(passes=["fetch"], dry_run=False, limit=2, yes=True, verbose=True))
        assert rc == 0, rc

    # ---- second identical run must change nothing ----
    rc = runner.run(cfg, runner.RunOptions(passes=runner.resolve_passes("offline"), dry_run=True, offline=True))
    last = sorted(p for p in cfg.runs_dir.iterdir() if not p.name.startswith("undo-"))[-1]
    summary = json.loads((last / "summary.json").read_text())
    print("second pass changes:", summary["changes_by_pass"])
    assert not summary["changes_by_pass"], "offline passes are not idempotent"

    # ---- undo everything, newest first ----
    write_runs = [p.name for p in sorted(cfg.runs_dir.iterdir())
                  if (p / "journal.jsonl").exists() and not p.name.startswith("undo-")]
    for rid in reversed(write_runs):
        rc = runner.undo(cfg, rid, dry_run=False, yes=True)
        assert rc == 0, f"undo {rid} rc={rc}"
    restored = dump(lib)
    diffs = []
    for a, b in zip(before, restored):
        for k in set(a) | set(b):
            if k in ("*booktype", "*metadata_status"):
                continue
            if a.get(k) != b.get(k):
                diffs.append((a["id"], k, a.get(k), b.get(k)))
    for d in diffs:
        print("DIFF", d)
    assert not diffs, "undo did not restore the original metadata"
    print(f"\nE2E OK ({'online' if online else 'offline'}). Work folder: {tmp}")
    if "--keep" not in sys.argv:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
