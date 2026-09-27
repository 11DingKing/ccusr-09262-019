"""复核服务时限：按机构日历计时，暂停不计时，超时生成升级记录（恰好一次）。

时钟随报告进入待复核（computed）隐式启动：起始时刻固化取报告创建时间，
策略快照（时限、日历机构）在首次物化时钟行时拷贝，此后策略调整不影响在途案件。
报告复核完成（reviewed/rejected 事件）即计时终止；超时事实以计时终止时刻判定，
即使复核先于评估发生，超时仍会补记升级记录。

超时判定：参考时刻（现在或计时终止时刻）晚于截止时间即为超时；
超时业务秒数为超出时限上限的业务时间，非工作时段不再累积。
"""
from __future__ import annotations

import sqlite3

from ..domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from ..domain.models import (
    Principal,
    Report,
    ReportStatus,
    SlaClock,
    SlaEscalation,
    SlaPolicy,
)
from ..domain.sla import (
    build_calendar,
    chargeable_seconds,
    deadline_after,
    format_instant,
    parse_instant,
)
from ..persistence.database import Database
from ..persistence.store import Store
from ..ports import Clock, IdGenerator
from .access import AccessPolicy

_CLOSE_EVENTS = ("reviewed", "rejected")


class ReviewSlaService:
    def __init__(self, db: Database, clock: Clock, ids: IdGenerator) -> None:
        self.db = db
        self.clock = clock
        self.ids = ids

    # ---- 机构日历与策略（主管单位维护）----
    def register_calendar(self, principal: Principal, *, institution_id: str,
                          utc_offset_minutes: int, work_windows: list[dict],
                          holidays: list[str]) -> dict:
        """登记或更新机构日历（整体替换，幂等）。"""
        if not principal.is_supervisor:
            raise PermissionDeniedError("仅主管单位可维护机构日历")
        calendar = build_calendar(
            institution_id,
            utc_offset_minutes=utc_offset_minutes,
            work_windows=work_windows,
            holidays=holidays,
        )
        now = self.clock.now()
        with self.db.uow() as uow:
            store = Store(uow.conn)
            created = store.get_sla_calendar_row(institution_id) is None
            store.upsert_sla_calendar({
                "institution_id": calendar.institution_id,
                "utc_offset_minutes": calendar.utc_offset_minutes,
                "work_windows": [
                    {"weekday": w.weekday, "start": w.start, "end": w.end}
                    for w in calendar.work_windows
                ],
                "holidays": list(calendar.holidays),
                "updated_by": principal.institution_id,
                "updated_at": now,
            })
        return {"institution_id": institution_id, "created": created,
                "updated_at": now}

    def get_calendar(self, principal: Principal, institution_id: str) -> dict:
        if not principal.is_supervisor \
                and principal.institution_id != institution_id:
            raise PermissionDeniedError("仅主管单位或本机构可查看其日历")
        with self.db.read() as conn:
            row = Store(conn).get_sla_calendar_row(institution_id)
        if row is None:
            raise NotFoundError(f"机构日历不存在: {institution_id}")
        return row

    def set_policy(self, principal: Principal, *, project_id: str,
                   calendar_institution_id: str,
                   limit_business_seconds: float) -> dict:
        """配置项目的复核时限策略；仅影响此后物化的时钟。"""
        if not principal.is_supervisor:
            raise PermissionDeniedError("仅主管单位可配置复核时限策略")
        if isinstance(limit_business_seconds, bool) \
                or not isinstance(limit_business_seconds, (int, float)) \
                or not limit_business_seconds > 0:
            raise ValidationError("limit_business_seconds 必须为正数")
        now = self.clock.now()
        with self.db.uow() as uow:
            store = Store(uow.conn)
            if store.get_sla_calendar_row(calendar_institution_id) is None:
                raise NotFoundError(
                    f"机构日历不存在: {calendar_institution_id}"
                )
            created = store.get_sla_policy(project_id) is None
            store.upsert_sla_policy(SlaPolicy(
                project_id=project_id,
                calendar_institution_id=calendar_institution_id,
                limit_business_seconds=float(limit_business_seconds),
                updated_by=principal.institution_id,
                updated_at=now,
            ))
        return {"project_id": project_id, "created": created,
                "limit_business_seconds": float(limit_business_seconds)}

    # ---- 暂停 / 恢复 ----
    def pause(self, principal: Principal, report_id: str, *,
              reason: str = "") -> dict:
        """暂停计时（如等待机构补充材料）；暂停期间不计入时限。"""
        with self.db.uow() as uow:
            store = Store(uow.conn)
            report = self._require_report(store, report_id)
            AccessPolicy(store).require(
                principal, report.project_id, "*", "review"
            )
            if report.status is not ReportStatus.COMPUTED:
                raise StateError("报告已复核，时限计时已终止，无法暂停")
            clock = self._ensure_clock(store, report)
            if store.open_sla_pause(report_id) is not None:
                raise ConflictError("该案件已处于暂停中")
            now = self.clock.now()
            store.add_sla_pause(report_id, now, reason or None,
                                principal.institution_id)
        return {"report_id": report_id, "state": "paused", "paused_at": now,
                "started_at": clock.started_at}

    def resume(self, principal: Principal, report_id: str) -> dict:
        """恢复计时；暂停区间自此封闭，不再计时。"""
        with self.db.uow() as uow:
            store = Store(uow.conn)
            report = self._require_report(store, report_id)
            AccessPolicy(store).require(
                principal, report.project_id, "*", "review"
            )
            if report.status is not ReportStatus.COMPUTED:
                raise StateError("报告已复核，时限计时已终止")
            open_pause = store.open_sla_pause(report_id)
            if open_pause is None:
                raise StateError("该案件当前未处于暂停中")
            now = self.clock.now()
            store.resume_sla_pause(open_pause["id"], now)
        return {"report_id": report_id, "state": "running", "resumed_at": now}

    # ---- 状态与评估 ----
    def status(self, principal: Principal, report_id: str) -> dict:
        """只读评估当前时限状态（不写升级记录）。"""
        with self.db.read() as conn:
            store = Store(conn)
            report = self._require_report(store, report_id)
            AccessPolicy(store).require(
                principal, report.project_id, "*", "view"
            )
            clock = self._clock_or_virtual(store, report)
            snapshot = self._snapshot(store, report, clock, self.clock.now())
            snapshot["escalation"] = store.get_sla_escalation(report_id)
        snapshot["escalated"] = snapshot["escalation"] is not None
        return snapshot

    def evaluate(self, principal: Principal, report_id: str) -> dict:
        """评估并在超时时生成升级记录；重复评估收敛为同一条记录。"""
        with self.db.uow() as uow:
            store = Store(uow.conn)
            report = self._require_report(store, report_id)
            AccessPolicy(store).require(
                principal, report.project_id, "*", "review"
            )
            clock = self._ensure_clock(store, report)
            now = self.clock.now()
            snapshot = self._snapshot(store, report, clock, now)
            escalation, created = self._escalate_if_breached(
                store, principal, clock, snapshot, now
            )
        snapshot["escalated"] = escalation is not None
        snapshot["escalation"] = escalation
        snapshot["escalation_created"] = created
        return snapshot

    def sweep(self, principal: Principal) -> dict:
        """主管单位巡查：补齐在途案件时钟，全部未升级时钟评估一遍。"""
        if not principal.is_supervisor:
            raise PermissionDeniedError("仅主管单位可执行时限巡查")
        now = self.clock.now()
        escalated: list[dict] = []
        with self.db.uow() as uow:
            store = Store(uow.conn)
            store.materialize_sla_clocks()
            for clock in store.unescalated_sla_clocks():
                report = store.get_report(clock.report_id)
                if report is None:
                    continue
                snapshot = self._snapshot(store, report, clock, now)
                escalation, created = self._escalate_if_breached(
                    store, principal, clock, snapshot, now
                )
                if created and escalation is not None:
                    escalated.append(escalation)
        return {"evaluated_at": now, "escalated_count": len(escalated),
                "escalated": escalated}

    def list_escalations(self, principal: Principal, project_id: str) -> dict:
        with self.db.read() as conn:
            store = Store(conn)
            AccessPolicy(store).require(principal, project_id, "*", "view")
            rows = store.list_sla_escalations(project_id)
        return {"project_id": project_id, "escalations": rows}

    # ---- 内部 ----
    @staticmethod
    def _require_report(store: Store, report_id: str) -> Report:
        report = store.get_report(report_id)
        if report is None:
            raise NotFoundError(f"报告不存在: {report_id}")
        return report

    def _ensure_clock(self, store: Store, report: Report) -> SlaClock:
        """物化时钟（写路径）；起始时刻取报告创建时间，策略参数快照。"""
        clock = store.get_sla_clock(report.id)
        if clock is not None:
            return clock
        policy = store.get_sla_policy(report.project_id)
        if policy is None:
            raise NotFoundError(
                f"项目 {report.project_id} 未配置复核服务时限策略"
            )
        clock = SlaClock(
            report_id=report.id,
            project_id=report.project_id,
            calendar_institution_id=policy.calendar_institution_id,
            limit_business_seconds=policy.limit_business_seconds,
            started_at=report.created_at,
        )
        store.insert_sla_clock(clock)  # INSERT OR IGNORE：并发下收敛
        return store.get_sla_clock(report.id) or clock

    def _clock_or_virtual(self, store: Store, report: Report) -> SlaClock:
        """只读路径：未物化时按策略虚拟一个时钟，不落库。"""
        clock = store.get_sla_clock(report.id)
        if clock is not None:
            return clock
        policy = store.get_sla_policy(report.project_id)
        if policy is None:
            raise NotFoundError(
                f"项目 {report.project_id} 未配置复核服务时限策略"
            )
        return SlaClock(
            report_id=report.id,
            project_id=report.project_id,
            calendar_institution_id=policy.calendar_institution_id,
            limit_business_seconds=policy.limit_business_seconds,
            started_at=report.created_at,
        )

    def _snapshot(self, store: Store, report: Report, clock: SlaClock,
                  now: str) -> dict:
        row = store.get_sla_calendar_row(clock.calendar_institution_id)
        if row is None:
            raise StateError(
                f"机构日历缺失: {clock.calendar_institution_id}"
            )
        calendar = build_calendar(
            row["institution_id"],
            utc_offset_minutes=row["utc_offset_minutes"],
            work_windows=row["work_windows"],
            holidays=row["holidays"],
        )
        pauses = store.list_sla_pauses(report.id)
        open_pause = next((p for p in pauses if p["resumed_at"] is None), None)
        closed_at = None
        for event in store.list_report_events(report.id):
            if event["event"] in _CLOSE_EVENTS:
                closed_at = event["at"]
                break
        reference = closed_at or now
        closed_pauses = [
            (p["paused_at"], p["resumed_at"]) for p in pauses if p["resumed_at"]
        ]
        charge_pauses = list(closed_pauses)
        if open_pause is not None:
            # 未恢复的暂停：计时在暂停起点冻结
            charge_pauses.append((open_pause["paused_at"], reference))
        consumed = chargeable_seconds(
            calendar, clock.started_at, reference, charge_pauses
        )
        limit = clock.limit_business_seconds
        if open_pause is not None:
            # 暂停未恢复则截止时间不可判定；暂停起点之前已超时的除外
            candidate = deadline_after(
                calendar, clock.started_at, limit, closed_pauses
            )
            deadline_dt = (
                candidate
                if candidate <= parse_instant(open_pause["paused_at"])
                else None
            )
        else:
            deadline_dt = deadline_after(
                calendar, clock.started_at, limit, closed_pauses
            )
        # 超时判定：参考时刻（现在或计时终止时刻）晚于截止时间即为超时；
        # 超时业务秒数为超出上限的业务时间（非工作时段不再累积）。
        overdue = max(0.0, consumed - limit)
        breached = (
            deadline_dt is not None and parse_instant(reference) > deadline_dt
        )
        state = "closed" if closed_at else ("paused" if open_pause else "running")
        return {
            "report_id": report.id,
            "project_id": report.project_id,
            "state": state,
            "calendar_institution_id": clock.calendar_institution_id,
            "limit_business_seconds": limit,
            "started_at": clock.started_at,
            "reference_at": reference,
            "closed_at": closed_at,
            "consumed_business_seconds": consumed,
            "remaining_business_seconds": max(0.0, limit - consumed),
            "overdue_business_seconds": overdue,
            "deadline_at": format_instant(deadline_dt) if deadline_dt else None,
            "breached": breached,
            "pauses": pauses,
        }

    def _escalate_if_breached(self, store: Store, principal: Principal,
                              clock: SlaClock, snapshot: dict, now: str
                              ) -> tuple[dict | None, bool]:
        existing = store.get_sla_escalation(clock.report_id)
        if existing is not None or not snapshot["breached"]:
            return existing, False
        escalation = SlaEscalation(
            id=self.ids.new_id("escalation"),
            report_id=clock.report_id,
            project_id=clock.project_id,
            calendar_institution_id=clock.calendar_institution_id,
            deadline_at=snapshot["deadline_at"],
            detected_at=now,
            overdue_business_seconds=snapshot["overdue_business_seconds"],
            detected_by=principal.institution_id,
            created_at=now,
        )
        try:
            store.add_sla_escalation(escalation)
        except sqlite3.IntegrityError:
            # 并发评估：另一事务已写入，收敛到既有记录
            return store.get_sla_escalation(clock.report_id), False
        return store.get_sla_escalation(clock.report_id), True
