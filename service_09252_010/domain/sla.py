"""复核服务时限的领域规则：机构日历、业务秒计时与截止时间推算。

- 日历以固定 UTC 偏移定义本地时间，不依赖时区数据库，保证结果可复算；
- 工作窗口按星期几配置，可跨午夜（end <= start 表示延伸至次日）；
- 节假日整日不计时；跨午夜窗口归属其开始日，次日为节假日也不中断；
- 暂停区间不计时；瞬时一律以 ISO8601 UTC 串表示。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .errors import ValidationError

# 计时推算的区间上限（约十年），防止异常输入导致无限循环。
MAX_SPAN_DAYS = 3660

_HM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class WorkWindow:
    """一个工作日工作窗口；end <= start 表示跨午夜延伸至次日。"""

    weekday: int  # 0=周一 … 6=周日
    start: str  # "HH:MM" 本地时间
    end: str  # "HH:MM" 本地时间


@dataclass(frozen=True)
class InstitutionCalendar:
    """机构日历：固定 UTC 偏移 + 每周工作窗口 + 节假日（本地日期）。"""

    institution_id: str
    utc_offset_minutes: int
    work_windows: tuple[WorkWindow, ...]
    holidays: tuple[str, ...]

    @property
    def offset(self) -> timedelta:
        return timedelta(minutes=self.utc_offset_minutes)


def parse_instant(raw: str) -> datetime:
    """ISO8601 串 → UTC aware 时间；朴素时间按 UTC 处理。"""
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"非法时间格式: {raw!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_instant(dt: datetime) -> str:
    """与系统其余部分一致的 ISO8601 UTC 串。"""
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def build_calendar(institution_id: str, *, utc_offset_minutes: int,
                   work_windows: list[dict], holidays: list[str]
                   ) -> InstitutionCalendar:
    """校验并构造机构日历；非法输入抛 ValidationError。"""
    if not institution_id or not isinstance(institution_id, str):
        raise ValidationError("institution_id 缺失")
    if isinstance(utc_offset_minutes, bool) or not isinstance(utc_offset_minutes, int):
        raise ValidationError("utc_offset_minutes 必须为整数分钟")
    if not -12 * 60 <= utc_offset_minutes <= 14 * 60:
        raise ValidationError("utc_offset_minutes 超出合理范围 [-720, 840]")
    if not isinstance(work_windows, list) or not work_windows:
        raise ValidationError("work_windows 至少需要一个工作窗口")
    windows: list[WorkWindow] = []
    seen: set[tuple[int, str, str]] = set()
    for raw in work_windows:
        if not isinstance(raw, dict):
            raise ValidationError("工作窗口必须为对象 {weekday, start, end}")
        weekday = raw.get("weekday")
        if isinstance(weekday, bool) or not isinstance(weekday, int) \
                or not 0 <= weekday <= 6:
            raise ValidationError(f"非法 weekday: {weekday!r}，应为 0(周一) 至 6(周日)")
        start, end = raw.get("start"), raw.get("end")
        for label, value in (("start", start), ("end", end)):
            if not isinstance(value, str) or not _HM_RE.match(value):
                raise ValidationError(f"工作窗口 {label} 非法: {value!r}，应为 HH:MM")
        if start == end:
            raise ValidationError("工作窗口起止时间不能相同（跨午夜请令 end < start）")
        key = (weekday, start, end)
        if key not in seen:
            seen.add(key)
            windows.append(WorkWindow(weekday, start, end))
    if not isinstance(holidays, list):
        raise ValidationError("holidays 必须为 YYYY-MM-DD 字符串数组")
    holiday_dates: list[str] = []
    for raw in holidays:
        if not isinstance(raw, str) or not _DATE_RE.match(raw):
            raise ValidationError(f"非法节假日格式: {raw!r}，应为 YYYY-MM-DD")
        try:
            date.fromisoformat(raw)
        except ValueError:
            raise ValidationError(f"非法节假日日期: {raw!r}") from None
        if raw not in holiday_dates:
            holiday_dates.append(raw)
    windows.sort(key=lambda w: (w.weekday, w.start, w.end))
    return InstitutionCalendar(
        institution_id=institution_id,
        utc_offset_minutes=utc_offset_minutes,
        work_windows=tuple(windows),
        holidays=tuple(sorted(holiday_dates)),
    )


# ---- 区间工具 ----

def _merge(intervals: list[tuple[datetime, datetime]]
           ) -> list[tuple[datetime, datetime]]:
    """排序并合并重叠/相接区间。"""
    merged: list[tuple[datetime, datetime]] = []
    for lo, hi in sorted(intervals):
        if hi <= lo:
            continue
        if merged and lo <= merged[-1][1]:
            if hi > merged[-1][1]:
                merged[-1] = (merged[-1][0], hi)
        else:
            merged.append((lo, hi))
    return merged


def _subtract(intervals: list[tuple[datetime, datetime]],
              cuts: list[tuple[datetime, datetime]]
              ) -> list[tuple[datetime, datetime]]:
    """从区间集合中扣除 cuts（如暂停区间）。"""
    remaining = list(intervals)
    for cut_lo, cut_hi in sorted(cuts):
        next_round: list[tuple[datetime, datetime]] = []
        for lo, hi in remaining:
            if cut_hi <= lo or cut_lo >= hi:
                next_round.append((lo, hi))
                continue
            if lo < cut_lo:
                next_round.append((lo, cut_lo))
            if cut_hi < hi:
                next_round.append((cut_hi, hi))
        remaining = next_round
    return remaining


def _hm(raw: str) -> time:
    return time(int(raw[:2]), int(raw[3:5]))


def _local_date(cal: InstitutionCalendar, dt_utc: datetime) -> date:
    """UTC 瞬时对应的机构本地日期。"""
    return (dt_utc.replace(tzinfo=None) + cal.offset).date()


def _local_to_utc(cal: InstitutionCalendar, wall: datetime) -> datetime:
    return (wall - cal.offset).replace(tzinfo=timezone.utc)


def _day_windows_utc(cal: InstitutionCalendar,
                     by_weekday: dict[int, list[tuple[time, time]]],
                     holidays: set[str],
                     local_day: date) -> list[tuple[datetime, datetime]]:
    """归属于某个本地日期的工作时段（UTC，未合并）。

    节假日整日无窗口；跨午夜窗口归属其开始日，延伸至次日的部分
    不因次日是节假日而截断。
    """
    if local_day.isoformat() in holidays:
        return []
    result: list[tuple[datetime, datetime]] = []
    for start_t, end_t in by_weekday.get(local_day.weekday(), ()):
        start_wall = datetime.combine(local_day, start_t)
        end_wall = datetime.combine(local_day, end_t)
        if end_wall <= start_wall:
            end_wall += timedelta(days=1)  # 跨午夜延伸至次日
        result.append((_local_to_utc(cal, start_wall),
                       _local_to_utc(cal, end_wall)))
    return result


def _windows_by_weekday(cal: InstitutionCalendar
                        ) -> dict[int, list[tuple[time, time]]]:
    grouped: dict[int, list[tuple[time, time]]] = {}
    for window in cal.work_windows:
        grouped.setdefault(window.weekday, []).append(
            (_hm(window.start), _hm(window.end))
        )
    return grouped


def _working_segments(cal: InstitutionCalendar, lo: datetime, hi: datetime
                      ) -> list[tuple[datetime, datetime]]:
    """[lo, hi] 内的工作时段（UTC，裁剪并合并）。前溯一日以覆盖跨午夜窗口。"""
    by_weekday = _windows_by_weekday(cal)
    holidays = set(cal.holidays)
    first = _local_date(cal, lo) - timedelta(days=1)
    last = _local_date(cal, hi)
    if (last - first).days > MAX_SPAN_DAYS:
        raise ValidationError("计时区间超出可计算范围")
    segments: list[tuple[datetime, datetime]] = []
    day = first
    while day <= last:
        segments.extend(_day_windows_utc(cal, by_weekday, holidays, day))
        day += timedelta(days=1)
    return [(max(a, lo), min(b, hi))
            for a, b in _merge(segments) if min(b, hi) > max(a, lo)]


def chargeable_seconds(cal: InstitutionCalendar, start: str, end: str,
                       pauses: list[tuple[str, str]] | None = None) -> float:
    """[start, end] 内应计时的业务秒数：工作时段扣除暂停区间。"""
    start_dt, end_dt = parse_instant(start), parse_instant(end)
    if end_dt <= start_dt:
        return 0.0
    cuts = [(parse_instant(a), parse_instant(b)) for a, b in (pauses or [])]
    total = 0.0
    for lo, hi in _working_segments(cal, start_dt, end_dt):
        for free_lo, free_hi in _subtract([(lo, hi)], cuts):
            total += (free_hi - free_lo).total_seconds()
    return total


def deadline_after(cal: InstitutionCalendar, start: str, limit_seconds: float,
                   pauses: list[tuple[str, str]] | None = None) -> datetime:
    """从 start 起累计 limit_seconds 业务秒（扣除暂停）的截止瞬时。"""
    start_dt = parse_instant(start)
    if limit_seconds <= 0:
        return start_dt
    cuts = sorted((parse_instant(a), parse_instant(b)) for a, b in (pauses or []))
    by_weekday = _windows_by_weekday(cal)
    holidays = set(cal.holidays)
    remaining = float(limit_seconds)
    cursor = start_dt
    day = _local_date(cal, start_dt) - timedelta(days=1)
    for _ in range(MAX_SPAN_DAYS):
        for a, b in _merge(_day_windows_utc(cal, by_weekday, holidays, day)):
            if b <= cursor:
                continue
            lo = max(a, cursor)
            for free_lo, free_hi in _subtract([(lo, b)], cuts):
                duration = (free_hi - free_lo).total_seconds()
                if duration >= remaining:
                    return free_lo + timedelta(seconds=remaining)
                remaining -= duration
            cursor = max(cursor, b)
        day += timedelta(days=1)
    raise ValidationError("时限在可计算范围内无法满足（工作日历过于稀疏）")
