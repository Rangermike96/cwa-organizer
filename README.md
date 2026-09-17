# CWA Organizer

CWA Organizer cleans up and organizes a [Calibre-Web-Automated](https://github.com/crocodilestick/Calibre-Web-Automated) library, or any Calibre library. It was built for a large collection of light novels and manga, but it also handles general fiction.

It fixes titles, series, authors, tags and publishers. It sorts every book into **Light Novel**, **Manga** or **Other Books**, fills in missing metadata and covers, and reports duplicates, series gaps and damaged files. Writes are careful: before each run it checks that nothing else is using the database and takes a backup, and every change is journaled so the whole run can be undone.

It is written in Python (3.11 or newer) with no third-party packages. Every write goes through calibre's own database API, so folder names, `metadata.opf` files and sort fields stay consistent.

## What it does

The organizer works in passes. You can run all of them or pick the ones you want.

| Pass | What it changes | Needs internet |
|---|---|---|
| `tags` | Decodes `&amp;`, splits `Fiction / Fantasy / General` style tags, maps synonyms (`light novel` → `Light Novel`, `Sci-fi` → `Science Fiction`), merges case duplicates, and drops junk tags like `ebook` and `New Subject`. | no |
| `publishers` | Merges spellings like `VIZ Media, LLC` and `VIZMedia` into one publisher, and applies aliases. | no |
| `junk_authors` | Replaces placeholder authors that came from release filenames (`Vol 02 [June][Scans][4FF7E520]`, `VeryPDF`, `© DENSUKE 2019`), but only when the lookup is confident. | yes |
| `authors` | Splits `A; B` into separate authors, removes illustrator and translator credits, flips `Last, First` names, and merges case and spacing variants. Fuzzier variants are only suggested. | no |
| `series` | Sets series and volume number from titles, merges series names that differ only in spelling, detects collisions, and reports gaps. | no |
| `titles` | Removes release junk and `(Light Novel)` markers, and standardizes titles to `Series, Vol. 3: Subtitle`. | no |
| `classify` | Sets the Book Type column and a matching tag to Light Novel, Manga or Other Books. | yes (MangaUpdates) |
| `series_lookup` | Uses Hardcover to find series for books whose titles have no volume number. | yes (Hardcover key) |
| `fetch` | Fills in missing descriptions, publishers, dates, identifiers and tags from Google, Open Library and Edelweiss, falling back to Hardcover. | yes |
| `covers` | Adds missing covers, first by extracting them from the book file and then from online sources. | sometimes |
| `tag_backfill` | Adds a genre tag to every volume in a series when all the tagged volumes already have it. | no |
| `files` | Report only: missing, empty or damaged EPUB, CBZ and PDF files. | no |
| `duplicates` | Report only: books that share an ISBN, or a title and author. | no |

Presets choose several passes at once. `all` runs every pass, `offline` runs the ones that need no internet, `classify` runs tags and classify, `fetch` runs tags, publishers and fetch, and `report` runs files and duplicates.

### Rules it never breaks

- It never deletes a book, a file or a tag list. Tags are always merged with what's already there, never replaced wholesale.
- It never guesses when there is a collision. If two books would get the same series number, neither is assigned. The pair is reported as DUPLICATE or AMBIGUOUS for you to decide.
- It never moves a book that already has a series into a different series. Disagreements are reported.
- It never trusts a bare trailing number on its own. `Sword Art Online Progressive 6` counts as volume 6 only when other books share that name with different numbers, so `Fahrenheit 451` is never volume 451.
- A fetched result is accepted only if it is in English, has an author that matches ours, has a title that matches, has the same volume number, and is the same edition. A `(Manga)` result is never applied to a light novel. Existing descriptions, publishers, dates, series and authors are never overwritten.
- A book's title is replaced by a provider's title only when the volume numbers match.
- Book type is written only when the evidence agrees. MangaUpdates often lists the manga and the novel under the same name, so the book's own files decide which one it is: a CBZ or an image-heavy EPUB is a comic, and a text EPUB is prose. **Other Books** needs positive evidence: prose that isn't on MangaUpdates and has no light novel or manga signal. Anything unclear goes to review.

## Safety model

`metadata.db` usually lives on a network share. SQLite's locking is unreliable over NFS, and two programs writing at the same time is the most likely cause of database corruption. To avoid that, the organizer does the following:

- It never opens the live database with SQLite. All reading and planning happens on a byte copy on the local disk.
- **Before any write**, it refuses to run if CWA still answers at `safety.cwa_url`, if calibre, calibre-server or calibre-mcp is running on this computer, if `metadata.db-wal` is not empty, or if the database fails an integrity check.
- It saves a verified backup to `backups/metadata-<run id>.db`, and keeps the last 20 by default.
- It opens a throwaway copy with this computer's calibre first, to make sure calibre won't upgrade the database schema. A newer schema could leave CWA unable to read the library.
- It writes in batches through calibre. Each book's write first checks that the values are still what was planned, and skips the book if not. Every write is appended to `runs/<run id>/journal.jsonl` with its before and after values.
- It never asks calibre to rename a folder only by capitalisation (for example, author `NISIOISIN` becoming `Nisioisin`, or a title changing only in case). On shares where one folder can be reached under several capitalisations, such as Unraid user shares over NFS, calibre can mistake that for a move and delete the book's files. Those changes are left out, and listed for review. Change them by hand only if your share is case-sensitive.
- calibre runs in its own process group, so Ctrl+C never kills it mid-write. It finishes the book it's on, closes the database and stops.
- If calibre crashes, the books it hadn't reached are retried, up to twice, and its full error output is saved next to the batch in the run folder.
- After writing, it checks the database's integrity and book count again.
- `./cwa-organizer undo <run id>` reverses a run, newest change first. Undo is better than restoring a backup, because calibre renames folders when titles and authors change, and an old database copy would point at folder names that no longer exist.

**You must stop the CWA container before any run that writes.** Dry runs are safe while CWA is running.

## Setup

**Install calibre on the computer that runs the organizer.** The organizer uses calibre's `calibre-debug`, `fetch-ebook-metadata` and `ebook-meta`. On CachyOS or Arch, run:

```bash
sudo pacman -S calibre
```

This calibre should be the same version as the one inside CWA, or newer. The schema check stops the run if it would cause a problem.

**Get the code and create your config files:**

```bash
cd ~/Software_Scripts
git clone https://github.com/Rangermike96/cwa-organizer.git cwa-organizer
cd cwa-organizer
chmod +x cwa-organizer
./cwa-organizer init
```

`init` creates three files, all of which git ignores:

- `config.toml` is your settings. Every option is documented inside it, and any line you delete falls back to the default in `cwa_organizer/defaults.toml`.
- `secrets.toml` holds your Hardcover API key.
- `aliases.toml` holds the spelling fixes you've approved.

**Edit `config.toml`.** Check `library.path` first. Then set `safety.cwa_url` to CWA's web address, for example `http://192.168.1.149:8083`, so the tool can confirm CWA is stopped. If you leave it empty, you'll be asked to confirm by hand.

**Add a Hardcover API key (optional).** It is used for series lookups, author lookups, and as the fallback source in the fetch pass when Google, Open Library and Edelweiss have nothing usable (they often carry only the omnibus or manga edition of a light novel). Hardcover has no publisher and only a release year, so a book filled from it keeps no status mark and is tried again on a later run, when the other sources may have the rest. Sign in at hardcover.app, open **Account Settings → Hardcover API** (`https://hardcover.app/account/api`), click **New API Key**, give it a label, pick an expiry and copy the token. Paste it into `secrets.toml`:

```toml
hardcover_api_key = "eyJ..."
```

You can use the `HARDCOVER_API_KEY` environment variable instead. The free plan allows 60 requests a minute and 5,000 a day.

**Check the setup:**

```bash
./cwa-organizer check
```

This checks the share, calibre, your key, and the integrity of a local copy of the database, without changing anything.

## First run

Start with a dry run while CWA is still running. It reads a copy of the database and writes only reports:

```bash
./cwa-organizer run --dry-run
```

The online passes are slow on a big library. MangaUpdates allows about one lookup a second, so a full classification takes a while, but results are cached in `state/` and the real run reuses them. Every slow pass shows a progress bar with the count, percentage, elapsed time and an estimate of the time left (when the output isn't a terminal, such as under cron, it prints a progress line about every 10% instead). In a dry run without `--limit`, the passes that look books up one at a time (placeholder authors, series lookup and fetch) preview only 10 books each; add `--full-preview` to look them all up. When it finishes, open `runs/<run id>/report.html`. It lists every planned change with the reason for it, plus everything that needs your review. The same data is in `changes.csv` and `review.csv`.

When the plan looks right, stop the CWA container, close calibre and calibre-mcp, and do a small real run:

```bash
./cwa-organizer run --limit 10
```

`--limit 10` changes at most 10 books in each pass. Start CWA and look at those books. If anything is wrong, stop CWA again and run `./cwa-organizer undo <run id>`. The run ID is printed at the end of every run, and `./cwa-organizer runs` lists them all.

After that you can run the whole library, or go pass by pass:

```bash
./cwa-organizer run --passes offline          # fast, no internet
./cwa-organizer run --passes classify
./cwa-organizer run --passes fetch            # long: roughly 10-15 seconds per book
```

The fetch pass is polite to Google, which rate-limits aggressive clients, so it takes many hours on thousands of books. You can stop it with Ctrl+C at any time: the work done so far is saved, and the next run continues with the books that don't have a Metadata Status yet.

## Everyday use

Running `./cwa-organizer` with no arguments opens an interactive menu. You can pick preview or write, choose the passes, set a limit, and turn on force rescan or offline mode.

For scripts and cron jobs, use the command-line options:

```text
./cwa-organizer run [--dry-run] [--passes LIST] [--skip LIST] [--limit N]
                    [--force-rescan] [--reclassify] [--offline]
                    [--deep-file-check] [--yes] [--verbose]
./cwa-organizer undo RUN_ID [--dry-run] [--yes]
./cwa-organizer runs | check | init | menu
```

- `--force-rescan` looks up books again even if they already have a Metadata Status or Book Type.
- `--reclassify` lets the classifier overwrite a Book Type that disagrees with the evidence. Without it, the disagreement is only reported.
- `--yes` skips confirmations, for unattended use. It does not skip the safety checks, so a cron job still refuses to write while CWA is running.
- `--deep-file-check` verifies every archive's checksum. This is slow over NFS.

### Reviewing suggestions

After each run, `review/aliases_suggested.toml` lists author and series spellings that might be the same, such as `Ao Juumonji` and `Ao Jyumonji`, or an old series name that differs from the titles. Nothing in that file is applied. To approve one, copy its line into the matching section of `aliases.toml`. The left side is the spelling to replace, and the right side is the spelling to keep:

```toml
[authors]
"Ao Jyumonji" = "Ao Juumonji"

[series]
"Death March to the Parallel World" = "Death March to the Parallel World Rhapsody"
```

The next run applies it.

### Where things are

| Path | What |
|---|---|
| `runs/<id>/report.html` | The readable report for that run |
| `runs/<id>/changes.csv`, `review.csv` | Every change and every review item |
| `runs/<id>/journal.jsonl` | What was actually written, and what undo uses |
| `runs/<id>/run.log` | The full log |
| `backups/` | `metadata.db` backups, one per write run |
| `review/aliases_suggested.toml` | Spelling suggestions |
| `state/` | Lookup cache, EPUB probe cache and the run lock |

### Calibre-Web columns

The organizer creates two custom columns the first time it writes: **Book Type** (`#booktype`) and **Metadata Status** (`#metadata_status`). Both are enumerations, so they show up as filters in Calibre-Web and calibre. By default the same values are also written as tags (`Light Novel`, `Manga`, `Other Books`, `Metadata-Fetched`, `Metadata-Fetch-Failed`), so shelves based on tags keep working. A book always carries exactly one type tag and one status tag. You can turn the tags off with `labels.mirror_to_tags`.

## If something goes wrong

- **A run was refused.** The message says why: CWA is running, calibre is open, the WAL file isn't empty, or the database failed its integrity check. Nothing was touched.
- **The changes look wrong.** Stop CWA and run `./cwa-organizer undo <run id>`. To preview the undo first, add `--dry-run`.
- **The post-run integrity check failed.** Don't start CWA. Undo usually still works. If the database can't be opened, copy the newest file from `backups/` over `metadata.db` in the library folder, then run calibre's **Check library** tool. Folder renames made by the run may need fixing, and `journal.jsonl` shows what changed.
- **Many books were skipped with "changed since planning".** Something else wrote to the library during the run. Find out what it was before running again.
- **calibre crashed ("free(): invalid pointer").** calibre's own process died. Books it hadn't reached are retried automatically; if it keeps crashing, the run stops. The full output is in `runs/<id>/plan-*.stderr.log`, which is worth attaching to a bug report. Nothing half-written is left behind, because each book is written in one step and journaled.
- **The file check reports "file missing" after a run with an older version.** Versions before the case-rename guard could lose files for books whose author changed only in capitalisation on a case-insensitive share. The book records and metadata are intact; add the book file back with calibre or Calibre-Web ("Add format" / upload).

## Tests

```bash
python3 -m unittest discover -s tests -v   # parsing, matching and classification rules
python3 tests/e2e_calibre.py               # builds a throwaway calibre library, writes, checks, undoes (needs calibre)
python3 tests/e2e_safety.py                # case-only rename guard, clean stop, crash retry (needs calibre)
python3 tests/e2e_calibre.py --online      # same, including MangaUpdates and a real metadata fetch
```

## Credits

Series data comes from [MangaUpdates](https://www.mangaupdates.com) and [Hardcover](https://hardcover.app). Metadata sources run through [calibre](https://calibre-ebook.com).

## License

Copyright (C) 2026 Michael

CWA Organizer is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE) for the full text.
