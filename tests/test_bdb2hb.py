import sqlite3
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from huckleberry_api.firebase_types import FirebaseBreastFeedIntervalData, FirebaseSleepIntervalData

from bdb2hb import sink, source
from bdb2hb.model import Interval, Nursing, Sleep, SourceKey

NY = ZoneInfo("America/New_York")
H = 3600_000  # one hour in ms
JAN = int(datetime(2024, 1, 10, 13, tzinfo=UTC).timestamp() * 1000)
JUL = int(datetime(2024, 7, 10, 13, tzinfo=UTC).timestamp() * 1000)


def make_db(path: Path, rows: list[tuple], *, extra_cols: str = ", uid TEXT, side TEXT, is_deleted INTEGER") -> Path:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE daily_actions (type TEXT, start_millis INTEGER, end_millis INTEGER, notes TEXT,"
        f" group_uid TEXT, volume REAL{extra_cols})"
    )
    db.execute("CREATE TABLE groups (uid TEXT, daily_action_type TEXT, title TEXT)")
    db.execute("INSERT INTO groups VALUES ('g-nap', 'sleeping', 'Nap')")
    db.executemany(f"INSERT INTO daily_actions VALUES ({','.join('?' * len(rows[0]))})", rows)
    db.commit()
    db.close()
    return path


ROWS = [
    # type, start, end, notes, group_uid, volume, uid, side, is_deleted
    ("sleeping", JAN, JAN + 2 * H, "nap in crib", "g-nap", None, "u1", None, 0),
    ("breastfeeding", JAN + 3 * H, JAN + 3 * H + 900_000, None, None, None, "u2", "right", 0),
    ("breastfeeding", JUL, JUL + 600_000, "", None, None, "u3", None, 0),
    ("sleeping", JUL, 0, None, "g-nap", None, "u4", None, 0),  # in progress
    ("sleeping", JUL, JUL + 30 * H, None, "g-nap", None, "u5", None, 0),  # implausible
    ("breastfeeding", JAN + 3 * H, JAN + 3 * H + 900_000, None, None, None, "u6", "right", 0),  # duplicate
    ("sleeping", JUL + 5 * H, JUL + 6 * H, None, "g-nap", None, "u7", None, 1),  # deleted
    ("diaper", JAN, JAN, None, None, None, "u8", None, 0),  # not imported
]


@pytest.fixture
def backup(tmp_path: Path) -> Path:
    return make_db(tmp_path / "bdb.db", ROWS)


def test_read_backup_maps_rows_and_explains_skips(backup: Path) -> None:
    report = source.read_backup(backup, default_side="left")
    assert [type(e).__name__ for e in report.events] == ["Sleep", "Nursing", "Nursing"]
    sleep, right, left = report.events
    assert isinstance(sleep, Sleep) and sleep.notes == "nap in crib" and sleep.span.seconds == 7200
    assert isinstance(right, Nursing) and (right.left_s, right.right_s, right.last_side) == (0, 900, "right")
    assert isinstance(left, Nursing) and (left.left_s, left.right_s, left.notes) == (600, 0, None)
    reasons = sorted(s.reason for s in report.skipped)
    assert len(reasons) == 4
    assert any("no end" in r for r in reasons)
    assert any("implausible" in r for r in reasons)
    assert any("duplicate" in r for r in reasons)
    assert any("deleted" in r for r in reasons)


def test_reads_zip_and_falls_back_to_type_start_key(tmp_path: Path) -> None:
    db = make_db(tmp_path / "x.db", [r[:6] for r in ROWS[:2]], extra_cols="")
    zpath = tmp_path / "backup.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.write(db, "data/BabyDaybook.db")
        z.writestr("readme.txt", "hi")
    report = source.read_backup(zpath)
    assert report.events[0].key == f"sleeping@{JAN}"
    assert isinstance(report.events[1], Nursing) and report.events[1].last_side == "left"


def test_schema_mismatch_lists_real_columns(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "bad.db")
    db.execute("CREATE TABLE daily_actions (kind TEXT, begins INTEGER)")
    db.execute("CREATE TABLE groups (uid TEXT, daily_action_type TEXT)")
    db.close()
    with pytest.raises(source.SchemaMismatch, match="begins"):
        source.read_backup(tmp_path / "bad.db")


def test_describe_shows_types(backup: Path) -> None:
    out = source.describe(backup)
    assert "daily_actions (8 rows)" in out and "breastfeeding: 3" in out and "sleeping / Nap" in out


def test_doc_ids_are_deterministic_and_marked() -> None:
    start = datetime.fromtimestamp(JAN / 1000, UTC)
    a = sink.doc_id("sleep", SourceKey("uid:u1"), start)
    assert a == sink.doc_id("sleep", SourceKey("uid:u1"), start)
    assert a != sink.doc_id("sleep", SourceKey("uid:u2"), start)
    f = sink.doc_id("feed", SourceKey("uid:u1"), start)
    assert len(a) == 16 and len(f.split("-", 1)[1]) == 20 and f.startswith(str(JAN))
    assert sink.is_imported(a) and sink.is_imported(f)
    # ids the app and library generate never match
    assert not any(sink.is_imported(uuid.uuid4().hex[:16]) for _ in range(5000))
    assert not sink.is_imported(f"{JAN}-{uuid.uuid4().hex[:20]}")


def test_offset_uses_event_time_not_now() -> None:
    assert sink.offset_minutes(datetime.fromtimestamp(JAN / 1000, UTC), NY) == 300
    assert sink.offset_minutes(datetime.fromtimestamp(JUL / 1000, UTC), NY) == 240


