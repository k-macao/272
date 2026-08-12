"""主题因子分析全链路：板块匹配、行情解析、评分、编排、渲染、推送（全程离线）。"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import octopus.agent as agent_mod
from octopus.agent import Agent
from octopus.config import Config
from octopus.factor import scoring
from octopus.factor.market import (
    Bar,
    Instrument,
    MarketData,
    _is_risky_name,
    _parse_kline,
    data_freshness,
    tokenize,
)
from octopus.factor.pipeline import ThemePipeline, build_facts, rule_based_report
from octopus.http import FetchError
from octopus.render import render_theme, render_theme_title
from tests.fixtures.factor_samples import (
    ANNOUNCEMENTS,
    BOARD_MEMBERS,
    CONCEPT_BOARDS,
    INDUSTRY_BOARDS,
    REF,
    TOP_AMOUNT,
    make_klines,
)


class FakeHttp:
    """模拟东财行情/公告接口。可按需让某类请求失败，测降级路径。"""

    def __init__(self, *, fail_boards=False, fail_kline=False, fail_ann=False,
                 short_kline=False, no_members=False):
        self.fail_boards = fail_boards
        self.fail_kline = fail_kline
        self.fail_ann = fail_ann
        self.short_kline = short_kline
        self.no_members = no_members
        self.calls: list[str] = []

    def json(self, url, params=None, headers=None, strip_jsonp=False):
        params = params or {}
        self.calls.append(url)

        if "clist" in url:
            # 按真实调用的 fs 精确分派：板块列表是 "m:90 t:3"/"m:90 t:2"，
            # 成分股是 "b:BKxxxx"，全市场榜是 "m:0+t:6,..."（含 t:2 子串，
            # 所以必须精确匹配而不是子串包含）。
            fs = str(params.get("fs", ""))
            if fs == "m:90 t:3":
                if self.fail_boards:
                    raise FetchError("板块接口 502")
                return CONCEPT_BOARDS
            if fs == "m:90 t:2":
                if self.fail_boards:
                    raise FetchError("板块接口 502")
                return INDUSTRY_BOARDS
            if fs.startswith("b:"):
                return {"data": {"diff": []}} if self.no_members else BOARD_MEMBERS
            if fs.startswith("m:0"):
                return TOP_AMOUNT
            raise AssertionError(f"未预期的 fs 参数：{fs}")

        if "kline" in url:
            if self.fail_kline:
                raise FetchError("K线接口 502")
            secid = str(params.get("secid", "0.000000"))
            seed = sum(ord(c) for c in secid)
            count = 40 if self.short_kline else int(params.get("lmt", 250))
            drift = [0.0018, -0.0012, 0.0005, 0.0022][seed % 4]
            return {
                "data": {
                    "code": secid,
                    "klines": make_klines(
                        count, start=8 + seed % 20, drift=drift,
                        vol=0.018 + 0.003 * (seed % 3), seed=seed, end_day=REF.date(),
                    ),
                }
            }

        if "security/ann" in url:
            if self.fail_ann:
                raise FetchError("公告接口 502")
            page = int(params.get("page_index", 1))
            return ANNOUNCEMENTS if page == 1 else {"data": {"list": []}}

        raise AssertionError(f"未预期的请求：{url}")

    def text(self, url, **kwargs):
        raise FetchError("离线测试不提供 text")

    def post_json(self, url, payload, headers=None):
        raise FetchError("离线测试不调用大模型")

    def close(self):
        pass


def pipeline(tmp: Path, **kwargs) -> ThemePipeline:
    kwargs.setdefault("stock_top", 3)
    return ThemePipeline(FakeHttp(**kwargs.pop("http_kwargs", {})), base_dir=tmp, **kwargs)


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
class TestTokenize(unittest.TestCase):
    def test_splits_and_drops_stopwords(self):
        words = tokenize("人形机器人板块分析")
        self.assertIn("人形机器人", " ".join(words) or "")
        self.assertNotIn("分析", words)

    def test_generates_substrings_for_long_words(self):
        words = tokenize("人形机器人产业链")
        self.assertTrue(any("机器人" in w for w in words))

    def test_longest_first(self):
        words = tokenize("半导体设备")
        self.assertEqual(words[0], "半导体设备")

    def test_empty_topic(self):
        self.assertEqual(tokenize(""), [])
        self.assertEqual(tokenize("的 了 分析"), [])


class TestKlineParsing(unittest.TestCase):
    def test_parses_full_row(self):
        bar = _parse_kline("2026-08-12,10.00,10.50,10.80,9.90,120000,126000000,8.5,5.0,0.50,3.21")
        self.assertEqual(bar.day, date(2026, 8, 12))
        self.assertEqual(bar.open, 10.0)
        self.assertEqual(bar.close, 10.5)
        self.assertEqual(bar.high, 10.8)
        self.assertEqual(bar.low, 9.9)
        self.assertEqual(bar.volume, 120000)
        self.assertAlmostEqual(bar.turnover, 3.21)
        self.assertAlmostEqual(bar.change, 5.0)

    def test_rejects_unparseable_date(self):
        """日期解析不出来的整根丢弃 —— 时间校验原则。"""
        self.assertIsNone(_parse_kline("not-a-date,1,2,3,4,5"))

    def test_rejects_short_row(self):
        self.assertIsNone(_parse_kline("2026-08-12,10.00"))

    def test_rejects_non_numeric(self):
        self.assertIsNone(_parse_kline("2026-08-12,-,-,-,-,-"))

    def test_vwap_falls_back_to_close(self):
        bar = Bar(day=date(2026, 8, 12), open=1, close=2, high=3, low=1, volume=0, amount=0)
        self.assertEqual(bar.vwap, 2)

    def test_vwap_from_amount(self):
        bar = Bar(day=date(2026, 8, 12), open=1, close=2, high=3, low=1,
                  volume=100, amount=100 * 100 * 2.5)
        self.assertAlmostEqual(bar.vwap, 2.5)


class TestRiskyNames(unittest.TestCase):
    def test_st_and_delisting_filtered(self):
        for name in ("ST步森", "*ST海投", "退市美尚"):
            self.assertTrue(_is_risky_name(name), name)

    def test_normal_names_pass(self):
        for name in ("汇川技术", "三花智控", "贵州茅台"):
            self.assertFalse(_is_risky_name(name), name)


class TestDataFreshness(unittest.TestCase):
    def test_today(self):
        self.assertIn("今日", data_freshness(REF.date(), ref=REF))

    def test_yesterday(self):
        from datetime import timedelta

        self.assertIn("上一交易日", data_freshness(REF.date() - timedelta(days=1), ref=REF))

    def test_unknown(self):
        self.assertIn("未知", data_freshness(None, ref=REF))


# ---------------------------------------------------------------------------
class TestMarketData(unittest.TestCase):
    def setUp(self):
        self.market = MarketData(FakeHttp())

    def test_matches_concept_board(self):
        board, candidates = self.market.match_board("人形机器人")
        self.assertIsNotNone(board)
        self.assertEqual(board.name, "人形机器人")
        self.assertEqual(board.kind, "概念")
        self.assertEqual(board.matched_by, "人形机器人")
        self.assertTrue(candidates)

    def test_matches_industry_board(self):
        board, _ = self.market.match_board("专用设备")
        self.assertEqual(board.name, "专用设备")
        self.assertEqual(board.kind, "行业")

    def test_partial_match(self):
        board, _ = self.market.match_board("机器人")
        self.assertIsNotNone(board)
        self.assertIn("机器人", board.name)

    def test_no_match_returns_none(self):
        board, candidates = self.market.match_board("完全不相干的东西xyz")
        self.assertIsNone(board)
        self.assertEqual(candidates, [])

    def test_members_exclude_st(self):
        members = self.market.board_members("BK0896", top=10)
        names = [name for _, name, _ in members]
        self.assertNotIn("ST步森", names)
        self.assertIn("汇川技术", names)

    def test_members_respect_top(self):
        self.assertEqual(len(self.market.board_members("BK0896", top=2)), 2)

    def test_secid_has_market_prefix(self):
        members = self.market.board_members("BK0896", top=3)
        secids = {name: secid for secid, name, _ in members}
        self.assertEqual(secids["汇川技术"], "0.300124")   # 深市
        self.assertEqual(secids["三花智控"], "1.002050")   # f13=1

    def test_kline_returns_sorted_bars(self):
        bars = self.market.kline("1.000001", limit=120)
        self.assertEqual(len(bars), 120)
        self.assertTrue(all(a.day < b.day for a, b in zip(bars, bars[1:])))

    def test_load_instrument_fills_quote(self):
        inst = self.market.load_instrument(
            "0.300124", "汇川技术", limit=100, quote={"f3": 4.1, "f8": 3.2, "f6": 2.9e9}
        )
        self.assertEqual(inst.code, "300124")
        self.assertAlmostEqual(inst.change, 4.1)
        self.assertAlmostEqual(inst.turnover, 3.2)
        self.assertEqual(len(inst.bars), 100)

    def test_board_failure_propagates_as_fetcherror(self):
        market = MarketData(FakeHttp(fail_boards=True))
        board, candidates = market.match_board("人形机器人")
        self.assertIsNone(board)   # 两类板块都失败 -> 无匹配，但不抛异常


class TestInstrument(unittest.TestCase):
    def test_ret_computation(self):
        bars = [
            Bar(day=date(2026, 8, d), open=1, close=float(c), high=1, low=1, volume=1)
            for d, c in zip(range(1, 6), (10, 11, 12, 13, 14))
        ]
        inst = Instrument(code="x", name="X", secid="0.x", bars=bars)
        self.assertAlmostEqual(inst.ret(1), (14 / 13 - 1) * 100)
        self.assertAlmostEqual(inst.ret(4), (14 / 10 - 1) * 100)
        self.assertIsNone(inst.ret(99))

    def test_fields_shape(self):
        bars = [Bar(day=date(2026, 8, 1), open=1, close=2, high=3, low=0.5, volume=100)]
        fields = Instrument(code="x", name="X", secid="0.x", bars=bars).fields()
        self.assertEqual(set(fields), {"open", "high", "low", "close", "volume", "amount", "vwap"})
        self.assertEqual(len(fields["close"]), 1)


# ---------------------------------------------------------------------------
class TestScoring(unittest.TestCase):
    def test_dimension_levels(self):
        cases = ((85, "偏强"), (60, "中性偏强"), (50, "中性"), (35, "中性偏弱"), (10, "偏弱"))
        for score, expected in cases:
            self.assertEqual(scoring.Dimension("k", "L", score, "").level, expected)

    def test_missing_score_level(self):
        self.assertEqual(scoring.Dimension("k", "L", None, "").level, "数据不足")

    def test_profile_from_empty_values(self):
        profile = scoring.build_profile("X", "000001", {})
        self.assertIsNone(profile.composite)
        self.assertIn("数据不足", profile.stance)

    def test_momentum_direction(self):
        """ROC = N日前价/最新价，<1 表示上涨 -> 动量应为高分。"""
        up = scoring.build_profile("U", "1", {"ROC5": 0.9, "ROC10": 0.85, "ROC20": 0.8, "ROC60": 0.7})
        down = scoring.build_profile("D", "2", {"ROC5": 1.1, "ROC10": 1.15, "ROC20": 1.2, "ROC60": 1.3})
        self.assertGreater(up.dim("momentum").score, 60)
        self.assertLess(down.dim("momentum").score, 40)

    def test_trend_above_ma_is_strong(self):
        """MA = 均价/最新价，<1 表示价格站上均线。"""
        profile = scoring.build_profile("X", "1", {"MA5": 0.95, "MA10": 0.93, "MA20": 0.9, "MA60": 0.85})
        dim = profile.dim("trend")
        self.assertGreater(dim.score, 60)
        self.assertIn("站上", dim.detail)

    def test_volume_ratio_wording(self):
        """VMA = 均量/最新量，0.5 表示放量一倍。"""
        profile = scoring.build_profile("X", "1", {"VMA5": 0.5, "VMA20": 0.5})
        self.assertIn("放量", profile.dim("volume").detail)

    def test_pricevolume_detail_matches_score(self):
        """量价维度的措辞必须与得分同向，不能出现高分配"不明显"。"""
        for values in (
            {"CORR10": 0.7, "CORR20": 0.65, "CORR60": 0.6},
            {"CORR10": -0.7, "CORR20": -0.65, "CORR60": -0.6},
            {"CORR10": 0.02, "CORR20": -0.01, "CORR60": 0.0},
        ):
            profile = scoring.build_profile("X", "1", values)
            dim = profile.dim("pricevolume")
            with self.subTest(values=values):
                if dim.score >= 65:
                    self.assertIn("正相关", dim.detail)
                elif dim.score <= 35:
                    self.assertIn("负相关", dim.detail)
                else:
                    self.assertIn("不明显", dim.detail)

    def test_momentum_wording_matches_signs(self):
        """方向分化时不能说"均为负"。"""
        profile = scoring.build_profile(
            "X", "1", {"ROC5": 1.05, "ROC10": 0.95, "ROC20": 1.1, "ROC60": 1.01}
        )
        detail = profile.dim("momentum").detail
        self.assertIn("分化", detail)
        self.assertNotIn("均为负", detail)

    def test_composite_is_mean_of_available(self):
        profile = scoring.build_profile("X", "1", {"ROC5": 0.9, "MA5": 0.95})
        scores = [d.score for d in profile.dimensions if d.score is not None]
        self.assertAlmostEqual(profile.composite, round(sum(scores) / len(scores), 1))

    def test_scores_are_bounded(self):
        extreme = {"ROC5": 0.01, "MA5": 0.01, "VMA5": 0.001, "CORR20": 5.0, "SUMP20": 9.9}
        profile = scoring.build_profile("X", "1", extreme)
        for dim in profile.dimensions:
            if dim.score is not None:
                self.assertGreaterEqual(dim.score, 0)
                self.assertLessEqual(dim.score, 100)

    def test_cross_section_sorted_desc(self):
        profiles = [
            scoring.FactorProfile(name="A", code="1", composite=30.0),
            scoring.FactorProfile(name="B", code="2", composite=80.0),
            scoring.FactorProfile(name="C", code="3", composite=None),
        ]
        ranking = scoring.cross_section(profiles)
        self.assertEqual([n for n, _ in ranking], ["B", "A"])


# ---------------------------------------------------------------------------
class TestPipelineRun(PipelineTestCase):
    def test_full_run_produces_report(self):
        analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)

        self.assertEqual(analysis.topic, "人形机器人")
        self.assertEqual(analysis.market.board.name, "人形机器人")
        self.assertTrue(analysis.profiles)
        self.assertTrue(analysis.benchmark_profiles)
        self.assertTrue(analysis.ai_report)
        self.assertFalse(analysis.used_ai)          # 没配 Key -> 规则化解读
        self.assertIsNotNone(analysis.compliance_result)

    def test_every_profile_has_six_dimensions(self):
        analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)
        for profile in analysis.all_profiles:
            self.assertEqual(len(profile.dimensions), 6, profile.name)
            self.assertIsNotNone(profile.composite, profile.name)

    def test_factor_values_are_computed(self):
        analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)
        inst = analysis.market.stocks[0]
        self.assertTrue(inst.factors)
        real = [v for v in inst.factors.values() if v is not None]
        self.assertGreater(len(real) / len(inst.factors), 0.9, "因子覆盖率过低")

    def test_supervision_focus_linked_to_holdings(self):
        analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)
        codes = {p.code for p in analysis.profiles}
        self.assertTrue(analysis.supervision.focus)
        self.assertTrue(
            any(e.code in codes for e in analysis.supervision.focus),
            "标的相关的监管事件未被识别",
        )

    def test_risk_level_elevated_by_focus(self):
        analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)
        self.assertIn(analysis.supervision.risk_level, ("中", "高"))

    def test_report_mentions_regulation(self):
        analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)
        self.assertIn("监管", analysis.ai_report)
        self.assertIn("风险提示", analysis.ai_report)

    def test_report_is_compliance_clean(self):
        """最终输出必须通过合规审查 —— 这是监管要求的底线。"""
        from octopus.factor import compliance

        analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)
        self.assertEqual(compliance.scan(analysis.ai_report), [])

    def test_provenance_disclosed(self):
        analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)
        self.assertIn("qlib", analysis.model.provenance)
        self.assertIn("因子模型", analysis.ai_report)

    def test_unmatched_topic_degrades_to_market_wide(self):
        analysis = pipeline(self.tmp).run("完全不相干xyz", ref=REF, use_ai=False)
        self.assertIsNone(analysis.market.board)
        self.assertIn("未匹配", analysis.market.universe_note)
        self.assertTrue(analysis.profiles)          # 仍有全市场标的兜底

    def test_index_hint_selects_benchmark(self):
        analysis = pipeline(self.tmp).run("创业板", ref=REF, use_ai=False)
        names = [p.name for p in analysis.benchmark_profiles]
        self.assertEqual(names, ["创业板指"])

    def test_short_history_yields_no_dimensions(self):
        """K线不足时如实标注"未计算"，不用臆造分数撑场面。"""
        p = ThemePipeline(FakeHttp(short_kline=True), base_dir=self.tmp, stock_top=2)
        analysis = p.run("人形机器人", ref=REF, use_ai=False)
        for profile in analysis.all_profiles:
            self.assertEqual(profile.dimensions, [])
        self.assertIn("未计算因子", analysis.ai_report)

    def test_supervision_failure_is_degraded_not_fatal(self):
        p = ThemePipeline(FakeHttp(fail_ann=True), base_dir=self.tmp, stock_top=2)
        analysis = p.run("人形机器人", ref=REF, use_ai=False)
        self.assertFalse(analysis.supervision.ok)
        self.assertTrue(analysis.profiles)          # 因子分析照常完成
        self.assertTrue(analysis.ai_report)

    def test_kline_failure_is_degraded_not_fatal(self):
        p = ThemePipeline(FakeHttp(fail_kline=True), base_dir=self.tmp, stock_top=2)
        analysis = p.run("人形机器人", ref=REF, use_ai=False)
        self.assertEqual(analysis.profiles, [])
        self.assertTrue(analysis.ai_report)
        self.assertTrue(analysis.market.errors)

    def test_empty_board_members_degrades(self):
        p = ThemePipeline(FakeHttp(no_members=True), base_dir=self.tmp, stock_top=2)
        analysis = p.run("人形机器人", ref=REF, use_ai=False)
        self.assertTrue(analysis.profiles)          # 降级到全市场成交额榜
        self.assertIn("未匹配到对应板块", analysis.market.universe_note)

    def test_notes_disclose_degradation(self):
        analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)
        joined = " ".join(analysis.notes)
        self.assertIn("内置", joined)               # 离线 -> 用了内置因子快照


class TestFactsForAI(PipelineTestCase):
    def test_facts_contain_all_sections(self):
        analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)
        facts = build_facts(analysis)
        for section in ("分析主题", "分析标的", "量化因子计算结果", "A股市场监督管理"):
            self.assertIn(section, facts)

    def test_facts_include_numbers(self):
        analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)
        facts = build_facts(analysis)
        self.assertRegex(facts, r"综合因子分 \d+\.\d")
        self.assertIn("关键因子值", facts)

    def test_facts_include_provenance(self):
        analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)
        self.assertIn("因子模型来源", build_facts(analysis))


class TestRuleBasedReport(PipelineTestCase):
    def test_has_all_sections(self):
        analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)
        report = rule_based_report(analysis)
        for section in ("【主题分类】", "【核心结论】", "【因子要点】",
                        "【监管与合规提示】", "【风险提示】", "【数据与口径】"):
            self.assertIn(section, report)

    def test_no_investment_advice(self):
        from octopus.factor import compliance

        analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)
        self.assertEqual(compliance.scan(rule_based_report(analysis)), [])


# ---------------------------------------------------------------------------
class TestRenderTheme(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.analysis = pipeline(self.tmp).run("人形机器人", ref=REF, use_ai=False)
        self.html = render_theme(self.analysis, ref=REF)

    def test_contains_core_sections(self):
        for text in ("章鱼 AI · 主题因子分析", "人形机器人", "量化因子评分",
                     "A股市场监督管理", "数据溯源与口径", "风险提示与免责声明"):
            self.assertIn(text, self.html)

    def test_only_wechat_safe_tags(self):
        """微信会剥 <style>，且 flex/grid 支持不稳 —— 只允许 div/span/a/table。"""
        tags = set(re.findall(r"<(\w+)", self.html))
        self.assertTrue(tags <= {"div", "span", "a", "table", "tr", "td"}, tags)
        self.assertNotIn("<style", self.html)
        self.assertNotIn("display:flex", self.html)

    def test_no_script_injection(self):
        self.assertNotIn("<script", self.html.lower())

    def test_escapes_model_output(self):
        self.analysis.ai_report = "<script>alert(1)</script> & <b>x</b>"
        html = render_theme(self.analysis, ref=REF)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_shows_supervision_events(self):
        self.assertIn("问询关注", self.html)

    def test_shows_disclaimer(self):
        self.assertIn("不构成", self.html)

    def test_title_includes_topic_and_score(self):
        title = render_theme_title("人形机器人", self.analysis, ref=REF)
        self.assertIn("人形机器人", title)
        self.assertIn("因子分析", title)
        self.assertRegex(title, r"因子\d+分")

    def test_title_flags_regulatory_risk(self):
        title = render_theme_title("人形机器人", self.analysis, ref=REF)
        self.assertIn("监管风险", title)

    def test_title_truncates_long_topic(self):
        title = render_theme_title("很" * 40, self.analysis, ref=REF)
        self.assertIn("…", title)

    def test_renders_without_analysis(self):
        self.assertIn("主题", render_theme_title("主题", None, ref=REF))


# ---------------------------------------------------------------------------
class RecordingPush:
    sent: list[tuple[str, str]] = []
    instances: list[tuple] = []

    def __init__(self, *args, **kwargs):
        RecordingPush.instances.append((args, kwargs))

    def send(self, title, content, dry_run=False):
        RecordingPush.sent.append((title, content))
        return True


class TestAgentPushTheme(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self._backup = agent_mod.PushPlus
        agent_mod.PushPlus = RecordingPush
        RecordingPush.sent = []
        RecordingPush.instances = []

        self.agent = Agent(
            Config(pushplus_token="test-token", pushplus_topics=["oai.1"], factor_stock_top=2),
            base_dir=self.tmp,
        )
        self.agent.http = FakeHttp()

    def tearDown(self):
        agent_mod.PushPlus = self._backup
        super().tearDown()

    def test_push_theme_sends_once(self):
        report = self.agent.push_theme("人形机器人", ref=REF, use_ai=False)
        self.assertTrue(report.pushed)
        self.assertEqual(len(RecordingPush.sent), 1)
        title, html = RecordingPush.sent[0]
        self.assertIn("人形机器人", title)
        self.assertIn("量化因子评分", html)

    def test_push_theme_is_one_to_one(self):
        """一对一：即使配了群组 topic，主题推送也不带 topic。"""
        self.agent.push_theme("人形机器人", ref=REF, use_ai=False)
        _, kwargs = RecordingPush.instances[0]
        self.assertEqual(kwargs["topics"], [])

    def test_report_carries_analysis(self):
        report = self.agent.push_theme("人形机器人", ref=REF, use_ai=False)
        self.assertIsNotNone(report.analysis)
        self.assertTrue(report.analysis.profiles)

    def test_preview_theme_returns_html(self):
        html = self.agent.preview_theme("人形机器人", ref=REF, use_ai=False)
        self.assertIn("主题因子分析", html)
        self.assertEqual(len(RecordingPush.sent), 0)

    def test_analyze_theme_does_not_push(self):
        analysis = self.agent.analyze_theme("人形机器人", ref=REF, use_ai=False)
        self.assertTrue(analysis.profiles)
        self.assertEqual(len(RecordingPush.sent), 0)


class TestWorkflowTemplate(unittest.TestCase):
    """粘贴用模版 theme_analysis.yml.txt 必须与真正的 workflow 保持一致。

    GitHub App 没有 workflows 权限，推不了 .github/workflows/，
    所以要靠人工复制。模版一旦悄悄和 .yml 脱节，人工复制出来的就是旧版本。
    """

    ROOT = Path(__file__).resolve().parent.parent
    WORKFLOW_DIR = ROOT / "deploy" / "github-workflows"
    SEPARATOR = "# ============================== 分隔线：以下为正文 ==========================\n\n"

    def setUp(self):
        self.yml = self.WORKFLOW_DIR / "theme_analysis.yml"
        self.tpl = self.WORKFLOW_DIR / "theme_analysis.yml.txt"

    def test_both_files_exist(self):
        self.assertTrue(self.yml.is_file(), "缺少 theme_analysis.yml")
        self.assertTrue(self.tpl.is_file(), "缺少粘贴用模版 theme_analysis.yml.txt")

    def test_template_body_matches_workflow_byte_for_byte(self):
        text = self.tpl.read_text(encoding="utf-8")
        self.assertEqual(text.count(self.SEPARATOR), 1, "分隔线必须恰好出现一次")
        body = text.split(self.SEPARATOR, 1)[1]
        self.assertEqual(
            body,
            self.yml.read_text(encoding="utf-8"),
            "模版分隔线之后的内容与 theme_analysis.yml 不一致，请重新同步",
        )

    def test_workflow_is_valid_yaml_with_expected_inputs(self):
        try:
            import yaml
        except ImportError:  # pragma: no cover - 环境无 pyyaml 时跳过
            self.skipTest("未安装 pyyaml")
        data = yaml.safe_load(self.yml.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "章鱼AI 主题因子分析")
        # YAML 会把裸 on 解析成 True
        inputs = data[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(
            set(inputs),
            {"theme", "stock_top", "supervision_days", "use_ai", "dry_run"},
        )
        self.assertTrue(inputs["theme"]["required"])

    def test_workflow_never_passes_group_topic(self):
        """一对一的最后一道闸：workflow 不得把 PUSHPLUS_TOPIC 作为环境变量传进去。

        模版顶部的说明文字里会提到这个变量名（解释「为什么刻意不传」），
        所以查的是 env 赋值 `PUSHPLUS_TOPIC:` 而不是单纯提及。
        """
        for path in (self.yml, self.tpl):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue  # 注释里提及无妨
                self.assertFalse(
                    stripped.startswith("PUSHPLUS_TOPIC:"),
                    f"{path.name} 不应传群组 topic：{line!r}",
                )

    def test_workflow_invokes_theme_entrypoint(self):
        body = self.yml.read_text(encoding="utf-8")
        self.assertIn("--theme", body)
        self.assertIn("PUSHPLUS_TOKEN", body)


if __name__ == "__main__":
    unittest.main()
