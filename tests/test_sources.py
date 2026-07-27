"""用真实响应样本回归各源的解析逻辑（离线，不发网络请求）."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from octopus.models import TimeQuality
from octopus.sources.cninfo import CninfoSource
from octopus.sources.eastmoney import EastmoneySource
from octopus.sources.ifind import IFindSource
from octopus.sources.iwencai import IWenCaiSource
from octopus.sources.jisilu import JisiluSource
from octopus.sources.research import HiborSource, MybbondSource
from octopus.sources.stats import StatsSource
from octopus.sources.stockstar import StockstarSource
from octopus.timeutil import CN_TZ
from tests.fixtures import samples

# 样本抓取时刻：2026-07-27 周一 10:10（盘中）
REF = datetime(2026, 7, 27, 10, 10, 0, tzinfo=CN_TZ)


class FakeHttp:
    """按 URL 关键字返回预置响应的假 HTTP 客户端。"""

    def __init__(self, json_map=None, text_map=None):
        self.json_map = json_map or {}
        self.text_map = text_map or {}
        self.calls: list[str] = []

    def _match(self, url, mapping):
        self.calls.append(url)
        for key, value in mapping.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"FakeHttp 没有为 {url} 准备响应")

    def json(self, url, **kwargs):
        return self._match(url, self.json_map)

    def text(self, url, **kwargs):
        return self._match(url, self.text_map)

    def post_json(self, url, payload, **kwargs):
        return self._match(url, self.json_map)


class TestIWenCai(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp(
            json_map={
                "limit_up_pool": samples.LIMIT_UP_POOL,
                "hot_list/v1/stock": samples.HOT_STOCKS,
            }
        )

    def test_parses_limit_up_with_unix_timestamps(self):
        items = IWenCaiSource(self.http, {}).collect()
        limit_ups = [i for i in items if i.extra.get("kind") == "涨停"]
        self.assertEqual(len(limit_ups), 2)

        first = limit_ups[0]
        self.assertIn("嘉美包装", first.title)
        self.assertIn("002969", first.title)
        self.assertIn("首板", first.title)
        self.assertIs(first.time_quality, TimeQuality.EXACT)
        # 1785116229 -> 2026-07-27 09:37:09 北京时间
        self.assertEqual(first.published_at.strftime("%Y-%m-%d %H:%M"), "2026-07-27 09:37")

    def test_limit_up_summary_has_metrics(self):
        items = IWenCaiSource(self.http, {}).collect()
        first = next(i for i in items if "嘉美包装" in i.title)
        self.assertIn("涨幅 9.98%", first.summary)
        self.assertIn("换手 1.57%", first.summary)
        self.assertIn("封单 3.76亿", first.summary)

    def test_reason_becomes_tags(self):
        items = IWenCaiSource(self.http, {}).collect()
        first = next(i for i in items if "嘉美包装" in i.title)
        self.assertIn("中报预增", first.tags)

    def test_hot_list_only_during_trading_hours(self):
        """非交易时段不产出人气榜，避免夜里刷屏。"""
        import octopus.sources.iwencai as mod

        original = mod.now
        try:
            mod.now = lambda: datetime(2026, 7, 26, 22, 0, tzinfo=CN_TZ)  # 周日晚上
            items = IWenCaiSource(self.http, {}).collect()
            self.assertEqual([i for i in items if i.extra.get("kind") == "热度榜"], [])
        finally:
            mod.now = original

    def test_hot_list_during_trading_hours(self):
        import octopus.sources.iwencai as mod

        original = mod.now
        try:
            mod.now = lambda: REF
            items = IWenCaiSource(self.http, {}).collect()
            hot = [i for i in items if i.extra.get("kind") == "热度榜"]
            self.assertEqual(len(hot), 1)
            self.assertIn("长鑫科技", hot[0].summary)
            self.assertIs(hot[0].time_quality, TimeQuality.DERIVED)
            self.assertEqual(hot[0].published_at.minute, 0)  # 对齐到整点
        finally:
            mod.now = original


class TestCninfo(unittest.TestCase):
    def test_falls_back_to_eastmoney_when_cninfo_fails(self):
        from octopus.http import FetchError

        http = FakeHttp(
            json_map={
                "hisAnnouncement": FetchError("WAF 拦截"),
                "security/ann": samples.EASTMONEY_ANN,
            }
        )
        items = CninfoSource(http, {}).collect()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].extra["via"], "eastmoney")

    def test_parses_colon_millisecond_timestamp(self):
        """display_time = 2026-07-27 07:53:06:817 这种畸形格式必须能解析。"""
        from octopus.http import FetchError

        http = FakeHttp(
            json_map={
                "hisAnnouncement": FetchError("boom"),
                "security/ann": samples.EASTMONEY_ANN,
            }
        )
        items = CninfoSource(http, {}).collect()
        self.assertIs(items[0].time_quality, TimeQuality.EXACT)
        self.assertEqual(
            items[0].published_at.strftime("%Y-%m-%d %H:%M:%S"), "2026-07-27 07:53:06"
        )

    def test_announcement_url_built(self):
        from octopus.http import FetchError

        http = FakeHttp(
            json_map={"hisAnnouncement": FetchError("x"), "security/ann": samples.EASTMONEY_ANN}
        )
        items = CninfoSource(http, {}).collect()
        self.assertIn("002569", items[0].url)
        self.assertIn("AN202607271827362101", items[0].url)


class TestEastmoney(unittest.TestCase):
    def test_parses_var_wrapped_json(self):
        http = FakeHttp(text_map={"kuaixun": samples.EASTMONEY_KUAIXUN_RAW})
        items = EastmoneySource(http, {}).collect()
        self.assertEqual(len(items), 2)

    def test_signal_items_sorted_first(self):
        """题材异动（'快速拉升'）应排在普通指数播报前面。"""
        http = FakeHttp(text_map={"kuaixun": samples.EASTMONEY_KUAIXUN_RAW})
        items = EastmoneySource(http, {}).collect()
        self.assertIn("CPO", items[0].title)
        self.assertTrue(items[0].extra["signal"])

    def test_bracket_titles_normalized(self):
        http = FakeHttp(text_map={"kuaixun": samples.EASTMONEY_KUAIXUN_RAW})
        items = EastmoneySource(http, {}).collect()
        digest = next(i for i in items if "创业板指" in i.title).summary
        self.assertNotIn("【", digest)

    def test_timestamps_exact(self):
        http = FakeHttp(text_map={"kuaixun": samples.EASTMONEY_KUAIXUN_RAW})
        items = EastmoneySource(http, {}).collect()
        self.assertTrue(all(i.time_quality is TimeQuality.EXACT for i in items))
        self.assertEqual(items[0].published_at.strftime("%H:%M:%S"), "10:00:57")


class TestJisilu(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp(json_map={"cb_list_new": samples.JISILU_CB})

    def test_redeem_alert_prioritized(self):
        import octopus.sources.jisilu as mod

        original = mod.now
        try:
            mod.now = lambda: REF
            items = JisiluSource(self.http, {}).collect()
            self.assertIn("强赎提醒", items[0].title)
            self.assertIn("Z精达转", items[0].title)
        finally:
            mod.now = original

    def test_time_only_field_resolved_to_today(self):
        """last_time 只有 '10:05:50'，要补成今天的日期。"""
        import octopus.sources.jisilu as mod

        original = mod.now
        try:
            mod.now = lambda: REF
            items = JisiluSource(self.http, {}).collect()
            self.assertEqual(
                items[0].published_at.strftime("%Y-%m-%d %H:%M:%S"), "2026-07-27 10:05:50"
            )
        finally:
            mod.now = original

    def test_unlisted_bond_without_time_dropped(self):
        """特宝转债待上市、last_time 为 None，不应产出条目。"""
        import octopus.sources.jisilu as mod

        original = mod.now
        try:
            mod.now = lambda: REF
            items = JisiluSource(self.http, {}).collect()
            self.assertNotIn("特宝转债", " ".join(i.title for i in items))
        finally:
            mod.now = original

    def test_mover_detected(self):
        import octopus.sources.jisilu as mod

        original = mod.now
        try:
            mod.now = lambda: REF
            items = JisiluSource(self.http, {}).collect()
            movers = [i for i in items if "转债异动" in i.title]
            self.assertTrue(any("水羊转债" in i.title for i in movers))
            self.assertIn("+7.31%", next(i for i in movers if "水羊" in i.title).title)
        finally:
            mod.now = original


class TestStats(unittest.TestCase):
    def test_parses_rss_with_cdata(self):
        http = FakeHttp(text_map={"rss.xml": samples.STATS_RSS})
        items = StatsSource(http, {}).collect()
        titles = [i.title for i in items]
        self.assertIn("2026年1—6月份全国规模以上工业企业利润增长18.7%", titles)

    def test_pubdate_exact(self):
        http = FakeHttp(text_map={"rss.xml": samples.STATS_RSS})
        items = StatsSource(http, {}).collect()
        self.assertIs(items[0].time_quality, TimeQuality.EXACT)
        self.assertEqual(items[0].published_at.strftime("%H:%M:%S"), "09:30:01")

    def test_key_indicator_tagged(self):
        http = FakeHttp(text_map={"rss.xml": samples.STATS_RSS})
        items = StatsSource(http, {}).collect()
        self.assertIn("工业企业利润", items[0].tags)


class TestStockstar(unittest.TestCase):
    def test_parses_timestamped_list(self):
        http = FakeHttp(text_map={"6095.shtml": samples.STOCKSTAR_HTML})
        items = StockstarSource(http, {}).collect()
        self.assertTrue(items)
        self.assertTrue(all(i.time_quality is TimeQuality.EXACT for i in items))

    def test_deduplicates_prefixed_duplicates(self):
        """站点会把同一条异动发两遍（带/不带'异动快报：'），只应保留一条。"""
        http = FakeHttp(text_map={"6095.shtml": samples.STOCKSTAR_HTML})
        items = StockstarSource(http, {}).collect()
        hongfe = [i for i in items if "宏和科技" in i.title]
        self.assertEqual(len(hongfe), 1)

    def test_entry_without_time_skipped(self):
        http = FakeHttp(text_map={"6095.shtml": samples.STOCKSTAR_HTML})
        items = StockstarSource(http, {}).collect()
        self.assertNotIn("没有时间戳的条目", [i.title for i in items])

    def test_limit_up_down_tagged(self):
        http = FakeHttp(text_map={"6095.shtml": samples.STOCKSTAR_HTML})
        items = StockstarSource(http, {}).collect()
        self.assertIn("涨停", next(i for i in items if "宏和" in i.title).tags)
        self.assertIn("跌停", next(i for i in items if "快意电梯" in i.title).tags)


class TestResearch(unittest.TestCase):
    def test_mybbond_falls_back_to_eastmoney(self):
        from octopus.http import FetchError

        http = FakeHttp(
            json_map={
                "mybbond.com": FetchError("站点不可达"),
                "reportapi": samples.EASTMONEY_REPORT,
            }
        )
        items = MybbondSource(http, {}).collect()
        self.assertEqual(len(items), 2)
        self.assertIn("华源证券", items[0].title)
        self.assertEqual(items[0].extra["via"], "eastmoney")

    def test_report_date_aligned_to_morning(self):
        """研报只有日期，要对齐到早上 8 点而非 00:00，否则窗口判断会失真。"""
        from octopus.http import FetchError

        http = FakeHttp(
            json_map={"mybbond.com": FetchError("x"), "reportapi": samples.EASTMONEY_REPORT}
        )
        items = MybbondSource(http, {}).collect()
        self.assertIs(items[0].time_quality, TimeQuality.DATE)
        self.assertEqual(items[0].published_at.hour, 8)

    def test_hibor_extracts_date_from_table(self):
        http = FakeHttp(text_map={"hibor.com.cn": samples.HIBOR_HTML})
        items = HiborSource(http, {}).collect()
        self.assertTrue(items)
        titles = " ".join(i.title for i in items)
        self.assertIn("中航证券", titles)
        self.assertTrue(all(i.published_at is not None for i in items))


class TestIFind(unittest.TestCase):
    def test_board_snapshot_built(self):
        import octopus.sources.ifind as mod

        http = FakeHttp(
            json_map={"clist/get": samples.EASTMONEY_BOARDS, "kamt": samples.EASTMONEY_HSGT}
        )
        original = mod.now
        try:
            mod.now = lambda: REF
            items = IFindSource(http, {}).collect()
        finally:
            mod.now = original

        board = next(i for i in items if i.extra.get("kind") == "板块热力")
        self.assertIn("个护小家电", board.title)
        self.assertIs(board.time_quality, TimeQuality.DERIVED)
        self.assertEqual(board.published_at.minute, 0)  # 10:10 -> 对齐到 10:00

    def test_northbound_uses_last_non_empty_minute(self):
        import octopus.sources.ifind as mod

        http = FakeHttp(
            json_map={"clist/get": samples.EASTMONEY_BOARDS, "kamt": samples.EASTMONEY_HSGT}
        )
        original = mod.now
        try:
            mod.now = lambda: REF
            items = IFindSource(http, {}).collect()
        finally:
            mod.now = original

        north = next(i for i in items if i.extra.get("kind") == "北向资金")
        self.assertIn("9:31", north.title)          # 最后一个有数的分钟
        self.assertIn("净流入", north.title)
        self.assertIn("0.03亿", north.title)        # 340.80 万 -> 0.03 亿

    def test_nothing_outside_trading_hours(self):
        import octopus.sources.ifind as mod

        http = FakeHttp(json_map={})
        original = mod.now
        try:
            mod.now = lambda: datetime(2026, 7, 27, 20, 0, tzinfo=CN_TZ)
            self.assertEqual(IFindSource(http, {}).collect(), [])
        finally:
            mod.now = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
