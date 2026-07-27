"""时间解析与校验 —— 本项目的核心防线.

所有源站的时间字符串格式各异（有的带毫秒、有的只有日期、有的是
"46分钟前"），这里统一归一到 Asia/Shanghai 时区的 tz-aware datetime，
并标注可信度（TimeQuality），供下游做"新鲜度"判定。

设计原则：宁可丢弃，不可臆造。解析不出来就返回 None，让条目被过滤掉，
绝不用 "抓取时刻" 冒充 "发布时刻"。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .models import TimeQuality

CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# 允许的时钟漂移：源站服务器时间可能比我们快一点点，
# 超过这个阈值的"未来时间"视为脏数据丢弃。
FUTURE_TOLERANCE = timedelta(minutes=10)


def now() -> datetime:
    """当前时间（东八区）。所有比较都以此为基准。"""
    return datetime.now(CN_TZ)


# --------------------------------------------------------------------------
# 绝对时间解析
# --------------------------------------------------------------------------

# 形如 2026-07-27 09:30:01:817 （东财公告接口的毫秒用冒号分隔，非标准）
_MS_COLON = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}):(\d{1,3})$")

_PATTERNS: tuple[tuple[str, TimeQuality], ...] = (
    ("%Y-%m-%d %H:%M:%S", TimeQuality.EXACT),
    ("%Y-%m-%dT%H:%M:%S", TimeQuality.EXACT),
    ("%Y-%m-%d %H:%M", TimeQuality.EXACT),
    ("%Y/%m/%d %H:%M:%S", TimeQuality.EXACT),
    ("%Y/%m/%d %H:%M", TimeQuality.EXACT),
    ("%Y年%m月%d日 %H:%M", TimeQuality.EXACT),
    ("%Y%m%d%H%M%S", TimeQuality.EXACT),
    ("%Y-%m-%d", TimeQuality.DATE),
    ("%Y/%m/%d", TimeQuality.DATE),
    ("%Y年%m月%d日", TimeQuality.DATE),
    ("%Y%m%d", TimeQuality.DATE),
)

# "46分钟前" / "1小时前" / "3天前" / "刚刚"
_RELATIVE = re.compile(r"^(\d+)\s*(秒|分钟|分|小时|天|周)前$")

_RELATIVE_UNITS = {
    "秒": "seconds",
    "分": "minutes",
    "分钟": "minutes",
    "小时": "hours",
    "天": "days",
    "周": "weeks",
}


def parse(value: object, *, ref: datetime | None = None) -> tuple[datetime | None, TimeQuality, str]:
    """把任意源站时间表示解析成 (datetime, 质量, 原始串).

    支持：
      - datetime 对象（naive 视为东八区）
      - Unix 时间戳（int/float/数字串，自动识别秒/毫秒）
      - 常见中式日期时间字符串
      - "46分钟前" 之类的相对时间（需要 ref，默认取 now()）

    解析失败返回 (None, MISSING, 原始串)。
    """
    raw = "" if value is None else str(value).strip()
    if not raw:
        return None, TimeQuality.MISSING, ""

    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=CN_TZ)
        return dt.astimezone(CN_TZ), TimeQuality.EXACT, raw

    # Unix 时间戳（10 位秒 / 13 位毫秒）
    if isinstance(value, (int, float)) or re.fullmatch(r"\d{10}|\d{13}", raw):
        ts = float(value if isinstance(value, (int, float)) else raw)
        if ts > 1e11:  # 毫秒
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, CN_TZ), TimeQuality.EXACT, raw
        except (OverflowError, OSError, ValueError):
            return None, TimeQuality.MISSING, raw

    # 相对时间
    if raw in ("刚刚", "刚才", "just now"):
        return (ref or now()), TimeQuality.DERIVED, raw
    rel = _RELATIVE.match(raw)
    if rel:
        amount, unit = int(rel.group(1)), rel.group(2)
        key = _RELATIVE_UNITS.get(unit)
        if key:
            base = ref or now()
            return base - timedelta(**{key: amount}), TimeQuality.DERIVED, raw

    text = _clean(raw)

    # 东财 "2026-07-27 09:30:01:817" 这种毫秒用冒号的畸形格式
    ms = _MS_COLON.match(text)
    if ms:
        text = ms.group(1)

    for fmt, quality in _PATTERNS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return dt.replace(tzinfo=CN_TZ), quality, raw

    # 无年份的 "07-27 09:30" / "9:30" —— 按最近的合理日期补齐
    dt = _parse_yearless(text, ref or now())
    if dt:
        return dt, TimeQuality.EXACT, raw

    return None, TimeQuality.MISSING, raw


_CLEAN_PREFIX = re.compile(r"^(发布时间|时间|更新时间|发表于|于)[:：]?\s*")


def _clean(text: str) -> str:
    text = _CLEAN_PREFIX.sub("", text)
    text = text.replace("　", " ").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


_YEARLESS_MD = re.compile(r"^(\d{1,2})[-/月](\d{1,2})日?[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_YEARLESS_HM = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def _parse_yearless(text: str, ref: datetime) -> datetime | None:
    """补齐缺失的年份/日期，就近取不晚于参考时刻的那一天。"""
    md = _YEARLESS_MD.match(text)
    if md:
        month, day, hour, minute = (int(md.group(i)) for i in range(1, 5))
        second = int(md.group(5) or 0)
        for year in (ref.year, ref.year - 1):
            try:
                dt = datetime(year, month, day, hour, minute, second, tzinfo=CN_TZ)
            except ValueError:
                continue
            if dt <= ref + FUTURE_TOLERANCE:
                return dt
        return None

    hm = _YEARLESS_HM.match(text)
    if hm:
        hour, minute = int(hm.group(1)), int(hm.group(2))
        second = int(hm.group(3) or 0)
        try:
            dt = ref.replace(hour=hour, minute=minute, second=second, microsecond=0)
        except ValueError:
            return None
        if dt > ref + FUTURE_TOLERANCE:  # 只可能是昨天的
            dt -= timedelta(days=1)
        return dt

    return None


# --------------------------------------------------------------------------
# 新鲜度校验
# --------------------------------------------------------------------------


def is_future(dt: datetime, *, ref: datetime | None = None) -> bool:
    """时间戳是否超出容忍范围地指向未来（脏数据特征）。"""
    return dt > (ref or now()) + FUTURE_TOLERANCE


def within_window(dt: datetime, window_minutes: int, *, ref: datetime | None = None) -> bool:
    """是否落在 [now - window, now + 容忍] 区间内 —— 即"够新"。"""
    base = ref or now()
    if is_future(dt, ref=base):
        return False
    return dt >= base - timedelta(minutes=window_minutes)


def age_minutes(dt: datetime, *, ref: datetime | None = None) -> float:
    return ((ref or now()) - dt).total_seconds() / 60.0


def humanize(dt: datetime, *, ref: datetime | None = None) -> str:
    """给推送用的人话时间：刚刚 / 12分钟前 / 今天 09:30 / 07-26 15:00。"""
    base = ref or now()
    delta = base - dt
    mins = delta.total_seconds() / 60.0
    if -1 <= mins < 1:
        return "刚刚"
    if 0 <= mins < 60:
        return f"{int(mins)}分钟前"
    if dt.date() == base.date():
        return f"今天 {dt:%H:%M}"
    if dt.date() == (base - timedelta(days=1)).date():
        return f"昨天 {dt:%H:%M}"
    return f"{dt:%m-%d %H:%M}"


def stamp(dt: datetime | None = None) -> str:
    return f"{dt or now():%Y-%m-%d %H:%M:%S}"
