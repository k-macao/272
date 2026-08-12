"""时间解析与校验的单元测试 —— 这是整个项目最该被测的部分."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from octopus.models import TimeQuality
from octopus.timeutil import (
    CN_TZ,
    age_minutes,
    humanize,
    in_quiet_hours,
    is_future,
    parse,
    parse_clock,
    quiet_remaining_seconds,
    within_window,
)

REF = datetime(2026, 7, 27, 10, 30, 0, tzinfo=CN_TZ)


class TestParseAbsolute(unittest.TestCase):
    def test_standard_datetime(self):
        dt, q, raw = parse("2026-07-27 09:30:01")
        self.assertEqual(dt, datetime(2026, 7, 27, 9, 30, 1, tzinfo=CN_TZ))
        self.assertIs(q, TimeQuality.EXACT)
        self.assertEqual(raw, "2026-07-27 09:30:01")

    def test_eastmoney_colon_milliseconds(self):
        """东财公告的 2026-07-27 07:53:06:817 —— 毫秒用冒号分隔的畸形格式。"""
        dt, q, _ = parse("2026-07-27 07:53:06:817")
        self.assertEqual(dt, datetime(2026, 7, 27, 7, 53, 6, tzinfo=CN_TZ))
        self.assertIs(q, TimeQuality.EXACT)

    def test_iso_t_separator(self):
        dt, q, _ = parse("2026-07-27T09:30:00")
        self.assertEqual(dt.hour, 9)
        self.assertIs(q, TimeQuality.EXACT)

    def test_date_only_is_weak(self):
        dt, q, _ = parse("2026-07-27")
        self.assertEqual(dt, datetime(2026, 7, 27, 0, 0, tzinfo=CN_TZ))
        self.assertIs(q, TimeQuality.DATE)

    def test_chinese_date(self):
        dt, q, _ = parse("2026年07月27日")
        self.assertEqual(dt.month, 7)
        self.assertIs(q, TimeQuality.DATE)

    def test_unix_seconds(self):
        ts = int(REF.timestamp())
        dt, q, _ = parse(ts)
        self.assertEqual(dt, REF)
        self.assertIs(q, TimeQuality.EXACT)

    def test_unix_seconds_as_string(self):
        """同花顺涨停池返回的是字符串型时间戳。"""
        dt, q, _ = parse(str(int(REF.timestamp())))
        self.assertEqual(dt, REF)
        self.assertIs(q, TimeQuality.EXACT)

    def test_unix_milliseconds(self):
        dt, _, _ = parse(int(REF.timestamp() * 1000))
        self.assertEqual(dt, REF)

    def test_datetime_passthrough(self):
        dt, q, _ = parse(REF)
        self.assertEqual(dt, REF)
        self.assertIs(q, TimeQuality.EXACT)

    def test_naive_datetime_gets_cn_tz(self):
        dt, _, _ = parse(datetime(2026, 7, 27, 10, 0))
        self.assertEqual(dt.tzinfo.utcoffset(None), timedelta(hours=8))


class TestParseRelative(unittest.TestCase):
    def test_minutes_ago(self):
        dt, q, _ = parse("46分钟前", ref=REF)
        self.assertEqual(dt, REF - timedelta(minutes=46))
        self.assertIs(q, TimeQuality.DERIVED)

    def test_hours_ago(self):
        dt, q, _ = parse("2小时前", ref=REF)
        self.assertEqual(dt, REF - timedelta(hours=2))
        self.assertIs(q, TimeQuality.DERIVED)

    def test_just_now(self):
        dt, q, _ = parse("刚刚", ref=REF)
        self.assertEqual(dt, REF)
        self.assertIs(q, TimeQuality.DERIVED)


class TestParseYearless(unittest.TestCase):
    def test_month_day_time(self):
        dt, q, _ = parse("07-26 15:00", ref=REF)
        self.assertEqual(dt, datetime(2026, 7, 26, 15, 0, tzinfo=CN_TZ))
        self.assertIs(q, TimeQuality.EXACT)

    def test_time_only_today(self):
        dt, _, _ = parse("09:45", ref=REF)
        self.assertEqual(dt, datetime(2026, 7, 27, 9, 45, tzinfo=CN_TZ))

    def test_time_only_rolls_back_to_yesterday(self):
        """23:50 相对于今天 10:30 只可能是昨天的。"""
        dt, _, _ = parse("23:50", ref=REF)
        self.assertEqual(dt, datetime(2026, 7, 26, 23, 50, tzinfo=CN_TZ))


class TestParseFailures(unittest.TestCase):
    def test_empty_returns_missing(self):
        dt, q, _ = parse("")
        self.assertIsNone(dt)
        self.assertIs(q, TimeQuality.MISSING)

    def test_none_returns_missing(self):
        dt, q, _ = parse(None)
        self.assertIsNone(dt)
        self.assertIs(q, TimeQuality.MISSING)

    def test_garbage_returns_missing(self):
        """解析不出来必须返回 None，绝不能拿当前时间冒充。"""
        dt, q, _ = parse("不久前的某一天")
        self.assertIsNone(dt)
        self.assertIs(q, TimeQuality.MISSING)

    def test_raw_preserved_on_failure(self):
        _, _, raw = parse("乱码时间")
        self.assertEqual(raw, "乱码时间")


class TestFreshness(unittest.TestCase):
    def test_within_window(self):
        self.assertTrue(within_window(REF - timedelta(minutes=100), 180, ref=REF))

    def test_outside_window(self):
        self.assertFalse(within_window(REF - timedelta(minutes=200), 180, ref=REF))

    def test_boundary_is_inclusive(self):
        self.assertTrue(within_window(REF - timedelta(minutes=180), 180, ref=REF))

    def test_slight_future_tolerated(self):
        """源站时钟快 3 分钟属于正常漂移，应放行。"""
        self.assertTrue(within_window(REF + timedelta(minutes=3), 180, ref=REF))
        self.assertFalse(is_future(REF + timedelta(minutes=3), ref=REF))

    def test_far_future_rejected(self):
        """明显指向未来的时间戳是脏数据。"""
        future = REF + timedelta(hours=5)
        self.assertTrue(is_future(future, ref=REF))
        self.assertFalse(within_window(future, 180, ref=REF))

    def test_age_minutes(self):
        self.assertAlmostEqual(age_minutes(REF - timedelta(minutes=42), ref=REF), 42.0)


class TestHumanize(unittest.TestCase):
    def test_just_now(self):
        self.assertEqual(humanize(REF, ref=REF), "刚刚")

    def test_minutes(self):
        self.assertEqual(humanize(REF - timedelta(minutes=25), ref=REF), "25分钟前")

    def test_today(self):
        self.assertEqual(humanize(REF - timedelta(hours=2), ref=REF), "今天 08:30")

    def test_yesterday(self):
        self.assertEqual(humanize(REF - timedelta(days=1), ref=REF), "昨天 10:30")

    def test_older(self):
        self.assertEqual(humanize(REF - timedelta(days=3), ref=REF), "07-24 10:30")


class TestParseClock(unittest.TestCase):
    def test_string_forms(self):
        self.assertEqual(parse_clock("23:00"), (23, 0))
        self.assertEqual(parse_clock("07:30"), (7, 30))
        self.assertEqual(parse_clock("7"), (7, 0))
        self.assertEqual(parse_clock(" 23：00 "), (23, 0))  # 全角冒号也认

    def test_number_forms(self):
        self.assertEqual(parse_clock(7), (7, 0))
        self.assertEqual(parse_clock(23), (23, 0))

    def test_yaml_unquoted_sexagesimal(self):
        """PyYAML 会把未加引号的 23:00 解析成六十进制整数 1380（23*60），兜住。"""
        self.assertEqual(parse_clock(1380), (23, 0))
        self.assertEqual(parse_clock(420), (7, 0))
        self.assertIsNone(parse_clock(1500))  # 25:00 越界

    def test_invalid(self):
        for bad in ("", " ", None, "abc", "25:00", "23:60", "-1", -1, True, 24):
            self.assertIsNone(parse_clock(bad), f"应当判非法：{bad!r}")


class TestQuietHours(unittest.TestCase):
    """夜间免打扰：北京时间 23:00 后暂停、次日 07:00 起床。"""

    START, END = "23:00", "07:00"

    @staticmethod
    def _ref(hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 7, 27, hour, minute, 0, tzinfo=CN_TZ)

    def _quiet(self, hour: int, minute: int = 0) -> bool:
        return in_quiet_hours(self.START, self.END, ref=self._ref(hour, minute))

    def test_overnight_boundaries(self):
        self.assertFalse(self._quiet(22, 59))  # 还没到点
        self.assertTrue(self._quiet(23, 0))    # 到点即睡
        self.assertTrue(self._quiet(23, 59))
        self.assertTrue(self._quiet(0, 0))     # 跨午夜
        self.assertTrue(self._quiet(6, 59))
        self.assertFalse(self._quiet(7, 0))    # 07:00 起床
        self.assertFalse(self._quiet(10, 30))
        self.assertFalse(self._quiet(15, 0))

    def test_same_day_window(self):
        ref = self._ref
        self.assertFalse(in_quiet_hours("09:00", "12:00", ref=ref(8, 59)))
        self.assertTrue(in_quiet_hours("09:00", "12:00", ref=ref(9, 0)))
        self.assertTrue(in_quiet_hours("09:00", "12:00", ref=ref(11, 59)))
        self.assertFalse(in_quiet_hours("09:00", "12:00", ref=ref(12, 0)))

    def test_disabled_forms(self):
        """任一端留空/非法、或起止相同，一律视为未开启，避免歧义造成全天静默。"""
        night = self._ref(23, 30)
        self.assertFalse(in_quiet_hours("", "07:00", ref=night))
        self.assertFalse(in_quiet_hours("23:00", "", ref=night))
        self.assertFalse(in_quiet_hours("23:00", "23:00", ref=night))
        self.assertFalse(in_quiet_hours("垃圾", "07:00", ref=night))

    def test_remaining_seconds(self):
        self.assertEqual(
            quiet_remaining_seconds(self.START, self.END, ref=self._ref(23, 30)),
            7 * 3600 + 30 * 60,  # 23:30 -> 07:00
        )
        self.assertEqual(
            quiet_remaining_seconds(self.START, self.END, ref=self._ref(6, 0)),
            3600,
        )
        self.assertEqual(quiet_remaining_seconds(self.START, self.END, ref=self._ref(10, 30)), 0)
        self.assertEqual(quiet_remaining_seconds("", "", ref=self._ref(23, 30)), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
