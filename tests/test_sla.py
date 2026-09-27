"""复核服务时限：机构日历计时、暂停不计时、超时升级。

验收场景：跨午夜请求的超时状态、节假日顺延、暂停区间剔除、
升级记录幂等生成、办结终态、权限与校验、HTTP 全链路。
"""
from __future__ import annotations

import tempfile
import unittest

from service_09252_010.domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from service_09252_010.persistence.database import Database
from service_09252_010.services.sla import ReviewSlaService
from support import INST_A, INST_B, SUPERVISOR, ManualClock, SeqIds

# 机构日历：UTC+8，周一至周五 09:00-17:00。
WEEK_WINDOWS = {day: [["09:00", "17:00"]]
                for day in ("mon", "tue", "wed", "thu", "fri")}


class SlaCase(unittest.TestCase):
    """每个用例一份全新数据库与可推进时钟。"""

    HOLIDAYS: list[str] = []

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="svc09252-sla-")
        self.db = Database(f"{self._tmp.name}/sla.db")
        self.clock = ManualClock("2026-09-25T08:00:00+00:00")  # 周五 16:00 北京
        self.ids = SeqIds()
        self.sla = ReviewSlaService(self.db, self.clock, self.ids)
        self.sla.register_calendar(
            SUPERVISOR, institution_id=INST_A.institution_id,
            tz_offset="+08:00", work_windows=WEEK_WINDOWS,
            holidays=list(self.HOLIDAYS),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def open_case(self, hours: float = 4.0, **kwargs) -> str:
        return self.sla.open_case(
            INST_A, title="报告复核", limit_business_hours=hours, **kwargs
        )["case_id"]


class CrossMidnightTests(SlaCase):
    """跨午夜请求：周五 16:00 开立，时限 4 个工作小时，下周一 12:00 超时。"""

    def test_cross_midnight_breach_instant_and_status(self) -> None:
        case_id = self.open_case(hours=4.0)

        # 周五 17:30（下班后）：已计 1 小时，预计下周一 12:00 超时
        self.clock.set("2026-09-25T09:30:00+00:00")
        status = self.sla.case_status(INST_A, case_id)
        self.assertFalse(status["breached"])
        self.assertEqual(status["elapsed_business_seconds"], 3600)
        self.assertEqual(status["remaining_business_seconds"], 10800)
        self.assertEqual(status["breach_at"],
                         "2026-09-28T04:00:00.000000+00:00")

        # 周一 11:00：已计 1 + 2 = 3 小时，仍未超时
        self.clock.set("2026-09-28T03:00:00+00:00")
        status = self.sla.case_status(INST_A, case_id)
        self.assertFalse(status["breached"])
        self.assertEqual(status["elapsed_business_seconds"], 10800)
        self.assertEqual(status["remaining_business_seconds"], 3600)

        # 周一 12:00:01：跨午夜后超时
        self.clock.set("2026-09-28T04:00:01+00:00")
        status = self.sla.case_status(INST_A, case_id)
        self.assertTrue(status["breached"])
        self.assertEqual(status["breach_at"],
                         "2026-09-28T04:00:00.000000+00:00")
        self.assertEqual(status["remaining_business_seconds"], 0)

    def test_sweep_generates_escalation_exactly_once(self) -> None:
        case_id = self.open_case(hours=4.0)
        self.clock.set("2026-09-28T04:00:01+00:00")  # 周一 12:00:01，已超时

        swept = self.sla.sweep(SUPERVISOR)
        self.assertEqual(swept["new_count"], 1)
        escalation = swept["new_escalations"][0]
        self.assertEqual(escalation["case_id"], case_id)
        self.assertEqual(escalation["level"], 1)
        self.assertEqual(escalation["breached_at"],
                         "2026-09-28T04:00:00.000000+00:00")
        self.assertEqual(escalation["limit_seconds"], 14400)
        self.assertEqual(escalation["elapsed_business_seconds"], 14401)

        # 幂等：再次扫描不重复升级
        self.assertEqual(self.sla.sweep(SUPERVISOR)["new_count"], 0)
        escalations = self.sla.list_escalations(SUPERVISOR, case_id)
        self.assertEqual(len(escalations["escalations"]), 1)
        self.assertTrue(self.sla.case_status(INST_A, case_id)["escalated"])

    def test_sweep_ignores_cases_within_limit(self) -> None:
        self.open_case(hours=4.0)
        self.clock.set("2026-09-28T03:59:59+00:00")  # 周一 11:59:59，未超时
        self.assertEqual(self.sla.sweep(SUPERVISOR)["new_count"], 0)


class HolidayTests(SlaCase):
    """节假日：周一为节假日，超时顺延至周二。"""

    HOLIDAYS = ["2026-09-28"]

    def test_holiday_defers_breach_to_next_business_day(self) -> None:
        case_id = self.open_case(hours=4.0)

        # 节假日当天深夜：仍只计了周五的 1 小时
        self.clock.set("2026-09-28T15:59:00+00:00")  # 周一 23:59 北京
        status = self.sla.case_status(INST_A, case_id)
        self.assertFalse(status["breached"])
        self.assertEqual(status["elapsed_business_seconds"], 3600)
        self.assertEqual(status["breach_at"],
                         "2026-09-29T04:00:00.000000+00:00")  # 周二 12:00

        # 周二 11:00：已计 3 小时
        self.clock.set("2026-09-29T03:00:00+00:00")
        status = self.sla.case_status(INST_A, case_id)
        self.assertFalse(status["breached"])
        self.assertEqual(status["elapsed_business_seconds"], 10800)

        # 周二 12:00:01：超时，升级记录落在节假日顺延后的时刻
        self.clock.set("2026-09-29T04:00:01+00:00")
        self.assertTrue(self.sla.case_status(INST_A, case_id)["breached"])
        swept = self.sla.sweep(SUPERVISOR)
        self.assertEqual(swept["new_count"], 1)
        self.assertEqual(swept["new_escalations"][0]["breached_at"],
                         "2026-09-29T04:00:00.000000+00:00")


class PauseTests(SlaCase):
    """暂停期间不计时。"""

    def setUp(self) -> None:
        super().setUp()
        self.clock.set("2026-09-28T02:00:00+00:00")  # 周一 10:00 北京
        self.case_id = self.open_case(hours=4.0)

    def test_pause_freezes_elapsed_and_suspends_breach(self) -> None:
        self.clock.set("2026-09-28T03:00:00+00:00")  # 周一 11:00
        self.sla.pause_case(INST_A, self.case_id, reason="等待机构补件")

        # 墙钟推进 3 小时，已用时限不变；暂停中超时时刻不可推算（None）
        self.clock.set("2026-09-28T06:00:00+00:00")  # 周一 14:00
        status = self.sla.case_status(INST_A, self.case_id)
        self.assertEqual(status["state"], "paused")
        self.assertEqual(status["elapsed_business_seconds"], 3600)
        self.assertIsNone(status["breach_at"])
        self.assertEqual(len(status["pauses"]), 1)
        self.assertIsNone(status["pauses"][0]["ended_at"])
        self.assertEqual(status["pauses"][0]["reason"], "等待机构补件")

        # 暂停中即使越过名义期限也不升级
        self.assertEqual(self.sla.sweep(SUPERVISOR)["new_count"], 0)

        # 恢复后预计超时时刻顺延暂停的 3 小时：17:00 北京
        self.sla.resume_case(INST_A, self.case_id)
        status = self.sla.case_status(INST_A, self.case_id)
        self.assertEqual(status["state"], "open")
        self.assertEqual(status["breach_at"],
                         "2026-09-28T09:00:00.000000+00:00")
        self.assertIsNotNone(status["pauses"][0]["ended_at"])

        # 16:00 已计 3 小时未超时；17:00:01 超时
        self.clock.set("2026-09-28T08:00:00+00:00")
        status = self.sla.case_status(INST_A, self.case_id)
        self.assertFalse(status["breached"])
        self.assertEqual(status["elapsed_business_seconds"], 10800)
        self.clock.set("2026-09-28T09:00:01+00:00")
        self.assertTrue(self.sla.case_status(INST_A, self.case_id)["breached"])

    def test_pause_state_machine(self) -> None:
        with self.assertRaises(StateError):
            self.sla.resume_case(INST_A, self.case_id)  # 未暂停
        self.sla.pause_case(INST_A, self.case_id)
        with self.assertRaises(StateError):
            self.sla.pause_case(INST_A, self.case_id)  # 重复暂停
        self.sla.resume_case(INST_A, self.case_id)
        with self.assertRaises(StateError):
            self.sla.resume_case(INST_A, self.case_id)


class CloseTests(SlaCase):
    """办结为终态：停止计时，不再升级。"""

    def test_closed_case_keeps_frozen_status_and_skips_sweep(self) -> None:
        self.clock.set("2026-09-28T01:00:00+00:00")  # 周一 09:00
        case_id = self.open_case(hours=1.0)
        self.clock.set("2026-09-28T03:00:00+00:00")  # 周一 11:00，已超时 1 小时
        self.sla.close_case(INST_A, case_id, reason="已办结")

        status = self.sla.case_status(INST_A, case_id)
        self.assertEqual(status["state"], "closed")
        self.assertTrue(status["breached"])
        self.assertEqual(status["elapsed_business_seconds"], 7200)
        self.assertEqual(status["breach_at"],
                         "2026-09-28T02:00:00.000000+00:00")  # 10:00 北京

        # 已办结案件不再生成升级记录
        self.assertEqual(self.sla.sweep(SUPERVISOR)["new_count"], 0)
        with self.assertRaises(StateError):
            self.sla.close_case(INST_A, case_id)
        with self.assertRaises(StateError):
            self.sla.pause_case(INST_A, case_id)

    def test_close_while_paused_closes_open_pause(self) -> None:
        self.clock.set("2026-09-28T01:00:00+00:00")
        case_id = self.open_case(hours=4.0)
        self.clock.set("2026-09-28T02:00:00+00:00")
        self.sla.pause_case(INST_A, case_id)
        self.clock.set("2026-09-28T05:00:00+00:00")
        self.sla.close_case(INST_A, case_id)
        status = self.sla.case_status(INST_A, case_id)
        self.assertEqual(status["state"], "closed")
        self.assertEqual(status["elapsed_business_seconds"], 3600)
        self.assertIsNotNone(status["pauses"][0]["ended_at"])


class PermissionAndValidationTests(SlaCase):
    def test_calendar_registration_requires_supervisor(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.sla.register_calendar(
                INST_A, institution_id=INST_A.institution_id,
                tz_offset="+08:00", work_windows=WEEK_WINDOWS,
            )

    def test_calendar_roundtrip(self) -> None:
        spec = self.sla.get_calendar(INST_A.institution_id)
        self.assertEqual(spec["tz_offset"], "+08:00")
        self.assertEqual(spec["work_windows"]["mon"], [["09:00", "17:00"]])
        self.assertNotIn("sat", spec["work_windows"])
        with self.assertRaises(NotFoundError):
            self.sla.get_calendar("机构B")

    def test_open_case_validations(self) -> None:
        with self.assertRaises(ValidationError):
            self.sla.open_case(INST_A, title="x", limit_business_hours=0)
        with self.assertRaises(ValidationError):
            self.sla.open_case(INST_A, title="x", limit_business_hours=-1)
        with self.assertRaises(ValidationError):
            self.sla.open_case(INST_A, title="", limit_business_hours=1)
        with self.assertRaises(ValidationError):
            self.sla.open_case(INST_B, title="x", limit_business_hours=1)  # 无日历
        with self.assertRaises(PermissionDeniedError):
            self.sla.open_case(INST_A, title="x", limit_business_hours=1,
                               institution_id=INST_B.institution_id)
        with self.assertRaises(NotFoundError):
            self.sla.open_case(INST_A, title="x", limit_business_hours=1,
                               report_id="rpt-不存在")

    def test_calendar_spec_validations(self) -> None:
        bad_specs = [
            {"tz_offset": "+25:00"},
            {"tz_offset": "北京"},
            {"work_windows": {"mon": [["17:00", "09:00"]]}},
            {"work_windows": {"mon": [["09:00", "12:00"], ["11:00", "13:00"]]}},
            {"work_windows": {"monday": [["09:00", "17:00"]]}},
            {"work_windows": {}},
            {"holidays": ["2026-13-01"]},
        ]
        for override in bad_specs:
            spec = {"institution_id": "机构X", "tz_offset": "+08:00",
                    "work_windows": WEEK_WINDOWS, "holidays": []}
            spec.update(override)
            with self.assertRaises(ValidationError, msg=str(override)):
                self.sla.register_calendar(SUPERVISOR, **spec)

    def test_case_access_control(self) -> None:
        case_id = self.open_case()
        with self.assertRaises(PermissionDeniedError):
            self.sla.case_status(INST_B, case_id)
        with self.assertRaises(PermissionDeniedError):
            self.sla.pause_case(INST_B, case_id)
        with self.assertRaises(PermissionDeniedError):
            self.sla.close_case(INST_B, case_id)
        with self.assertRaises(PermissionDeniedError):
            self.sla.sweep(INST_A)  # 仅主管单位可扫描
        with self.assertRaises(NotFoundError):
            self.sla.case_status(SUPERVISOR, "case-不存在")
        # 主管单位可操作任意机构案件；机构列表仅见本机构
        self.assertEqual(
            self.sla.case_status(SUPERVISOR, case_id)["case_id"], case_id
        )
        self.assertEqual(len(self.sla.list_cases(INST_A)["cases"]), 1)
        self.assertEqual(len(self.sla.list_cases(INST_B)["cases"]), 0)
        self.assertEqual(len(self.sla.list_cases(SUPERVISOR)["cases"]), 1)


class HttpAcceptanceTests(unittest.TestCase):
    """跨午夜 + 节假日请求的 HTTP 全链路验收。"""

    def setUp(self) -> None:
        from service_09252_010.container import Container
        from service_09252_010.interfaces.wsgi_app import Application

        self._tmp = tempfile.TemporaryDirectory(prefix="svc09252-sla-http-")
        self.clock = ManualClock("2026-09-25T08:30:00+00:00")  # 周五 16:30 北京
        container = Container(f"{self._tmp.name}/http.db")
        container.clock = self.clock
        container.sla = ReviewSlaService(container.db, self.clock, container.ids)
        self.app = Application(container)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, method: str, path: str, body: dict | None = None,
             headers: dict | None = None, query: str = ""):
        import io
        import json

        payload = json.dumps(body).encode("utf-8") if body is not None else b""
        env = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_LENGTH": str(len(payload)),
            "wsgi.input": io.BytesIO(payload),
        }
        for key, value in (headers or {}).items():
            env["HTTP_" + key.upper().replace("-", "_")] = value
        captured: dict = {}

        def start_response(status, response_headers):
            captured["status"] = int(status.split()[0])

        chunks = self.app(env, start_response)
        return captured["status"], json.loads(b"".join(chunks).decode("utf-8"))

    SUP = {"X-Institution-Id": "主管单位", "X-Role": "supervisor"}
    A = {"X-Institution-Id": "机构A"}
    B = {"X-Institution-Id": "机构B"}

    def register_calendar(self, holidays: list[str]) -> None:
        status, body = self.call("POST", "/sla/calendars", {
            "institution_id": "机构A", "tz_offset": "+08:00",
            "work_windows": WEEK_WINDOWS, "holidays": holidays,
        }, self.SUP)
        self.assertEqual(status, 201, body)

    def test_cross_midnight_holiday_request_flow(self) -> None:
        self.register_calendar(holidays=["2026-09-28"])  # 周一节假日

        # 周五 16:30 开立，时限 2 个工作小时
        status, body = self.call("POST", "/review-cases", {
            "title": "报告复核", "limit_business_hours": 2,
        }, self.A)
        self.assertEqual(status, 201, body)
        case_id = body["case_id"]

        # 周五 17:30：已计 0.5 小时
        # 跨午夜 + 跨周末 + 节假日：周五 0.5h + 周二 1.5h → 周二 10:30 超时
        self.clock.set("2026-09-25T09:30:00+00:00")
        status, body = self.call("GET", f"/review-cases/{case_id}",
                                 headers=self.A)
        self.assertEqual(status, 200, body)
        self.assertFalse(body["breached"])
        self.assertEqual(body["elapsed_business_seconds"], 1800)
        self.assertEqual(body["breach_at"],
                         "2026-09-29T02:30:00.000000+00:00")

        # 周六查询：非工作时间不计时
        self.clock.set("2026-09-26T06:00:00+00:00")
        _, body = self.call("GET", f"/review-cases/{case_id}", headers=self.A)
        self.assertEqual(body["elapsed_business_seconds"], 1800)
        self.assertFalse(body["breached"])

        # 周二 10:31：超时
        self.clock.set("2026-09-29T02:31:00+00:00")
        _, body = self.call("GET", f"/review-cases/{case_id}", headers=self.A)
        self.assertTrue(body["breached"])
        self.assertEqual(body["breach_at"],
                         "2026-09-29T02:30:00.000000+00:00")

        # 扫描生成升级记录，且幂等
        status, body = self.call("POST", "/review-cases/sweep", {}, self.SUP)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["new_count"], 1)
        self.assertEqual(body["new_escalations"][0]["case_id"], case_id)
        self.assertEqual(body["new_escalations"][0]["breached_at"],
                         "2026-09-29T02:30:00.000000+00:00")
        _, body = self.call("POST", "/review-cases/sweep", {}, self.SUP)
        self.assertEqual(body["new_count"], 0)

        # 升级事件已落库，可查询
        status, body = self.call("GET", f"/review-cases/{case_id}/escalations",
                                 headers=self.A)
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["escalations"]), 1)
        self.assertEqual(body["escalations"][0]["level"], 1)

        # 案件列表
        _, body = self.call("GET", "/review-cases", headers=self.A)
        self.assertEqual(len(body["cases"]), 1)

    def test_pause_resume_close_over_http(self) -> None:
        self.register_calendar(holidays=[])
        self.clock.set("2026-09-28T01:00:00+00:00")  # 周一 09:00
        status, body = self.call("POST", "/review-cases", {
            "title": "加急复核", "limit_business_hours": 4,
        }, self.A)
        case_id = body["case_id"]

        # 12:00 暂停（已计 3 小时），15:00 恢复
        self.clock.set("2026-09-28T04:00:00+00:00")
        status, body = self.call("POST", f"/review-cases/{case_id}/pause",
                                 {"reason": "等待补件"}, self.A)
        self.assertEqual(status, 200, body)
        _, body = self.call("GET", f"/review-cases/{case_id}", headers=self.A)
        self.assertEqual(body["state"], "paused")
        self.assertEqual(body["elapsed_business_seconds"], 10800)
        self.assertIsNone(body["breach_at"])

        self.clock.set("2026-09-28T07:00:00+00:00")
        status, _ = self.call("POST", f"/review-cases/{case_id}/resume",
                              {}, self.A)
        self.assertEqual(status, 200)
        _, body = self.call("GET", f"/review-cases/{case_id}", headers=self.A)
        self.assertEqual(body["breach_at"],
                         "2026-09-28T08:00:00.000000+00:00")  # 16:00 北京

        # 办结为终态；此后不再升级，操作返回 409
        status, _ = self.call("POST", f"/review-cases/{case_id}/close",
                              {"reason": "已办结"}, self.A)
        self.assertEqual(status, 200)
        status, _ = self.call("POST", f"/review-cases/{case_id}/resume",
                              {}, self.A)
        self.assertEqual(status, 409)
        _, body = self.call("POST", "/review-cases/sweep", {}, self.SUP)
        self.assertEqual(body["new_count"], 0)

    def test_http_auth_and_errors(self) -> None:
        # 机构无权登记日历
        status, body = self.call("POST", "/sla/calendars", {
            "institution_id": "机构A", "tz_offset": "+08:00",
            "work_windows": WEEK_WINDOWS,
        }, self.A)
        self.assertEqual(status, 403)
        self.register_calendar(holidays=[])

        status, body = self.call("POST", "/review-cases", {
            "title": "x", "limit_business_hours": 1,
        }, self.A)
        case_id = body["case_id"]
        # 他机构无权操作；未知案件 404；非法日历 422
        status, _ = self.call("POST", f"/review-cases/{case_id}/pause",
                              {}, self.B)
        self.assertEqual(status, 403)
        status, _ = self.call("GET", "/review-cases/case-不存在",
                              headers=self.SUP)
        self.assertEqual(status, 404)
        status, _ = self.call("POST", "/sla/calendars", {
            "institution_id": "机构C", "tz_offset": "北京",
            "work_windows": WEEK_WINDOWS,
        }, self.SUP)
        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()
