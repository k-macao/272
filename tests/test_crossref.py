"""多源印证：本轮跨源匹配 / 外部新闻检索 / 时间校验 / 熔断（全程离线）。"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from octopus import crossref
from octopus.crossref import (
    build_query,
    classify_pair,
    corroboration_summary,
    event_tags,
    filter_results,
    is_relevant,
    link_batch,
    normalize_title,
    parse_rss,
    pick_candidates,
    same_outlet,
    search_external,
    signature,
    split_source_from_title,
    title_overlap,
)
from octopus.http import FetchError
from octopus.models import Item, RelatedNews, TimeQuality
from octopus.timeutil import CN_TZ, parse

REF = datetime(2026, 9, 7, 10, 30, 0, tzinfo=CN_TZ)


def _item(source: str, label: str, title: str, *, summary: str = "", minutes_ago: int = 5,
          extra=None, url: str = "") -> Item:
    return Item(
        source=source,
        source_label=label,
        title=title,
        summary=summary,
        url=url or f"https://example.com/{source}/{abs(hash(title)) % 10000}",
        published_at=REF - timedelta(minutes=minutes_ago),
        time_quality=TimeQuality.EXACT,
        extra=extra or {},
    )


def _rss(items: list[tuple[str, str, str, str]]) -> str:
    """(title, link, pubDate, source) -> RSS 2.0 文本。"""
    body = "".join(
        f"<item><title><![CDATA[{t}]]></title><link>{l}</link>"
        f"<pubDate>{p}</pubDate><source url=\"https://{s}.example\">{s}</source></item>"
        for t, l, p, s in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'


class FakeHttp:
    def __init__(self, responses=None, fail_engines=()):
        self.responses = responses or {}
        self.fail_engines = set(fail_engines)
        self.calls: list[str] = []

    def text(self, url, **kwargs):
        self.calls.append(url)
        if "news.google.com" in url:
            engine = "google"
        elif "bing.com" in url:
            engine = "bing"
        else:
            raise FetchError(f"unexpected {url}")
        if engine in self.fail_engines:
            raise FetchError(f"{engine} down")
        return self.responses.get(engine, _rss([]))


# ---------------------------------------------------------------------------
class TestTextFeatures(unittest.TestCase):
    def test_event_tags(self):
        self.assertIn("回购", event_tags("宁德时代拟回购400亿"))
        self.assertIn("问询", event_tags("收到深交所关注函"))
        self.assertIn("宏观数据", event_tags("8月 CPI 同比上涨"))
        self.assertEqual(event_tags("无关文本"), [])

    def test_normalize_strips_punct_and_source_tail(self):
        self.assertEqual(normalize_title("宁德时代：拟回购 - 证券时报"), "宁德时代拟回购")
        self.assertEqual(normalize_title("【快讯】宁德时代(300750)拟回购！"), "快讯宁德时代300750拟回购")

    def test_indicator_aliases_are_canonicalized(self):
        self.assertEqual(normalize_title("8月份居民消费价格同比上涨0.6%"), "8月cpi同比上涨06")
        self.assertEqual(normalize_title("8月CPI同比上涨0.6%"), "8月cpi同比上涨06")

    def test_official_and_media_phrasing_of_same_data_match(self):
        """统计局「居民消费价格」 vs 媒体「CPI」——同一份数据必须能对上。"""
        official = _item("stats", "国家统计局", "2026年8月份居民消费价格同比上涨0.6%")
        sig = signature(official)
        self.assertTrue(is_relevant(official, sig, "8月CPI同比上涨0.6% PPI降幅收窄"))
        self.assertFalse(is_relevant(official, sig, "7月PPI同比下降3.6%"))

    def test_title_overlap_is_symmetric_and_bounded(self):
        a, b = "宁德时代拟回购400亿元股份", "宁德时代公告：拟回购不超400亿元"
        o1, s1 = title_overlap(a, b)
        o2, s2 = title_overlap(b, a)
        self.assertAlmostEqual(o1, o2)
        self.assertEqual(s1, s2)
        self.assertGreater(o1, 0.4)
        self.assertLessEqual(o1, 1.0)

    def test_split_source_from_title(self):
        self.assertEqual(split_source_from_title("宁德时代回购进展 - 证券时报"), ("宁德时代回购进展", "证券时报"))
        self.assertEqual(split_source_from_title("没有来源尾巴"), ("没有来源尾巴", ""))

    def test_signature_collects_codes_names_events(self):
        item = _item("cninfo", "巨潮资讯", "宁德时代(300750) 关于回购股份的公告",
                     extra={"code": "300750", "stock": "宁德时代", "kind": "公告"})
        sig = signature(item)
        self.assertIn("300750", sig.codes)
        self.assertIn("宁德时代", sig.names)
        self.assertIn("回购", sig.events)
        self.assertFalse(sig.snapshot)

    def test_snapshot_kinds_are_flagged(self):
        item = _item("ifind", "iFinD", "板块热力 10:30 · 领涨 半导体 +2.10%", extra={"kind": "板块热力"})
        self.assertTrue(signature(item).snapshot)


# ---------------------------------------------------------------------------
class TestBatchLinking(unittest.TestCase):
    def test_same_ticker_same_event_links(self):
        a = _item("cninfo", "巨潮资讯", "宁德时代(300750) 关于回购公司股份方案的公告",
                  extra={"code": "300750", "stock": "宁德时代", "kind": "公告"})
        b = _item("eastmoney", "东方财富", "宁德时代：拟以不超400亿元回购股份", minutes_ago=3)
        linked = link_batch([a, b])
        self.assertEqual(linked, 2)
        self.assertEqual(a.related[0].source_label, "东方财富")
        self.assertEqual(a.related[0].relation, "same_event")
        self.assertEqual(a.related[0].via, "batch")
        self.assertEqual(b.related[0].source_label, "巨潮资讯")

    def test_same_source_never_links(self):
        a = _item("eastmoney", "东方财富", "宁德时代拟回购400亿")
        b = _item("eastmoney", "东方财富", "宁德时代回购400亿获通过")
        link_batch([a, b])
        self.assertEqual(a.related, [])
        self.assertEqual(b.related, [])

    def test_same_ticker_different_event_is_same_subject(self):
        a = _item("cninfo", "巨潮资讯", "宁德时代(300750) 关于回购公司股份的公告",
                  extra={"code": "300750", "stock": "宁德时代", "kind": "公告"})
        b = _item("stockstar", "证券之星", "宁德时代（300750）9月7日10点02分触及涨停板", minutes_ago=20)
        link_batch([a, b])
        self.assertEqual(len(a.related), 1)
        self.assertEqual(a.related[0].relation, "same_subject")

    def test_same_subject_requires_close_in_time(self):
        a = _item("cninfo", "巨潮资讯", "宁德时代(300750) 关于回购公司股份的公告",
                  extra={"code": "300750", "stock": "宁德时代", "kind": "公告"})
        b = _item("stockstar", "证券之星", "宁德时代（300750）触及涨停板", minutes_ago=60 * 5)
        link_batch([a, b])
        self.assertEqual(a.related, [])

    def test_unrelated_items_do_not_link(self):
        a = _item("cninfo", "巨潮资讯", "宁德时代(300750) 关于回购公司股份的公告",
                  extra={"code": "300750", "stock": "宁德时代", "kind": "公告"})
        b = _item("stats", "国家统计局", "2026年8月份居民消费价格同比上涨0.6%")
        c = _item("stockstar", "证券之星", "贵州茅台（600519）9月7日10点02分触及涨停板")
        link_batch([a, b, c])
        self.assertEqual(a.related, [])
        self.assertEqual(b.related, [])
        self.assertEqual(c.related, [])

    def test_high_title_overlap_without_ticker_links(self):
        a = _item("stats", "国家统计局", "2026年8月份居民消费价格同比上涨0.6%")
        b = _item("eastmoney", "东方财富", "统计局：8月份居民消费价格同比上涨0.6%")
        link_batch([a, b])
        self.assertEqual(a.related[0].relation, "same_event")

    def test_snapshots_are_excluded_both_ways(self):
        heat = _item("ifind", "iFinD", "板块热力 10:30 · 领涨 宁德时代 +2.10%", extra={"kind": "板块热力"})
        news = _item("eastmoney", "东方财富", "宁德时代拟回购400亿元")
        link_batch([heat, news])
        self.assertEqual(heat.related, [])
        self.assertEqual(news.related, [])

    def test_related_capped_and_same_event_first(self):
        base = _item("cninfo", "巨潮资讯", "宁德时代(300750) 关于回购公司股份的公告",
                     extra={"code": "300750", "stock": "宁德时代", "kind": "公告"})
        others = [
            _item(f"s{i}", f"源{i}", f"宁德时代：拟回购股份 第{i}稿", minutes_ago=i)
            for i in range(6)
        ]
        subject = _item("stockstar", "证券之星", "宁德时代（300750）触及涨停板", minutes_ago=2)
        link_batch([base, subject] + others)
        self.assertLessEqual(len(base.related), crossref.MAX_RELATED_PER_ITEM)
        self.assertTrue(all(r.relation == "same_event" for r in base.related))

    def test_classify_pair_direct(self):
        a = _item("a", "A", "某某公司收到证监会立案告知书", extra={"stock": "某某公司"})
        b = _item("b", "B", "某某公司被证监会立案调查", extra={"stock": "某某公司"})
        self.assertEqual(classify_pair(a, signature(a), b, signature(b)), "same_event")


# ---------------------------------------------------------------------------
class TestQueryAndCandidates(unittest.TestCase):
    def test_query_prefers_name_plus_event(self):
        item = _item("cninfo", "巨潮资讯", "宁德时代(300750) 关于回购公司股份的公告",
                     extra={"code": "300750", "stock": "宁德时代"})
        self.assertEqual(build_query(item), "宁德时代 回购")

    def test_query_falls_back_to_clean_title(self):
        item = _item("stats", "国家统计局", "2026年8月份居民消费价格同比上涨0.6%")
        q = build_query(item)
        self.assertNotIn("(", q)
        self.assertIn("居民消费价格", q)
        self.assertLessEqual(len(q), 30)

    def test_query_strips_codes(self):
        item = _item("eastmoney", "东方财富", "快讯｜300750.SZ 盘中拉升")
        self.assertNotIn("300750", build_query(item))

    def test_candidates_prefer_uncorroborated_with_subject(self):
        corroborated = _item("cninfo", "巨潮资讯", "宁德时代(300750) 关于回购公司股份的公告",
                             extra={"code": "300750", "stock": "宁德时代", "kind": "公告"})
        corroborated.related.append(RelatedNews(source_label="东方财富", title="x", relation="same_event"))
        lonely = _item("stockstar", "证券之星", "贵州茅台（600519）触及涨停板", extra={"kind": "盘中异动"})
        heat = _item("ifind", "iFinD", "板块热力", extra={"kind": "板块热力"})
        picked = pick_candidates([corroborated, lonely, heat], max_items=1)
        self.assertEqual(picked, [lonely])
        self.assertNotIn(heat, pick_candidates([corroborated, lonely, heat], max_items=10))


# ---------------------------------------------------------------------------
class TestRssAndFilters(unittest.TestCase):
    def test_parse_rss_reads_fields(self):
        rows = parse_rss(_rss([("宁德时代回购 - 证券时报", "https://a/1", "Mon, 07 Sep 2026 02:00:00 GMT", "证券时报")]))
        self.assertEqual(rows[0]["title"], "宁德时代回购 - 证券时报")
        self.assertEqual(rows[0]["url"], "https://a/1")
        self.assertEqual(rows[0]["source"], "证券时报")
        self.assertEqual(rows[0]["pubdate"], "Mon, 07 Sep 2026 02:00:00 GMT")

    def test_parse_rss_regex_fallback_on_broken_xml(self):
        broken = "<rss><channel><item><title>坏掉的 & XML</title><link>https://a</link><pubDate>Mon, 07 Sep 2026 02:00:00 GMT</pubDate></item>"
        rows = parse_rss(broken)
        self.assertEqual(rows[0]["title"], "坏掉的 & XML")

    def test_rfc822_pubdate_converts_to_cn_tz(self):
        dt, quality, _ = parse("Mon, 07 Sep 2026 02:00:00 GMT")
        self.assertEqual(dt, datetime(2026, 9, 7, 10, 0, tzinfo=CN_TZ))
        self.assertIs(quality, TimeQuality.EXACT)
        dt2, _, _ = parse("Mon, 07 Sep 2026 10:00:00 +0800")
        self.assertEqual(dt2, dt)

    def test_filter_drops_missing_or_future_or_far_time(self):
        item = _item("cninfo", "巨潮资讯", "宁德时代(300750) 关于回购公司股份的公告",
                     extra={"code": "300750", "stock": "宁德时代"})
        rows = [
            {"title": "宁德时代拟回购股份 - 证券时报", "url": "https://a/1", "pubdate": ""},
            {"title": "宁德时代拟回购股份 - 财联社", "url": "https://a/2", "pubdate": "Mon, 07 Sep 2026 06:00:00 GMT"},   # 14:00 北京，未来
            {"title": "宁德时代拟回购股份 - 新浪财经", "url": "https://a/3", "pubdate": "Thu, 03 Sep 2026 02:00:00 GMT"},  # 4 天前，太远
            {"title": "宁德时代拟回购股份 - 每经网", "url": "https://a/4", "pubdate": "Mon, 07 Sep 2026 01:50:00 GMT"},    # 09:50，OK
        ]
        out = filter_results(item, rows, "google", ref=REF, max_gap_hours=36)
        self.assertEqual([r.source_label for r in out], ["每经网"])
        self.assertEqual(out[0].via, "google")
        self.assertEqual(out[0].published_at, datetime(2026, 9, 7, 9, 50, tzinfo=CN_TZ))

    def test_filter_drops_irrelevant_titles(self):
        item = _item("cninfo", "巨潮资讯", "宁德时代(300750) 关于回购公司股份的公告",
                     extra={"code": "300750", "stock": "宁德时代"})
        rows = [
            {"title": "宁德时代发布新款储能产品 - 某网", "url": "https://a/1", "pubdate": "Mon, 07 Sep 2026 02:00:00 GMT"},
            {"title": "锂电板块午后走强 - 某网", "url": "https://a/2", "pubdate": "Mon, 07 Sep 2026 02:00:00 GMT"},
        ]
        self.assertEqual(filter_results(item, rows, "bing", ref=REF, max_gap_hours=36), [])

    def test_filter_drops_same_outlet(self):
        item = _item("eastmoney", "东方财富", "宁德时代：拟回购不超400亿元股份")
        rows = [
            {"title": "宁德时代：拟回购不超400亿元股份 - 东方财富网", "url": "https://finance.eastmoney.com/x", "pubdate": "Mon, 07 Sep 2026 02:00:00 GMT"},
        ]
        self.assertEqual(filter_results(item, rows, "google", ref=REF, max_gap_hours=36), [])
        self.assertTrue(same_outlet(item, "东方财富网", ""))
        self.assertTrue(same_outlet(item, "", "https://finance.eastmoney.com/a"))
        self.assertFalse(same_outlet(item, "证券时报", "https://www.stcn.com/a"))

    def test_is_relevant_requires_subject_when_titles_differ(self):
        item = _item("cninfo", "巨潮资讯", "宁德时代(300750) 关于回购公司股份的公告",
                     extra={"code": "300750", "stock": "宁德时代"})
        sig = signature(item)
        self.assertTrue(is_relevant(item, sig, "宁德时代披露回购进展"))
        self.assertFalse(is_relevant(item, sig, "比亚迪披露回购进展"))
        # 「比亚迪拟回购股份的公告」与原题尾巴高度重合，但标的不同 —— 不算
        self.assertFalse(is_relevant(item, sig, "比亚迪拟回购股份的公告"))

    def test_long_subject_name_does_not_inflate_overlap(self):
        """标的名很长时，光靠名字就能让整题重合度飙高，必须先剥掉再比。"""
        item = _item("cninfo", "巨潮资讯", "中国平安保险集团 关于回购股份的公告",
                     extra={"stock": "中国平安保险集团"})
        sig = signature(item)
        self.assertFalse(is_relevant(item, sig, "中国平安保险集团新任首席执行官"))
        self.assertTrue(is_relevant(item, sig, "中国平安保险集团披露回购方案"))

    def test_relevance_without_known_subject_uses_whole_title(self):
        item = _item("eastmoney", "东方财富", "央行开展3000亿元MLF操作")
        sig = signature(item)
        self.assertTrue(is_relevant(item, sig, "央行今日开展3000亿元MLF操作 利率持平"))
        self.assertFalse(is_relevant(item, sig, "央行行长出席论坛发表讲话"))


# ---------------------------------------------------------------------------
class TestSearchExternal(unittest.TestCase):
    def _item(self):
        return _item("cninfo", "巨潮资讯", "宁德时代(300750) 关于回购公司股份的公告",
                     extra={"code": "300750", "stock": "宁德时代", "kind": "公告"})

    def test_hits_attached_and_deduped_across_engines(self):
        same = ("宁德时代拟回购股份 - 证券时报", "https://stcn/1", "Mon, 07 Sep 2026 02:00:00 GMT", "证券时报")
        http = FakeHttp(responses={
            "google": _rss([same]),
            "bing": _rss([same, ("宁德时代披露回购方案 - 财联社", "https://cls/1", "Mon, 07 Sep 2026 02:05:00 GMT", "财联社")]),
        })
        item = self._item()
        stats = search_external([item], http=http, mode="on", ref=REF)
        self.assertTrue(item.related_searched)
        labels = sorted(r.source_label for r in item.related)
        self.assertEqual(labels, ["证券时报", "财联社"])
        self.assertEqual(stats.searched, 1)
        self.assertEqual(stats.hits, 2)
        self.assertEqual(stats.items_with_hits, 1)

    def test_off_mode_makes_no_requests(self):
        http = FakeHttp()
        item = self._item()
        stats = search_external([item], http=http, mode="off", ref=REF)
        self.assertEqual(http.calls, [])
        self.assertFalse(item.related_searched)
        self.assertEqual(stats.searched, 0)

    def test_no_http_makes_no_requests(self):
        item = self._item()
        stats = search_external([item], http=None, mode="on", ref=REF)
        self.assertFalse(item.related_searched)
        self.assertEqual(stats.searched, 0)

    def test_auto_mode_breaks_circuit_after_repeated_failures(self):
        http = FakeHttp(fail_engines={"google", "bing"})
        items = [
            _item(f"s{i}", f"源{i}", f"公司{i}(60000{i}) 关于回购股份的公告", extra={"code": f"60000{i}", "stock": f"公司{i}"})
            for i in range(6)
        ]
        stats = search_external(items, http=http, mode="auto", ref=REF, workers=1)
        self.assertEqual(sorted(stats.disabled), ["bing", "google"])
        google_calls = sum(1 for c in http.calls if "news.google.com" in c)
        self.assertLessEqual(google_calls, 2)  # 连续两次失败后不再请求
        self.assertTrue(all(it.related == [] for it in items))

    def test_on_mode_does_not_break_circuit(self):
        http = FakeHttp(fail_engines={"google", "bing"})
        items = [
            _item(f"s{i}", f"源{i}", f"公司{i}(60000{i}) 关于回购股份的公告", extra={"code": f"60000{i}", "stock": f"公司{i}"})
            for i in range(4)
        ]
        stats = search_external(items, http=http, mode="on", ref=REF, workers=1)
        self.assertEqual(stats.disabled, [])
        self.assertEqual(sum(1 for c in http.calls if "news.google.com" in c), 4)

    def test_max_items_limits_requests(self):
        http = FakeHttp()
        items = [
            _item(f"s{i}", f"源{i}", f"公司{i}(60000{i}) 关于回购股份的公告", extra={"code": f"60000{i}", "stock": f"公司{i}"})
            for i in range(5)
        ]
        stats = search_external(items, http=http, mode="on", max_items=2, ref=REF)
        self.assertEqual(stats.searched, 2)
        self.assertEqual(sum(1 for it in items if it.related_searched), 2)

    def test_snapshots_never_searched(self):
        http = FakeHttp()
        heat = _item("ifind", "iFinD", "板块热力 10:30 · 领涨 半导体", extra={"kind": "板块热力"})
        search_external([heat], http=http, mode="on", ref=REF)
        self.assertEqual(http.calls, [])
        self.assertFalse(heat.related_searched)

    def test_query_is_url_encoded_and_engine_specific(self):
        http = FakeHttp()
        search_external([self._item()], http=http, mode="on", ref=REF)
        google = [c for c in http.calls if "news.google.com" in c][0]
        bing = [c for c in http.calls if "bing.com" in c][0]
        self.assertIn("hl=zh-CN", google)
        self.assertIn("%E5%AE%81%E5%BE%B7%E6%97%B6%E4%BB%A3", google)  # 宁德时代
        self.assertIn("format=RSS", bing)


# ---------------------------------------------------------------------------
class TestCorroborationSummary(unittest.TestCase):
    def test_phrasing(self):
        item = _item("cninfo", "巨潮资讯", "x")
        self.assertIn("未做外部检索", corroboration_summary(item))
        item.related_searched = True
        self.assertIn("外部检索均未见", corroboration_summary(item))
        item.related.append(RelatedNews(source_label="证券之星", title="y", relation="same_subject"))
        self.assertIn("同标的消息", corroboration_summary(item))
        item.related.insert(0, RelatedNews(source_label="证券时报", title="z", relation="same_event", via="bing"))
        text = corroboration_summary(item)
        self.assertIn("1 个源头报道同一事件", text)
        self.assertIn("证券时报（Bing News）", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
