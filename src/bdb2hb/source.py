"""Baby Daybook backup reader. The only module that knows the Baby Daybook schema.

The schema comes from a ~2014 reference (github.com/dstran/BabyDayBook) and has not been
checked against a current backup. Every assumption lives in `_SCHEMA`. `read_backup` checks it
against PRAGMA table_info before reading rows, so a mismatch fails with the real columns listed.
Run `bdb2hb inspect BACKUP` first and edit `_SCHEMA` to match.
"""

from __future__ import annotations

import sqlite3
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from bdb2hb.model import Event, Interval, Nursing, Side, Skip, Sleep, SourceKey, SourceReport

_SQLITE_MAGIC = b"SQLite format 3\x00"


class SchemaMismatch(Exception):
    """The backup does not match `_SCHEMA`. The message names what is expected and what exists."""


@dataclass(frozen=True, slots=True)
class _Schema:
    actions_table: str = "daily_actions"
    groups_table: str = "groups"
    required_action_cols: frozenset[str] = frozenset({"type", "start_millis", "end_millis", "notes", "group_uid"})
    required_group_cols: frozenset[str] = frozenset({"uid", "daily_action_type"})
    # Optional columns: used when present, with a documented fallback when absent.
    uid_col: str = "uid"  # row identity; fallback "{type}@{start_millis}"
    baby_col: str = "baby_uid"  # filters a multi-child backup
    deleted_col: str = "is_deleted"  # soft-deleted rows are skipped
    side_col: str = "side"  # breastfeeding side; fallback --default-side
    sleep_types: frozenset[str] = frozenset({"sleeping"})
    nursing_types: frozenset[str] = frozenset({"breastfeeding"})
    max_sleep_s: float = 24 * 3600
    max_nursing_s: float = 3 * 3600


_SCHEMA = _Schema()


@contextmanager
def open_backup(path: Path) -> Iterator[sqlite3.Connection]:
    """Read-only connection to a raw SQLite file, or to the single SQLite member of a zip."""
    if not path.exists():
        raise FileNotFoundError(path)
    with tempfile.TemporaryDirectory() as tmp:
        db_path = path
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                members = [m for m in zf.infolist() if not m.is_dir() and zf.read(m.filename)[:16] == _SQLITE_MAGIC]
                if len(members) != 1:
                    names = ", ".join(m.filename for m in zf.infolist())
                    raise SchemaMismatch(f"expected exactly one SQLite file in the zip, found {len(members)}: {names}")
                db_path = Path(zf.extract(members[0], tmp))
        with open(db_path, "rb") as f:
            if f.read(16) != _SQLITE_MAGIC:
                raise SchemaMismatch(f"{path} is not a SQLite database or a zip containing one")
        db = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()


