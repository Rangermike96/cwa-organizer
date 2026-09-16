"""Run reports: changes.csv, review.csv, report.html and review/aliases_suggested.toml."""
from __future__ import annotations

import csv
import html
import json
from collections import Counter, defaultdict
from pathlib import Path

CATEGORY_HELP = {
    "series-ambiguous": "Different books would share a series number. Decide which is right.",
    "series-duplicate": "Probably the same book imported twice. Check with calibre's Find Duplicates.",
    "series-conflict": "The book's series disagrees with its title.",
    "index-mismatch": "Series number disagrees with the title.",
    "series-gap": "Volumes missing from a series.",
    "unclassified": "Could not tell Light Novel / Manga / Other with confidence.",
    "classification-conflict": "Existing book type disagrees with the evidence.",
    "junk-author": "Placeholder author that could not be replaced confidently.",
    "fetch-failed": "No acceptable metadata match.",
    "duplicate-isbn": "Several books share an ISBN.",
    "duplicate-title": "Several books share title and author.",
    "file-problem": "Missing, empty or damaged book file.",
    "no-files": "Book record without any file.",
    "missing-cover": "No cover found.",
    "write-problem": "calibre skipped or failed a write.",
    "publisher-suggestion": "Possible publisher spelling variants.",
    "omnibus": "Omnibus volume range; no series number assigned.",
    "part-volume": "Part-numbered volume without a series number.",
}


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    s = str(v)
    return s if len(s) <= 500 else s[:500] + "..."


