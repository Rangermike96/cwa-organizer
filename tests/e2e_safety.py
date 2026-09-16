"""Safety tests for the calibre writer (needs calibre installed).

    python3 tests/e2e_safety.py

1. A case-only author or title change is withheld, never handed to calibre
   (calibre can delete a book's files for that on some network shares).
2. A stop request makes the applier finish cleanly between books.
3. If calibre crashes, the books it had not reached are retried and written.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from cwa_organizer import calibre_bridge  # noqa: E402
from cwa_organizer.config import load_config  # noqa: E402
from e2e_calibre import calibredb, dump, make_epub  # noqa: E402


def main():
    tmp = Path(tempfile.mkdtemp(prefix="cwa-safety-"))
    lib, work, proj = tmp / "Calibre Library", tmp / "work", tmp / "proj"
    lib.mkdir()
    work.mkdir()
    proj.mkdir()
    for i, (title, author) in enumerate([("Kizumonogatari", "NISIOISIN"), ("Some Title", "Other Writer"),
                                         ("Book Three", "Third Writer"), ("Book Four", "Fourth Writer")]):
        f = work / f"b{i}.epub"
        make_epub(f, title, text_kb=50, images=0)
        calibredb(lib, "add", "--duplicates", "-t", title, "-a", author, str(f))
    (proj / "config.toml").write_text(f'[library]\npath = "{lib}"\n')
    cfg = load_config(project_dir=proj)
    rows = {r["title"]: r for r in dump(lib)}

    # 1) case-only author + case-only title are withheld; the tag change still goes through
    b1, b2 = rows["Kizumonogatari"]["id"], rows["Some Title"]["id"]
    ops = [
        {"book_id": b1, "title": "Kizumonogatari", "fields": {"authors": ["Nisioisin"], "tags": ["Light Novel"]},
         "expect": {"authors": ["NISIOISIN"], "tags": []}},
        {"book_id": b2, "title": "Some Title", "fields": {"title": "SOME TITLE"}, "expect": {"title": "Some Title"}},
    ]
    recs = calibre_bridge.apply_plan(cfg, ops, proj / "plan-case.json", proj / "journal.jsonl")
    by = {r["book_id"]: r for r in recs}
    assert "authors" in by[b1]["withheld"] and by[b1]["after"] == {"tags": ["Light Novel"]}, by[b1]
    assert "title" in by[b2]["withheld"] and by[b2]["after"] == {}, by[b2]
    now = {r["id"]: r for r in dump(lib)}
    assert now[b1]["authors"] == "NISIOISIN" and now[b1]["tags"] == ["Light Novel"], now[b1]
    assert now[b2]["title"] == "Some Title", now[b2]
    assert any(p.suffix == ".epub" for p in (lib / "NISIOISIN").rglob("*")), "book file missing"
    print("1 ok: case-only renames withheld, other fields written")

    # 2) stop file present before start -> nothing written, clean exit
    b3 = rows["Book Three"]["id"]
    plan = proj / "plan-stop.json"
    plan.write_text(json.dumps({"ops": [{"book_id": b3, "fields": {"tags": ["X"]}, "expect": {"tags": []}}]}))
    stop = proj / "plan-stop.stop"
    stop.touch()
    out = subprocess.run(["calibre-debug", "-e", str(calibre_bridge.APPLIER), "--", "apply", str(lib), str(plan),
                          str(proj / "journal-stop.jsonl"), str(stop)], capture_output=True, text=True)
    assert out.returncode == 0 and '"stopped": true' in out.stdout, out.stdout + out.stderr
    assert {r["id"]: r for r in dump(lib)}[b3]["tags"] in ([], None)
    print("2 ok: stop request honoured before writing")

    # 3) calibre-debug that crashes once (SIGABRT) -> retried, all books written
    real = subprocess.run(["which", "calibre-debug"], capture_output=True, text=True).stdout.strip()
    flag = proj / "crashed-once"
    wrapper = proj / "flaky-calibre-debug"
    wrapper.write_text(f"#!/bin/sh\nif [ ! -e '{flag}' ]; then touch '{flag}'; kill -ABRT $$; fi\nexec '{real}' \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    cfg.data["calibre"]["calibre_debug"] = str(wrapper)
    b4 = rows["Book Four"]["id"]
    ops = [{"book_id": b3, "fields": {"tags": ["Three"]}, "expect": {"tags": []}},
           {"book_id": b4, "fields": {"tags": ["Four"]}, "expect": {"tags": []}}]
    logs = []
    recs = calibre_bridge.apply_plan(cfg, ops, proj / "plan-crash.json", proj / "journal-crash.jsonl", log=logs.append)
    assert flag.exists() and logs and "Retrying" in logs[0], logs
    assert sorted(r["status"] for r in recs) == ["applied", "applied"], recs
    now = {r["id"]: r for r in dump(lib)}
    assert now[b3]["tags"] == ["Three"] and now[b4]["tags"] == ["Four"], (now[b3], now[b4])
    assert list(proj.glob("plan-crash.stderr.log")), "stderr log not saved"
    print("3 ok: crash retried, books written, stderr saved")
    print(f"\nSAFETY OK. Work folder: {tmp}")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    main()
