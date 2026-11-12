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

Checks: `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run ty check`

## Credits

- [dstran/BabyDayBook](https://github.com/dstran/BabyDayBook): a Flask site that reads the Baby Daybook Android backup. Its SQL queries were our starting point for the backup schema (`daily_actions` and `groups`, millisecond timestamps, `breastfeeding` and `sleeping` types). The current schema was then confirmed against a real backup.
- [Woyken/py-huckleberry-api](https://github.com/Woyken/py-huckleberry-api) ([PyPI](https://pypi.org/project/huckleberry-api/)): an unofficial, reverse-engineered Python client for Huckleberry's Firebase backend. bdb2hb uses it to log in, and it uses its pydantic models for the shape of the sleep and feed interval documents. We also read how its `log_sleep` and `log_nursing` work to see why we needed fixed document ids and offsets computed per event.