def write_reports(ctx, header: dict) -> dict:
    lib, run_dir = ctx.lib, ctx.run_dir
    titles = {b.id: b.title for b in lib.books.values()}

    with open(run_dir / "changes.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["status", "pass", "book_id", "title", "field", "before", "after", "reason"])
        for c in lib.changes:
            w.writerow([c.status, c.pass_name, c.book_id, titles.get(c.book_id, ""), c.field,
                        _cell(c.before), _cell(c.after), c.reason])

    with open(run_dir / "review.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["category", "book_ids", "detail"])
        for r in lib.review:
            w.writerow([r["category"], " ".join(str(i) for i in r["book_ids"]), r["detail"]])

    sugg_path = write_alias_suggestions(ctx)

    by_pass = Counter(c.pass_name for c in lib.changes)
    books_by_pass = defaultdict(set)
    for c in lib.changes:
        books_by_pass[c.pass_name].add(c.book_id)
    review_counts = Counter(r["category"] for r in lib.review)
    summary = dict(header)
    summary.update({
        "changes_by_pass": {p: {"field_changes": n, "books": len(books_by_pass[p])} for p, n in by_pass.items()},
        "review_counts": dict(review_counts),
        "writes": ctx.write_counts,
        "alias_suggestions_file": str(sugg_path) if sugg_path else None,
    })
    ctx.save_summary(summary)
    (run_dir / "report.html").write_text(_html(ctx, summary), encoding="utf-8")
    return summary


def write_alias_suggestions(ctx) -> Path | None:
    authors = getattr(ctx, "author_suggestions", []) or []
    series = getattr(ctx, "series_suggestions", []) or []
    if not authors and not series:
        return None
    ctx.cfg.review_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.cfg.review_dir / "aliases_suggested.toml"
    q = lambda s: json.dumps(s, ensure_ascii=False)  # noqa: E731  (TOML basic strings == JSON strings here)
    lines = [
        "# Suggested aliases from run " + ctx.run_id,
        "# Nothing here is applied. To approve one, copy the line (without the leading '# ')",
        "# into the matching section of aliases.toml in the project folder, and make sure the",
        "# right-hand side is the spelling you want to keep. The next run applies it.",
        "",
        "[authors]",
    ]
    for a, b, why, na, nb in sorted(authors, key=lambda x: (-(x[3] + x[4]), x[0])):
        keep, drop = (a, b) if na >= nb else (b, a)
        lines.append(f"# {q(drop)} = {q(keep)}    # {why}; {max(na, nb)} vs {min(na, nb)} book(s)")
    lines += ["", "[series]"]
    for a, b, why in series:
        lines.append(f"# {q(b)} = {q(a)}    # {why}")
    lines += ["", "[publishers]", "# (see review.csv, category publisher-suggestion)", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _html(ctx, summary: dict) -> str:
    lib = ctx.lib
    e = html.escape
    titles = {b.id: b.title for b in lib.books.values()}
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CWA Organizer run {e(ctx.run_id)}</title>
<style>
:root {{ --bg:#fbfaf7; --fg:#1f2328; --muted:#5f6670; --line:#e3e1dc; --card:#ffffff; --accent:#2f6f5e; --warn:#9a5b00; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#15181b; --fg:#e6e6e3; --muted:#9aa1a9; --line:#2b3035; --card:#1c2024; --accent:#7cc4ad; --warn:#e0a84a; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; margin:0; padding:24px 16px; }}
main {{ max-width:1100px; margin:0 auto; }}
h1 {{ font-size:1.5rem; margin:0 0 4px; }} h2 {{ font-size:1.15rem; margin:28px 0 8px; }}
.muted {{ color:var(--muted); }} .warn {{ color:var(--warn); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; margin:14px 0; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 12px; }}
.card b {{ display:block; font-size:1.3rem; color:var(--accent); }}
details {{ background:var(--card); border:1px solid var(--line); border-radius:8px; margin:8px 0; padding:6px 12px; }}
summary {{ cursor:pointer; font-weight:600; }}
.table-wrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; margin:8px 0; }}
th, td {{ text-align:left; vertical-align:top; padding:4px 6px; border-bottom:1px solid var(--line); }}
th {{ color:var(--muted); font-weight:600; }}
td.num {{ white-space:nowrap; }}
</style></head><body><main>
<h1>CWA Organizer run {e(ctx.run_id)}</h1>
<p class="muted">{'DRY RUN: nothing was written.' if ctx.dry_run else 'Changes were written to the library.'}
 Library: {e(str(ctx.cfg.library))} &middot; {len(lib.books)} books &middot; passes: {e(', '.join(summary.get('passes', [])))}</p>
"""]
    w = summary.get("writes", {})
    cards = [("Field changes", len(lib.changes)), ("Books changed", len({c.book_id for c in lib.changes})),
             ("Review items", len(lib.review))]
    if not ctx.dry_run:
        cards += [("Books written", w.get("applied", 0)), ("Skipped/failed", w.get("skipped", 0) + w.get("failed", 0) + w.get("partial", 0))]
    parts.append('<div class="grid">' + "".join(f'<div class="card"><b>{v}</b>{e(k)}</div>' for k, v in cards) + "</div>")
    if summary.get("backup"):
        parts.append(f'<p class="muted">Backup: {e(str(summary["backup"]))} &middot; undo with <code>./cwa-organizer undo {e(ctx.run_id)}</code></p>')

    parts.append("<h2>Changes by pass</h2>")
    by_pass = defaultdict(list)
    for c in lib.changes:
        by_pass[c.pass_name].append(c)
    for p, items in by_pass.items():
        rows = "".join(
            f"<tr><td class='num'>{c.book_id}</td><td>{e(titles.get(c.book_id, ''))}</td><td>{e(c.field)}</td>"
            f"<td>{e(_cell(c.before))}</td><td>{e(_cell(c.after))}</td><td>{e(c.reason)}</td><td>{e(c.status)}</td></tr>"
            for c in items[:3000])
        more = f"<p class='muted'>Showing 3000 of {len(items)}; see changes.csv.</p>" if len(items) > 3000 else ""
        parts.append(f"<details><summary>{e(p)} &middot; {len(items)} change(s) on {len({c.book_id for c in items})} book(s)</summary>"
                     f"<div class='table-wrap'><table><tr><th>ID</th><th>Title</th><th>Field</th><th>Before</th><th>After</th><th>Why</th><th>Status</th></tr>{rows}</table></div>{more}</details>")

    parts.append("<h2>Needs your review</h2>")
    by_cat = defaultdict(list)
    for r in lib.review:
        by_cat[r["category"]].append(r)
    if not by_cat:
        parts.append("<p class='muted'>Nothing to review.</p>")
    for cat, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        rows = "".join(f"<tr><td class='num'>{e(' '.join(str(i) for i in r['book_ids']))}</td><td>{e(r['detail'])}</td></tr>"
                       for r in items[:3000])
        parts.append(f"<details><summary>{e(cat)} &middot; {len(items)}</summary><p class='muted'>{e(CATEGORY_HELP.get(cat, ''))}</p>"
                     f"<div class='table-wrap'><table><tr><th>Book IDs</th><th>Detail</th></tr>{rows}</table></div></details>")
    if summary.get("alias_suggestions_file"):
        parts.append(f"<p>Author/series spelling suggestions: <code>{e(summary['alias_suggestions_file'])}</code></p>")
    parts.append("</main></body></html>")
    return "".join(parts)
