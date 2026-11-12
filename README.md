# bdb2hb

Import Baby Daybook sleep and breastfeeding history into Huckleberry. Safe to re-run, and it can be undone.

```sh
export HUCKLEBERRY_EMAIL=... HUCKLEBERRY_PASSWORD=... HUCKLEBERRY_TZ=America/New_York

uv run bdb2hb inspect backup.db                    # tables, columns, type counts (no network)
uv run bdb2hb import backup.db                     # dry run: counts, skips, create/update/unchanged, overlaps
uv run bdb2hb import backup.db --limit 2 --write   # try two entries, check them in the app
uv run bdb2hb import backup.db --write             # everything; re-running is a no-op
uv run bdb2hb undo                                 # dry run of deleting everything bdb2hb wrote
uv run bdb2hb undo --write
```

Options: `--child NAME` (Huckleberry child, needed when there are several), `--baby ID` (Baby Daybook baby in a multi-child backup), `--since/--until YYYY-MM-DD`, `--default-side left|right` (for nursing rows with no side), `--include-overlaps`, `--show-overlaps`, `--tz`.

The backup can be the raw SQLite file or a zip containing it. The schema the tool expects is listed in `_SCHEMA` in `src/bdb2hb/source.py`. If `import` reports a schema mismatch, compare it with `inspect` output and edit that one place.

Notes:

- Only email/password Huckleberry accounts can log in, because the library uses Firebase email auth.
- Every document the tool writes has an id containing `bdb2hb`. Re-runs overwrite those documents in place, and `undo` deletes only them. Entries you logged in Huckleberry are never modified. If an imported event overlaps one of the same type (sleep with sleep, breastfeeding with breastfeeding), the Huckleberry entry wins: the event is skipped, and a copy imported earlier is deleted. `--include-overlaps` imports them anyway, and `--show-overlaps` lists them all.
- The tool writes to Huckleberry's Firestore through a private method of `huckleberry-api`, which is why that dependency is pinned exactly. See `docs/DESIGN.md`.

Tests: `uv run pytest`
