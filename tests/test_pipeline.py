"""源流水线（时间过滤 + 去重）与渲染的测试."""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from octopus.models import Item, SourceResult, TimeQuality
from octopus.render import (
    CARD_BG,
    PUSH_TITLE,
    ROW_BG_A,
    ROW_BG_B,
    render_html,
    render_title,
)
from octopus.sources.base import Source
from octopus.state import SeenStore
from octopus.timeutil import CN_TZ

REF = datetime(2026, 7, 27, 10, 30, 0, tzinfo=CN_TZ)


def item(title: str, minutes_ago: float, quality=TimeQuality.EXACT, url: str = "") -> Item:
    return Item(
        source="demo",
        source_label="示例源",
        title=title,
        url=url or f"https://example.com/{title}",
        published_at=REF - timedelta(minutes=minutes_ago),
        time_quality=quality,
    )


class FakeSource(Source):
    name = "demo"
    label = "示例源"

    def __init__(self, items, allow_date_only=False):
        super().__init__(http=None, config={"limit": 50})
        self._items = items
        self.allow_date_only = allow_date_only

    def collect(self):
        return list(self._items)


class BrokenSource(Source):
    name = "broken"
    label = "会炸的源"

    def __init__(self):
        super().__init__(http=None, config={})

    def collect(self):
        raise RuntimeError("接口 502")


class TestSourcePipeline(unittest.TestCase):
    def test_keeps_fresh_items(self):
        src = FakeSource([item("新消息", 10), item("稍旧", 100)])
        result = src.run(window_minutes=180, seen=None, ref=REF)
        self.assertEqual(result.kept, 2)
        self.assertEqual(result.fetched, 2)

    def test_drops_stale_items(self):
        src = FakeSource([item("新的", 10), item("太旧了", 400)])
        result = src.run(window_minutes=180, seen=None, ref=REF)
        self.assertEqual(result.kept, 1)
        self.assertEqual(result.dropped_stale, 1)
        self.assertEqual(result.items[0].title, "新的")

    def test_drops_items_without_time(self):
        """没有时间戳的条目一律丢弃 —— 核心要求。"""
        bad = Item(source="demo", source_label="示例源", title="没有时间的消息")
        src = FakeSource([item("正常", 5), bad])
        result = src.run(window_minutes=180, seen=None, ref=REF)
        self.assertEqual(result.kept, 1)
        self.assertEqual(result.dropped_no_time, 1)

    def test_drops_future_timestamps(self):
        future = Item(
            source="demo",
            source_label="示例源",
            title="来自未来",
            published_at=REF + timedelta(hours=3),
            time_quality=TimeQuality.EXACT,
        )
        src = FakeSource([future])
        result = src.run(window_minutes=180, seen=None, ref=REF)
        self.assertEqual(result.kept, 0)
        self.assertEqual(result.dropped_future, 1)

    def test_date_only_rejected_by_default(self):
        src = FakeSource([item("只有日期", 30, TimeQuality.DATE)])
        result = src.run(window_minutes=180, seen=None, ref=REF)
        self.assertEqual(result.kept, 0)
        self.assertEqual(result.dropped_no_time, 1)

    def test_date_only_allowed_for_research_sources(self):
        src = FakeSource([item("研报", 30, TimeQuality.DATE)], allow_date_only=True)
        result = src.run(window_minutes=180, seen=None, ref=REF)
        self.assertEqual(result.kept, 1)

    def test_sorted_newest_first(self):
        src = FakeSource([item("旧", 90), item("新", 5), item("中", 40)])
        result = src.run(window_minutes=180, seen=None, ref=REF)
        self.assertEqual([i.title for i in result.items], ["新", "中", "旧"])

    def test_respects_limit(self):
        src = FakeSource([item(f"消息{i}", i) for i in range(1, 30)])
        src.limit = 5
        result = src.run(window_minutes=180, seen=None, ref=REF)
        self.assertEqual(result.kept, 5)

    def test_source_failure_is_contained(self):
        """单个源抛异常不能让整轮任务崩掉。"""
        result = BrokenSource().run(window_minutes=180, seen=None, ref=REF)
        self.assertFalse(result.ok)
        self.assertIn("502", result.error)
        self.assertEqual(result.kept, 0)


