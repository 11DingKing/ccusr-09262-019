"""仓储：Store 封装全部 SQL，连接由调用方（服务层）提供。"""
from __future__ import annotations

import json
import sqlite3

from ..domain.calendar import (
    InstitutionCalendar,
    windows_from_jsonable,
    windows_to_jsonable,
)
from ..domain.models import (
    CaseState,
    ConversionRule,
    Escalation,
    EvidenceSource,
    Grant,
    ImportBatch,
    Indicator,
    IndicatorVersion,
    Observation,
    PauseInterval,
    Report,
    ReportStatus,
    ReviewCase,
    RuleStatus,
    TaskStatus,
    ComputationTask,
)


class Store:
    """面向聚合的 SQL 仓储。所有方法都不开启事务，由调用方控制。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---- 指标定义 ----
    def add_indicator(self, ind: Indicator) -> None:
        self.conn.execute(
            "INSERT INTO indicators (code, name, category, unit, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (ind.code, ind.name, ind.category, ind.unit, ind.created_at),
        )

    def get_indicator(self, code: str) -> Indicator | None:
        row = self.conn.execute(
            "SELECT * FROM indicators WHERE code = ?", (code,)
        ).fetchone()
        return Indicator(**dict(row)) if row else None

    def list_indicators(self) -> list[Indicator]:
        rows = self.conn.execute("SELECT * FROM indicators ORDER BY code").fetchall()
        return [Indicator(**dict(r)) for r in rows]

    def add_indicator_version(self, ver: IndicatorVersion) -> None:
        self.conn.execute(
            "INSERT INTO indicator_versions"
            " (code, version_no, formula_json, missing_policy, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (ver.code, ver.version_no, json.dumps(ver.formula, sort_keys=True),
             ver.missing_policy.value, ver.created_at),
        )

    def get_indicator_version(self, code: str, version_no: int) -> IndicatorVersion | None:
        row = self.conn.execute(
            "SELECT * FROM indicator_versions WHERE code = ? AND version_no = ?",
            (code, version_no),
        ).fetchone()
        return self._to_indicator_version(row) if row else None

    def latest_indicator_version(self, code: str) -> IndicatorVersion | None:
        row = self.conn.execute(
            "SELECT * FROM indicator_versions WHERE code = ?"
            " ORDER BY version_no DESC LIMIT 1",
            (code,),
        ).fetchone()
        return self._to_indicator_version(row) if row else None

    def list_indicator_versions(self, code: str) -> list[IndicatorVersion]:
        rows = self.conn.execute(
            "SELECT * FROM indicator_versions WHERE code = ? ORDER BY version_no",
            (code,),
        ).fetchall()
        return [self._to_indicator_version(r) for r in rows]

    @staticmethod
    def _to_indicator_version(row: sqlite3.Row) -> IndicatorVersion:
        from ..domain.models import MissingPolicy

        return IndicatorVersion(
            code=row["code"],
            version_no=row["version_no"],
            formula=json.loads(row["formula_json"]),
            missing_policy=MissingPolicy(row["missing_policy"]),
            created_at=row["created_at"],
        )

    # ---- 证据来源 ----
    def add_evidence(self, ev: EvidenceSource) -> None:
        self.conn.execute(
            "INSERT INTO evidence (id, project_id, kind, uri, sha256,"
            " registered_by, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ev.id, ev.project_id, ev.kind, ev.uri, ev.sha256,
             ev.registered_by, ev.registered_at),
        )

    def get_evidence(self, evidence_id: str) -> EvidenceSource | None:
        row = self.conn.execute(
            "SELECT * FROM evidence WHERE id = ?", (evidence_id,)
        ).fetchone()
        return EvidenceSource(**dict(row)) if row else None

    def evidence_by_ids(self, ids: list[str]) -> list[EvidenceSource]:
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT * FROM evidence WHERE id IN ({marks}) ORDER BY id", ids
        ).fetchall()
        return [EvidenceSource(**dict(r)) for r in rows]

    # ---- 数据版本与观测 ----
    def latest_batch_seq(self, project_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(seq) AS seq FROM import_batches WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return row["seq"] if row and row["seq"] is not None else None

    def add_batch(self, batch: ImportBatch) -> None:
        self.conn.execute(
            "INSERT INTO import_batches (id, project_id, seq, reason,"
            " created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (batch.id, batch.project_id, batch.seq, batch.reason,
             batch.created_by, batch.created_at),
        )

    def add_observation(self, obs: Observation) -> None:
        self.conn.execute(
            "INSERT INTO observations (batch_id, project_id, measure, period,"
            " caliber, value, retracted, evidence_id, institution_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (obs.batch_id, obs.project_id, obs.measure, obs.period, obs.caliber,
             obs.value, 1 if obs.retracted else 0, obs.evidence_id,
             obs.institution_id, obs.created_at),
        )

    def snapshot(self, project_id: str, seq: int) -> list[Observation]:
        """重放指定数据版本的有效观测（同一自然键取最高批次）。"""
        rows = self.conn.execute(
            """
            SELECT o.* FROM observations o
            JOIN import_batches b ON b.id = o.batch_id
            WHERE o.project_id = ? AND b.seq <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM observations o2
                  JOIN import_batches b2 ON b2.id = o2.batch_id
                  WHERE o2.project_id = o.project_id
                    AND o2.measure = o.measure AND o2.period = o.period
                    AND o2.caliber = o.caliber
                    AND b2.seq <= ? AND b2.seq > b.seq
              )
            ORDER BY o.measure, o.period, o.caliber
            """,
            (project_id, seq, seq),
        ).fetchall()
        return [self._to_observation(r) for r in rows]

    @staticmethod
    def _to_observation(row: sqlite3.Row) -> Observation:
        return Observation(
            batch_id=row["batch_id"],
            project_id=row["project_id"],
            measure=row["measure"],
            period=row["period"],
            caliber=row["caliber"],
            value=row["value"],
            retracted=bool(row["retracted"]),
            evidence_id=row["evidence_id"],
            institution_id=row["institution_id"],
            created_at=row["created_at"],
        )

    # ---- 换算规则与会签 ----
    def next_rule_version(self, rule_key: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(version_no) AS v FROM conversion_rules WHERE rule_key = ?",
            (rule_key,),
        ).fetchone()
        return (row["v"] or 0) + 1 if row else 1

    def add_rule(self, rule: ConversionRule) -> None:
        self.conn.execute(
            "INSERT INTO conversion_rules (id, rule_key, version_no, measure,"
            " from_caliber, to_caliber, factor, offset, status,"
            " required_signatories_json, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rule.id, rule.rule_key, rule.version_no, rule.measure,
             rule.from_caliber, rule.to_caliber, rule.factor, rule.offset,
             rule.status.value, json.dumps(list(rule.required_signatories)),
             rule.created_by, rule.created_at),
        )

    def get_rule(self, rule_id: str) -> ConversionRule | None:
        row = self.conn.execute(
            "SELECT * FROM conversion_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        return self._to_rule(row) if row else None

    def get_rule_by_version(self, rule_key: str, version_no: int) -> ConversionRule | None:
        row = self.conn.execute(
            "SELECT * FROM conversion_rules WHERE rule_key = ? AND version_no = ?",
            (rule_key, version_no),
        ).fetchone()
        return self._to_rule(row) if row else None

    def list_rule_versions(self, rule_key: str) -> list[ConversionRule]:
        rows = self.conn.execute(
            "SELECT * FROM conversion_rules WHERE rule_key = ? ORDER BY version_no",
            (rule_key,),
        ).fetchall()
        return [self._to_rule(r) for r in rows]

    def list_rules(self) -> list[ConversionRule]:
        rows = self.conn.execute(
            "SELECT * FROM conversion_rules ORDER BY rule_key, version_no"
        ).fetchall()
        return [self._to_rule(r) for r in rows]

    @staticmethod
    def _to_rule(row: sqlite3.Row) -> ConversionRule:
        return ConversionRule(
            id=row["id"],
            rule_key=row["rule_key"],
            version_no=row["version_no"],
            measure=row["measure"],
            from_caliber=row["from_caliber"],
            to_caliber=row["to_caliber"],
            factor=row["factor"],
            offset=row["offset"],
            status=RuleStatus(row["status"]),
            required_signatories=tuple(json.loads(row["required_signatories_json"])),
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    def add_signature(self, rule_id: str, signatory: str, signed_at: str) -> None:
        """记录会签；重复签署触发 IntegrityError，由服务层映射为冲突。"""
        self.conn.execute(
            "INSERT INTO rule_signatures (rule_id, signatory, signed_at)"
            " VALUES (?, ?, ?)",
            (rule_id, signatory, signed_at),
        )

    def list_signatures(self, rule_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT signatory FROM rule_signatures WHERE rule_id = ?"
            " ORDER BY signatory",
            (rule_id,),
        ).fetchall()
        return [r["signatory"] for r in rows]

    def set_rule_status(self, rule_id: str, status: RuleStatus) -> None:
        self.conn.execute(
            "UPDATE conversion_rules SET status = ? WHERE id = ?",
            (status.value, rule_id),
        )

    def get_active_rule_id(self, rule_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT active_rule_id FROM rule_key_state WHERE rule_key = ?",
            (rule_key,),
        ).fetchone()
        return row["active_rule_id"] if row else None

    def set_active_rule_id(self, rule_key: str, rule_id: str) -> None:
        self.conn.execute(
            "INSERT INTO rule_key_state (rule_key, active_rule_id) VALUES (?, ?)"
            " ON CONFLICT(rule_key) DO UPDATE SET active_rule_id = excluded.active_rule_id",
            (rule_key, rule_id),
        )

    def active_rules(self) -> list[ConversionRule]:
        rows = self.conn.execute(
            "SELECT r.* FROM conversion_rules r"
            " JOIN rule_key_state s ON s.active_rule_id = r.id"
        ).fetchall()
        return [self._to_rule(r) for r in rows]

    # ---- 计算任务与断点 ----
    def insert_task(self, task: ComputationTask) -> None:
        self.conn.execute(
            "INSERT INTO tasks (id, idempotency_key, project_id, window_start,"
            " window_end, target_caliber, request_fingerprint, status,"
            " current_step, report_id, error, created_by, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task.id, task.idempotency_key, task.project_id, task.window_start,
             task.window_end, task.target_caliber, task.request_fingerprint,
             task.status.value, task.current_step, task.report_id, task.error,
             task.created_by, task.created_at, task.updated_at),
        )

    def task_by_key(self, idempotency_key: str) -> ComputationTask | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return self._to_task(row) if row else None

    def task_by_id(self, task_id: str) -> ComputationTask | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._to_task(row) if row else None

    def update_task(self, task: ComputationTask) -> None:
        self.conn.execute(
            "UPDATE tasks SET status = ?, current_step = ?, report_id = ?,"
            " error = ?, updated_at = ? WHERE id = ?",
            (task.status.value, task.current_step, task.report_id, task.error,
             task.updated_at, task.id),
        )

    @staticmethod
    def _to_task(row: sqlite3.Row) -> ComputationTask:
        return ComputationTask(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            project_id=row["project_id"],
            window_start=row["window_start"],
            window_end=row["window_end"],
            target_caliber=row["target_caliber"],
            request_fingerprint=row["request_fingerprint"],
            status=TaskStatus(row["status"]),
            current_step=row["current_step"],
            report_id=row["report_id"],
            error=row["error"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def save_checkpoint(self, task_id: str, step: str, payload: dict, at: str) -> None:
        self.conn.execute(
            "INSERT INTO task_checkpoints (task_id, step, payload_json, created_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(task_id, step) DO UPDATE SET"
            " payload_json = excluded.payload_json, created_at = excluded.created_at",
            (task_id, step, json.dumps(payload, sort_keys=True), at),
        )

    def load_checkpoints(self, task_id: str) -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT step, payload_json FROM task_checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        return {r["step"]: json.loads(r["payload_json"]) for r in rows}

    def delete_checkpoints(self, task_id: str) -> None:
        self.conn.execute(
            "DELETE FROM task_checkpoints WHERE task_id = ?", (task_id,)
        )

    # ---- 报告 ----
    def add_report(self, report: Report) -> None:
        self.conn.execute(
            "INSERT INTO reports (id, project_id, window_start, window_end,"
            " target_caliber, data_version_no, pins_json, lines_json,"
            " input_fingerprint, result_fingerprint, status, created_by,"
            " created_at, task_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (report.id, report.project_id, report.window_start, report.window_end,
             report.target_caliber, report.data_version_no,
             json.dumps(report.pins, sort_keys=True),
             json.dumps(report.lines, sort_keys=True),
             report.input_fingerprint, report.result_fingerprint,
             report.status.value, report.created_by, report.created_at, report.task_id),
        )

    def get_report(self, report_id: str) -> Report | None:
        row = self.conn.execute(
            "SELECT * FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        return self._to_report(row) if row else None

    def list_reports(self, project_id: str) -> list[Report]:
        rows = self.conn.execute(
            "SELECT * FROM reports WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        return [self._to_report(r) for r in rows]

    def set_report_status(self, report_id: str, status: ReportStatus) -> None:
        self.conn.execute(
            "UPDATE reports SET status = ? WHERE id = ?",
            (status.value, report_id),
        )

    @staticmethod
    def _to_report(row: sqlite3.Row) -> Report:
        return Report(
            id=row["id"],
            project_id=row["project_id"],
            window_start=row["window_start"],
            window_end=row["window_end"],
            target_caliber=row["target_caliber"],
            data_version_no=row["data_version_no"],
            pins=json.loads(row["pins_json"]),
            lines=json.loads(row["lines_json"]),
            input_fingerprint=row["input_fingerprint"],
            result_fingerprint=row["result_fingerprint"],
            status=ReportStatus(row["status"]),
            created_by=row["created_by"],
            created_at=row["created_at"],
            task_id=row["task_id"],
        )

    def add_report_event(self, report_id: str, event: str, actor: str,
                         reason: str | None, at: str) -> None:
        self.conn.execute(
            "INSERT INTO report_events (report_id, event, actor, reason, at)"
            " VALUES (?, ?, ?, ?, ?)",
            (report_id, event, actor, reason, at),
        )

    def list_report_events(self, report_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT event, actor, reason, at FROM report_events"
            " WHERE report_id = ? ORDER BY id",
            (report_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 授权 ----
    def add_grant(self, grant: Grant) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO grants (institution_id, project_id, category,"
            " permission) VALUES (?, ?, ?, ?)",
            (grant.institution_id, grant.project_id, grant.category, grant.permission),
        )

    def has_grant(self, institution_id: str, project_id: str, category: str,
                  permission: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM grants WHERE institution_id = ?"
            " AND project_id IN (?, '*') AND category IN (?, '*') AND permission = ?"
            " LIMIT 1",
            (institution_id, project_id, category, permission),
        ).fetchone()
        return row is not None

    def list_grants(self, institution_id: str) -> list[Grant]:
        rows = self.conn.execute(
            "SELECT * FROM grants WHERE institution_id = ?"
            " ORDER BY project_id, category, permission",
            (institution_id,),
        ).fetchall()
        return [Grant(**dict(r)) for r in rows]

    # ---- 导出 ----
    def add_export(self, export_id: str, report_id: str, exported_by: str,
                   exported_at: str, digest: str) -> None:
        self.conn.execute(
            "INSERT INTO exports (id, report_id, exported_by, exported_at, digest)"
            " VALUES (?, ?, ?, ?, ?)",
            (export_id, report_id, exported_by, exported_at, digest),
        )

    def list_exports(self, report_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM exports WHERE report_id = ? ORDER BY exported_at",
            (report_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 复核服务时限：机构日历 ----
    def upsert_calendar(self, cal: InstitutionCalendar, updated_by: str,
                        updated_at: str) -> None:
        self.conn.execute(
            "INSERT INTO sla_calendars (institution_id, tz_offset_minutes,"
            " work_windows_json, holidays_json, updated_by, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(institution_id) DO UPDATE SET"
            " tz_offset_minutes = excluded.tz_offset_minutes,"
            " work_windows_json = excluded.work_windows_json,"
            " holidays_json = excluded.holidays_json,"
            " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (cal.institution_id, cal.tz_offset_minutes,
             json.dumps(windows_to_jsonable(cal.work_windows), sort_keys=True),
             json.dumps(sorted(cal.holidays)), updated_by, updated_at),
        )

    def get_calendar(self, institution_id: str) -> InstitutionCalendar | None:
        row = self.conn.execute(
            "SELECT * FROM sla_calendars WHERE institution_id = ?",
            (institution_id,),
        ).fetchone()
        if not row:
            return None
        return InstitutionCalendar(
            institution_id=row["institution_id"],
            tz_offset_minutes=row["tz_offset_minutes"],
            work_windows=windows_from_jsonable(json.loads(row["work_windows_json"])),
            holidays=frozenset(json.loads(row["holidays_json"])),
        )

    # ---- 复核服务时限：案件 ----
    def add_case(self, case: ReviewCase) -> None:
        self.conn.execute(
            "INSERT INTO review_cases (id, institution_id, title, report_id,"
            " limit_seconds, state, opened_by, opened_at, closed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (case.id, case.institution_id, case.title, case.report_id,
             case.limit_seconds, case.state.value, case.opened_by,
             case.opened_at, case.closed_at),
        )

    def get_case(self, case_id: str) -> ReviewCase | None:
        row = self.conn.execute(
            "SELECT * FROM review_cases WHERE id = ?", (case_id,)
        ).fetchone()
        return self._to_case(row) if row else None

    def list_cases(self, institution_id: str | None = None) -> list[ReviewCase]:
        if institution_id is None:
            rows = self.conn.execute(
                "SELECT * FROM review_cases ORDER BY opened_at, id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM review_cases WHERE institution_id = ?"
                " ORDER BY opened_at, id",
                (institution_id,),
            ).fetchall()
        return [self._to_case(r) for r in rows]

    def list_active_cases(self) -> list[ReviewCase]:
        """未办结（计时中或暂停中）的案件，供超时扫描。"""
        rows = self.conn.execute(
            "SELECT * FROM review_cases WHERE state IN ('open', 'paused')"
            " ORDER BY opened_at, id"
        ).fetchall()
        return [self._to_case(r) for r in rows]

    def set_case_state(self, case_id: str, state: CaseState,
                       closed_at: str | None = None) -> None:
        self.conn.execute(
            "UPDATE review_cases SET state = ?, closed_at = ? WHERE id = ?",
            (state.value, closed_at, case_id),
        )

    @staticmethod
    def _to_case(row: sqlite3.Row) -> ReviewCase:
        return ReviewCase(
            id=row["id"],
            institution_id=row["institution_id"],
            title=row["title"],
            report_id=row["report_id"],
            limit_seconds=row["limit_seconds"],
            state=CaseState(row["state"]),
            opened_by=row["opened_by"],
            opened_at=row["opened_at"],
            closed_at=row["closed_at"],
        )

    # ---- 复核服务时限：暂停区间 ----
    def add_pause(self, case_id: str, started_at: str,
                  reason: str | None) -> None:
        self.conn.execute(
            "INSERT INTO review_case_pauses (case_id, started_at, ended_at, reason)"
            " VALUES (?, ?, NULL, ?)",
            (case_id, started_at, reason),
        )

    def close_open_pause(self, case_id: str, ended_at: str) -> None:
        self.conn.execute(
            "UPDATE review_case_pauses SET ended_at = ?"
            " WHERE case_id = ? AND ended_at IS NULL",
            (ended_at, case_id),
        )

    def list_pauses(self, case_id: str) -> list[PauseInterval]:
        rows = self.conn.execute(
            "SELECT case_id, started_at, ended_at, reason"
            " FROM review_case_pauses WHERE case_id = ? ORDER BY id",
            (case_id,),
        ).fetchall()
        return [PauseInterval(**dict(r)) for r in rows]

    # ---- 复核服务时限：升级记录 ----
    def add_escalation(self, esc: Escalation) -> None:
        self.conn.execute(
            "INSERT INTO sla_escalations (id, case_id, level, breached_at,"
            " detected_at, elapsed_business_seconds, limit_seconds, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (esc.id, esc.case_id, esc.level, esc.breached_at, esc.detected_at,
             esc.elapsed_business_seconds, esc.limit_seconds, esc.created_by),
        )

    def escalation_for_case(self, case_id: str) -> Escalation | None:
        row = self.conn.execute(
            "SELECT * FROM sla_escalations WHERE case_id = ?", (case_id,)
        ).fetchone()
        return self._to_escalation(row) if row else None

    def list_escalations(self, case_id: str | None = None) -> list[Escalation]:
        if case_id is None:
            rows = self.conn.execute(
                "SELECT * FROM sla_escalations ORDER BY detected_at, id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM sla_escalations WHERE case_id = ?"
                " ORDER BY detected_at, id",
                (case_id,),
            ).fetchall()
        return [self._to_escalation(r) for r in rows]

    @staticmethod
    def _to_escalation(row: sqlite3.Row) -> Escalation:
        return Escalation(
            id=row["id"],
            case_id=row["case_id"],
            level=row["level"],
            breached_at=row["breached_at"],
            detected_at=row["detected_at"],
            elapsed_business_seconds=row["elapsed_business_seconds"],
            limit_seconds=row["limit_seconds"],
            created_by=row["created_by"],
        )
