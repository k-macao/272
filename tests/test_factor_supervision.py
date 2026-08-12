"""A股市场监督管理层：监管事件识别、时间校验、风险分级、合规审查（全程离线）。"""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from octopus.factor import compliance
from octopus.factor.supervision import (
    RegulatoryEvent,
    SupervisionReport,
    SupervisionSource,
    classify_event,
    freshness_note,
    policy_context,
)
from octopus.http import FetchError
from octopus.models import TimeQuality
from tests.fixtures.factor_samples import ANNOUNCEMENTS, REF


class FakeHttp:
    def __init__(self, payload=None, *, fail=False):
        self.payload = payload if payload is not None else ANNOUNCEMENTS
        self.fail = fail
        self.pages: list[int] = []

    def json(self, url, params=None, headers=None, strip_jsonp=False):
        if self.fail:
            raise FetchError("公告接口 502")
        page = int((params or {}).get("page_index", 1))
        self.pages.append(page)
        return self.payload if page == 1 else {"data": {"list": []}}


class TestClassifyEvent(unittest.TestCase):
    def test_recognises_categories(self):
        cases = {
            "关于对某公司的问询函": "问询关注",
            "收到中国证监会立案告知书": "立案调查",
            "股票交易异常波动公告": "异常波动",
            "收到深圳证券交易所关注函": "问询关注",
            "关于公司股票被实施其他风险警示的公告": "退市风险",
            "收到行政处罚决定书": "行政处罚",
            "关于收到警示函的公告": "监管措施",
            "关于被公开谴责的公告": "纪律处分",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                category, severity = classify_event(title)
                self.assertEqual(category, expected)
                self.assertGreater(severity, 0)

    def test_non_regulatory_returns_empty(self):
        for title in ("2026年半年度业绩预增公告", "关于变更公司注册地址的公告", "高管增持计划"):
            with self.subTest(title=title):
                self.assertEqual(classify_event(title), ("", 0))

    def test_most_severe_wins(self):
        """一条标题同时含多个关键词时，按最严重的归类。"""
        category, severity = classify_event("关于收到立案告知书及问询函的公告")
        self.assertEqual(category, "立案调查")
        self.assertGreaterEqual(severity, 95)


class TestSupervisionCollect(unittest.TestCase):
    def test_filters_to_regulatory_only(self):
        report = SupervisionSource(FakeHttp()).collect(ref=REF)
        titles = [e.title for e in report.events]
        self.assertTrue(any("问询函" in t for t in titles))
        self.assertFalse(any("业绩预增" in t for t in titles), "非监管公告不应进入")

    def test_drops_out_of_window(self):
        report = SupervisionSource(FakeHttp(), window_days=30).collect(ref=REF)
        self.assertFalse(any("旧闻" in e.title for e in report.events))
        self.assertGreaterEqual(report.dropped_stale, 1)

    def test_drops_missing_timestamp(self):
        """无时间戳一律丢弃 —— 绝不用抓取时刻冒充发布时刻。"""
        report = SupervisionSource(FakeHttp()).collect(ref=REF)
        self.assertFalse(any("无时间" in e.title for e in report.events))
        self.assertGreaterEqual(report.dropped_no_time, 1)

    def test_drops_future_timestamp(self):
        report = SupervisionSource(FakeHttp()).collect(ref=REF)
        self.assertFalse(any("未来股份" in e.title for e in report.events))

    def test_all_events_have_exact_time(self):
        report = SupervisionSource(FakeHttp()).collect(ref=REF)
        self.assertTrue(report.events)
        for event in report.events:
            self.assertIsNotNone(event.published_at)
            self.assertIsNot(event.time_quality, TimeQuality.MISSING)
            self.assertLessEqual(event.published_at, REF + timedelta(minutes=10))

    def test_sorted_by_severity(self):
        report = SupervisionSource(FakeHttp()).collect(ref=REF)
        severities = [e.severity for e in report.events]
        self.assertEqual(severities, sorted(severities, reverse=True))

    def test_keyword_filter_narrows_results(self):
        report = SupervisionSource(FakeHttp()).collect(keywords=["汇川技术"], ref=REF)
        self.assertTrue(report.events)
        self.assertTrue(all("汇川" in e.title or "汇川" in e.stock for e in report.events))

    def test_code_filter_narrows_results(self):
        report = SupervisionSource(FakeHttp()).collect(codes={"002050"}, ref=REF)
        self.assertTrue(report.events)
        self.assertTrue(all(e.code == "002050" for e in report.events))

    def test_fetch_failure_is_captured_not_raised(self):
        report = SupervisionSource(FakeHttp(fail=True)).collect(ref=REF)
        self.assertFalse(report.ok)
        self.assertIn("502", report.error)
        self.assertEqual(report.events, [])

    def test_urls_are_built_for_detail_page(self):
        report = SupervisionSource(FakeHttp()).collect(ref=REF)
        target = next(e for e in report.events if e.code == "300124")
        self.assertIn("data.eastmoney.com/notices/detail/300124", target.url)


class TestRiskLevel(unittest.TestCase):
    def _event(self, severity: int) -> RegulatoryEvent:
        return RegulatoryEvent(
            title="t", url="", published_at=REF, time_quality=TimeQuality.EXACT,
            category="c", severity=severity,
        )

    def test_level_reflects_focus_only(self):
        """全市场噪音不该抬高单个主题的风险等级。"""
        report = SupervisionReport()
        report.events = [self._event(100)]     # 全市场有立案
        report.focus = []                      # 但与本主题无关
        self.assertEqual(report.risk_level, "低")

    def test_level_high_when_focus_severe(self):
        report = SupervisionReport()
        report.focus = [self._event(95)]
        self.assertEqual(report.risk_level, "高")

    def test_level_medium(self):
        report = SupervisionReport()
        report.focus = [self._event(70)]
        self.assertEqual(report.risk_level, "中")

    def test_level_low_when_mild(self):
        report = SupervisionReport()
        report.focus = [self._event(55)]
        self.assertEqual(report.risk_level, "偏低")

    def test_summary_mentions_both_scopes(self):
        report = SupervisionReport()
        report.events = [self._event(70), self._event(60)]
        report.focus = [self._event(70)]
        line = report.summary_line()
        self.assertIn("分析标的", line)
        self.assertIn("全市场", line)

    def test_summary_when_no_events(self):
        report = SupervisionReport()
        report.scanned = 42
        self.assertIn("42", report.summary_line())


class TestMatching(unittest.TestCase):
    def test_for_codes_and_keywords(self):
        report = SupervisionSource(FakeHttp()).collect(ref=REF)
        self.assertTrue(report.for_codes({"300124"}))
        self.assertTrue(report.for_keywords(["三花智控"]))
        self.assertEqual(report.for_keywords([]), [])
        self.assertEqual(report.for_keywords(["锂"]), [])

    def test_single_char_keywords_ignored(self):
        """单字关键词会命中一切，必须忽略。"""
        report = SupervisionSource(FakeHttp()).collect(ref=REF)
        self.assertEqual(report.for_keywords(["的"]), [])


class TestPolicyContext(unittest.TestCase):
    def test_detects_policy_words(self):
        self.assertIn("证监会", policy_context("证监会新规对量化交易的影响"))
        self.assertIn("程序化交易", policy_context("程序化交易监管细则解读"))

    def test_returns_empty_for_plain_topic(self):
        self.assertEqual(policy_context("人形机器人"), [])


class TestFreshnessNote(unittest.TestCase):
    def test_hours_for_recent(self):
        events = [
            RegulatoryEvent("t", "", REF - timedelta(hours=3), TimeQuality.EXACT, "c", 50)
        ]
        self.assertIn("小时前", freshness_note(events, ref=REF))

    def test_days_for_older(self):
        events = [
            RegulatoryEvent("t", "", REF - timedelta(days=4), TimeQuality.EXACT, "c", 50)
        ]
        self.assertIn("天前", freshness_note(events, ref=REF))


class TestCompliance(unittest.TestCase):
    def test_detects_and_rewrites_buy_advice(self):
        result = compliance.review("建议立即买入该股，目标价：25.6元")
        self.assertFalse(result.clean)
        self.assertNotIn("建议立即买入", result.text)
        self.assertNotIn("25.6元", result.text)

    def test_detects_return_promises(self):
        for bad in ("明天必涨", "稳赚不赔", "无风险套利", "涨停在望", "保本保收益"):
            with self.subTest(bad=bad):
                self.assertFalse(compliance.review(bad).clean)

    def test_detects_insider_and_manipulation(self):
        result = compliance.review("内幕消息显示主力已经建仓完毕，跟着主力操作")
        categories = {h.category for h in result.hits}
        self.assertIn("内幕信息", categories)
        self.assertIn("操纵暗示", categories)

    def test_clean_text_passes_through_unchanged(self):
        text = "该板块20日动量因子转正，量能温和放大，需注意估值波动风险。"
        result = compliance.review(text)
        self.assertTrue(result.clean)
        self.assertEqual(result.text, text)

    def test_rewrite_is_idempotent(self):
        """改写后的文本再审一次必须干净 —— 替换词自身不能触发规则。"""
        once = compliance.review(
            "建议买入，稳赚不赔，无风险，必涨，内幕消息，目标价：10元，跟着主力"
        )
        twice = compliance.review(once.text)
        self.assertTrue(twice.clean, f"二次审查仍有命中：{[h.matched for h in twice.hits]}")

    def test_scan_does_not_modify(self):
        text = "建议买入"
        hits = compliance.scan(text)
        self.assertTrue(hits)
        self.assertEqual(text, "建议买入")

    def test_summary_reports_counts(self):
        result = compliance.review("建议买入，必涨")
        self.assertIn("检出", result.summary())
        self.assertIn("中性化改写", result.summary())

    def test_disclaimer_present_and_clean(self):
        lines = compliance.disclaimer()
        self.assertGreaterEqual(len(lines), 3)
        joined = "".join(lines)
        self.assertIn("不构成", joined)
        self.assertNotIn("independently", joined)

    def test_ai_rules_cover_key_prohibitions(self):
        rules = compliance.AI_COMPLIANCE_RULES
        for word in ("买入", "目标价", "收益承诺", "内幕", "监管"):
            self.assertIn(word, rules)


if __name__ == "__main__":
    unittest.main()