def _columns(db: sqlite3.Connection) -> dict[str, list[str]]:
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {t: [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')] for t in tables}


def describe(path: Path) -> str:
    """Tables, columns and row counts, plus per-type counts of the actions table. Backs `inspect`."""
    lines: list[str] = []
    with open_backup(path) as db:
        cols = _columns(db)
        for table, names in cols.items():
            n = db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            lines.append(f"{table} ({n} rows): {', '.join(names)}")
        s = _SCHEMA
        if s.actions_table in cols and "type" in cols[s.actions_table]:
            lines.append("")
            lines.append(f"{s.actions_table}.type counts:")
            for row in db.execute(f'SELECT type, count(*) FROM "{s.actions_table}" GROUP BY type ORDER BY 2 DESC'):
                lines.append(f"  {row[0]}: {row[1]}")
        if s.groups_table in cols and {"daily_action_type", "title"} <= set(cols[s.groups_table]):
            lines.append("")
            lines.append(f"{s.groups_table} (daily_action_type / title):")
            for row in db.execute(f'SELECT daily_action_type, title FROM "{s.groups_table}" ORDER BY 1, 2'):
                lines.append(f"  {row[0]} / {row[1]}")
    return "\n".join(lines)


def _check_schema(cols: dict[str, list[str]]) -> frozenset[str]:
    """Return the actions table's columns, or raise SchemaMismatch."""
    s = _SCHEMA
    for table, required in ((s.actions_table, s.required_action_cols), (s.groups_table, s.required_group_cols)):
        if table not in cols:
            raise SchemaMismatch(f"table {table!r} not found; tables are: {', '.join(cols)}. Edit _SCHEMA in source.py")
        missing = required - set(cols[table])
        if missing:
            raise SchemaMismatch(
                f"{table} is missing {sorted(missing)}; it has {cols[table]}. Edit _SCHEMA in source.py"
            )
    return frozenset(cols[s.actions_table])


def read_backup(path: Path, *, baby: str | None = None, default_side: Side = "left") -> SourceReport:
    """Parse sleep and nursing rows into events. Bad rows become `Skip`s, never exceptions."""
    s = _SCHEMA
    with open_backup(path) as db:
        cols = _check_schema(_columns(db))
        where, params = "", []
        if s.baby_col in cols:
            babies = [r[0] for r in db.execute(f'SELECT DISTINCT "{s.baby_col}" FROM "{s.actions_table}"')]
            if baby is None and len(babies) > 1:
                raise SchemaMismatch(f"backup has several babies, pass --baby with one of: {babies}")
            if baby is not None:
                if baby not in babies:
                    raise SchemaMismatch(f"--baby {baby!r} not found; babies are: {babies}")
                where, params = f' AND a."{s.baby_col}" = ?', [baby]
        types = sorted(s.sleep_types | s.nursing_types)
        marks = ",".join("?" * len(types))
        rows = db.execute(
            f'SELECT a.*, g.daily_action_type AS g_type FROM "{s.actions_table}" a '
            f'LEFT JOIN "{s.groups_table}" g ON a.group_uid = g.uid '
            f"WHERE (a.type IN ({marks}) OR g.daily_action_type IN ({marks})){where} "
            f"ORDER BY a.start_millis",
            [*types, *types, *params],
        ).fetchall()

    events: list[Event] = []
    skipped: list[Skip] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        parsed = _parse_row(row, cols, default_side)
        if isinstance(parsed, Skip):
            skipped.append(parsed)
            continue
        kind = "sleep" if isinstance(parsed, Sleep) else "nursing"
        start = int(parsed.span.start.timestamp() * 1000)
        if (kind, start) in seen:
            skipped.append(Skip(parsed.key, kind, "duplicate of another row with the same start"))
            continue
        seen.add((kind, start))
        events.append(parsed)
    return SourceReport(tuple(events), tuple(skipped))


def _source_key(row: sqlite3.Row, cols: frozenset[str]) -> SourceKey:
    """Stable row identity: the app's uid column if the schema has one, else type and start.

    The choice depends on the schema, never on the row. Changing this function re-keys every
    imported document, so run `bdb2hb undo --write` before importing with a new key scheme.
    """
    if _SCHEMA.uid_col in cols and row[_SCHEMA.uid_col] is not None:
        return SourceKey(f"uid:{row[_SCHEMA.uid_col]}")
    return SourceKey(f"{row['type']}@{row['start_millis']}")


def _parse_row(row: sqlite3.Row, cols: frozenset[str], default_side: Side) -> Event | Skip:
    s = _SCHEMA
    key = _source_key(row, cols)
    raw_type = row["type"] or row["g_type"] or ""
    is_sleep = raw_type in s.sleep_types or row["g_type"] in s.sleep_types
    if s.deleted_col in cols and row[s.deleted_col]:
        return Skip(key, raw_type, "deleted in Baby Daybook")
    try:
        span = Interval.of(int(row["start_millis"] or 0), int(row["end_millis"] or 0))
    except ValueError as e:
        return Skip(key, raw_type, str(e))
    notes = (row["notes"] or "").strip() or None
    if is_sleep:
        if span.seconds > s.max_sleep_s:
            return Skip(key, raw_type, f"implausible sleep of {span.seconds / 3600:.1f} h")
        return Sleep(key, span, notes)
    if span.seconds > s.max_nursing_s:
        return Skip(key, raw_type, f"implausible nursing of {span.seconds / 3600:.1f} h")
    side = default_side
    if s.side_col in cols and row[s.side_col] is not None:
        value = str(row[s.side_col]).lower()
        if "right" in value or value == "r":
            side = "right"
        elif "left" in value or value == "l":
            side = "left"
    seconds = span.seconds
    return Nursing(
        key,
        span,
        left_s=seconds if side == "left" else 0.0,
        right_s=seconds if side == "right" else 0.0,
        last_side=side,
        notes=notes,
    )
