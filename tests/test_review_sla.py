"""复核服务时限：机构日历计时、暂停不计时、超时升级留痕。

覆盖：跨午夜工作窗口、节假日/周末不计时、暂停区间扣除、复核完成终止计时、
巡查恰好升级一次、策略快照不受后续调整影响、授权粒度、SQLite 升级事件留痕，
以及 HTTP 全链路的跨午夜请求超时状态。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from service_09252_010.domain.errors import (
    ConflictError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from service_09252_010.domain.models import Grant, Report, ReportStatus
from service_09252_010.domain.sla import (
    build_calendar,
    chargeable_seconds,
    deadline_after,
    format_instant,
)
from service_09252_010.interfaces.wsgi_app import make_app
from service_09252_010.persistence.database import Database
from service_09252_010.persistence.store import Store
from service_09252_010.services.access import grant_access
from service_09252_010.services.review import ReviewService
from service_09252_010.services.review_sla import ReviewSlaService
from support import INST_A, SUPERVISOR, SeqIds
from test_http_api import call

SUP = {"X-Institution-Id": "主管单位", "X-Role": "supervisor"}

# 2026-01-05 为周一；UTC+8 机构日历
DAY_WINDOWS = [{"weekday": d, "start": "09:00", "end": "17:00"} for d in range(5)]
NIGHT_WINDOWS = [{"weekday": 0, "start": "22:00", "end": "02:00"}]  # 跨午夜


class ManualClock:
    """可设定当前时刻的时钟，时限测试专用。"""

    def __init__(self, start: str) -> None:
        self._now = start

    def now(self) -> str:
        return self._now

    def set(self, value: str) -> None:
        self._now = value


class SlaRig:
    def __init__(self, tmpdir: str) -> None:
        self.db = Database(os.path.join(tmpdir, "sla.db"))
        self.clock = ManualClock("2026-01-05T00:00:00+00:00")
        self.ids = SeqIds()
        self.sla = ReviewSlaService(self.db, self.clock, self.ids)
        self.review = ReviewService(self.db, self.clock)

    def add_report(self, report_id: str, project_id: str, created_at: str,
                   created_by: str = "机构A") -> None:
        with self.db.uow() as uow:
            Store(uow.conn).add_report(Report(
                id=report_id, project_id=project_id,
                window_start="2024-01", window_end="2024-12",
                target_caliber="CN-STD", data_version_no=1,
                pins={}, lines=[], input_fingerprint="in",
                result_fingerprint="out", status=ReportStatus.COMPUTED,
                created_by=created_by, created_at=created_at,
                task_id=f"task-{report_id}",
            ))

    def grant(self, institution: str, project: str, permission: str) -> None:
        with self.db.uow() as uow:
            grant_access(uow.conn, Grant(institution, project, "*", permission))

    def register_day_calendar(self, holidays: list[str] | None = None) -> None:
        self.sla.register_calendar(
            SUPERVISOR, institution_id="机构R", utc_offset_minutes=480,
            work_windows=list(DAY_WINDOWS), holidays=holidays or [],
        )


class SlaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="svc09252-sla-")
        self.rig = SlaRig(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class CrossMidnightTests(SlaTestCase):
    """跨午夜工作窗口：请求在深夜进入，时限跨越零点。"""

    def test_cross_midnight_timeout_and_escalation_persisted(self) -> None:
        rig = self.rig
        rig.sla.register_calendar(
            SUPERVISOR, institution_id="机构R", utc_offset_minutes=480,
            work_windows=list(NIGHT_WINDOWS), holidays=[],
        )
        rig.sla.set_policy(SUPERVISOR, project_id="P1",
                           calendar_institution_id="机构R",
                           limit_business_seconds=4 * 3600)
        # 周一 21:00 本地进入复核；工作窗口 22:00–次日02:00
        rig.add_report("R1", "P1", created_at="2026-01-05T13:00:00+00:00")

        # 周二 01:00 本地（跨午夜后）：已计 3h，剩余 1h
        rig.clock.set("2026-01-05T17:00:00+00:00")
        status = rig.sla.status(SUPERVISOR, "R1")
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["consumed_business_seconds"], 10800.0)
        self.assertEqual(status["remaining_business_seconds"], 3600.0)
        self.assertEqual(status["deadline_at"],
                         "2026-01-05T18:00:00.000000+00:00")  # 周二 02:00 本地
        self.assertFalse(status["breached"])
        self.assertFalse(status["escalated"])

        # 周二 02:00:00 本地（恰到截止时间）：尚未超时
        rig.clock.set("2026-01-05T18:00:00+00:00")
        on_wire = rig.sla.status(SUPERVISOR, "R1")
        self.assertFalse(on_wire["breached"])

        # 周二 02:00:01 本地：已过截止时间，判定超时；
        # 窗口已结束，超时后尚无业务时间流逝，超时业务秒数为 0
        rig.clock.set("2026-01-05T18:00:01+00:00")
        result = rig.sla.evaluate(SUPERVISOR, "R1")
        self.assertTrue(result["breached"])
        self.assertEqual(result["overdue_business_seconds"], 0.0)
        self.assertTrue(result["escalation_created"])
        escalation_id = result["escalation"]["escalation_id"]

        # SQLite 中留下升级事件，且内容完整
        with rig.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM sla_escalations WHERE report_id = 'R1'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["project_id"], "P1")
        self.assertEqual(row["calendar_institution_id"], "机构R")
        self.assertEqual(row["deadline_at"],
                         "2026-01-05T18:00:00.000000+00:00")
        self.assertEqual(row["overdue_business_seconds"], 0.0)
        self.assertEqual(row["detected_by"], "主管单位")

        # 重复评估收敛为同一条升级记录（恰好一次）
        again = rig.sla.evaluate(SUPERVISOR, "R1")
        self.assertFalse(again["escalation_created"])
        self.assertEqual(again["escalation"]["escalation_id"], escalation_id)
        with rig.db.read() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM sla_escalations WHERE report_id = 'R1'"
            ).fetchone()["n"]
        self.assertEqual(count, 1)


class HolidayTests(SlaTestCase):
    def test_weekend_and_holiday_not_counted(self) -> None:
        rig = self.rig
        rig.register_day_calendar(holidays=["2026-01-12"])  # 周一节假日
        rig.sla.set_policy(SUPERVISOR, project_id="P1",
                           calendar_institution_id="机构R",
                           limit_business_seconds=2 * 3600)
        # 周五 16:00 本地进入复核，时限 2h
        rig.add_report("R2", "P1", created_at="2026-01-09T08:00:00+00:00")

        # 节假日（周一）晚间：周末与节假日均未计时，仍只消耗周五的 1h
        rig.clock.set("2026-01-12T12:00:00+00:00")
        status = rig.sla.status(SUPERVISOR, "R2")
        self.assertEqual(status["consumed_business_seconds"], 3600.0)
        self.assertEqual(status["remaining_business_seconds"], 3600.0)
        self.assertEqual(status["deadline_at"],
                         "2026-01-13T02:00:00.000000+00:00")  # 周二 10:00 本地
        self.assertFalse(status["breached"])

        # 周二 10:00:01 本地超时
        rig.clock.set("2026-01-13T02:00:01+00:00")
        result = rig.sla.evaluate(SUPERVISOR, "R2")
        self.assertTrue(result["breached"])
        self.assertEqual(result["overdue_business_seconds"], 1.0)
        self.assertTrue(result["escalation_created"])


class PauseTests(SlaTestCase):
    def test_pause_interval_not_counted(self) -> None:
        rig = self.rig
        rig.register_day_calendar()
        rig.sla.set_policy(SUPERVISOR, project_id="P1",
                           calendar_institution_id="机构R",
                           limit_business_seconds=4 * 3600)
        # 周一 09:00 本地进入复核，时限 4h
        rig.add_report("R3", "P1", created_at="2026-01-05T01:00:00+00:00")

        # 10:00 暂停（等待机构补充材料）；重复暂停冲突
        rig.clock.set("2026-01-05T02:00:00+00:00")
        paused = rig.sla.pause(SUPERVISOR, "R3", reason="等待补充材料")
        self.assertEqual(paused["state"], "paused")
        with self.assertRaises(ConflictError):
            rig.sla.pause(SUPERVISOR, "R3")

        # 暂停期间：计时冻结，截止时间不可判定
        rig.clock.set("2026-01-05T05:00:00+00:00")
        status = rig.sla.status(SUPERVISOR, "R3")
        self.assertEqual(status["state"], "paused")
        self.assertEqual(status["consumed_business_seconds"], 3600.0)
        self.assertIsNone(status["deadline_at"])
        self.assertFalse(status["breached"])

        # 16:00 恢复：暂停 6h 不计时，截止时间顺延至周二 11:00 本地
        rig.clock.set("2026-01-05T08:00:00+00:00")
        resumed = rig.sla.resume(SUPERVISOR, "R3")
        self.assertEqual(resumed["state"], "running")
        status = rig.sla.status(SUPERVISOR, "R3")
        self.assertEqual(status["consumed_business_seconds"], 3600.0)
        self.assertEqual(status["deadline_at"],
                         "2026-01-06T03:00:00.000000+00:00")
        self.assertEqual(len(status["pauses"]), 1)
        self.assertEqual(status["pauses"][0]["reason"], "等待补充材料")

        # 周二 11:00:01 本地超时
        rig.clock.set("2026-01-06T03:00:01+00:00")
        result = rig.sla.evaluate(SUPERVISOR, "R3")
        self.assertTrue(result["breached"])
        self.assertEqual(result["overdue_business_seconds"], 1.0)

    def test_resume_without_pause_rejected(self) -> None:
        self.rig.register_day_calendar()
        self.rig.sla.set_policy(SUPERVISOR, project_id="P1",
                                calendar_institution_id="机构R",
                                limit_business_seconds=3600)
        self.rig.add_report("R9", "P1",
                            created_at="2026-01-05T01:00:00+00:00")
        with self.assertRaises(StateError):
            self.rig.sla.resume(SUPERVISOR, "R9")


class ReviewCloseTests(SlaTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.rig.register_day_calendar()
        self.rig.sla.set_policy(SUPERVISOR, project_id="P1",
                                calendar_institution_id="机构R",
                                limit_business_seconds=2 * 3600)

    def test_review_before_deadline_closes_clock(self) -> None:
        rig = self.rig
        rig.add_report("R4", "P1", created_at="2026-01-05T01:00:00+00:00")
        # 周一 10:30 本地复核通过（耗时 1.5h < 2h）
        rig.clock.set("2026-01-05T02:30:00+00:00")
        rig.review.review(SUPERVISOR, "R4", approve=True)
        # 复核后无法再暂停
        with self.assertRaises(StateError):
            rig.sla.pause(SUPERVISOR, "R4")
        # 两周后评估：计时已在复核时刻终止，永不超时
        rig.clock.set("2026-01-20T00:00:00+00:00")
        result = rig.sla.evaluate(SUPERVISOR, "R4")
        self.assertEqual(result["state"], "closed")
        self.assertEqual(result["consumed_business_seconds"], 5400.0)
        self.assertFalse(result["breached"])
        self.assertFalse(result["escalated"])

    def test_breach_before_review_still_escalated(self) -> None:
        rig = self.rig
        rig.add_report("R5", "P1", created_at="2026-01-05T01:00:00+00:00")
        # 周一 12:00 本地复核通过（耗时 3h > 2h，复核前已超时）
        rig.clock.set("2026-01-05T04:00:00+00:00")
        rig.review.review(SUPERVISOR, "R5", approve=True)
        # 复核后评估：超时事实以计时终止时刻判定，补记升级
        rig.clock.set("2026-01-20T00:00:00+00:00")
        result = rig.sla.evaluate(SUPERVISOR, "R5")
        self.assertEqual(result["state"], "closed")
        self.assertEqual(result["reference_at"],
                         "2026-01-05T04:00:00+00:00")
        self.assertTrue(result["breached"])
        self.assertEqual(result["overdue_business_seconds"], 3600.0)
        self.assertTrue(result["escalation_created"])
        self.assertEqual(result["escalation"]["deadline_at"],
                         "2026-01-05T03:00:00.000000+00:00")  # 周一 11:00 本地


class SweepTests(SlaTestCase):
    def test_sweep_materializes_and_escalates_once(self) -> None:
        rig = self.rig
        rig.register_day_calendar()
        rig.sla.set_policy(SUPERVISOR, project_id="P1",
                           calendar_institution_id="机构R",
                           limit_business_seconds=2 * 3600)
        rig.sla.set_policy(SUPERVISOR, project_id="P2",
                           calendar_institution_id="机构R",
                           limit_business_seconds=8 * 3600)
        rig.add_report("R6", "P1", created_at="2026-01-05T01:00:00+00:00")
        rig.add_report("R7", "P2", created_at="2026-01-05T01:00:00+00:00")

        # 周一 12:00 本地巡查：R6 超时升级，R7 仍在时限内
        rig.clock.set("2026-01-05T04:00:00+00:00")
        first = rig.sla.sweep(SUPERVISOR)
        self.assertEqual(first["escalated_count"], 1)
        self.assertEqual(first["escalated"][0]["report_id"], "R6")

        # 再次巡查不产生新的升级记录
        second = rig.sla.sweep(SUPERVISOR)
        self.assertEqual(second["escalated_count"], 0)

        status = rig.sla.status(SUPERVISOR, "R7")
        self.assertEqual(status["consumed_business_seconds"], 10800.0)
        self.assertEqual(status["remaining_business_seconds"], 18000.0)
        self.assertFalse(status["breached"])

        escalations = rig.sla.list_escalations(SUPERVISOR, "P1")
        self.assertEqual(len(escalations["escalations"]), 1)


class PolicySnapshotTests(SlaTestCase):
    def test_clock_immune_to_later_policy_change(self) -> None:
        rig = self.rig
        rig.register_day_calendar()
        rig.sla.set_policy(SUPERVISOR, project_id="P1",
                           calendar_institution_id="机构R",
                           limit_business_seconds=2 * 3600)
        rig.add_report("R8", "P1", created_at="2026-01-05T01:00:00+00:00")
        # 物化时钟（周一 09:30 本地评估一次）
        rig.clock.set("2026-01-05T01:30:00+00:00")
        rig.sla.evaluate(SUPERVISOR, "R8")
        # 策略调整为 8h：在途案件仍按快照的 2h 判定
        rig.sla.set_policy(SUPERVISOR, project_id="P1",
                           calendar_institution_id="机构R",
                           limit_business_seconds=8 * 3600)
        rig.clock.set("2026-01-05T04:00:00+00:00")
        status = rig.sla.status(SUPERVISOR, "R8")
        self.assertEqual(status["limit_business_seconds"], 7200.0)
        self.assertTrue(status["breached"])


class PermissionTests(SlaTestCase):
    def test_permissions(self) -> None:
        rig = self.rig
        with self.assertRaises(PermissionDeniedError):
            rig.sla.register_calendar(
                INST_A, institution_id="机构R", utc_offset_minutes=480,
                work_windows=list(DAY_WINDOWS), holidays=[],
            )
        rig.register_day_calendar()
        with self.assertRaises(PermissionDeniedError):
            rig.sla.set_policy(INST_A, project_id="P1",
                               calendar_institution_id="机构R",
                               limit_business_seconds=3600)
        rig.sla.set_policy(SUPERVISOR, project_id="P1",
                           calendar_institution_id="机构R",
                           limit_business_seconds=3600)
        rig.add_report("R10", "P1", created_at="2026-01-05T01:00:00+00:00")

        with self.assertRaises(PermissionDeniedError):
            rig.sla.pause(INST_A, "R10")
        with self.assertRaises(PermissionDeniedError):
            rig.sla.status(INST_A, "R10")
        with self.assertRaises(PermissionDeniedError):
            rig.sla.sweep(INST_A)

        rig.grant("机构A", "P1", "review")
        rig.grant("机构A", "P1", "view")
        paused = rig.sla.pause(INST_A, "R10", reason="内部协调")
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(rig.sla.resume(INST_A, "R10")["state"], "running")
        self.assertFalse(rig.sla.status(INST_A, "R10")["breached"])
        self.assertIn("state", rig.sla.evaluate(INST_A, "R10"))


class CalendarDomainTests(unittest.TestCase):
    """日历纯函数：跨午夜、节假日归属、午休窗口与入参校验。"""

    def test_overnight_window_belongs_to_start_day(self) -> None:
        cal = build_calendar(
            "机构R", utc_offset_minutes=480, work_windows=list(NIGHT_WINDOWS),
            holidays=["2026-01-06"],  # 周二节假日
        )
        # 周一 23:00 → 周二 01:30 本地：窗口归属周一，次日节假日不截断
        self.assertEqual(
            chargeable_seconds(cal, "2026-01-05T15:00:00+00:00",
                               "2026-01-05T17:30:00+00:00"),
            9000.0,
        )
        # 周二（节假日）与周三（无窗口）完全不计时
        self.assertEqual(
            chargeable_seconds(cal, "2026-01-06T03:00:00+00:00",
                               "2026-01-07T04:00:00+00:00"),
            0.0,
        )

    def test_split_windows_lunch_break(self) -> None:
        cal = build_calendar(
            "机构R", utc_offset_minutes=480,
            work_windows=[{"weekday": 0, "start": "09:00", "end": "12:00"},
                          {"weekday": 0, "start": "13:00", "end": "17:00"}],
            holidays=[],
        )
        # 周一 11:00–14:00 本地：午休 12:00–13:00 不计时
        self.assertEqual(
            chargeable_seconds(cal, "2026-01-05T03:00:00+00:00",
                               "2026-01-05T06:00:00+00:00"),
            7200.0,
        )
        # 周一 11:30 起 2h：上午 30min + 下午 13:00–14:30
        deadline = deadline_after(cal, "2026-01-05T03:30:00+00:00", 7200)
        self.assertEqual(format_instant(deadline),
                         "2026-01-05T06:30:00.000000+00:00")

    def test_calendar_validation(self) -> None:
        with self.assertRaises(ValidationError):
            build_calendar("机构R", utc_offset_minutes=480,
                           work_windows=[{"weekday": 7, "start": "09:00",
                                          "end": "17:00"}], holidays=[])
        with self.assertRaises(ValidationError):
            build_calendar("机构R", utc_offset_minutes=480,
                           work_windows=[{"weekday": 0, "start": "09:00",
                                          "end": "09:00"}], holidays=[])
        with self.assertRaises(ValidationError):
            build_calendar("机构R", utc_offset_minutes=480,
                           work_windows=list(DAY_WINDOWS),
                           holidays=["2026-13-01"])
        with self.assertRaises(ValidationError):
            build_calendar("机构R", utc_offset_minutes=480,
                           work_windows=[], holidays=[])
        with self.assertRaises(ValidationError):
            build_calendar("机构R", utc_offset_minutes=True,
                           work_windows=list(DAY_WINDOWS), holidays=[])


class HttpSlaTests(unittest.TestCase):
    """HTTP 全链路：跨午夜请求的超时状态与升级记录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="svc09252-sla-http-")
        self.db_path = os.path.join(self._tmp.name, "api.db")
        self.app = make_app(self.db_path)
        # 用可设定时钟替换 SLA 服务的系统时钟，保证场景确定
        self.clock = ManualClock("2026-01-05T15:30:00+00:00")
        self.app.container.review_sla = ReviewSlaService(
            self.app.container.db, self.clock, self.app.container.ids
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_http_cross_midnight_timeout_status(self) -> None:
        # 机构日历：周一夜班 22:00–次日02:00（UTC+8）
        status, body = call(self.app, "POST", "/sla/calendars", {
            "institution_id": "机构R", "utc_offset_minutes": 480,
            "work_windows": [{"weekday": 0, "start": "22:00", "end": "02:00"}],
            "holidays": [],
        }, SUP)
        self.assertEqual(status, 201, body)

        status, body = call(self.app, "POST", "/sla/policies", {
            "project_id": "P1", "calendar_institution_id": "机构R",
            "limit_business_seconds": 3600,
        }, SUP)
        self.assertEqual(status, 201, body)

        # 非 ASCII 路径段读取机构日历
        status, body = call(self.app, "GET", "/sla/calendars/机构R", headers=SUP)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["utc_offset_minutes"], 480)
        self.assertEqual(body["work_windows"],
                         [{"weekday": 0, "start": "22:00", "end": "02:00"}])

        # 复核案件周一 23:30 本地进入（直接落库，创建时刻即计时起点）
        with Database(self.db_path).uow() as uow:
            Store(uow.conn).add_report(Report(
                id="R-http", project_id="P1",
                window_start="2024-01", window_end="2024-12",
                target_caliber="CN-STD", data_version_no=1,
                pins={}, lines=[], input_fingerprint="in",
                result_fingerprint="out", status=ReportStatus.COMPUTED,
                created_by="机构A", created_at="2026-01-05T15:30:00+00:00",
                task_id="task-R-http",
            ))

        # 周二 00:00 本地（恰跨午夜）：未超时，剩余 30 分钟
        self.clock.set("2026-01-05T16:00:00+00:00")
        status, body = call(self.app, "GET", "/reports/R-http/sla", headers=SUP)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "running")
        self.assertFalse(body["breached"])
        self.assertEqual(body["consumed_business_seconds"], 1800.0)
        self.assertEqual(body["remaining_business_seconds"], 1800.0)
        self.assertEqual(body["deadline_at"],
                         "2026-01-05T16:30:00.000000+00:00")  # 周二 00:30 本地

        # 周二 00:30:01 本地：超时 1 秒
        self.clock.set("2026-01-05T16:30:01+00:00")
        status, body = call(self.app, "GET", "/reports/R-http/sla", headers=SUP)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["breached"])
        self.assertEqual(body["overdue_business_seconds"], 1.0)
        self.assertFalse(body["escalated"])  # 只读评估不写升级记录

        # 评估生成升级记录；重复评估恰好一次
        status, body = call(self.app, "POST",
                            "/reports/R-http/sla/evaluate", {}, SUP)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["escalation_created"])
        status, body = call(self.app, "POST",
                            "/reports/R-http/sla/evaluate", {}, SUP)
        self.assertFalse(body["escalation_created"])

        # 升级记录可查询，且已留痕 SQLite
        status, body = call(self.app, "GET", "/escalations",
                            headers=SUP, query="project_id=P1")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["escalations"]), 1)
        self.assertEqual(body["escalations"][0]["report_id"], "R-http")
        self.assertEqual(body["escalations"][0]["deadline_at"],
                         "2026-01-05T16:30:00.000000+00:00")
        with Database(self.db_path).read() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM sla_escalations"
                " WHERE report_id = 'R-http'"
            ).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_http_validation_and_auth(self) -> None:
        # 非法日历 → 422
        status, body = call(self.app, "POST", "/sla/calendars", {
            "institution_id": "机构R", "utc_offset_minutes": 480,
            "work_windows": [{"weekday": 9, "start": "09:00", "end": "17:00"}],
            "holidays": [],
        }, SUP)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "validation_error")
        # 机构无权维护日历 → 403
        status, body = call(self.app, "POST", "/sla/calendars", {
            "institution_id": "机构R", "utc_offset_minutes": 480,
            "work_windows": [{"weekday": 0, "start": "09:00", "end": "17:00"}],
            "holidays": [],
        }, {"X-Institution-Id": "机构A"})
        self.assertEqual(status, 403)
        # 未配置策略的项目 → 404
        status, body = call(self.app, "GET", "/reports/no-such/sla", headers=SUP)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
