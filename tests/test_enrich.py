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
    clip_headline,
    code_to_secid,
    enrich_news,
    extract_tickers,
    fetch_quotes,
    format_brief,
    market_context,
    parse_news_analyses,
    parse_news_briefs,
    parse_news_reports,
    rule_analysis,
    rule_headline,
    split_headline_and_analysis,
)
from octopus.http import FetchError
from octopus.models import (
    ANALYSIS_BRIEF,
    ANALYSIS_SECURITY,
    Item,
    RelatedNews,
    SourceResult,
    TimeQuality,
)
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
        item = _item(
            "宁德时代(300750)" + "很长的标题" * 5,
            summary="涨幅 9.98%，换手 1.57%，封单 3.76亿",
        )
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
        item = _item("某公司披露回购公告")
        self.assertIn("未做外部检索", rule_analysis(item))
        item.related_searched = True
        self.assertIn("外部检索均未见", rule_analysis(item))

    def test_rule_focus_by_event(self):
        self.assertIn("监管口径", rule_analysis(_item("某公司收到问询函")))
        self.assertIn("统计局", rule_analysis(_item("7月 CPI 同比上涨0.5%")))
        self.assertIn("盘中信号", rule_analysis(_item("某股快速拉升")))

    def test_rule_analysis_has_required_securities_fields_and_probabilities(self):
        text = rule_analysis(_item("宁德时代(300750)拟回购股份"))
        for field in (
            "【事件重塑】",
            "【利弊挖掘】",
            "【深度溯源】",
            "【多维推演】",
            "【事实核查】",
        ):
            self.assertIn(field, text)
        self.assertIn("偏多 62% / 偏空 38%", text)
        self.assertIn("非统计预测", text)

    def test_rule_analysis_event_skeleton_skips_source_and_title(self):
        """事件重塑只补时间/归类/板块/概念，不复述已在卡片上的来源名与标题。"""
        item = _item(
            "宁德时代(300750)" + "很长的标题" * 5, summary="涨幅 9.98%，换手 1.57%"
        )
        text = rule_analysis(item)
        self.assertNotIn("示例源", text)
        self.assertNotIn("很长的标题", text)
        self.assertIn("本条于 07-27 10:25 披露", text)
        self.assertIn("行业板块（本地词典匹配）", text)
        self.assertIn("相关概念", text)

    def test_rule_analysis_traces_root_cause_and_fact_check(self):
        """深度溯源要追问触发与上游，事实核查要交代证据强度。"""
        text = rule_analysis(_item("某公司收到交易所问询函"))
        self.assertIn("直接触发", text)
        self.assertIn("上游一层", text)
        self.assertIn("推测", text)
        self.assertIn("待核实", text)
        self.assertIn("监管口径", text)
        # 事实核查：没有同题报道时必须写明证据不足
        self.assertIn("观点样本不足", text)

    # -- 兜底条款：没内容 / 分析不出就不显示 -------------------------------
    UNCLASSIFIABLE = (
        "某公司发布提示性公告",
        "关于召开2025年第一次临时股东大会的通知",
        "很长的标题" * 5,
    )

    def test_rule_analysis_empty_when_nothing_can_be_analyzed(self):
        """事件类型、标的、行业概念一个都认不出：整块不显示，不写兜底条款凑字数。"""
        for title in self.UNCLASSIFIABLE:
            with self.subTest(title=title):
                self.assertEqual(rule_analysis(_item(title)), "")

    def test_rule_analysis_never_prints_placeholder_words(self):
        """认不出的字段直接省略，不再出现「未归类/未识别/依据不足」这类占位文本。"""
        for title in (
            "宁德时代(300750)拟回购股份",
            "7月 CPI 同比上涨0.5%",
            "某公司发布公告",
            "Z精达转 · 强赎提醒",
            *self.UNCLASSIFIABLE,
        ):
            text = rule_analysis(_item(title))
            for word in ("未归类", "未识别", "时间待核", "依据不足", "信息不足"):
                self.assertNotIn(word, text)
            # 没命中事件类型时不给概率，也就不会出现「默认 50/50」的假中性
            if "【多维推演】" not in text:
                self.assertNotIn("偏多", text)
                self.assertNotIn("规则情景权重", text)

    def test_rule_analysis_drops_modules_without_content(self):
        """转债类只认得出事件归类：利弊/溯源/推演三块没内容，整块不写。"""
        text = rule_analysis(_item("Z精达转 · 强赎提醒"))
        self.assertIn("【事件重塑】", text)
        self.assertIn("事件归类：转债", text)
        self.assertIn("【事实核查】", text)
        for field in ("【利弊挖掘】", "【深度溯源】", "【多维推演】"):
            self.assertNotIn(field, text)
        self.assertNotIn("规则情景权重", text)  # 没给概率就不挂这句注解

    def test_rule_analysis_keeps_subject_and_sector_without_event_type(self):
        """认得出标的与行业、认不出事件类型：只留事件重塑 + 事实核查两块真内容。"""
        text = rule_analysis(_item("宁德时代(300750)发布公告"))
        self.assertIn("【事件重塑】", text)
        self.assertIn("行业板块（本地词典匹配）：电力设备", text)
        self.assertIn("【事实核查】", text)
        self.assertNotIn("【多维推演】", text)

    def test_rule_headline_empty_when_event_type_unknown(self):
        """认不出事件类型时不再输出「先把这条消息看清楚」这种对谁都成立的话。"""
        for title in self.UNCLASSIFIABLE:
            with self.subTest(title=title):
                self.assertEqual(rule_headline(_item(title)), "")

    def test_enrich_leaves_unanalyzable_item_blank(self):
        """无 Key 时，分析不出的条目一句话与分析都留空，推送里不显示这两块。"""
        item = _item("关于召开2025年第一次临时股东大会的通知")
        stats = enrich_news([item], http=None, api_key="")
        self.assertEqual(item.ai_headline, "")
        self.assertEqual(item.ai_analysis, "")
        self.assertFalse(item.ai_headline_from_model)
        self.assertFalse(item.ai_analysis_from_model)
        self.assertEqual(stats["analysis_skipped"], 1)
        self.assertEqual(stats["headline_skipped"], 1)
        self.assertEqual(stats["rule"], 0)

    def test_market_context_uses_local_sector_and_concept_dictionary(self):
        context = market_context(_item("宁德时代(300750)拟回购股份"))
        self.assertEqual(context.sector, "电力设备")
        self.assertIn("锂电池", context.concepts)
        self.assertLessEqual(len(context.concepts), 3)
        macro = market_context(_item("央行宣布降准"))
        self.assertEqual(macro.sector, "全市场（宏观）")

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

    def test_viewpoint_does_not_count_as_factual_corroboration(self):
        item = _item("某公司拟回购")
        item.related.append(
            RelatedNews(
                source_label="某媒体", title="回购影响解读", relation="similar_viewpoint"
            )
        )
        http = MagicMock()
        http.json.side_effect = FetchError("offline")
        http.text.side_effect = FetchError("offline")
        http.post_json.return_value = {
            "choices": [{"message": {"content": "1. 该回购事实已获多方证实。"}}]
        }
        enrich_news([item], http=http, api_key="sk-test", crossref_mode="off")
        self.assertFalse(item.ai_analysis_from_model)

    def test_ai_probability_fields_must_sum_to_one_hundred(self):
        item = _item("宁德时代拟回购")
        http = MagicMock()
        http.json.side_effect = FetchError("offline")
        http.text.side_effect = FetchError("offline")
        invalid = (
            "1. 分析：【事件重塑】宁德时代披露400亿回购；【利弊挖掘】股东受益；"
            "【深度溯源】现金流充裕；【多维推演】偏多 70% / 偏空 40%：执行风险；"
            "【事实核查】观点样本不足。"
        )
        http.post_json.return_value = {"choices": [{"message": {"content": invalid}}]}
        enrich_news([item], http=http, api_key="sk-test", crossref_mode="off")
        self.assertFalse(item.ai_analysis_from_model)
        self.assertIn("偏多 62% / 偏空 38%", item.ai_analysis)  # 回退透明规则权重

    def test_ai_partial_securities_fields_are_rejected(self):
        item = _item("宁德时代拟回购")
        http = MagicMock()
        http.json.side_effect = FetchError("offline")
        http.text.side_effect = FetchError("offline")
        partial = (
            "1. 分析：【事件重塑】宁德时代披露回购；【利弊挖掘】股东受益；"
            "【多维推演】偏多 58% / 偏空 42%：信心改善；【事实核查】样本不足。"
        )
        http.post_json.return_value = {"choices": [{"message": {"content": partial}}]}
        enrich_news([item], http=http, api_key="sk-test", crossref_mode="off")
        self.assertFalse(item.ai_analysis_from_model)
        self.assertIn("【深度溯源】", item.ai_analysis)

    def test_ai_probability_outside_multi_dimension_is_rejected(self):
        """概率必须写在【多维推演】里，写在别的模块同样拒收。"""
        item = _item("宁德时代拟回购")
        http = MagicMock()
        http.json.side_effect = FetchError("offline")
        http.text.side_effect = FetchError("offline")
        misplaced = (
            "1. 分析：【事件重塑】偏多 58% / 偏空 42%；【利弊挖掘】股东受益；"
            "【深度溯源】现金流充裕；【多维推演】短线情绪改善；【事实核查】样本不足。"
        )
        http.post_json.return_value = {"choices": [{"message": {"content": misplaced}}]}
        enrich_news([item], http=http, api_key="sk-test", crossref_mode="off")
        self.assertFalse(item.ai_analysis_from_model)
        self.assertIn("【多维推演】", item.ai_analysis)

    def test_ai_valid_probability_fields_are_kept(self):
        item = _item("宁德时代拟回购")
        http = MagicMock()
        http.json.side_effect = FetchError("offline")
        http.text.side_effect = FetchError("offline")
        valid = (
            "1. 分析：【事件重塑】宁德时代披露400亿回购方案；"
            "【利弊挖掘】股东受益，扩产资金方承压；"
            "【深度溯源】现金流充裕叠加股价偏离；"
            "【多维推演】情绪偏暖、业绩待验证、披露程序合规；偏多 58% / 偏空 42%：规模待验证；"
            "【事实核查】观点样本不足。"
        )
        http.post_json.return_value = {"choices": [{"message": {"content": valid}}]}
        enrich_news([item], http=http, api_key="sk-test", crossref_mode="off")
        self.assertTrue(item.ai_analysis_from_model)
        self.assertIn("偏多 58% / 偏空 42%", item.ai_analysis)

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


