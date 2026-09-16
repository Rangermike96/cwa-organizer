"""Command line and interactive console for CWA Organizer."""
from __future__ import annotations

import argparse
import shutil
import sys

from . import runner, safety
from .config import DEFAULTS_FILE, ConfigError, load_config

EPILOG = """
examples:
  cwa-organizer                          interactive menu
  cwa-organizer check                    test the setup without changing anything
  cwa-organizer run --dry-run            preview every pass
  cwa-organizer run --limit 10           write changes, at most 10 books per pass
  cwa-organizer run --passes offline     only the passes that need no internet
  cwa-organizer run --passes classify,fetch --yes     unattended (cron)
  cwa-organizer undo 20260916-201500     reverse everything a run wrote
passes: """ + ", ".join(runner.ORDER) + """
presets: """ + ", ".join(f"{k} ({', '.join(v) if k != 'all' else 'every pass'})" for k, v in runner.PRESETS.items())


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cwa-organizer", description="Organize a Calibre-Web-Automated library safely.",
                                 epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.toml (default: config.toml in the project folder)")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="run passes (use --dry-run first)")
    r.add_argument("--dry-run", action="store_true", help="plan and report only; write nothing")
    r.add_argument("--passes", default="all", help="comma-separated passes or presets (default: all)")
    r.add_argument("--skip", default="", help="comma-separated passes to leave out")
    r.add_argument("--limit", type=int, default=0, help="change at most N books per pass (0 = no limit)")
    r.add_argument("--force-rescan", action="store_true", help="re-fetch and re-classify books already processed")
    r.add_argument("--reclassify", action="store_true", help="overwrite an existing Book Type when evidence disagrees")
    r.add_argument("--offline", action="store_true", help="no network lookups")
    r.add_argument("--deep-file-check", action="store_true", help="verify every archive CRC (slow over NFS)")
    r.add_argument("--allow-schema-upgrade", action="store_true", help="allow this computer's calibre to upgrade the DB schema")
    r.add_argument("--full-preview", action="store_true",
                   help="in a dry run, look up every book online instead of previewing 10 per slow pass")
    r.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation (for cron)")
    r.add_argument("--verbose", "-v", action="store_true", help="show per-book details on screen")

    u = sub.add_parser("undo", help="reverse the writes of a previous run")
    u.add_argument("run_id")
    u.add_argument("--dry-run", action="store_true", help="only show what would be reversed")
    u.add_argument("--yes", "-y", action="store_true")

    sub.add_parser("runs", help="list previous runs")
    sub.add_parser("check", help="check config, calibre, library access and safety without changing anything")
    sub.add_parser("init", help="create config.toml, secrets.toml and aliases.toml from templates")
    sub.add_parser("menu", help="interactive console")
    return ap


def cmd_init(project_dir) -> int:
    made = []
    cfg_file = project_dir / "config.toml"
    if not cfg_file.exists():
        shutil.copyfile(DEFAULTS_FILE, cfg_file)
        made.append(cfg_file.name)
    sec = project_dir / "secrets.toml"
    if not sec.exists():
        sec.write_text('# Private. Never commit this file.\n# Hardcover API key from https://hardcover.app/account/api\nhardcover_api_key = ""\n')
        sec.chmod(0o600)
        made.append(sec.name)
    al = project_dir / "aliases.toml"
    if not al.exists():
        al.write_text(
            "# Approved spelling fixes, applied on every run.\n"
            "# Format: \"spelling to replace\" = \"spelling to keep\"\n"
            "# Suggestions appear in review/aliases_suggested.toml after each run.\n\n"
            "[authors]\n\n[series]\n\n[publishers]\n")
        made.append(al.name)
    print("Created: " + ", ".join(made) if made else "Nothing to do; config.toml, secrets.toml and aliases.toml exist.")
    return 0