class TestDedup(unittest.TestCase):
    def test_same_url_same_key(self):
        a = item("标题A", 5, url="https://x.com/1")
        b = item("标题B", 9, url="https://x.com/1")
        self.assertEqual(a.dedupe_key(), b.dedupe_key())

    def test_different_url_different_key(self):
        a = item("标题", 5, url="https://x.com/1")
        b = item("标题", 5, url="https://x.com/2")
        self.assertNotEqual(a.dedupe_key(), b.dedupe_key())

    def test_title_fallback_when_no_url(self):
        a = Item(source="s", source_label="S", title="同一条消息")
        b = Item(source="s", source_label="S", title="  同一条消息  ")
        self.assertEqual(a.dedupe_key(), b.dedupe_key())

    def test_seen_store_filters_repeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SeenStore(Path(tmp) / "seen.json")
            first = FakeSource([item("重复消息", 5)]).run(
                window_minutes=180, seen=store, ref=REF
            )
            self.assertEqual(first.kept, 1)
            store.add_many(i.dedupe_key() for i in first.items)

            second = FakeSource([item("重复消息", 5)]).run(
                window_minutes=180, seen=store, ref=REF
            )
            self.assertEqual(second.kept, 0)
            self.assertEqual(second.dropped_seen, 1)

    def test_seen_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            store = SeenStore(path)
            store.add("abc123")
            store.save()

            reloaded = SeenStore(path)
            self.assertTrue(reloaded.has("abc123"))
            self.assertFalse(reloaded.has("nope"))

    def test_corrupt_state_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            path.write_text("{ 这不是 JSON", encoding="utf-8")
            store = SeenStore(path)
            self.assertEqual(len(store), 0)