class TestBriefFallback(unittest.TestCase):
    """总编极简简报：证券分析没内容时的兜底体裁（核心快讯/关键要素/发展脉络）。"""

    BRIEF_TEXT = (
        "1. 【核心快讯】公司拟以自有资金回购不超过 40 亿元股份。\n"
        "   【关键要素】· 时间：9 月 19 日；· 地点：未提及；"
        "· 涉事方：宁德时代、董事会；· 起因：股价低于内在价值\n"
        "   【发展脉络】① 董事会通过回购议案；② 公告披露上限 40 亿元；"
        "③ 待股东大会审议（据材料推断）\n"
    )

    def _http(self, security_text: str, brief_text: str) -> MagicMock:
        """两次模型调用：证券五模块在前，总编简报兜底在后。"""
        from octopus.ai import NEWS_BRIEF_PROMPT

        http = MagicMock()
        http.json.side_effect = FetchError("offline")
        http.text.side_effect = FetchError("offline")

        def post_json(url, payload, headers=None):
            system = payload["messages"][0]["content"]
            content = brief_text if system == NEWS_BRIEF_PROMPT else security_text
            return {"choices": [{"message": {"content": content}}]}

        http.post_json.side_effect = post_json
        return http

    def test_format_brief_breaks_modules_and_items(self):
        text = format_brief(
            "1. 【核心快讯】公司拟回购。 【关键要素】· 时间：9月19日；· 地点：未提及；"
            "· 涉事方：公司；· 起因：股价偏低 【发展脉络】① 董事会通过；② 公告披露"
        )
        lines = text.split("\n")
        self.assertEqual(lines[0], "【核心快讯】公司拟回购。")
        self.assertIn("【关键要素】", lines[1])
        self.assertTrue(any(line.startswith("· 时间：") for line in lines))
        self.assertTrue(any(line.startswith("① ") for line in lines))
        self.assertTrue(any(line.startswith("② ") for line in lines))
        self.assertFalse(text.startswith("1."))  # 编号残留清掉

    def test_format_brief_truncates_at_break_point(self):
        long = "【核心快讯】一句话。" + "【发展脉络】① 阶段说明。" * 60
        text = format_brief(long, limit=200)
        self.assertLessEqual(len(text), 201)
        self.assertTrue(text.endswith("…"))

    def test_parse_news_briefs_by_number(self):
        mapping = parse_news_briefs(
            "1. 【核心快讯】第一条。\n   【关键要素】· 时间：未提及\n"
            "2. 【核心快讯】第二条。\n   【发展脉络】① 阶段\n"
            "3. 只有正文没有模块的第三条\n"
        )
        self.assertEqual(set(mapping), {1, 2})  # 没写【核心快讯】的丢掉
        self.assertIn("【关键要素】", mapping[1])
        self.assertIn("① 阶段", mapping[2])

    def test_brief_used_when_security_analysis_unusable(self):
        """证券分析被合规红线拒收：改问总编，简报顶上，标签变成 AI 简报。"""
        item = _item("宁德时代拟回购400亿", summary="公司公告回购。")
        stats = enrich_news(
            [item],
            http=self._http("1. 一句话：建议买入，稳赚不赔。分析：建议买入。", self.BRIEF_TEXT),
            api_key="sk-test",
            crossref_mode="off",
        )
        self.assertTrue(item.ai_analysis_from_model)
        self.assertEqual(item.ai_analysis_kind, ANALYSIS_BRIEF)
        self.assertIn("【核心快讯】", item.ai_analysis)
        self.assertIn("【发展脉络】", item.ai_analysis)
        self.assertNotIn("买入", item.ai_analysis)
        self.assertEqual(stats["brief"], 1)
        self.assertEqual(stats["ai"], 0)
        self.assertEqual(stats["rule"], 0)

    def test_brief_used_when_analysis_cannot_be_produced(self):
        """规则化也分析不出的通知类条目：总编简报补上，不再是空白。"""
        item = _item("关于召开2025年第一次临时股东大会的通知")
        self.assertEqual(rule_analysis(item), "")  # 前置条件：规则化无话可说
        stats = enrich_news(
            [item], http=self._http("（模型没返回这条）", self.BRIEF_TEXT),
            api_key="sk-test", crossref_mode="off",
        )
        self.assertEqual(item.ai_analysis_kind, ANALYSIS_BRIEF)
        self.assertIn("【核心快讯】", item.ai_analysis)
        self.assertEqual(stats["analysis_skipped"], 0)
        self.assertEqual(stats["brief"], 1)

    def test_brief_replaces_rule_headline(self):
        """核心快讯已经顶上一句人话的位置，规则化通用句同时清掉。"""
        item = _item("宁德时代拟回购400亿")
        enrich_news(
            [item], http=self._http("（无产出）", self.BRIEF_TEXT),
            api_key="sk-test", crossref_mode="off",
        )
        self.assertEqual(item.ai_headline, "")
        self.assertFalse(item.ai_headline_from_model)

    def test_model_headline_kept_next_to_brief(self):
        """模型写过一句人话、分析被拒收：一句话保留，与简报合并成一块。"""
        item = _item("宁德时代拟回购400亿")
        enrich_news(
            [item],
            http=self._http(
                "1. 一句话：回购分量取决于执行规模。\n   分析：建议买入，稳赚不赔。",
                self.BRIEF_TEXT,
            ),
            api_key="sk-test",
            crossref_mode="off",
        )
        self.assertEqual(item.ai_headline, "回购分量取决于执行规模。")
        self.assertTrue(item.ai_headline_from_model)
        self.assertEqual(item.ai_analysis_kind, ANALYSIS_BRIEF)

    def test_banned_brief_rejected_keeps_rule_text(self):
        item = _item("宁德时代拟回购400亿")
        enrich_news(
            [item],
            http=self._http("（无产出）", "1. 【核心快讯】建议买入，稳赚不赔。"),
            api_key="sk-test",
            crossref_mode="off",
        )
        self.assertFalse(item.ai_analysis_from_model)
        self.assertNotIn("买入", item.ai_analysis)
        self.assertIn("【事件重塑】", item.ai_analysis)  # 保留规则化五模块

    def test_brief_overclaim_rejected_on_single_source(self):
        item = _item("某公司拟回购")
        enrich_news(
            [item],
            http=self._http("（无产出）", "1. 【核心快讯】该消息已获多方证实。"),
            api_key="sk-test",
            crossref_mode="off",
        )
        self.assertFalse(item.ai_analysis_from_model)
        self.assertNotIn("多方证实", item.ai_analysis)

    def test_brief_not_called_when_security_analysis_ok(self):
        from octopus.ai import NEWS_BRIEF_PROMPT

        item = _item("宁德时代拟回购400亿")
        http = self._http(
            "1. 一句话：回购分量看执行。\n   分析：【事件重塑】公司披露回购；"
            "【利弊挖掘】股东受益；【深度溯源】现金充裕；"
            "【多维推演】偏多 62% / 偏空 38%；【事实核查】观点样本不足。",
            self.BRIEF_TEXT,
        )
        enrich_news([item], http=http, api_key="sk-test", crossref_mode="off")
        self.assertEqual(item.ai_analysis_kind, ANALYSIS_SECURITY)
        systems = [c[0][1]["messages"][0]["content"] for c in http.post_json.call_args_list]
        self.assertNotIn(NEWS_BRIEF_PROMPT, systems)  # 有产出就不必再问一次

    def test_no_brief_without_api_key(self):
        item = _item("关于召开2025年第一次临时股东大会的通知")
        http = self._http("（不会用到）", self.BRIEF_TEXT)
        stats = enrich_news([item], http=http, api_key="", crossref_mode="off")
        http.post_json.assert_not_called()
        self.assertEqual(item.ai_analysis, "")  # 分析不出：整块不显示
        self.assertEqual(stats["brief"], 0)
        self.assertEqual(stats["analysis_skipped"], 1)

    def test_brief_sends_publish_time_and_materials(self):
        """总编要梳理发展脉络，输入里必须带发布时间与已检索到的公开材料。"""
        from octopus.ai import NEWS_BRIEF_PROMPT

        item = _item("宁德时代拟回购400亿", summary="公司公告回购。")
        item.related.append(
            RelatedNews(source_label="证券时报", title="宁德时代披露回购", relation="same_event")
        )
        http = self._http("（无产出）", self.BRIEF_TEXT)
        enrich_news([item], http=http, api_key="sk-test", crossref_mode="off")
        brief_calls = [
            c for c in http.post_json.call_args_list
            if c[0][1]["messages"][0]["content"] == NEWS_BRIEF_PROMPT
        ]
        self.assertEqual(len(brief_calls), 1)
        user = brief_calls[0][0][1]["messages"][1]["content"]
        self.assertIn("发布时间 2026-07-27 10:25", user)
        self.assertIn("标题：宁德时代拟回购400亿", user)
        self.assertIn("公司公告回购。", user)
        self.assertIn("证券时报", user)


