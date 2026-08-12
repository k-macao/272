"""国外免费数据源（Yahoo Finance）与多源降级链路的测试。

本沙箱出口被白名单限制（连不上 Yahoo），所以这里全部用真实格式的
fixture 离线验证：Yahoo v8 chart / spark 的 JSON 结构、复权算法、
代码映射、Provider 优先级与降级、以及 yahoo 模式下整条主题分析链路。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from octopus.factor.concepts import (
    BOARDS,
    CONCEPTS,
    CORE_UNIVERSE,
    INDUSTRIES,
    board_kind,
    secid_to_code,
    secid_to_yahoo_symbol,
    to_secid,
    to_yahoo_symbol,
)
from octopus.factor.market import Bar, Board, MarketData
from octopus.factor.pipeline import ThemePipeline
from octopus.factor.providers import YahooProvider
from octopus.http import FetchError
from tests.fixtures.factor_samples import ANNOUNCEMENTS, REF


# ---------------------------------------------------------------------------
# Yahoo 响应构造（与真实接口同构）
# ---------------------------------------------------------------------------
def make_yahoo_chart(symbol: str, days: int = 200, *, seed: int = 7,
                     end: date | None = None) -> dict:
    """构造 Yahoo v8 chart 响应。价格从 seed 派生，保证确定性。"""
    end = end or REF.date()
    start_ts = int((end - timedelta(days=days - 1)).strftime("%s"))
    ts_list = [start_ts + i * 86400 for i in range(days)]
    base = 10.0 + (seed % 5)
    opens, highs, lows, closes, vols, adj = [], [], [], [], [], []
    for i in range(days):
        c = base + (i % 23) * 0.31 + (seed % 7) * 0.05
        o = c - 0.2
        h = c + 0.4
        l = c - 0.3
        closes.append(round(c, 2))
        opens.append(round(o, 2))
        highs.append(round(h, 2))
        lows.append(round(l, 2))
        vols.append(10000 + i * 137 + seed * 31)
        adj.append(round(c * (1.0 + 0.0004 * i), 2))   # 缓慢上移的复权因子
    return {
        "chart": {
            "result": [{
                "meta": {"symbol": symbol, "regularMarketPrice": closes[-1]},
                "timestamp": ts_list,
                "indicators": {
                    "quote": [{"open": opens, "high": highs, "low": lows,
                               "close": closes, "volume": vols}],
                    "adjclose": [{"adjclose": adj}],
                },
            }],
            "error": None,
        }
    }


def make_yahoo_spark(symbols: list[str], *, price: float = 20.0,
                     volume: int = 1_000_000) -> dict:
    """构造 Yahoo v8 spark 报价响应。"""
    results = []
    for i, sym in enumerate(symbols):
        p = round(price + i * 0.7, 2)
        prev = round(p - 0.5, 2)
        results.append({
            "symbol": sym,
            "response": [{
                "meta": {
                    "regularMarketPrice": p,
                    "chartPreviousClose": prev,
                    "regularMarketVolume": volume + i * 1000,
                },
                "timestamp": [int(REF.strftime("%s"))],
                "indicators": {"quote": [{"close": [p], "volume": [volume + i * 1000]}]},
            }],
        })
    return {"spark": {"result": results, "error": None}}


class FakeYahooHttp:
    """只服务 Yahoo 与监管公告接口；东财/未知 URL 一律 FetchError。

    这样能验证「东财全挂 -> 自动降级 Yahoo」的路径，而不是断言报错。
    """

    def __init__(self, *, fail_all: bool = False) -> None:
        self.fail_all = fail_all
        self.calls: list[str] = []

    def json(self, url, params=None, headers=None, strip_jsonp=False):
        params = params or {}
        self.calls.append(url)
        if self.fail_all:
            raise FetchError("模拟全挂")
        if "chart" in url:
            symbol = str(params.get("symbol") or "600519.SS")
            return make_yahoo_chart(symbol, days=250)
        if "spark" in url:
            symbols = str(params.get("symbols", "")).split(",")
            return make_yahoo_spark([s for s in symbols if s])
        if "quote" in url:
            raise FetchError("v7 quote 不可用，验证 spark 兜底路径")
        if "security/ann" in url:
            page = int(params.get("page_index", 1))
            return ANNOUNCEMENTS if page == 1 else {"data": {"list": []}}
        raise FetchError(f"未预期的请求：{url}")


# ---------------------------------------------------------------------------
class TestSymbolMapping(unittest.TestCase):
    def test_sh_stocks(self):
        self.assertEqual(to_yahoo_symbol("600519"), "600519.SS")
        self.assertEqual(to_yahoo_symbol("688981"), "688981.SS")

    def test_sz_stocks(self):
        self.assertEqual(to_yahoo_symbol("000001"), "000001.SZ")
        self.assertEqual(to_yahoo_symbol("300750"), "300750.SZ")
        self.assertEqual(to_yahoo_symbol("002594"), "002594.SZ")

    def test_invalid_codes(self):
        for bad in ("", "12345", "1234567", "abc", "x600519"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    to_yahoo_symbol(bad)

    def test_secid_roundtrip(self):
        self.assertEqual(to_secid("600519"), "1.600519")
        self.assertEqual(to_secid("300750"), "0.300750")
        self.assertEqual(secid_to_code("1.600519"), "600519")
        self.assertEqual(secid_to_code("600519"), "600519")

    def test_index_symbol_disambiguation(self):
        """1.000001 是上证指数(.SS)，0.000001 是平安银行(.SZ)，不能撞车。"""
        self.assertEqual(secid_to_yahoo_symbol("1.000001"), "000001.SS")
        self.assertEqual(secid_to_yahoo_symbol("0.000001"), "000001.SZ")
        self.assertEqual(secid_to_yahoo_symbol("0.399006"), "399006.SZ")
        self.assertEqual(secid_to_yahoo_symbol("1.000300"), "000300.SS")
        self.assertEqual(secid_to_yahoo_symbol("1.600519"), "600519.SS")
        self.assertEqual(secid_to_yahoo_symbol("600519"), "600519.SS")
        self.assertEqual(secid_to_yahoo_symbol("000300.SS"), "000300.SS")  # 幂等


class TestConceptDict(unittest.TestCase):
    def test_boards_nonempty(self):
        self.assertGreaterEqual(len(CONCEPTS), 50)
        self.assertGreaterEqual(len(INDUSTRIES), 10)

    def test_all_codes_are_6_digit(self):
        for name, members in BOARDS.items():
            for code, stock_name in members:
                with self.subTest(board=name, code=code):
                    self.assertRegex(code, r"^\d{6}$")
                    self.assertTrue(stock_name.strip(), f"{name} 有空名称")

    def test_no_duplicate_codes_in_one_board(self):
        for name, members in BOARDS.items():
            codes = [c for c, _ in members]
            self.assertEqual(len(codes), len(set(codes)), name)

    def test_core_universe_deduped_and_large(self):
        codes = [c for c, _ in CORE_UNIVERSE]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertGreaterEqual(len(CORE_UNIVERSE), 300)

    def test_board_kind(self):
        self.assertEqual(board_kind("银行"), "行业")
        self.assertEqual(board_kind("人形机器人"), "概念")


# ---------------------------------------------------------------------------
class TestYahooKline(unittest.TestCase):
    def setUp(self):
        self.provider = YahooProvider(FakeYahooHttp())

    def test_parse_chart_bars(self):
        bars = self.provider.kline("1.600519", limit=200)
        self.assertEqual(len(bars), 200)
        self.assertTrue(all(a.day < b.day for a, b in zip(bars, bars[1:])))
        self.assertIsInstance(bars[0], Bar)

    def test_adjusted_close_uses_adjclose_ratio(self):
        raw = make_yahoo_chart("600519.SS", days=30, seed=3)
        result = raw["chart"]["result"][0]
        q = result["indicators"]["quote"][0]
        adj = result["indicators"]["adjclose"][0]["adjclose"]
        from octopus.factor.providers import _parse_yahoo_chart

        bars = _parse_yahoo_chart(result)
        i = 5
        ratio = adj[i] / q["close"][i]
        self.assertAlmostEqual(bars[i].close, q["close"][i] * ratio)

    def test_volume_converted_to_lots(self):
        bars = self.provider.kline("0.300750", limit=30)
        # kline 返回 250 天序列的**最后** 30 根，按同一序列对齐索引
        raw = make_yahoo_chart("300750.SZ", days=250)["chart"]["result"][0][
            "indicators"]["quote"][0]["volume"]
        self.assertAlmostEqual(bars[10].volume, raw[250 - 30 + 10] / 100.0)

    def test_change_derived_from_close(self):
        bars = self.provider.kline("1.600519", limit=50)
        for i in range(1, len(bars)):
            expected = (bars[i].close / bars[i - 1].close - 1.0) * 100.0
            self.assertAlmostEqual(bars[i].change, expected)

    def test_drops_bad_rows(self):
        """时间/价格缺失的交易日整根丢弃 —— 时间校验原则。"""
        raw = make_yahoo_chart("600519.SS", days=10)
        result = raw["chart"]["result"][0]
        result["timestamp"][3] = None
        result["indicators"]["quote"][0]["close"][6] = None
        from octopus.factor.providers import _parse_yahoo_chart

        bars = _parse_yahoo_chart(result)
        self.assertEqual(len(bars), 8)
        for b in bars:
            self.assertIsNotNone(b.day)

    def test_empty_result_raises(self):
        class EmptyHttp:
            def json(self, url, params=None, headers=None, strip_jsonp=False):
                return {"chart": {"result": None, "error": {"description": "not found"}}}

        provider = YahooProvider(EmptyHttp())  # type: ignore[arg-type]
        with self.assertRaises(FetchError):
            provider.kline("1.600519")


class TestYahooBoards(unittest.TestCase):
    def setUp(self):
        self.provider = YahooProvider(FakeYahooHttp())

    def test_boards_from_dict(self):
        boards = self.provider.boards("concept")
        names = {b.name for b in boards}
        self.assertIn("人形机器人", names)
        self.assertTrue(all(b.change_derived for b in boards))
        industries = self.provider.boards("industry")
        self.assertIn("银行", {b.name for b in industries})

    def test_board_members_ranked_by_amount(self):
        board = Board(code="人形机器人", name="人形机器人", kind="概念")
        members = self.provider.board_members(board, top=4)
        self.assertEqual(len(members), 4)
        amounts = [q["f6"] or 0 for _, _, q in members]
        self.assertEqual(amounts, sorted(amounts, reverse=True))
        # 板块涨跌幅被成分股行情回填（推算）
        self.assertNotEqual(board.change, 0.0)
        self.assertTrue(board.leader)

    def test_member_secid_market_prefix(self):
        board = Board(code="人形机器人", name="人形机器人", kind="概念")
        members = self.provider.board_members(board, top=50)
        secids = {secid for secid, _, _ in members}
        for secid in secids:
            code = secid.split(".", 1)[1]
            prefix = secid.split(".", 1)[0]
            if code[0] in "69":
                self.assertEqual(prefix, "1")
            else:
                self.assertEqual(prefix, "0")

    def test_top_amount_fallback(self):
        members = self.provider.top_amount_stocks(top=5)
        self.assertEqual(len(members), 5)
        self.assertEqual({q["f14"] for _, _, q in members}, {m[1] for m in members})


# ---------------------------------------------------------------------------
class TestProviderChain(unittest.TestCase):
    def test_yahoo_only(self):
        market = MarketData(FakeYahooHttp(), source="yahoo")
        bars = market.kline("1.600519", limit=100)
        self.assertEqual(len(bars), 100)
        self.assertEqual(market.used, ["yahoo"])

    def test_eastmoney_only(self):
        from tests.test_factor_pipeline import FakeHttp as EastmoneyFakeHttp

        market = MarketData(EastmoneyFakeHttp(), source="eastmoney")
        bars = market.kline("1.000001", limit=120)
        self.assertEqual(len(bars), 120)
        self.assertEqual(market.used, ["eastmoney"])

    def test_auto_falls_back_to_yahoo(self):
        """东财全挂（FakeYahooHttp 对东财 URL 抛 FetchError）-> 自动用 Yahoo。"""
        http = FakeYahooHttp()
        market = MarketData(http, source="auto")
        board, candidates = market.match_board("人形机器人")
        self.assertIsNotNone(board)
        self.assertEqual(board.name, "人形机器人")
        members = market.board_members(board, top=3)
        self.assertEqual(len(members), 3)
        self.assertIn("yahoo", market.used)
        # 东财先被尝试过（clist 请求确实发出去了），失败后才降级
        self.assertTrue(any("clist" in url for url in http.calls), http.calls)

    def test_all_sources_dead_raises(self):
        market = MarketData(FakeYahooHttp(fail_all=True), source="auto")
        with self.assertRaises(FetchError):
            market.kline("1.600519")

    def test_unknown_source_rejected(self):
        with self.assertRaises(ValueError):
            MarketData(FakeYahooHttp(), source="sina")


# ---------------------------------------------------------------------------
class TestYahooThemePipeline(unittest.TestCase):
    """yahoo 模式下整条主题分析链路（离线 fixture 驱动）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_analysis(self, topic="人形机器人"):
        p = ThemePipeline(
            FakeYahooHttp(), base_dir=self.tmp, stock_top=3,
            kline_limit=120, market_source="yahoo",
        )
        return p.run(topic, ref=REF, use_ai=False)

    def test_full_run_with_yahoo(self):
        analysis = self.run_analysis()
        self.assertEqual(analysis.market.board.name, "人形机器人")
        self.assertTrue(analysis.market.board.change_derived)
        self.assertTrue(analysis.profiles)
        self.assertTrue(analysis.benchmark_profiles)   # 指数也能从 Yahoo 取
        self.assertTrue(analysis.ai_report)

    def test_report_discloses_source_and_derived(self):
        analysis = self.run_analysis()
        joined = " ".join(analysis.notes)
        self.assertIn("行情数据源", joined)
        self.assertIn("Yahoo", joined)
        self.assertIn("推算", analysis.market.universe_note)
        self.assertIn("推算", analysis.ai_report or "")

    def test_unmatched_topic_degrades(self):
        analysis = self.run_analysis("完全不相干xyz")
        self.assertIsNone(analysis.market.board)
        self.assertIn("未匹配", analysis.market.universe_note)
        self.assertTrue(analysis.profiles)

    def test_factors_computed(self):
        analysis = self.run_analysis()
        inst = analysis.market.stocks[0]
        self.assertTrue(inst.factors)
        real = [v for v in inst.factors.values() if v is not None]
        self.assertGreater(len(real) / len(inst.factors), 0.9)


if __name__ == "__main__":
    unittest.main()
