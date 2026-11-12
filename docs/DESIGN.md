# Design

## Problem

The tool moves completed sleep and breastfeeding history from a Baby Daybook SQLite backup into Huckleberry, and it has to be safe to run again. The expected loop is to run it, look at the app, fix the mapping and run it again. Two facts about the `huckleberry-api` library (0.4.11) rule out calling `log_sleep` or `log_nursing` in a loop:

- Both write each entry under a random document id, so every re-run duplicates history.
- Both take the timezone offset from *now*, so entries on the other side of a DST change are stored one hour off.

The Baby Daybook schema is known only from a reference repo from about 2014 (dstran/BabyDayBook), so all schema knowledge must sit in one place and fail loudly when it doesn't match.

## Usage

See README.md. The CLI is the whole public surface. Internally it calls:

```python
report = source.read_backup(path, baby=..., default_side=...)
async with sink.Huckleberry.connect(email=..., password=..., tz=tz, child=...) as hb:
    plan = sink.plan_import(report.events, await hb.snapshot(), tz)   # or sink.plan_undo(...)
    await hb.apply(plan)
```

## Shape

| module | owns |
|---|---|
| `model.py` | Pure domain types: `Interval` (UTC, end > start), `Sleep`, `Nursing`, `Skip`, `SourceReport`, `SourceKey` |
| `source.py` | All Baby Daybook schema knowledge (`_SCHEMA`), with a PRAGMA check before any row is read, `.db` and `.zip` input, and row-to-event-or-`Skip` mapping |
| `sink.py` | All Huckleberry knowledge: document ids, per-event offsets, payloads built with the library's pydantic models, pure `plan_import` / `plan_undo`, and the I/O class with `snapshot` and `apply` |
| `cli.py` | Arguments, env credentials, printing |

Load-bearing decisions:

- **Document id = hash(kind, SourceKey)** with a `bdb2hb` marker. `SourceKey` is the Baby Daybook row uid, or `type@start_millis` when the schema has no uid column. A full `set()` is then an idempotent upsert, so a crash part-way converges on re-run, and a mapping fix updates documents in place.
- **The marker contains a non-hex character (`h`)**, so no app- or library-generated hex id can match it. `apply` refuses any op whose id does not match. The tool cannot modify or delete an entry it did not create.
- **Planning is pure, and the dry run is exact.** `snapshot()` reads every interval document of the child, and read errors raise rather than return partial results. The plan labels each event create, update or unchanged, ignoring `lastUpdated`. It also reports overlaps with entries made by hand, including those inside the app's `multi` container docs, but never touches them.
- **Offsets are computed at each event's own start and end** in the configured IANA zone, with the library's sign convention (minutes west of UTC).
- **`prefs.lastSleep` / `lastNursing` are never written.** History is older than live data, and leaving prefs alone keeps undo exact.

## Synthesis decision

Two candidates were designed. Both ran on Opus. The intended second runner model (grok) is not available in this harness, and no cross-family judge was available, so the base was picked without one.

- **Base: candidate 2.** It writes with deterministic ids directly through the library's Firestore client. That gives idempotent re-runs, an exact diff, a cheap undo and correct DST offsets, all behind one `sink` interface.
- **Grafted from candidate 1 (public API plus time-based diff):**
  - skipping implausible durations;
  - collapsing duplicate source rows with the same start;
  - `--limit N` for a trial write;
  - reading back after `--write` to confirm the plan is empty;
  - treating a failed read as fatal before any write. Here that comes from streaming Firestore directly instead of from the library's `list_*` methods, which log and swallow errors.
- **Rejected from candidate 1:** time-tolerance matching as identity, and logging in once per UTC offset to work around the DST bug. The public API also has no update or delete, so a bad first import could not be cleaned up.
- **Changed from candidate 2:**
  - prefs are never written (it updated them once per run);
  - one full `snapshot()` replaces batched `get_all` plus a separate `list_documents`;
  - the marker changed from `bdb`, whose letters are all hex digits, to `bdb2hb`.

## Tradeoffs accepted

- We depend on the private `_get_firestore_client` and on undocumented document shapes, in exchange for idempotency, correct offsets and undo. The library version is pinned exactly, and the coupling is one method in `sink.py`.
- A re-run overwrites edits made in the app to imported entries, in exchange for the fix-and-re-run workflow. The dry run shows these as `update`.
- Changing `_source_key` re-keys everything (run `undo --write` first), in exchange for ids that don't depend on the mapping.
- One timezone covers all of history. Travel periods would be stored with the home offset.
- `snapshot()` reads every interval document of the child, which is thousands at most.

## Alternatives considered

- **Public `log_*` plus diffing on time with a tolerance.** Its identity is fuzzy, it cannot update or delete, and DST stays wrong without hacks. It hides less complexity behind a similar surface.
- **A local ledger that maps rows to random ids.** It is a second source of truth that can drift, and losing the file means duplicates.
- **Patching the library upstream so `log_*` accepts an id and uses the event-time offset.** This is the right long-term fix. It would remove `_firestore()` without changing the `sink` interface.

## Open questions and risks

- Does the app show interval docs with our id shape and without an `_id` field? Run `--limit 2 --write` and check before the bulk import.
- How does current Baby Daybook store breastfeeding sides and pauses? Run `inspect` on a real backup and adjust `_SCHEMA` and `_parse_row`.
- Should the sleep group title (Nap / Nighttime) go into notes?

## Next implementation step

Run `bdb2hb inspect` on a real backup and adjust `_SCHEMA` to match it.