class TestOneLiner(unittest.TestCase):
    """每条新闻的一句人话：模型产出解析、规则化兜底与合规红线。"""

    MODEL_TEXT = (
        "1. 一句话：公司自己掏钱回购，等于管理层觉得现在不贵。\n"
        "   分析：事件要点：公司公告拟回购。多源印证：目前仅见单一来源，待其它渠道确认。"
        "关注点：留意回购进展公告。\n"
        "2. 一句话：宏观数据要看和市场预期的差。\n"
        "   分析：事件要点：统计局发布数据。多源印证：目前仅见单一来源，待其它渠道确认。"
        "关注点：以正式口径为准。\n"
    )

    def _ai_http(self, content: str) -> MagicMock:
        http = MagicMock()
        http.json.side_effect = FetchError("offline")
        http.text.side_effect = FetchError("offline")
        http.post_json.return_value = {"choices": [{"message": {"content": content}}]}
        return http

    def test_parse_headline_and_analysis(self):
        reports = parse_news_reports(self.MODEL_TEXT)
        self.assertEqual(reports[1].headline, "公司自己掏钱回购，等于管理层觉得现在不贵。")
        self.assertIn("多源印证：目前仅见单一来源", reports[1].analysis)
        self.assertEqual(reports[2].headline, "宏观数据要看和市场预期的差。")
        # 老接口只取分析部分，行为不变
        self.assertEqual(
            parse_news_analyses(self.MODEL_TEXT)[2].startswith("事件要点"), True
        )

    def test_parse_without_headline_marker_keeps_analysis(self):
        """模型没按新格式写「一句话」：分析照用，一句人话交给规则化兜底。"""
        text = "1. 事件要点：公司公告回购。多源印证：单一来源。关注点：公告原文。"
        report = parse_news_reports(text)[1]
        self.assertEqual(report.headline, "")
        self.assertIn("事件要点", report.analysis)

    def test_headline_takes_first_sentence_only(self):
        head, analysis = split_headline_and_analysis(
            "一句话：先说这一句。后面还有第二句。分析：这是正文。"
        )
        self.assertEqual(head, "先说这一句。")
        self.assertEqual(analysis, "这是正文。")

    def test_clip_headline_limits_length(self):
        self.assertEqual(clip_headline("很长的一句话" * 20)[-1], "…")
        self.assertLessEqual(len(clip_headline("很长的一句话" * 20)), 49)

    def test_rule_headline_by_event(self):
        self.assertIn("公告", rule_headline(_item("宁德时代拟回购400亿")))
        self.assertIn("监管", rule_headline(_item("某公司被证监会立案调查")))
        self.assertIn("预期", rule_headline(_item("7月 CPI 同比上涨0.5%")))
        self.assertIn("研报", rule_headline(_item("某券商首次覆盖并给予买入评级")))
        # 认不出事件类型：不写通用兜底句，整行留空由渲染层不显示
        self.assertEqual(rule_headline(_item("某公司发布提示性公告")), "")

    def test_rule_headline_is_neutral(self):
        from octopus.enrich import _BANNED_ANALYSIS

        for title in ("宁德时代拟回购", "某公司被立案调查", "某股涨停", "统计局发布 CPI"):
            text = rule_headline(_item(title))
            for word in _BANNED_ANALYSIS:
                self.assertNotIn(word, text)

    def test_enrich_fills_rule_headline_without_key(self):
        item = _item("宁德时代拟回购400亿")
        stats = enrich_news([item], http=None, api_key="")
        self.assertTrue(item.ai_headline)
        self.assertFalse(item.ai_headline_from_model)
        self.assertEqual(stats["headline_rule"], 1)
        self.assertEqual(stats["headline_ai"], 0)

    def test_ai_headline_and_analysis_both_from_model(self):
        item = _item("宁德时代拟回购400亿")
        stats = enrich_news(
            [item], http=self._ai_http(self.MODEL_TEXT), api_key="sk-test", crossref_mode="off"
        )
        self.assertTrue(item.ai_headline_from_model)
        self.assertEqual(item.ai_headline, "公司自己掏钱回购，等于管理层觉得现在不贵。")
        self.assertTrue(item.ai_analysis_from_model)
        self.assertEqual(stats["headline_ai"], 1)
        self.assertEqual(stats["ai"], 1)

    def test_banned_headline_rejected_but_analysis_kept(self):
        """一句话里出现荐股措辞 —— 只回退这一句，不连累分析。"""
        item = _item("某股盘中异动")
        enrich_news(
            [item],
            http=self._ai_http("1. 一句话：建议买入，稳赚不赔。分析：事件要点：盘中异动。"),
            api_key="sk-test",
            crossref_mode="off",
        )
        self.assertFalse(item.ai_headline_from_model)
        self.assertNotIn("买入", item.ai_headline)
        self.assertTrue(item.ai_analysis_from_model)

    def test_overclaim_headline_rejected_on_single_source(self):
        item = _item("某公司拟回购")
        enrich_news(
            [item],
            http=self._ai_http("1. 一句话：该消息已获多方证实。分析：事件要点：公司拟回购。"),
            api_key="sk-test",
            crossref_mode="off",
        )
        self.assertFalse(item.ai_headline_from_model)
        self.assertTrue(item.ai_headline)  # 仍有规则化一句话兜底


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
        self.assertNotIn("javascript:", html)

    def test_ai_block_merges_one_liner_and_analysis_at_end(self):
        """一句人话与分析合并成一块，排在现价 / 多源 / 时间 / 标签之后（新闻最后）。"""
        item = _item("宁德时代拟回购")
        item.last_price = 188.5
        item.price_code = "300750"
        item.tags = ["回购"]
        item.ai_headline = "公司自己掏钱回购，等于管理层觉得现在不贵。"
        item.ai_headline_from_model = True
        item.ai_analysis = "【事件重塑】公司公告拟回购；【事实核查】观点样本不足。"
        item.ai_analysis_from_model = True
        item.ai_analysis_kind = ANALYSIS_SECURITY
        html = self._html(item)
        self.assertIn(">AI 一句话</span>", html)
        self.assertIn(">AI 分析</span>", html)
        self.assertIn("公司自己掏钱回购，等于管理层觉得现在不贵。", html)
        self.assertIn("#1c1f23", html)  # 一句人话仍用深底高亮
        # 合并块整体在新闻最后：一句话在现价与多源之后，块级标签在两句正文之前
        self.assertLess(html.index(">现价</span>"), html.index(">AI 一句话</span>"))
        self.assertLess(html.index(">AI 分析</span>"), html.index(">AI 一句话</span>"))
        self.assertLess(html.index(">AI 一句话</span>"), html.index("【事件重塑】"))
        self.assertGreater(html.index(">AI 分析</span>"), html.index(">回购</span>"))

    def test_brief_block_is_labelled_as_brief(self):
        """总编极简简报挂「AI 简报」标签，三个模块分行排版。"""
        item = _item("关于召开2025年第一次临时股东大会的通知")
        item.ai_analysis = (
            "【核心快讯】公司定于下月召开临时股东大会审议多项议案。\n"
            "【关键要素】· 时间：未提及；· 地点：未提及；\n"
            "· 涉事方：公司；· 起因：常规治理安排\n"
            "【发展脉络】① 董事会提议；② 通知公告发出"
        )
        item.ai_analysis_from_model = True
        item.ai_analysis_kind = ANALYSIS_BRIEF
        html = self._html(item)
        self.assertIn(">AI 简报</span>", html)
        self.assertNotIn(">AI 分析</span>", html)
        for label in ("【核心快讯】", "【关键要素】", "【发展脉络】"):
            self.assertIn(label, html)
        self.assertIn("<br>", html)  # 清单项分行，不糊成一段

    def test_rule_one_liner_does_not_pretend_to_be_ai(self):
        item = _item("某公司公告")
        item.ai_headline = "先把这条消息说了什么看清楚，再判断它有多重。"
        html = self._html(item)
        self.assertIn(">一句话</span>", html)
        self.assertNotIn(">AI 一句话</span>", html)

    def test_missing_one_liner_omits_block(self):
        item = _item("某公司公告")
        item.ai_analysis = "分析。"
        self.assertNotIn("一句话</span>", self._html(item))

    def test_missing_analysis_omits_block(self):
        """分析为空（规则化也分析不出）：整块不显示，卡片上不留「分析」标签。"""
        html = self._html(_item("关于召开2025年第一次临时股东大会的通知"))
        self.assertNotIn(">分析</span>", html)
        self.assertNotIn(">AI 分析</span>", html)
        self.assertNotIn("【事件重塑】", html)

    def test_empty_module_rows_are_not_rendered(self):
        """只剩空模块标签的兜底文本：不渲染空行，也不渲染只有标签的空卡片。"""
        item = _item("某公司公告")
        item.ai_analysis = "【事件重塑】【利弊挖掘】【事实核查】"
        item.ai_analysis_from_model = True
        html = self._html(item)
        self.assertNotIn("【事件重塑】", html)
        self.assertNotIn(">AI 分析</span>", html)

    def test_partially_empty_modules_render_only_filled_rows(self):
        item = _item("宁德时代拟回购")
        item.ai_analysis = (
            "【事件重塑】公司披露回购方案；【利弊挖掘】；【事实核查】观点样本不足。"
        )
        item.ai_analysis_from_model = True
        html = self._html(item)
        self.assertIn("【事件重塑】", html)
        self.assertIn("公司披露回购方案", html)
        self.assertIn("【事实核查】", html)
        self.assertNotIn("【利弊挖掘】", html)  # 没内容的模块整行不显示

    def test_one_liner_is_escaped(self):
        item = _item("某公司公告")
        item.ai_headline = "<script>alert(1)</script>"
        item.ai_headline_from_model = True
        html = self._html(item)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_structured_securities_analysis_is_split_into_readable_rows(self):
        item = _item("宁德时代拟回购")
        item.ai_analysis = (
            "【事件重塑】宁德时代披露400亿回购方案；【利弊挖掘】股东受益、扩产资金方承压；"
            "【深度溯源】现金流充裕叠加股价偏离；"
            "【多维推演】情绪偏暖；偏多 58% / 偏空 42%：规模待验证；【事实核查】观点样本不足。"
        )
        item.ai_analysis_from_model = True
        html = self._html(item)
        for label in (
            "【事件重塑】",
            "【利弊挖掘】",
            "【深度溯源】",
            "【多维推演】",
            "【事实核查】",
        ):
            self.assertIn(label, html)
        self.assertIn("偏多", html)
        self.assertIn("偏空", html)
        # 概率本身按 A 股配色高亮：偏多红、偏空绿
        self.assertIn('#a63a2b;font-weight:700;">58%</span>', html)
        self.assertIn('#2c6b4f;font-weight:700;">42%</span>', html)

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
