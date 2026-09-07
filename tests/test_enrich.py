"""情报增强：现价提取 / 行情降级 / 多源印证 / AI 分析（全程离线）。"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from octopus.enrich import (
    Quote,
    Ticker,
    clip_analysis,
    clip_brief,
    code_to_secid,
    enrich_news,
    extract_tickers,
    fetch_quotes,
    parse_news_analyses,
    rule_analysis,
)
from octopus.http import FetchError
from octopus.models import Item, RelatedNews, SourceResult, TimeQuality
from octopus.render import render_html
from octopus.timeutil import CN_TZ

REF = datetime(2026, 7, 27, 10, 30, 0, tzinfo=CN_TZ)


def _item(title: str, summary: str = "", extra=None) -> Item:
    return Item(
        source="demo",
        source_label="示例源",
        title=title,
        summary=summary,
        url="https://example.com/x",
        published_at=REF - timedelta(minutes=5),
        time_quality=TimeQuality.EXACT,
        extra=extra or {},
    )


class FakeHttp:
    def __init__(self, json_map=None):
        self.json_map = json_map or {}
        self.calls: list[str] = []

    def json(self, url, **kwargs):
        self.calls.append(url)
        for key, value in self.json_map.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise FetchError(f"no fixture for {url}")

    def post_json(self, url, payload, **kwargs):
        raise FetchError("offline")

    def text(self, url, **kwargs):
        self.calls.append(url)
        raise FetchError("offline")


class TestExtractTickers(unittest.TestCase):
    def test_extra_code_wins(self):
        item = _item("随便一个标题", extra={"code": "300750", "stock": "宁德时代"})
        tickers = extract_tickers(item)
        self.assertEqual(tickers[0].code, "300750")
        self.assertTrue(tickers[0].secid.startswith("0."))

    def test_paren_fullwidth_and_halfwidth(self):
        a = extract_tickers(_item("宏和科技（603256）触及涨停板"))
        b = extract_tickers(_item("宏和科技(603256)触及涨停板"))
        self.assertEqual(a[0].code, "603256")
        self.assertEqual(b[0].code, "603256")
        self.assertTrue(a[0].secid.startswith("1."))

    def test_dotted_suffix(self):
        sh = extract_tickers(_item("贵州茅台 600519.SH 回购"))
        sz = extract_tickers(_item("关注 000001.SZ"))
        self.assertEqual(sh[0].secid, "1.600519")
        self.assertEqual(sz[0].secid, "0.000001")

    def test_bond_id(self):
        item = _item("水羊转债异动", extra={"bond_id": "123188", "price": 135.3})
        tickers = extract_tickers(item)
        self.assertEqual(tickers[0].code, "123188")
        self.assertTrue(tickers[0].secid.startswith("0."))

    def test_sh_convertible_secid(self):
        self.assertEqual(code_to_secid("110074"), "1.110074")
        self.assertEqual(code_to_secid("118074"), "1.118074")

    def test_name_match_ningde(self):
        tickers = extract_tickers(_item("宁德时代拟回购400亿"))
        self.assertTrue(any(t.code == "300750" for t in tickers))

    def test_board_name_not_treated_as_stock(self):
        """「机器人板块」不能命中个股 机器人(300024)。"""
        tickers = extract_tickers(_item("机器人板块快速拉升"))
        self.assertFalse(any(t.code == "300024" for t in tickers))

    def test_index_alias(self):
        tickers = extract_tickers(_item("创业板指涨逾2% 上涨个股近4800只"))
        self.assertTrue(any(t.secid == "0.399006" for t in tickers))

    def test_shanghai_index_not_pingan(self):
        self.assertEqual(
            code_to_secid("000001", title="上证指数下跌"),
            "1.000001",
        )
        self.assertEqual(code_to_secid("000001", name="平安银行"), "0.000001")

    def test_bare_six_digits_in_title_ignored(self):
        """标题里裸写 202607 这种六位数不能当代码。"""
        tickers = extract_tickers(_item("202607 宏观数据点评"))
        self.assertEqual(tickers, [])


class TestQuotes(unittest.TestCase):
    def test_eastmoney_batch(self):
        http = FakeHttp(
            json_map={
                "ulist": {
                    "data": {
                        "diff": [
                            {"f2": 188.5, "f3": 2.31, "f12": "300750", "f14": "宁德时代"},
                        ]
                    }
                }
            }
        )
        quotes = fetch_quotes(http, [Ticker("300750", "宁德时代", "0.300750")])
        self.assertEqual(quotes["300750"].price, 188.5)
        self.assertAlmostEqual(quotes["300750"].change, 2.31)
        self.assertEqual(quotes["300750"].source, "eastmoney")

    def test_eastmoney_fail_falls_back_to_yahoo(self):
        http = FakeHttp(
            json_map={
                "ulist": FetchError("timeout"),
                "finance.yahoo.com": {
                    "quoteResponse": {
                        "result": [
                            {
                                "symbol": "300750.SZ",
                                "regularMarketPrice": 187.0,
                                "regularMarketChangePercent": -0.5,
                                "shortName": "CATL",
                            }
                        ]
                    }
                },
            }
        )
        quotes = fetch_quotes(http, [Ticker("300750", "宁德时代", "0.300750")])
        self.assertEqual(quotes["300750"].price, 187.0)
        self.assertEqual(quotes["300750"].source, "yahoo")

    def test_dash_price_skipped(self):
        http = FakeHttp(
            json_map={"ulist": {"data": {"diff": [{"f2": "-", "f3": "-", "f12": "300750"}]}}}
        )
        quotes = fetch_quotes(http, [Ticker("300750", secid="0.300750")])
        self.assertNotIn("300750", quotes)

    def test_extra_price_fallback_when_live_missing(self):
        item = _item(
            "Z精达转 · 强赎提醒",
            extra={"bond_id": "110074", "price": 221.84, "increase_rt": 1.99, "bond_nm": "Z精达转"},
        )
        stats = enrich_news([item], http=FakeHttp(json_map={"ulist": {"data": {"diff": []}}}))
        self.assertEqual(item.last_price, 221.84)
        self.assertAlmostEqual(item.price_change, 1.99)
        self.assertEqual(stats["quotes"], 1)


class TestAnalysis(unittest.TestCase):
    def test_rule_analysis_does_not_restate_title_or_summary(self):
        """标题/摘要已在卡片上，规则化分析只补印证情况与关注点。"""
        item = _item("很长的标题" * 5, summary="涨幅 9.98%，换手 1.57%，封单 3.76亿")
        text = rule_analysis(item)
        self.assertNotIn("涨幅 9.98%", text)
        self.assertNotIn("很长的标题", text)
        self.assertIn("单一来源", text)
        self.assertTrue(text.endswith("。"))

    def test_rule_analysis_mentions_other_sources(self):
        item = _item("宁德时代(300750)拟回购")
        item.related.append(RelatedNews(source_label="证券之星", title="宁德时代回购公告", relation="same_event"))
        item.related.append(RelatedNews(source_label="证券时报", title="宁德时代披露回购", relation="same_event", via="google"))
        text = rule_analysis(item)
        self.assertIn("2 个源头报道同一事件", text)
        self.assertIn("证券之星", text)
        self.assertIn("证券时报（Google News）", text)
        self.assertIn("公告原文", text)

    def test_rule_analysis_distinguishes_searched_and_not(self):
        item = _item("某公司发布公告")
        self.assertIn("未做外部检索", rule_analysis(item))
        item.related_searched = True
        self.assertIn("外部检索均未见", rule_analysis(item))

    def test_rule_focus_by_event(self):
        self.assertIn("监管口径", rule_analysis(_item("某公司收到问询函")))
        self.assertIn("统计局", rule_analysis(_item("7月 CPI 同比上涨0.5%")))
        self.assertIn("盘中信号", rule_analysis(_item("某股快速拉升")))

    def test_parse_numbered_variants(self):
        text = "1. 宁德时代拟回购\n2、嘉美包装首板封死\n【3】北向资金净流入\n4: 创业板指走强"
        mapping = parse_news_analyses(text)
        self.assertEqual(mapping[1], "宁德时代拟回购")
        self.assertEqual(mapping[2], "嘉美包装首板封死")
        self.assertEqual(mapping[3], "北向资金净流入")
        self.assertEqual(mapping[4], "创业板指走强")

    def test_parse_multiline_entry(self):
        text = "1. 事件要点：公司拟回购。\n多源印证：证券时报报道一致。\n关注点：公告原文。\n2. 第二条。"
        mapping = parse_news_analyses(text)
        self.assertIn("多源印证：证券时报报道一致。", mapping[1])
        self.assertEqual(mapping[2], "第二条。")

    def test_clip_analysis_cuts_at_sentence(self):
        long = "第一句话。" * 40
        clipped = clip_analysis(long, limit=60)
        self.assertLessEqual(len(clipped), 60)
        self.assertTrue(clipped.endswith("。"))

    def test_clip_brief_takes_first_sentence(self):
        self.assertEqual(clip_brief("先说这句。后面不要。"), "先说这句")

    def test_enrich_without_key_uses_rule_analysis(self):
        item = _item("统计局发布 PMI", summary="官方制造业 PMI 为 50.2。")
        stats = enrich_news([item], http=None, api_key="")
        self.assertIn("单一来源", item.ai_analysis)
        self.assertIn("统计局", item.ai_analysis)
        self.assertFalse(item.ai_analysis_from_model)
        self.assertEqual(stats["rule"], 1)
        self.assertEqual(stats["ai"], 0)
        self.assertEqual(stats["searched"], 0)  # http=None 不联网

    def test_ai_analysis_overwrites_rule(self):
        item = _item("宁德时代拟回购400亿", summary="公司公告回购。")
        http = MagicMock()
        http.json.side_effect = FetchError("offline")
        http.text.side_effect = FetchError("offline")
        http.post_json.return_value = {
            "choices": [{"message": {"content": "1. 事件要点：公司公告大规模回购。多源印证：目前仅见单一来源，待其它渠道确认。关注点：留意回购进展公告。"}}]
        }
        stats = enrich_news([item], http=http, api_key="sk-test", crossref_mode="off")
        self.assertTrue(item.ai_analysis_from_model)
        self.assertIn("回购", item.ai_analysis)
        self.assertIn("单一来源", item.ai_analysis)
        self.assertEqual(stats["ai"], 1)

    def test_ai_prompt_carries_other_sources(self):
        """模型收到的不只是标题：同一新闻的其它源头报道也要进 prompt。"""
        item = _item("宁德时代(300750)拟回购")
        item.related.append(RelatedNews(source_label="证券之星", title="宁德时代公告回购", relation="same_event"))
        http = MagicMock()
        http.json.side_effect = FetchError("offline")
        http.text.side_effect = FetchError("offline")
        http.post_json.return_value = {"choices": [{"message": {"content": "1. 分析文本。"}}]}
        enrich_news([item], http=http, api_key="sk-test", crossref_mode="off")
        _, payload = http.post_json.call_args[0]
        user = payload["messages"][1]["content"]
        self.assertIn("其它源头报道", user)
        self.assertIn("证券之星：宁德时代公告回购", user)

    def test_ai_buy_recommendation_rejected(self):
        item = _item("某股异动")
        http = MagicMock()
        http.json.side_effect = FetchError("offline")
        http.text.side_effect = FetchError("offline")
        http.post_json.return_value = {
            "choices": [{"message": {"content": "1. 建议立即买入，稳赚不赔。"}}]
        }
        enrich_news([item], http=http, api_key="sk-test", crossref_mode="off")
        self.assertFalse(item.ai_analysis_from_model)
        self.assertNotIn("买入", item.ai_analysis)
        self.assertIn("单一来源", item.ai_analysis)

    def test_ai_overclaim_on_single_source_rejected(self):
        """没有任何其它源头，模型却说「已获多方证实」—— 拒收，回退规则化分析。"""
        item = _item("某公司拟回购")
        http = MagicMock()
        http.json.side_effect = FetchError("offline")
        http.text.side_effect = FetchError("offline")
        http.post_json.return_value = {
            "choices": [{"message": {"content": "1. 该消息已获多方证实，公司拟回购。"}}]
        }
        enrich_news([item], http=http, api_key="sk-test", crossref_mode="off")
        self.assertFalse(item.ai_analysis_from_model)

    def test_quote_failure_does_not_drop_item(self):
        item = _item("宁德时代(300750)拟回购")
        stats = enrich_news([item], http=FakeHttp(json_map={"ulist": FetchError("boom")}))
        self.assertEqual(item.title, "宁德时代(300750)拟回购")
        self.assertTrue(item.ai_analysis)
        self.assertEqual(stats["items"], 1)

    def test_external_search_failure_does_not_drop_item(self):
        """外部检索全挂：条目仍在，标记为已检索、单一来源。"""
        item = _item("宁德时代(300750)拟回购")
        http = FakeHttp(json_map={"ulist": {"data": {"diff": []}}})
        stats = enrich_news([item], http=http, api_key="", crossref_mode="on")
        self.assertTrue(item.related_searched)
        self.assertEqual(item.related, [])
        self.assertEqual(stats["external_hits"], 0)
        self.assertIn("单一来源", item.ai_analysis)
        self.assertTrue(any("news.google.com" in c or "bing.com" in c for c in http.calls))


class TestRenderNewsEnrichment(unittest.TestCase):
    def _html(self, item: Item) -> str:
        result = SourceResult(source="demo", source_label="示例源")
        result.items = [item]
        return render_html(
            [(result, [item])], total=1, window_minutes=180, ref=REF,
            failures=[], degraded=[],
        )

    def test_price_and_ai_rendered(self):
        item = _item("宁德时代拟回购")
        item.last_price = 188.5
        item.price_change = 2.31
        item.price_name = "宁德时代"
        item.price_code = "300750"
        item.ai_analysis = "公司拟斥资回购，另有证券时报报道一致，关注进度。"
        item.ai_analysis_from_model = True
        html = self._html(item)
        self.assertIn(">现价</span>", html)
        self.assertIn("188.50", html)
        self.assertIn("+2.31%", html)
        self.assertIn(">AI 分析</span>", html)
        self.assertIn("公司拟斥资回购，另有证券时报报道一致，关注进度。", html)

    def test_missing_price_omitted(self):
        item = _item("统计局发布数据")
        item.ai_analysis = "官方数据发布。"
        html = self._html(item)
        self.assertNotIn(">现价</span>", html)
        self.assertIn(">分析</span>", html)
        self.assertNotIn(">AI 分析</span>", html)  # 规则化分析不假装是 AI

    def test_related_sources_rendered(self):
        item = _item("宁德时代(300750)拟回购")
        item.related.append(
            RelatedNews(
                source_label="证券时报", title="宁德时代披露回购进展",
                url="https://example.com/stcn/1", published_at=REF - timedelta(minutes=9),
                relation="same_event", via="google",
            )
        )
        item.related.append(
            RelatedNews(source_label="证券之星", title="宁德时代盘中异动", relation="same_subject")
        )
        item.ai_analysis = "分析。"
        html = self._html(item)
        self.assertIn(">多源 1</span>", html)
        self.assertIn("证券时报（Google News）", html)
        self.assertIn('href="https://example.com/stcn/1"', html)
        self.assertIn("宁德时代披露回购进展", html)
        self.assertIn("（同标的，待核对）", html)

    def test_single_source_badge_only_after_search(self):
        item = _item("某公司公告")
        item.ai_analysis = "分析。"
        self.assertNotIn(">单一来源</span>", self._html(item))
        item.related_searched = True
        self.assertIn(">单一来源</span>", self._html(item))

    def test_analysis_and_related_are_escaped(self):
        item = _item("标题")
        item.ai_analysis = "<script>alert(1)</script>"
        item.related.append(RelatedNews(source_label="<b>x</b>", title="<img src=x onerror=1>", url="javascript:alert(1)\"", relation="same_event"))
        html = self._html(item)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("<b>x</b>", html)

    def test_down_move_uses_green(self):
        item = _item("某股跳水")
        item.last_price = 10.0
        item.price_change = -3.2
        item.price_code = "000001"
        html = self._html(item)
        self.assertIn("-3.20%", html)
        self.assertIn("#2c6b4f", html)  # GREEN


if __name__ == "__main__":
    unittest.main(verbosity=2)