class TestRender(unittest.TestCase):
    def _groups(self):
        result = SourceResult(source="demo", source_label="示例源")
        items = [item("宁德时代拟回购400亿", 12), item("CPO概念快速拉升", 45)]
        result.items = items
        return [(result, items)]

    def test_html_contains_styling(self):
        html = render_html(
            self._groups(), total=2, window_minutes=180, ref=REF,
            failures=[], degraded=[],
        )
        self.assertIn("#eceef0", html)   # 浅灰卡片底
        self.assertIn("#111111", html)   # 正文主色
        self.assertIn("章鱼 AI", html)

    def test_html_contains_items_and_times(self):
        html = render_html(
            self._groups(), total=2, window_minutes=180, ref=REF,
            failures=[], degraded=[],
        )
        self.assertIn("宁德时代拟回购400亿", html)
        self.assertIn("12分钟前", html)
        self.assertIn("07-27 10:18", html)

    def test_hides_all_source_section_headings_but_keeps_items(self):
        """来源标题整级隐藏：正文不出现任何同级来源标题，条目一条不少。"""
        em = SourceResult(source="eastmoney", source_label="东方财富")
        iw = SourceResult(source="iwencai", source_label="问财·同花顺")
        news = Item(
            source="eastmoney",
            source_label="东方财富",
            title="宁德时代拟回购400亿",
            published_at=REF,
            time_quality=TimeQuality.EXACT,
        )
        wencai = Item(
            source="iwencai",
            source_label="问财·同花顺",
            title="嘉美包装封板",
            published_at=REF,
            time_quality=TimeQuality.EXACT,
        )
        em.items = [news]
        iw.items = [wencai]
        html = render_html(
            [(em, [news]), (iw, [wencai])], total=2, window_minutes=180, ref=REF,
            failures=[], degraded=[],
        )
        self.assertNotIn("▍", html)          # 没有任何同级来源标题
        self.assertNotIn("东方财富", html)
        self.assertNotIn("问财·同花顺", html)
        self.assertNotIn("· 1 条", html)     # 标题上的条数角标一并去掉
        self.assertIn("宁德时代拟回购400亿", html)
        self.assertIn("嘉美包装封板", html)

    def test_degraded_note_only_lives_in_footer(self):
        """来源标题隐藏后，降级说明只在页脚出现一次，不在正文重复挂来源名。"""
        em = SourceResult(
            source="eastmoney",
            source_label="东方财富",
            degraded="（主接口超时，改用备用接口）",
        )
        news = Item(
            source="eastmoney",
            source_label="东方财富",
            title="宁德时代拟回购400亿",
            published_at=REF,
            time_quality=TimeQuality.EXACT,
        )
        em.items = [news]
        html = render_html(
            [(em, [news])], total=1, window_minutes=180, ref=REF,
            failures=[], degraded=[em],
        )
        self.assertIn("降级说明：东方财富（主接口超时，改用备用接口）", html)
        self.assertEqual(html.count("东方财富"), 1)

    def test_html_escapes_dangerous_input(self):
        result = SourceResult(source="x", source_label="源")
        evil = Item(
            source="x", source_label="源",
            title="<script>alert(1)</script>",
            published_at=REF, time_quality=TimeQuality.EXACT,
        )
        result.items = [evil]
        html = render_html(
            [(result, [evil])], total=1, window_minutes=180, ref=REF,
            failures=[], degraded=[],
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_empty_state_renders(self):
        html = render_html([], total=0, window_minutes=180, ref=REF,
                           failures=[], degraded=[])
        self.assertIn("本轮无新增内容", html)
        self.assertIn("抓取程序运行正常", html)

    def test_footer_omits_operation_explanation_lines(self):
        html = render_html([], total=0, window_minutes=180, ref=REF,
                           failures=[], degraded=[])
        for text in (
            "数据源：问财 · 巨潮",
            "时间校验：仅推送带可验证发布时间的条目",
            "现价为抓取时刻行情快照",
            "多源印证：先在本轮十个源之间匹配同一事件",
            "每条开头一句「人话」结论",
            "每条附 AI 分析（事件要点 / 多源印证 / 关注点）",
            "内容由程序自动抓取整合",
        ):
            self.assertNotIn(text, html)

    def test_footer_explains_directional_probabilities(self):
        html = render_html([], total=0, window_minutes=180, ref=REF,
                           failures=[], degraded=[])
        self.assertIn("事件情景权重", html)
        self.assertIn("不是统计预测或收益承诺", html)
        self.assertIn("不构成投资建议", html)

    def test_failures_listed_in_footer(self):
        broken = SourceResult(source="b", source_label="坏源", ok=False, error="timeout")
        html = render_html([], total=0, window_minutes=180, ref=REF,
                           failures=[broken], degraded=[])
        self.assertIn("坏源", html)

    def test_title_with_items(self):
        """全部推送标题统一为固定品牌标题，不再拼条数/时间/头条。"""
        title = render_title(7, REF, item("长鑫科技上市首日大涨", 3))
        self.assertEqual(title, PUSH_TITLE)
        self.assertEqual(title, "章鱼 AI · 全景分析（实时事件因子）")

    def test_title_when_empty(self):
        self.assertEqual(render_title(0, REF, None), PUSH_TITLE)

    def test_title_ignores_long_headline(self):
        long_item = item("这是一条非常非常长的新闻标题" * 5, 1)
        self.assertEqual(render_title(1, REF, long_item), PUSH_TITLE)

    def _row_backgrounds(self, count: int) -> list[str]:
        """按出现顺序取出每一条情报行的底色。"""
        result = SourceResult(source="demo", source_label="示例源")
        items = [item(f"第{i}条情报", 5 * (i + 1)) for i in range(count)]
        result.items = items
        html = render_html(
            [(result, items)], total=count, window_minutes=180, ref=REF,
            failures=[], degraded=[],
        )
        return re.findall(
            r"background:(#[0-9a-f]{6});border-radius:6px;padding:8px 10px;", html
        )

    def test_adjacent_rows_use_alternating_backgrounds(self):
        """前后两条新闻底色一深一浅交替，靠背景色就能清晰区分。"""
        self.assertNotEqual(ROW_BG_A, ROW_BG_B)
        self.assertEqual(self._row_backgrounds(3), [ROW_BG_A, ROW_BG_B, ROW_BG_A])
        self.assertEqual(self._row_backgrounds(4), [ROW_BG_A, ROW_BG_B, ROW_BG_A, ROW_BG_B])

    def test_row_backgrounds_differ_from_card(self):
        """两档底色都不等于卡片底，单独一条也不会糊在卡片上。"""
        for bg in (ROW_BG_A, ROW_BG_B):
            self.assertNotEqual(bg, CARD_BG)

    def test_html_renders_price_and_brief(self):
        result = SourceResult(source="demo", source_label="示例源")
        news = item("宁德时代拟回购400亿", 12)
        news.last_price = 188.5
        news.price_change = 2.31
        news.price_name = "宁德时代"
        news.price_code = "300750"
        news.ai_analysis = "拟斥资回购，关注后续进度。"
        news.ai_analysis_from_model = True
        result.items = [news]
        html = render_html(
            [(result, [news])], total=1, window_minutes=180, ref=REF,
            failures=[], degraded=[],
        )
        self.assertIn(">现价</span>", html)
        self.assertIn("188.50", html)
        self.assertIn("拟斥资回购，关注后续进度。", html)
        self.assertIn(">AI 分析</span>", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