def test_payloads_validate_against_library_models(backup: Path) -> None:
    events = source.read_backup(backup).events
    plan = sink.plan_import(events, {}, NY, now=1.0)
    for op in plan.ops:
        model = FirebaseSleepIntervalData if op.kind == "sleep" else FirebaseBreastFeedIntervalData
        model.model_validate(op.payload)
    sleep = plan.ops[0].payload
    assert sleep == {"start": JAN // 1000, "duration": 7200, "offset": 300.0, "end_offset": 300.0,
                     "details": {"notes": "nap in crib"}, "lastUpdated": 1.0}  # fmt: skip
    assert plan.ops[2].payload["offset"] == 240.0


def snapshot_after(plan: sink.Plan) -> dict:
    out: dict = {"sleep": {}, "feed": {}}
    for op in plan.ops:
        out[op.kind][op.doc_id] = dict(op.payload, lastUpdated=99.0)
    return out


def test_rerun_is_unchanged_and_mapping_fix_is_update(backup: Path) -> None:
    events = source.read_backup(backup).events
    first = sink.plan_import(events, {}, NY)
    assert {op.action for op in first.ops} == {"create"}
    existing = snapshot_after(first)
    again = sink.plan_import(events, existing, NY)
    assert {op.action for op in again.ops} == {"unchanged"} and again.pending == ()
    assert again.overlaps == ()  # our own docs never count as overlaps

    fixed = source.read_backup(backup, default_side="right").events
    redo = sink.plan_import(fixed, existing, NY)
    assert [op.action for op in redo.ops] == ["unchanged", "unchanged", "update"]


def test_overlaps_with_manual_entries_are_reported_including_multi_docs(backup: Path) -> None:
    events = source.read_backup(backup).events
    existing = {
        "sleep": {"a1b2c3d4e5f60718": {"start": JAN / 1000 + 1800, "duration": 600, "offset": 300}},
        "feed": {
            "multi1": {"multi": True, "data": {"x": {"mode": "breast", "start": JUL / 1000 + 60,
                                                     "leftDuration": 60, "rightDuration": 0, "offset": 240}}},
            "bottle1": {"mode": "bottle", "start": JAN / 1000 + 3 * 3600, "amount": 90, "offset": 300},
        },
    }  # fmt: skip
    plan = sink.plan_import(events, existing, NY)
    assert sorted(o.kind for o in plan.overlaps) == ["feed", "sleep"]


def test_undo_deletes_only_imported(backup: Path) -> None:
    events = source.read_backup(backup).events
    existing = snapshot_after(sink.plan_import(events, {}, NY))
    existing["sleep"]["a1b2c3d4e5f60718"] = {"start": 1, "duration": 1, "offset": 0}
    undo = sink.plan_undo(existing)
    assert len(undo.ops) == 3 and all(op.action == "delete" for op in undo.ops)
    assert "a1b2c3d4e5f60718" not in {op.doc_id for op in undo.ops}


def test_interval_rejects_open_spans() -> None:
    with pytest.raises(ValueError):
        Interval.of(JAN, 0)


class FakeDoc:
    def __init__(self, store: dict, path: tuple) -> None:
        self.store, self.path = store, path

    def collection(self, name: str) -> "FakeCol":
        return FakeCol(self.store, (*self.path, name))


class FakeCol:
    def __init__(self, store: dict, path: tuple) -> None:
        self.store, self.path = store, path

    def document(self, doc_id: str) -> FakeDoc:
        return FakeDoc(self.store, (*self.path, doc_id))

    async def stream(self):
        for path, data in list(self.store.items()):
            if path[:-1] == self.path:
                yield type("Snap", (), {"id": path[-1], "to_dict": lambda self, d=data: dict(d)})()


class FakeClient:
    def __init__(self) -> None:
        self.store: dict = {}
        self.commits = 0

    def collection(self, name: str) -> FakeCol:
        return FakeCol(self.store, (name,))

    def batch(self):
        client, ops = self, []

        class Batch:
            def set(self, ref, payload):
                ops.append((ref.path, payload))

            def delete(self, ref):
                ops.append((ref.path, None))

            async def commit(self):
                client.commits += 1
                for path, payload in ops:
                    if payload is None:
                        client.store.pop(path, None)
                    else:
                        client.store[path] = payload

        return Batch()


async def test_apply_then_snapshot_converges_and_undo_cleans_up(backup: Path, monkeypatch) -> None:
    client = FakeClient()
    client.store[("sleep", "cid", "intervals", "a1b2c3d4e5f60718")] = {"start": 1, "duration": 1, "offset": 0}
    hb = sink.Huckleberry(api=None, child=sink.Child("cid", "Kid"))  # type: ignore[arg-type]

    async def fake_firestore():
        return client

    monkeypatch.setattr(hb, "_firestore", fake_firestore)
    events = source.read_backup(backup).events
    plan = sink.plan_import(events, await hb.snapshot(), NY)
    assert await hb.apply(plan) == 3
    assert sink.plan_import(events, await hb.snapshot(), NY).pending == ()
    assert await hb.apply(sink.plan_undo(await hb.snapshot())) == 3
    assert list(client.store) == [("sleep", "cid", "intervals", "a1b2c3d4e5f60718")]

    with pytest.raises(AssertionError, match="did not create"):
        await hb.apply(sink.Plan((sink.Op("sleep", "a1b2c3d4e5f60718", "delete", datetime.now(UTC), None),)))
