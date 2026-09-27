"""复核服务时限：按机构日历计时，暂停期间不计时，超时生成升级记录。

- 案件（review case）由服务台为机构开立，预算以工作小时计；
- 计时基准为机构日历（本地工作窗口 + 节假日），暂停区间剔除；
- sweep 扫描未办结案件，对已超时案件生成升级记录（每案件恰好一次）。
"""
from __future__ import annotations

from ..domain.calendar import (
    HORIZON_DAYS,
    MAX_LIMIT_SECONDS,
    InstitutionCalendar,
    PauseSpan,
    format_instant,
    parse_instant,
)
from ..domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from ..domain.models import (
    CaseState,
    Escalation,
    PauseInterval,
    Principal,
    ReviewCase,
)
from ..persistence.database import Database
from ..persistence.store import Store
from ..ports import Clock, IdGenerator


class ReviewSlaService:
    def __init__(self, db: Database, clock: Clock, ids: IdGenerator) -> None:
        self.db = db
        self.clock = clock
        self.ids = ids

    # ---- 机构日历 ----
    def register_calendar(self, principal: Principal, *, institution_id: str,
                          tz_offset: str, work_windows: dict,
                          holidays: list[str] | None = None) -> dict:
        """登记（或整体替换）机构日历。仅主管单位可配置。"""
        if not principal.is_supervisor:
            raise PermissionDeniedError("仅主管单位可登记机构日历")
        calendar = InstitutionCalendar.from_spec(
            institution_id, tz_offset, work_windows, holidays
        )
        with self.db.uow() as uow:
            Store(uow.conn).upsert_calendar(
                calendar, principal.institution_id, self.clock.now()
            )
        return calendar.to_spec()

    def get_calendar(self, institution_id: str) -> dict:
        with self.db.read() as conn:
            calendar = Store(conn).get_calendar(institution_id)
        if calendar is None:
            raise NotFoundError(f"机构日历未登记: {institution_id}")
        return calendar.to_spec()

    # ---- 案件生命周期 ----
    def open_case(self, principal: Principal, *, title: str,
                  limit_business_hours: float,
                  institution_id: str | None = None,
                  report_id: str | None = None) -> dict:
        """开立复核案件，时限预算按机构日历的工作时间计。"""
        institution = institution_id or principal.institution_id
        if not principal.is_supervisor and institution != principal.institution_id:
            raise PermissionDeniedError("仅可为本机构开立案件")
        if not title or not str(title).strip():
            raise ValidationError("案件标题不能为空")
        try:
            hours = float(limit_business_hours)
        except (TypeError, ValueError):
            raise ValidationError("limit_business_hours 必须为数值") from None
        limit_seconds = int(round(hours * 3600))
        if limit_seconds <= 0:
            raise ValidationError("服务时限预算必须大于 0")
        if limit_seconds > MAX_LIMIT_SECONDS:
            raise ValidationError(
                f"服务时限预算超出可推算范围（{HORIZON_DAYS} 天视界）"
            )
        with self.db.uow() as uow:
            store = Store(uow.conn)
            if store.get_calendar(institution) is None:
                raise ValidationError(f"机构 {institution} 未登记日历，无法计时")
            if report_id is not None and store.get_report(report_id) is None:
                raise NotFoundError(f"报告不存在: {report_id}")
            case = ReviewCase(
                id=self.ids.new_id("case"),
                institution_id=institution,
                title=str(title).strip(),
                report_id=report_id,
                limit_seconds=limit_seconds,
                state=CaseState.OPEN,
                opened_by=principal.institution_id,
                opened_at=self.clock.now(),
                closed_at=None,
            )
            store.add_case(case)
        return {"case_id": case.id, "institution_id": institution,
                "state": case.state.value, "opened_at": case.opened_at,
                "limit_seconds": limit_seconds}

    def pause_case(self, principal: Principal, case_id: str, *,
                   reason: str = "") -> dict:
        """暂停计时（如等待机构补件）。暂停期间不计入已用时限。"""
        with self.db.uow() as uow:
            store = Store(uow.conn)
            case = self._require_case(store, case_id)
            self._require_owner(principal, case)
            if case.state is CaseState.CLOSED:
                raise StateError("案件已办结，为不可变终态")
            if case.state is CaseState.PAUSED:
                raise StateError("案件已处于暂停中")
            now = self.clock.now()
            store.add_pause(case_id, now, reason or None)
            store.set_case_state(case_id, CaseState.PAUSED)
        return {"case_id": case_id, "state": CaseState.PAUSED.value,
                "paused_at": now}

    def resume_case(self, principal: Principal, case_id: str) -> dict:
        """恢复计时，结束当前暂停区间。"""
        with self.db.uow() as uow:
            store = Store(uow.conn)
            case = self._require_case(store, case_id)
            self._require_owner(principal, case)
            if case.state is CaseState.CLOSED:
                raise StateError("案件已办结，为不可变终态")
            if case.state is not CaseState.PAUSED:
                raise StateError("案件未在暂停中")
            now = self.clock.now()
            store.close_open_pause(case_id, now)
            store.set_case_state(case_id, CaseState.OPEN)
        return {"case_id": case_id, "state": CaseState.OPEN.value,
                "resumed_at": now}

    def close_case(self, principal: Principal, case_id: str, *,
                   reason: str = "") -> dict:
        """办结案件（终态）。办结后不再计时、不再升级。"""
        with self.db.uow() as uow:
            store = Store(uow.conn)
            case = self._require_case(store, case_id)
            self._require_owner(principal, case)
            if case.state is CaseState.CLOSED:
                raise StateError("案件已办结，为不可变终态")
            now = self.clock.now()
            if case.state is CaseState.PAUSED:
                store.close_open_pause(case_id, now)
            store.set_case_state(case_id, CaseState.CLOSED, closed_at=now)
        return {"case_id": case_id, "state": CaseState.CLOSED.value,
                "closed_at": now}

    # ---- 状态查询 ----
    def case_status(self, principal: Principal, case_id: str) -> dict:
        with self.db.read() as conn:
            store = Store(conn)
            case = self._require_case(store, case_id)
            self._require_owner(principal, case)
            calendar = store.get_calendar(case.institution_id)
            pauses = store.list_pauses(case_id)
            escalation = store.escalation_for_case(case_id)
        as_of = (case.closed_at if case.state is CaseState.CLOSED
                 else self.clock.now())
        return self._status_payload(case, calendar, pauses, escalation, as_of)

    def list_cases(self, principal: Principal,
                   institution_id: str | None = None) -> dict:
        """机构仅见本机构案件；主管单位可指定机构或查看全部。"""
        if not principal.is_supervisor:
            institution_id = principal.institution_id
        with self.db.read() as conn:
            cases = Store(conn).list_cases(institution_id)
        return {"cases": [
            {"case_id": c.id, "institution_id": c.institution_id,
             "title": c.title, "report_id": c.report_id, "state": c.state.value,
             "opened_at": c.opened_at, "closed_at": c.closed_at,
             "limit_seconds": c.limit_seconds}
            for c in cases
        ]}

    # ---- 超时扫描与升级 ----
    def sweep(self, principal: Principal) -> dict:
        """扫描未办结案件，为已超时案件生成升级记录（幂等：每案件一次）。"""
        if not principal.is_supervisor:
            raise PermissionDeniedError("仅主管单位可执行超时扫描")
        now = self.clock.now()
        now_dt = parse_instant(now)
        created: list[Escalation] = []
        with self.db.uow() as uow:
            store = Store(uow.conn)
            for case in store.list_active_cases():
                calendar = store.get_calendar(case.institution_id)
                if calendar is None:
                    continue
                spans = self._pause_spans(store.list_pauses(case.id))
                elapsed = calendar.business_seconds_between(
                    parse_instant(case.opened_at), now_dt, spans
                )
                if elapsed < case.limit_seconds:
                    continue
                if store.escalation_for_case(case.id) is not None:
                    continue
                breach_at = calendar.breach_instant(
                    parse_instant(case.opened_at), case.limit_seconds, spans
                )
                escalation = Escalation(
                    id=self.ids.new_id("esc"),
                    case_id=case.id,
                    level=1,
                    breached_at=(format_instant(breach_at)
                                 if breach_at is not None else now),
                    detected_at=now,
                    elapsed_business_seconds=elapsed,
                    limit_seconds=case.limit_seconds,
                    created_by=principal.institution_id,
                )
                store.add_escalation(escalation)
                created.append(escalation)
        return {"scanned_at": now, "new_count": len(created),
                "new_escalations": [self._escalation_payload(e) for e in created]}

    def list_escalations(self, principal: Principal, case_id: str) -> dict:
        with self.db.read() as conn:
            store = Store(conn)
            case = self._require_case(store, case_id)
            self._require_owner(principal, case)
            escalations = store.list_escalations(case_id)
        return {"case_id": case_id,
                "escalations": [self._escalation_payload(e) for e in escalations]}

    # ---- 内部 ----
    @staticmethod
    def _require_case(store: Store, case_id: str) -> ReviewCase:
        case = store.get_case(case_id)
        if case is None:
            raise NotFoundError(f"案件不存在: {case_id}")
        return case

    @staticmethod
    def _require_owner(principal: Principal, case: ReviewCase) -> None:
        if principal.is_supervisor:
            return
        if case.institution_id != principal.institution_id:
            raise PermissionDeniedError("仅案件所属机构或主管单位可操作")

    @staticmethod
    def _pause_spans(pauses: list[PauseInterval]) -> list[PauseSpan]:
        return [
            (parse_instant(p.started_at),
             parse_instant(p.ended_at) if p.ended_at else None)
            for p in pauses
        ]

    def _status_payload(self, case: ReviewCase, calendar: InstitutionCalendar,
                        pauses: list[PauseInterval],
                        escalation: Escalation | None, as_of: str) -> dict:
        spans = self._pause_spans(pauses)
        opened = parse_instant(case.opened_at)
        elapsed = calendar.business_seconds_between(
            opened, parse_instant(as_of), spans
        )
        breached = elapsed >= case.limit_seconds
        breach_at = calendar.breach_instant(opened, case.limit_seconds, spans)
        return {
            "case_id": case.id,
            "institution_id": case.institution_id,
            "title": case.title,
            "report_id": case.report_id,
            "state": case.state.value,
            "opened_by": case.opened_by,
            "opened_at": case.opened_at,
            "closed_at": case.closed_at,
            "as_of": as_of,
            "limit_seconds": case.limit_seconds,
            "limit_business_hours": case.limit_seconds / 3600,
            "elapsed_business_seconds": int(round(elapsed)),
            "remaining_business_seconds": int(round(
                max(0.0, case.limit_seconds - elapsed)
            )),
            "breached": breached,
            # 已超时为实际超时时刻；计时中为预计超时时刻；暂停冻结时为 null。
            "breach_at": format_instant(breach_at) if breach_at else None,
            "pauses": [
                {"started_at": p.started_at, "ended_at": p.ended_at,
                 "reason": p.reason}
                for p in pauses
            ],
            "escalated": escalation is not None,
        }

    @staticmethod
    def _escalation_payload(esc: Escalation) -> dict:
        return {
            "escalation_id": esc.id,
            "case_id": esc.case_id,
            "level": esc.level,
            "breached_at": esc.breached_at,
            "detected_at": esc.detected_at,
            "elapsed_business_seconds": int(round(esc.elapsed_business_seconds)),
            "limit_seconds": esc.limit_seconds,
            "created_by": esc.created_by,
        }
