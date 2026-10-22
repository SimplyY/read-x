#!/usr/bin/env python3
"""Catch up due weekly and monthly report periods in a deterministic order."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import time

from long_read_workflow_report_delivery import deliver_report
from long_read_workflow_reports import REPORT_TZ, create_report_snapshot, report_window
from long_read_workflow_store import WorkflowStore


def due_report_runs(state_dir: Path, *, now: datetime | None = None) -> list[tuple[str, datetime]]:
    now = (now or datetime.now(REPORT_TZ)).astimezone(REPORT_TZ)
    with WorkflowStore(Path(state_dir)) as store:
        row = store.connection.execute("SELECT value FROM workflow_meta WHERE key='tracking_started_at'").fetchone()
        tracking_started = float(row["value"]) if row else now.timestamp()
        floor_row = store.connection.execute(
            "SELECT value FROM workflow_meta WHERE key='report_retention_floor'"
        ).fetchone()
        retention_floor = float(floor_row["value"]) if floor_row else None
        retained_pending = {
            row["report_key"]: row["status"]
            for row in store.connection.execute("SELECT report_key,status FROM reports")
        }
    first_date = datetime.fromtimestamp(tracking_started, REPORT_TZ).date()
    last_date = now.date()
    candidates: list[tuple[str, datetime]] = []
    cursor = first_date
    while cursor <= last_date:
        if cursor.weekday() == 6:
            due_at = datetime(cursor.year, cursor.month, cursor.day, 9, tzinfo=REPORT_TZ)
            if tracking_started <= due_at.timestamp() <= now.timestamp():
                candidates.append(("weekly", due_at))
        if cursor.day == 1:
            due_at = datetime(cursor.year, cursor.month, cursor.day, 9, tzinfo=REPORT_TZ)
            if tracking_started <= due_at.timestamp() <= now.timestamp():
                candidates.append(("monthly", due_at))
        cursor += timedelta(days=1)
    # A month boundary that is also Sunday sends the required monthly report first.
    candidates.sort(key=lambda item: (item[1], 0 if item[0] == "monthly" else 1))
    if retention_floor is None:
        return candidates
    result = []
    for period, due_at in candidates:
        report_key, start, _ = report_window(period, now=due_at)
        old_snapshot = retained_pending.get(report_key)
        if start.timestamp() < retention_floor and old_snapshot not in {"pending_delivery"}:
            continue
        result.append((period, due_at))
    return result


def run_due_reports(state_dir: Path, *, now: datetime | None = None,
                    delivery_target: dict | None = None, client=None,
                    clock=time.time) -> dict:
    """Create every due immutable snapshot; optionally deliver each in order."""
    from long_read_workflow_retention import apply_retention
    retention_now = (now or datetime.now(REPORT_TZ)).astimezone(REPORT_TZ)
    apply_retention(Path(state_dir), now=retention_now.timestamp())
    runs = due_report_runs(state_dir, now=retention_now)
    results = []
    for period, due_at in runs:
        report = create_report_snapshot(Path(state_dir), period, now=due_at)
        item = {"period": period, "due_at": due_at.isoformat(), "report_key": report["report_key"],
                "status": report["status"], "reused": report.get("reused", False)}
        if report["status"] == "pending_delivery" and delivery_target is not None:
            item["delivery"] = deliver_report(
                state_dir=Path(state_dir), report_key=report["report_key"], client=client,
                clock=clock, **delivery_target,
            )
        results.append(item)
        if (delivery_target is not None and report["status"] == "pending_delivery"
                and item.get("delivery", {}).get("status") != "delivered"):
            # Catch-up periods are strictly serialized. An unresolved monthly
            # result blocks a same-day weekly message until it is reconciled.
            break
    return {"status": "checked", "due_count": len(results), "reports": results,
            "delivery_enabled": delivery_target is not None}