def cmd_check(cfg) -> int:
    ok = True
    print(f"Config:   {cfg.config_file or '(defaults only; run init to create config.toml)'}")
    print(f"Library:  {cfg.library}")
    pf = safety.preflight(cfg, for_write=True)
    for m in pf.info:
        print("  ok    " + m)
    for m in pf.warnings:
        print("  warn  " + m)
    for m in pf.problems:
        print("  STOP  " + m)
        ok = False
    for tool in ("calibre_debug", "fetch_ebook_metadata", "ebook_meta"):
        path = shutil.which(cfg["calibre"][tool])
        print(f"  {'ok  ' if path else 'MISS'}  {cfg['calibre'][tool]}: {path or 'not found (sudo pacman -S calibre)'}")
        ok = ok and bool(path)
    print(f"  {'ok  ' if cfg.hardcover_token else 'warn'}  Hardcover API key: {'found' if cfg.hardcover_token else 'not set (series lookup skipped)'}")
    if cfg.metadata_db.exists():
        try:
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as td:
                snap = safety.take_snapshot(cfg, Path(td) / "check.db", include_wal=True)
                print(f"  {'ok  ' if safety.integrity(snap) == 'ok' else 'STOP'}  integrity of a local copy: {safety.integrity(snap)}")
                print(f"  info  {safety.book_count(snap)} books, schema {safety.user_version(snap)}")
        except Exception as e:  # noqa: BLE001
            print(f"  STOP  could not copy metadata.db: {e}")
            ok = False
    if shutil.which(cfg["calibre"]["calibre_debug"]):
        try:
            import subprocess
            out = subprocess.run([shutil.which(cfg["calibre"]["calibre_debug"]), "-c",
                                  "from calibre.constants import numeric_version as v; print('.'.join(map(str, v)))"],
                                 capture_output=True, text=True, timeout=120)
            print(f"  info  calibre on this computer: {out.stdout.strip() or out.stderr.strip()[:200]}")
        except Exception as e:  # noqa: BLE001
            print(f"  warn  could not ask calibre for its version: {e}")
    print("Ready for a write run." if ok else "Fix the STOP/MISS items before a write run (dry runs may still work).")
    return 0 if ok else 1


# ------------------------------------------------------------------ menu ---

def _ask(prompt: str, default: str = "") -> str:
    try:
        ans = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        return default
    return ans or default


def _yes(prompt: str, default=False) -> bool:
    ans = _ask(prompt + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not ans else ans in ("y", "yes")


def menu(cfg) -> int:
    print("=== CWA Organizer ===")
    print(f"Library: {cfg.library}\n")
    print("  1) Preview (dry run): see what would change, write nothing")
    print("  2) Run and write changes")
    print("  3) Undo a previous run")
    print("  4) List previous runs")
    print("  5) Check setup")
    print("  q) Quit")
    choice = _ask("Choice", "1").lower()
    if choice in ("q", "quit"):
        return 0
    if choice == "3":
        runner.list_runs(cfg)
        rid = _ask("Run ID to undo")
        return runner.undo(cfg, rid, dry_run=_yes("Only preview the undo?"), yes=False) if rid else 1
    if choice == "4":
        return runner.list_runs(cfg)
    if choice == "5":
        return cmd_check(cfg)
    if choice not in ("1", "2"):
        print("Unknown choice.")
        return 1
    print("\nWhich passes?")
    print("  1) Everything")
    print("  2) Offline cleanup: tags, publishers, authors, series, titles, tag backfill, checks")
    print("  3) Classification (Light Novel / Manga / Other)")
    print("  4) Metadata fetch only")
    print("  5) Reports only: missing files and duplicates")
    print("  6) Pick passes by name")
    pc = _ask("Choice", "1")
    spec = {"1": "all", "2": "offline", "3": "classify", "4": "fetch", "5": "report"}.get(pc)
    if pc == "6":
        print("Passes: " + ", ".join(runner.ORDER))
        spec = _ask("Comma-separated passes")
    try:
        passes = runner.resolve_passes(spec or "all")
    except ValueError as e:
        print(e)
        return 1
    limit_s = _ask("Limit books changed per pass (0 = no limit)", "0")
    limit = int(limit_s) if limit_s.isdigit() else 0
    opt = runner.RunOptions(
        passes=passes, dry_run=(choice == "1"), limit=limit,
        force_rescan=_yes("Re-process books already fetched/classified (force rescan)?"),
        offline=True if pc in ("2", "5") else _yes("Offline (no internet lookups)?"),
    )
    if choice == "2":
        opt.reclassify = _yes("Overwrite existing Book Type values when evidence disagrees?")
        print("\nBefore writing: stop the calibre-web-automated container and close calibre / calibre-mcp.")
        if not _yes("Continue?"):
            return 1
        opt.yes = True
    return runner.run(cfg, opt)


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    if args.cmd == "init":
        return cmd_init(cfg.project_dir)
    if args.cmd in (None, "menu"):
        if not sys.stdin.isatty() and args.cmd is None:
            ap.print_help()
            return 1
        return menu(cfg)
    if args.cmd == "check":
        return cmd_check(cfg)
    if args.cmd == "runs":
        return runner.list_runs(cfg)
    if args.cmd == "undo":
        return runner.undo(cfg, args.run_id, dry_run=args.dry_run, yes=args.yes)
    if args.cmd == "run":
        try:
            passes = runner.resolve_passes(args.passes, args.skip)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        if args.limit < 0:
            print("--limit must be 0 or more", file=sys.stderr)
            return 2
        opt = runner.RunOptions(passes=passes, dry_run=args.dry_run, limit=args.limit, force_rescan=args.force_rescan,
                                offline=args.offline, reclassify=args.reclassify, deep_files=args.deep_file_check,
                                yes=args.yes, allow_schema_upgrade=args.allow_schema_upgrade, verbose=args.verbose,
                                full_preview=args.full_preview)
        return runner.run(cfg, opt)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
