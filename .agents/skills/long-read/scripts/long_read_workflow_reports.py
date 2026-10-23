#!/usr/bin/env python3
"""Frozen weekly and monthly reliability snapshots for long-read jobs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from html import escape
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from long_read_workflow_store import WorkflowStore, is_verified_test_task


REPORT_TZ = ZoneInfo("Asia/Shanghai")
RELIABILITY_EVENT_TYPES = (
    "manual_intervention", "manual_reconciled", "manual_repair",
    "repair_applied", "root_cause_fixed", "repair_comparison",
)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _epoch(value: datetime) -> float:
    return value.astimezone(timezone.utc).timestamp()


def _previous_month(value: datetime) -> datetime:
    first = value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first - timedelta(days=1)).replace(day=1)


def report_window(period: str, *, now: datetime | None = None) -> tuple[str, datetime, datetime]:
    """Return fixed-period key and [start,end) data bounds for the latest due report."""
    if period not in {"weekly", "monthly"}:
        raise ValueError("period must be weekly or monthly")
    now = (now or datetime.now(REPORT_TZ)).astimezone(REPORT_TZ)
    if period == "weekly":
        days_since_sunday = (now.weekday() + 1) % 7
        run_at = (now - timedelta(days=days_since_sunday)).replace(hour=9, minute=0, second=0, microsecond=0)
        if run_at > now:
            run_at -= timedelta(days=7)
        end = run_at
        start = end - timedelta(days=7)
        return f"weekly:{end.date().isoformat()}", start, end
    run_at = now.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
    if run_at > now:
        run_at = _previous_month(now).replace(day=1, hour=9, minute=0, second=0, microsecond=0)
    end = run_at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = _previous_month(end)
    return f"monthly:{start.strftime('%Y-%m')}", start, end


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = max(0, math.ceil(fraction * len(values)) - 1)
    return round(values[index], 3)


def _report_text(value: object) -> str:
    """Render validated operator text as one safe Markdown line."""
    return (escape(str(value), quote=False).replace("\\", "\\\\")
            .replace("`", "\\`").replace("*", "\\*").replace("_", "\\_")
            .replace("[", "\\[").replace("]", "\\]").replace("|", "\\|"))


def _markdown(snapshot: dict) -> str:
    counts = snapshot["counts"]
    long_term = snapshot["long_term_counts"]
    duration = snapshot["duration_seconds"]
    rate = snapshot["final_failure_rate"]
    rate_text = "—" if rate is None else f"{rate * 100:.1f}%"
    long_term_rate = snapshot["long_term_final_failure_rate"]
    long_term_rate_text = "—" if long_term_rate is None else f"{long_term_rate * 100:.1f}%"
    coverage = "完整" if snapshot["coverage_complete"] else "不完整（仅统计持久工作流上线后数据）"
    trigger_text = "超过 10%（触发周报）" if snapshot["failure_rate_over_10_percent"] else "未超过 10%（周报不触发）"
    long_term_text = (
        "达标" if snapshot["long_term_target_met"] else
        "未达标" if snapshot["long_term_target_eligible"] else
        "暂不判定（需要至少 50 个真实任务、完整覆盖且没有未结项）"
    )
    lines = [
        f"# Read-X 长文精读可靠性报告｜{snapshot['period_label']}",
        "",
        f"统计窗口：{snapshot['window_start_local']} 至 {snapshot['window_end_local']}（左闭右开，Asia/Shanghai）",
        f"数据水位：{snapshot['data_watermark_local']}；覆盖：{coverage}",
        f"报告快照：{snapshot['report_key']}；版本：{snapshot['version']}；摘要：`{snapshot['snapshot_sha256']}`",
        "",
        "## 任务结果",
        "",
        f"- 有效任务 N：{counts['N']}",
        f"- 最终成功 S：{counts['S']}",
        f"- 最终失败 F：{counts['F']}",
        f"- 待核实 U：{counts['U']}",
        f"- 处理中：{counts['processing']}",
        f"- 明确最终失败占比 F/N：{rate_text}",
        "- 本统计月无有效任务。" if snapshot["period_type"] == "monthly" and counts["N"] == 0 else "- 有效任务口径：仅纳入持久接管的真实 long_read 任务。",
        f"- 周报阈值：{trigger_text}；长期 ≤10% 目标仅在 N≥50、覆盖完整且无未结项时判定",
        f"- 长期最终失败率目标：{long_term_text}",
        f"- 长期累计（工作流启用至数据水位）：N {long_term['N']} · S {long_term['S']} · F {long_term['F']} · 未结 {long_term['U'] + long_term['processing']} · F/N {long_term_rate_text}",
        "",
        "## 阶段故障与恢复",
        "",
        f"- 自动恢复尝试计数：{snapshot['retry_attempts']}",
        f"- 至少一次恢复后最终成功的任务：{snapshot['recovered_successes']}",
        f"- 人工介入记录：{snapshot['manual_interventions']}",
        "",
        "### 阶段失败指纹",
        "",
    ]
    if snapshot["failure_fingerprints"]:
        lines.extend(f"- `{item['fingerprint']}`：{item['count']} 次" for item in snapshot["failure_fingerprints"])
    else:
        lines.append("- 本窗口没有已记录的阶段失败。")
    lines.extend([
        "",
        "## 耗时",
        "",
        f"- 任务总耗时：中位数 {duration['median'] if duration['median'] is not None else '—'} 秒；P90 {duration['p90'] if duration['p90'] is not None else '—'} 秒",
        f"- 阶段耗时：{snapshot['stage_duration_coverage']}；可核验样本 {snapshot['stage_duration_samples']} 个",
        "",
        "## 修复与趋势",
        "",
        f"- 已记录修复：{snapshot['repair_records']} 条；修复前后可比证据：{snapshot['repair_comparisons']} 组",
        "- 趋势比较从完整持久化窗口开始；上线前零散日志不补造历史基线。",
    ])
    repairs = [item for item in snapshot.get("repair_evidence", [])
               if item["event_type"] in {"repair_applied", "root_cause_fixed"}]
    comparisons = [item for item in snapshot.get("repair_evidence", [])
                   if item["event_type"] == "repair_comparison"]
    interventions = [item for item in snapshot.get("repair_evidence", [])
                     if item["event_type"] in {"manual_intervention", "manual_reconciled", "manual_repair"}]
    if repairs:
        lines.extend(["", "### 已实施修复", ""])
        lines.extend(
            f"- `{_report_text(item['details']['repair_id'])}` · "
            f"`{_report_text(item['details']['failure_fingerprint'])}`："
            f"{_report_text(item['details']['summary'])}（变更 `{_report_text(item['details']['change_ref'])}`）"
            for item in repairs
        )
    if comparisons:
        lines.extend(["", "### 修复前后对比", ""])
        for item in comparisons:
            details = item["details"]
            metric = details["metric"]
            suffix = " 秒" if metric == "p90_duration_seconds" else "%"
            before = details["before_value"] if suffix == " 秒" else details["before_value"] * 100
            after = details["after_value"] if suffix == " 秒" else details["after_value"] * 100
            lines.append(
                f"- `{_report_text(details['repair_id'])}` · `{metric}`："
                f"{before:g}{suffix} → {after:g}{suffix}；"
                f"窗口 `{details['before_window']}`（n={details['before_sample_size']}）→ "
                f"`{details['after_window']}`（n={details['after_sample_size']}）"
            )
    if interventions:
        lines.extend(["", "### 人工介入明细", ""])
        lines.extend(
            f"- {item['event_type']}（{item['details']['category']}）："
            f"{_report_text(item['details']['summary'])}"
            + (f"（参考 `{_report_text(item['details']['reference'])}`）"
               if item['details'].get("reference") else "")
            for item in interventions
        )
    lines.append("")
    return "\n".join(lines)


def render_report_card(snapshot: dict) -> dict:
    """Build the concise Card 2.0 companion to the complete Markdown report."""
    if snapshot.get("schema") != "read-x-long-read-reliability-report/v1":
        raise ValueError("invalid long-read report snapshot")
    counts = snapshot.get("counts")
    if not isinstance(counts, dict) or any(key not in counts for key in ("N", "S", "F", "U", "processing")):
        raise ValueError("report snapshot is missing result counts")
    rate = snapshot.get("final_failure_rate")
    rate_text = "—" if rate is None else f"{rate * 100:.1f}%"
    period_label = "周报" if snapshot.get("period_type") == "weekly" else "月报"
    body = (
        f"**统计窗口：** {snapshot['period_label']}\n\n"
        f"有效任务 **{counts['N']}** · 成功 **{counts['S']}** · 失败 **{counts['F']}** · "
        f"待核实 **{counts['U']}** · 处理中 **{counts['processing']}**\n\n"
        f"最终失败占比 **{rate_text}**\n\n"
        f"自动恢复 **{snapshot['retry_attempts']}** 次 · 恢复后成功 **{snapshot['recovered_successes']}** · "
        f"人工介入 **{snapshot['manual_interventions']}** · 已记录修复 **{snapshot['repair_records']}** "
        f"（对比 {snapshot['repair_comparisons']} 组）\n\n"
        f"完整 Markdown 报告文件已作为配套消息发送；数据覆盖{'完整' if snapshot['coverage_complete'] else '尚未覆盖完整统计周期'}。"
    )
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "default", "summary": {"content": f"Read-X 长文精读 {period_label}｜{snapshot['period_label']}"}},
        "header": {
            "title": {"tag": "plain_text", "content": f"Read-X 长文精读{period_label}"},
            "subtitle": {"tag": "plain_text", "content": f"{snapshot['period_label']} · F/N {rate_text}"},
            "template": "indigo",
            "icon": {"tag": "standard_icon", "token": "chart_colorful"},
        },
        "body": {"direction": "vertical", "padding": "12px 12px 20px 12px", "elements": [{"tag": "markdown", "content": body}]},
    }


def create_report_snapshot(state_dir: Path, period: str, *, now: datetime | None = None) -> dict:
    report_now = (now or datetime.now(REPORT_TZ)).astimezone(REPORT_TZ)
    report_key, start, end = report_window(period, now=report_now)
    start_epoch, end_epoch = _epoch(start), _epoch(end)
    from long_read_workflow_retention import apply_retention, assert_report_window_retained
    apply_retention(Path(state_dir), now=_epoch(report_now))
    with WorkflowStore(Path(state_dir)) as store:
        connection = store.connection
        prior = connection.execute("SELECT * FROM reports WHERE report_key=?", (report_key,)).fetchone()
        if prior:
            result = dict(prior)
            result["snapshot"] = json.loads(result.pop("snapshot_json"))
            result["snapshot"]["snapshot_sha256"] = result["snapshot_sha256"]
            result["reused"] = True
            return result
        assert_report_window_retained(store, start_epoch)
        tracked_row = connection.execute("SELECT value FROM workflow_meta WHERE key='tracking_started_at'").fetchone()
        tracked_at = float(tracked_row["value"]) if tracked_row else start_epoch
        rows = connection.execute(
            "SELECT job_id,status,created_at,updated_at,payload_json FROM jobs "
            "WHERE created_at>=? AND created_at<? ORDER BY created_at,job_id",
            (tracked_at, end_epoch),
        ).fetchall()
        jobs = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("synthetic") is True or is_verified_test_task(payload):
                continue
            jobs.append({**dict(row), "payload": payload})
        window_jobs = [row for row in jobs if start_epoch <= float(row["created_at"]) < end_epoch]
        job_ids = [row["job_id"] for row in jobs]
        window_job_ids = [row["job_id"] for row in window_jobs]
        statuses_by_job = {row["job_id"]: "pending" for row in jobs}
        terminal_at: dict[str, float] = {}
        artifact_specs: dict[tuple[str, str], dict] = {}
        effect_specs: dict[str, dict] = {}
        event_rows = []
        status_event_jobs = set()
        if job_ids:
            marks = ",".join("?" for _ in job_ids)
            status_rows = connection.execute(
                f"SELECT job_id,details_json,at,seq FROM events "
                f"WHERE job_id IN ({marks}) AND event_type='job_status' AND at<? ORDER BY at,seq",
                (*job_ids, end_epoch),
            ).fetchall()
            status_event_jobs = {row["job_id"] for row in status_rows}
            for row in status_rows:
                state = json.loads(row["details_json"]).get("status")
                if isinstance(state, str):
                    statuses_by_job[row["job_id"]] = state
                    if state in {"succeeded", "failed"}:
                        terminal_at[row["job_id"]] = float(row["at"])
                    else:
                        terminal_at.pop(row["job_id"], None)
            if window_job_ids:
                window_marks = ",".join("?" for _ in window_job_ids)
                event_rows = connection.execute(
                    f"SELECT seq,job_id,event_type,details_json,at FROM events "
                    f"WHERE job_id IN ({window_marks}) AND event_type NOT IN ({','.join('?' for _ in RELIABILITY_EVENT_TYPES)}) "
                    f"AND at>=? AND at<? ORDER BY at,seq",
                    (*window_job_ids, *RELIABILITY_EVENT_TYPES, start_epoch, end_epoch),
                ).fetchall()
            reliability_rows = connection.execute(
                f"SELECT seq,job_id,event_type,details_json,at FROM events "
                f"WHERE job_id IN ({marks}) AND event_type IN ({','.join('?' for _ in RELIABILITY_EVENT_TYPES)}) "
                f"AND at>=? AND at<? ORDER BY at,seq",
                (*job_ids, *RELIABILITY_EVENT_TYPES, start_epoch, end_epoch),
            ).fetchall()
            event_rows.extend(reliability_rows)
            artifacts = connection.execute(
                f"SELECT job_id,artifact_key,kind FROM artifacts WHERE job_id IN ({marks})", job_ids
            ).fetchall()
            artifact_specs = {(row["job_id"], row["artifact_key"]): dict(row) for row in artifacts}
            effects = connection.execute(
                f"SELECT op_key,job_id,artifact_key,action FROM effects WHERE job_id IN ({marks})", job_ids
            ).fetchall()
            effect_specs = {row["op_key"]: dict(row) for row in effects}
        event_rows.sort(key=lambda row: (float(row["at"]), int(row["seq"])))
        decoded_events = []
        for row in event_rows:
            event = dict(row)
            event["details"] = json.loads(event.pop("details_json"))
            decoded_events.append(event)
        # Retain conservative compatibility for old rows without status events.
        for row in jobs:
            job_id = row["job_id"]
            if (job_id not in status_event_jobs and statuses_by_job[job_id] == "pending"
                    and row["status"] != "pending" and float(row["updated_at"]) < end_epoch):
                statuses_by_job[job_id] = row["status"]
                if row["status"] in {"succeeded", "failed"}:
                    terminal_at[job_id] = float(row["updated_at"])
        statuses = [statuses_by_job[row["job_id"]] for row in window_jobs]
        counts = {
            "N": len(window_jobs),
            "S": sum(status == "succeeded" for status in statuses),
            "F": sum(status == "failed" for status in statuses),
            "U": sum(status == "needs_review" for status in statuses),
        }
        counts["processing"] = counts["N"] - counts["S"] - counts["F"] - counts["U"]
        failure_rate = counts["F"] / counts["N"] if counts["N"] else None
        cumulative_statuses = list(statuses_by_job.values())
        archived_row = connection.execute(
            "SELECT value FROM workflow_meta WHERE key='lifetime_archived_counts'"
        ).fetchone()
        archived = json.loads(archived_row["value"]) if archived_row else {"N": 0, "S": 0, "F": 0}
        if (not isinstance(archived, dict)
                or any(type(archived.get(key, 0)) is not int or archived.get(key, 0) < 0
                       for key in ("N", "S", "F"))
                or archived.get("S", 0) + archived.get("F", 0) > archived.get("N", 0)):
            raise ValueError("retained lifetime counters are invalid")
        long_term_counts = {
            "N": int(archived.get("N", 0)) + len(jobs),
            "S": int(archived.get("S", 0)) + sum(status == "succeeded" for status in cumulative_statuses),
            "F": int(archived.get("F", 0)) + sum(status == "failed" for status in cumulative_statuses),
            "U": sum(status == "needs_review" for status in cumulative_statuses),
        }
        long_term_counts["processing"] = (
            long_term_counts["N"] - long_term_counts["S"]
            - long_term_counts["F"] - long_term_counts["U"]
        )
        long_term_failure_rate = (
            long_term_counts["F"] / long_term_counts["N"] if long_term_counts["N"] else None
        )
        fingerprints: dict[str, int] = {}
        failed_effect_artifacts = set()
        stage_ms: list[float] = []
        stage_outcomes = 0
        retry_attempts = 0
        retried_jobs = set()
        for event in decoded_events:
            event_type, job_id, details = event["event_type"], event["job_id"], event["details"]
            if event_type == "image_retry":
                retry_attempts += 1
                retried_jobs.add(job_id)
            if event_type in {"effect_confirmed", "effect_failed", "effect_uncertain"}:
                spec = effect_specs.get(details.get("op_key"))
                metadata = details.get("metadata") if isinstance(details.get("metadata"), dict) else {}
                stage_outcomes += 1
                if event_type == "effect_failed" and spec:
                    failed_effect_artifacts.add((job_id, spec["artifact_key"]))
                    stage = metadata.get("failure_stage") or spec["action"]
                    reason = metadata.get("reason_code") or "unknown"
                    fingerprint = f"{stage}:{reason}"
                    fingerprints[fingerprint] = fingerprints.get(fingerprint, 0) + 1
                duration_ms = metadata.get("duration_ms")
                if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool) and math.isfinite(duration_ms) and duration_ms >= 0:
                    stage_ms.append(float(duration_ms))
            if event_type == "artifact_status" and details.get("status") == "failed":
                artifact_key = details.get("artifact_key")
                if (job_id, artifact_key) in failed_effect_artifacts:
                    continue
                metadata = details.get("metadata") if isinstance(details.get("metadata"), dict) else {}
                kind = artifact_specs.get((job_id, artifact_key), {}).get("kind", "artifact")
                stage = metadata.get("failure_stage") or kind
                reason = metadata.get("reason_code") or "unknown"
                fingerprint = f"{stage}:{reason}"
                fingerprints[fingerprint] = fingerprints.get(fingerprint, 0) + 1
        recovered_successes = sum(statuses_by_job[row["job_id"]] == "succeeded" and row["job_id"] in retried_jobs for row in window_jobs)
        manual_interventions = sum(event["event_type"] in {"manual_intervention", "manual_reconciled", "manual_repair"} for event in decoded_events)
        repair_records = sum(event["event_type"] in {"repair_applied", "root_cause_fixed"} for event in decoded_events)
        repair_comparisons = sum(event["event_type"] == "repair_comparison" for event in decoded_events)
        repair_evidence = [
            {"event_type": event["event_type"], "job_id": event["job_id"],
             "at": float(event["at"]), "details": event["details"]}
            for event in decoded_events
            if event["event_type"] in {
                "manual_intervention", "manual_reconciled", "manual_repair",
                "repair_applied", "root_cause_fixed", "repair_comparison",
            }
        ]
        created_at_by_job = {row["job_id"]: float(row["created_at"]) for row in window_jobs}
        durations = [max(0.0, terminal_at[job_id] - created_at_by_job[job_id])
                     for job_id in terminal_at if job_id in created_at_by_job]
        coverage_complete = start_epoch >= tracked_at
        has_unresolved = counts["U"] + counts["processing"] > 0
        long_term_has_unresolved = long_term_counts["U"] + long_term_counts["processing"] > 0
        long_term_coverage_complete = tracked_row is not None and tracked_at <= end_epoch
        long_term_eligible = (
            long_term_counts["N"] >= 50 and long_term_coverage_complete
            and not long_term_has_unresolved
        )
        snapshot = {
            "schema": "read-x-long-read-reliability-report/v1", "version": 1,
            "report_key": report_key, "period_type": period,
            "period_label": (f"{start:%Y-%m-%d} 至 {(end - timedelta(seconds=1)):%Y-%m-%d}"
                             if period == "weekly" else start.strftime("%Y-%m")),
            "window_start": start_epoch, "window_end_exclusive": end_epoch,
            "window_start_local": start.strftime("%Y-%m-%d %H:%M %Z"),
            "window_end_local": end.strftime("%Y-%m-%d %H:%M %Z"),
            "data_watermark": end_epoch, "data_watermark_local": end.strftime("%Y-%m-%d %H:%M %Z"),
            "tracking_started_at": tracked_at, "coverage_complete": coverage_complete,
            "counts": counts, "final_failure_rate": failure_rate,
            "long_term_counts": long_term_counts,
            "long_term_final_failure_rate": long_term_failure_rate,
            "long_term_coverage_complete": long_term_coverage_complete,
            "long_term_has_unresolved": long_term_has_unresolved,
            "failure_rate_over_10_percent": bool(counts["N"] and failure_rate > 0.10),
            "has_unresolved": has_unresolved,
            "long_term_target_eligible": long_term_eligible,
            "long_term_target_met": bool(long_term_eligible and long_term_failure_rate is not None and long_term_failure_rate <= 0.10),
            "retry_attempts": retry_attempts, "recovered_successes": recovered_successes,
            "manual_interventions": manual_interventions,
            "repair_evidence": repair_evidence,
            "failure_fingerprints": [
                {"fingerprint": key, "count": value}
                for key, value in sorted(fingerprints.items(), key=lambda item: (-item[1], item[0]))[:10]
            ],
            "duration_seconds": {"median": _percentile(durations, 0.5), "p90": _percentile(durations, 0.9), "completed_samples": len(durations)},
            "stage_duration_coverage": (
                "完整记录" if stage_outcomes and len(stage_ms) == stage_outcomes else
                "部分记录" if stage_ms else "暂无可核验阶段耗时"
            ),
            "stage_duration_samples": len(stage_ms),
            "stage_duration_ms": {"median": _percentile(stage_ms, 0.5), "p90": _percentile(stage_ms, 0.9)},
            "repair_records": repair_records, "repair_comparisons": repair_comparisons,
        }
        snapshot_text = _canonical(snapshot)
        snapshot_sha = _sha(snapshot_text)
        snapshot["snapshot_sha256"] = snapshot_sha
        markdown = _markdown(snapshot)
        markdown_sha = _sha(markdown)
        weekly_send = period == "monthly" or (
            counts["N"] > 0 and snapshot["failure_rate_over_10_percent"]
        )
        status = "pending_delivery" if weekly_send else "suppressed_weekly"
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Recheck after taking the write lock so concurrent catch-up runs
            # preserve one immutable snapshot per period.
            prior = connection.execute("SELECT * FROM reports WHERE report_key=?", (report_key,)).fetchone()
            if prior:
                connection.execute("COMMIT")
                result = dict(prior)
                result["snapshot"] = json.loads(result.pop("snapshot_json"))
                result["snapshot"]["snapshot_sha256"] = result["snapshot_sha256"]
                result["reused"] = True
                return result
            connection.execute(
                "INSERT INTO reports(report_key,period_type,window_start,window_end,snapshot_json,snapshot_sha256,markdown,markdown_sha256,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (report_key, period, start_epoch, end_epoch, snapshot_text, snapshot_sha, markdown, markdown_sha, status, store.clock()),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return {
            "report_key": report_key, "period_type": period, "status": status,
            "snapshot": snapshot, "snapshot_sha256": snapshot_sha,
            "markdown": markdown, "markdown_sha256": markdown_sha, "reused": False,
        }

