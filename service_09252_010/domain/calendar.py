"""机构日历：复核服务时限的计时基准。

- 工作时间按机构本地历法的星期与节假日定义，窗口为本地时间 HH:MM 区间；
- 持久化与接口中的时间串一律为 ISO8601 UTC，计算时换算到机构固定偏移时区；
- 暂停区间不计入已用时限；超时时刻按工作时段顺序推算。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterator, Sequence

from .errors import ValidationError

# 时限推算视界：预算超出该范围视为配置错误，避免异常输入导致无限推算。
HORIZON_DAYS = 366 * 3
MAX_LIMIT_SECONDS = 366 * 24 * 3600

_HM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OFFSET_RE = re.compile(r"^([+-])([01]\d|2[0-3]):([0-5]\d)$")

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# 暂停区间：(起点, 终点)；终点为 None 表示仍在暂停（时间冻结）。
PauseSpan = tuple[datetime, "datetime | None"]


def parse_instant(raw: str) -> datetime:
    """解析 ISO8601 时间串为 UTC 感知时间；朴素时间按 UTC 处理。"""
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"非法时间格式: {raw!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_instant(dt: datetime) -> str:
    """格式化为与系统其余部分一致的 ISO8601 UTC 串。"""
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_tz_offset(raw: str) -> int:
    """解析 ±HH:MM 形式的固定时区偏移，返回分钟数。"""
    if raw in ("Z", "UTC", "+00:00", "-00:00"):
        return 0
    match = _OFFSET_RE.match(raw) if isinstance(raw, str) else None
    if not match:
        raise ValidationError(f"非法时区偏移: {raw!r}，应为 ±HH:MM")
    sign = 1 if match.group(1) == "+" else -1
    minutes = sign * (int(match.group(2)) * 60 + int(match.group(3)))
    if abs(minutes) > 14 * 60:
        raise ValidationError(f"时区偏移超出范围: {raw!r}")
    return minutes


def format_tz_offset(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    abs_min = abs(minutes)
    return f"{sign}{abs_min // 60:02d}:{abs_min % 60:02d}"


def _parse_hhmm(raw: object) -> int:
    match = _HM_RE.match(raw) if isinstance(raw, str) else None
    if not match:
        raise ValidationError(f"非法时间: {raw!r}，应为 HH:MM")
    return int(match.group(1)) * 60 + int(match.group(2))


def _format_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def subtract_pauses(
    intervals: list[tuple[datetime, datetime]], pauses: Sequence[PauseSpan]
) -> list[tuple[datetime, datetime]]:
    """从区间列表中剔除暂停覆盖的部分（开放暂停冻结其后的全部时间）。"""
    busy = list(intervals)
    for p_start, p_end in pauses:
        end = p_end if p_end is not None else datetime.max.replace(tzinfo=timezone.utc)
        kept: list[tuple[datetime, datetime]] = []
        for seg_start, seg_end in busy:
            if end <= seg_start or p_start >= seg_end:
                kept.append((seg_start, seg_end))
                continue
            if p_start > seg_start:
                kept.append((seg_start, min(p_start, seg_end)))
            if end < seg_end:
                kept.append((max(end, seg_start), seg_end))
        busy = kept
    return busy


@dataclass(frozen=True)
class InstitutionCalendar:
    """机构日历：每周工作窗口（本地时间）+ 节假日（本地日期）。

    work_windows 索引为星期（0=周一 … 6=周日），值为 ((起分, 止分), …)，
    起止均为当地午夜以来的分钟数。
    """

    institution_id: str
    tz_offset_minutes: int
    work_windows: tuple[tuple[tuple[int, int], ...], ...]
    holidays: frozenset[str]

    @classmethod
    def from_spec(
        cls,
        institution_id: str,
        tz_offset: str,
        work_windows: dict,
        holidays: list[str] | None,
    ) -> "InstitutionCalendar":
        """从接口规格构造并校验。窗口键为 mon..sun，值为 [["09:00","17:00"], …]。"""
        offset_minutes = parse_tz_offset(tz_offset)
        if not isinstance(work_windows, dict) or not work_windows:
            raise ValidationError("work_windows 必须为非空对象，键为 mon..sun")
        normalized: dict[int, tuple[tuple[int, int], ...]] = {}
        for key, windows in work_windows.items():
            if key not in WEEKDAY_KEYS:
                raise ValidationError(f"非法星期键: {key!r}，应为 mon..sun")
            if not isinstance(windows, (list, tuple)) or not windows:
                raise ValidationError(f"星期 {key} 的工作窗口必须为非空数组")
            day_windows: list[tuple[int, int]] = []
            for window in windows:
                if not (isinstance(window, (list, tuple)) and len(window) == 2):
                    raise ValidationError(f"非法工作窗口: {window!r}，应为 [起, 止]")
                start_m, end_m = _parse_hhmm(window[0]), _parse_hhmm(window[1])
                if start_m >= end_m:
                    raise ValidationError(f"工作窗口起点须早于终点: {window!r}")
                day_windows.append((start_m, end_m))
            day_windows.sort()
            for prev, nxt in zip(day_windows, day_windows[1:]):
                if nxt[0] < prev[1]:
                    raise ValidationError(f"星期 {key} 的工作窗口重叠: {windows!r}")
            normalized[WEEKDAY_KEYS.index(key)] = tuple(day_windows)
        full = tuple(normalized.get(i, ()) for i in range(7))
        if not any(full):
            raise ValidationError("机构日历至少需要一个工作窗口")
        holiday_set: set[str] = set()
        for raw in holidays or []:
            if not isinstance(raw, str) or not _DATE_RE.match(raw):
                raise ValidationError(f"非法节假日日期: {raw!r}，应为 YYYY-MM-DD")
            try:
                date.fromisoformat(raw)
            except ValueError:
                raise ValidationError(f"非法节假日日期: {raw!r}") from None
            holiday_set.add(raw)
        return cls(institution_id, offset_minutes, full, frozenset(holiday_set))

    @property
    def local_tz(self) -> timezone:
        return timezone(timedelta(minutes=self.tz_offset_minutes))

    def to_spec(self) -> dict:
        """还原为接口可读规格。"""
        windows = {
            WEEKDAY_KEYS[day]: [[_format_hhmm(s), _format_hhmm(e)] for s, e in wins]
            for day, wins in enumerate(self.work_windows)
            if wins
        }
        return {
            "institution_id": self.institution_id,
            "tz_offset": format_tz_offset(self.tz_offset_minutes),
            "work_windows": windows,
            "holidays": sorted(self.holidays),
        }

    def work_segments(
        self, start: datetime, end: datetime
    ) -> Iterator[tuple[datetime, datetime]]:
        """生成 [start, end) 内的工作时段（UTC，按时间顺序，已裁剪）。"""
        if end <= start:
            return
        local_tz = self.local_tz
        day = start.astimezone(local_tz).date() - timedelta(days=1)
        last_day = end.astimezone(local_tz).date() + timedelta(days=1)
        while day <= last_day:
            if day.isoformat() not in self.holidays:
                for win_start, win_end in self.work_windows[day.weekday()]:
                    seg_start = datetime.combine(
                        day, time(win_start // 60, win_start % 60), tzinfo=local_tz
                    ).astimezone(timezone.utc)
                    seg_end = datetime.combine(
                        day, time(win_end // 60, win_end % 60), tzinfo=local_tz
                    ).astimezone(timezone.utc)
                    lo, hi = max(seg_start, start), min(seg_end, end)
                    if lo < hi:
                        yield lo, hi
            day += timedelta(days=1)

    def business_seconds_between(
        self, start: datetime, end: datetime, pauses: Sequence[PauseSpan] = ()
    ) -> float:
        """[start, end) 内扣除暂停后的工作秒数。"""
        if end <= start:
            return 0.0
        total = 0.0
        for segment in self.work_segments(start, end):
            for seg_start, seg_end in subtract_pauses([segment], pauses):
                total += (seg_end - seg_start).total_seconds()
        return total

    def breach_instant(
        self, start: datetime, budget_seconds: int,
        pauses: Sequence[PauseSpan] = (),
    ) -> datetime | None:
        """工作预算耗尽的时刻。

        已耗尽时返回过去时刻；计时中返回未来的预计超时时刻；
        暂停中（开放暂停冻结计时）或预算超出推算视界时返回 None。
        """
        horizon = start + timedelta(days=HORIZON_DAYS)
        open_pauses = [p for p in pauses if p[1] is None]
        if open_pauses:
            # 开放暂停之后时间冻结，推算到暂停起点即可。
            horizon = min(horizon, min(p[0] for p in open_pauses))
        remaining = float(budget_seconds)
        for segment in self.work_segments(start, horizon):
            for seg_start, seg_end in subtract_pauses([segment], pauses):
                duration = (seg_end - seg_start).total_seconds()
                if remaining <= duration:
                    return seg_start + timedelta(seconds=remaining)
                remaining -= duration
        return None


def windows_to_jsonable(
    windows: tuple[tuple[tuple[int, int], ...], ...]
) -> dict[str, list[list[int]]]:
    """持久化用：星期键 → [[起分, 止分], …]。"""
    return {
        WEEKDAY_KEYS[day]: [[s, e] for s, e in wins]
        for day, wins in enumerate(windows)
        if wins
    }


def windows_from_jsonable(
    data: dict[str, list[list[int]]]
) -> tuple[tuple[tuple[int, int], ...], ...]:
    normalized: dict[int, tuple[tuple[int, int], ...]] = {}
    for key, wins in data.items():
        normalized[WEEKDAY_KEYS.index(key)] = tuple((int(s), int(e)) for s, e in wins)
    return tuple(normalized.get(day, ()) for day in range(7))
