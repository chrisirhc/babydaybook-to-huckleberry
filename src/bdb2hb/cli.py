"""Thin shell: arguments, environment credentials, printing.

    bdb2hb inspect BACKUP
    bdb2hb import  BACKUP [--child NAME] [--baby ID] [--since DATE] [--limit N] [--default-side left|right] [--write]
    bdb2hb undo    [--child NAME] [--write]

Without --write, import and undo only print the plan. Credentials come from HUCKLEBERRY_EMAIL and
HUCKLEBERRY_PASSWORD, the timezone from HUCKLEBERRY_TZ or --tz (IANA name, e.g. Europe/Berlin).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bdb2hb import sink, source
from bdb2hb.model import Nursing, SourceReport

_SAMPLES = 5


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "inspect":
            print(source.describe(args.backup))
            return 0
        return asyncio.run(_run(args))
    except (source.SchemaMismatch, sink.ChildNotFound, FileNotFoundError, _UsageError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


class _UsageError(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bdb2hb", description="Import Baby Daybook sleep and nursing into Huckleberry.")
    sub = p.add_subparsers(dest="cmd", required=True)

    ins = sub.add_parser("inspect", help="show tables, columns and type counts of a backup (no network)")
    ins.add_argument("backup", type=Path)

    imp = sub.add_parser("import", help="plan (default) or --write an import")
    imp.add_argument("backup", type=Path)
    imp.add_argument("--baby", help="Baby Daybook baby id, for multi-child backups")
    imp.add_argument("--since", type=date.fromisoformat, help="only events starting on or after this local date")
    imp.add_argument("--until", type=date.fromisoformat, help="only events starting before this local date")
    imp.add_argument("--limit", type=int, help="write at most N pending ops (to try a few first)")
    imp.add_argument("--default-side", choices=("left", "right"), default="left")

    und = sub.add_parser("undo", help="plan (default) or --write deletion of everything bdb2hb imported")

    for s in (imp, und):
        s.add_argument("--child", help="Huckleberry child name or id (optional with one child)")
        s.add_argument("--tz", help="IANA timezone (default: $HUCKLEBERRY_TZ)")
        s.add_argument("--write", action="store_true", help="actually write; without it, only print the plan")
    return p


async def _run(args: argparse.Namespace) -> int:
    email, password = _env("HUCKLEBERRY_EMAIL"), _env("HUCKLEBERRY_PASSWORD")
    tz = _tz(args.tz or os.environ.get("HUCKLEBERRY_TZ"))

    report: SourceReport | None = None
    if args.cmd == "import":
        report = source.read_backup(args.backup, baby=args.baby, default_side=args.default_side)
        report = _window(report, tz, args.since, args.until)
        _print_source(report, tz)

    async with sink.Huckleberry.connect(email=email, password=password, tz=tz, child=args.child) as hb:
        print(f"\nHuckleberry child: {hb.child.name}")
        existing = await hb.snapshot()
        plan = sink.plan_import(report.events, existing, tz) if report else sink.plan_undo(existing)
        if getattr(args, "limit", None) is not None:
            plan = sink.Plan(plan.pending[: args.limit], plan.overlaps)
        _print_plan(plan, tz)
        if not args.write:
            print("\nDry run. Nothing was written. Add --write to apply.")
            return 0
        n = await hb.apply(plan)
        print(f"\nWrote {n} document(s).")

        # Read back and re-plan to confirm the write landed.
        existing = await hb.snapshot()
        if report:
            check = sink.plan_import(report.events, existing, tz)
            left = len(check.pending)
            print(f"Verify: {left} op(s) still pending" + (" (expected with --limit)" if args.limit else ""))
        else:
            left = len(sink.plan_undo(existing).ops)
            print(f"Verify: {left} imported document(s) remain")
    return 0


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise _UsageError(f"set {name}")
    return value


def _tz(name: str | None) -> ZoneInfo:
    if not name:
        raise _UsageError("set HUCKLEBERRY_TZ or pass --tz (an IANA name such as Europe/Berlin)")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise _UsageError(f"unknown timezone {name!r}") from None


def _window(report: SourceReport, tz: ZoneInfo, since: date | None, until: date | None) -> SourceReport:
    def keep(start: datetime) -> bool:
        d = start.astimezone(tz).date()
        return (since is None or d >= since) and (until is None or d < until)

    return SourceReport(tuple(e for e in report.events if keep(e.span.start)), report.skipped)


def _local(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _print_source(report: SourceReport, tz: ZoneInfo) -> None:
    kinds = Counter("nursing" if isinstance(e, Nursing) else "sleep" for e in report.events)
    print("Baby Daybook backup:")
    for kind, n in sorted(kinds.items()):
        print(f"  {kind}: {n}")
    if report.events:
        print(f"  range: {_local(report.events[0].span.start, tz)} .. {_local(report.events[-1].span.start, tz)}")
    if report.skipped:
        print(f"  skipped: {len(report.skipped)}")
        for reason, n in Counter(f"{s.kind}: {s.reason}" for s in report.skipped).most_common():
            print(f"    {n:5d}  {reason}")


def _print_plan(plan: sink.Plan, tz: ZoneInfo) -> None:
    print("Plan:")
    counts = plan.counts()
    if not counts:
        print("  nothing to do")
    for (kind, action), n in sorted(counts.items()):
        print(f"  {kind:5s} {action:9s} {n}")
    for action in ("create", "update", "delete"):
        sample = [op for op in plan.ops if op.action == action][:_SAMPLES]
        if sample:
            print(f"  sample {action}:")
            for op in sample:
                print(f"    {op.kind:5s} {_local(op.start, tz)}  {_describe(op)}")
    if plan.overlaps:
        print(f"\n  {len(plan.overlaps)} imported event(s) overlap entries already in Huckleberry (not touched):")
        for o in plan.overlaps[:_SAMPLES]:
            mins = o.theirs_seconds / 60
            print(f"    {o.kind:5s} ours {_local(o.ours_start, tz)}  theirs {_local(o.theirs_start, tz)} ({mins:.0f} min)")


def _describe(op: sink.Op) -> str:
    p = op.payload or {}
    if op.kind == "sleep" and "duration" in p:
        return f"{p['duration'] / 60:.0f} min"
    if op.kind == "feed" and "leftDuration" in p:
        return f"L {p['leftDuration'] / 60:.0f} min, R {p['rightDuration'] / 60:.0f} min"
    return ""


if __name__ == "__main__":
    raise SystemExit(main())

