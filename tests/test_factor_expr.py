"""qlib 因子表达式引擎：算子语义、缺失值传染、安全白名单（全程离线）。"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from octopus.factor.expr import ExprError, FactorEngine, Vec, compile_expr

CLOSE = [10.0, 10.5, 10.2, 11.0, 11.5, 11.2, 12.0, 12.4, 12.1, 12.8]
OPEN = [9.8, 10.4, 10.4, 10.3, 11.4, 11.4, 11.3, 12.3, 12.3, 12.2]
HIGH = [10.2, 10.7, 10.6, 11.1, 11.7, 11.5, 12.1, 12.6, 12.5, 12.9]
LOW = [9.7, 10.2, 10.1, 10.2, 11.2, 11.1, 11.2, 12.1, 12.0, 12.1]
VOLUME = [1000.0, 1200.0, 900.0, 1500.0, 1800.0, 1100.0, 2000.0, 2200.0, 1300.0, 2500.0]


def engine() -> FactorEngine:
    return FactorEngine(
        {"close": CLOSE, "open": OPEN, "high": HIGH, "low": LOW, "volume": VOLUME}
    )


class TestBasicOps(unittest.TestCase):
    def test_arithmetic_on_fields(self):
        # 最后一根：(12.8-12.2)/12.2
        self.assertAlmostEqual(
            engine().evaluate_last("($close-$open)/$open"), (12.8 - 12.2) / 12.2
        )

    def test_ref_shifts_series(self):
        got = engine().evaluate("Ref($close, 1)").data
        self.assertIsNone(got[0])          # 首位无前值，必须是 None 而不是 0
        self.assertEqual(got[1], 10.0)
        self.assertEqual(got[-1], 12.1)

    def test_ref_zero_is_identity(self):
        self.assertEqual(engine().evaluate("Ref($close, 0)").data, CLOSE)

    def test_ref_beyond_length_is_all_none(self):
        got = engine().evaluate("Ref($close, 99)").data
        self.assertTrue(all(v is None for v in got))

    def test_mean_matches_manual(self):
        self.assertAlmostEqual(engine().evaluate_last("Mean($close, 3)"), sum(CLOSE[-3:]) / 3)

    def test_std_uses_sample_ddof1(self):
        chunk = CLOSE[-4:]
        mu = sum(chunk) / 4
        expected = math.sqrt(sum((v - mu) ** 2 for v in chunk) / 3)  # pandas 默认 ddof=1
        self.assertAlmostEqual(engine().evaluate_last("Std($close, 4)"), expected)

    def test_max_min_over_window(self):
        self.assertEqual(engine().evaluate_last("Max($high, 5)"), max(HIGH[-5:]))
        self.assertEqual(engine().evaluate_last("Min($low, 5)"), min(LOW[-5:]))

    def test_sum_and_abs(self):
        self.assertAlmostEqual(engine().evaluate_last("Sum($close, 3)"), sum(CLOSE[-3:]))
        self.assertAlmostEqual(engine().evaluate_last("Abs(0-$close)"), 12.8)

    def test_greater_less_are_elementwise_max_min(self):
        self.assertEqual(engine().evaluate_last("Greater($open, $close)"), 12.8)
        self.assertEqual(engine().evaluate_last("Less($open, $close)"), 12.2)

    def test_log_of_nonpositive_is_none(self):
        got = FactorEngine({"close": [1.0, 0.0, -1.0]}).evaluate("Log($close)").data
        self.assertAlmostEqual(got[0], 0.0)
        self.assertIsNone(got[1])
        self.assertIsNone(got[2])

    def test_comparison_yields_one_zero(self):
        got = engine().evaluate("$close>Ref($close, 1)").data
        self.assertIsNone(got[0])
        self.assertEqual(got[1], 1.0)      # 10.5 > 10.0
        self.assertEqual(got[2], 0.0)      # 10.2 < 10.5

    def test_mean_of_comparison_is_up_ratio(self):
        # CNTP 的语义：过去 N 日收阳比例
        value = engine().evaluate_last("Mean($close>Ref($close, 1), 4)")
        ups = sum(1 for a, b in zip(CLOSE[-4:], CLOSE[-5:-1]) if a > b)
        self.assertAlmostEqual(value, ups / 4)


class TestRollingStats(unittest.TestCase):
    def test_quantile_linear_interpolation(self):
        # 与 pandas rolling().quantile() 的线性插值一致
        value = FactorEngine({"close": [1.0, 2.0, 3.0, 4.0, 5.0]}).evaluate_last(
            "Quantile($close, 5, 0.8)"
        )
        self.assertAlmostEqual(value, 4.2)

    def test_rank_is_percentile(self):
        value = FactorEngine({"close": [1.0, 2.0, 3.0, 4.0, 10.0]}).evaluate_last(
            "Rank($close, 5)"
        )
        self.assertAlmostEqual(value, 1.0)   # 最新值最大 -> 100% 分位

    def test_idxmax_is_one_based(self):
        value = FactorEngine({"high": [1.0, 9.0, 2.0, 3.0]}).evaluate_last("IdxMax($high, 4)")
        self.assertEqual(value, 2.0)

    def test_slope_of_linear_series(self):
        value = FactorEngine({"close": [1.0, 2.0, 3.0, 4.0, 5.0]}).evaluate_last(
            "Slope($close, 5)"
        )
        self.assertAlmostEqual(value, 1.0)

    def test_rsquare_of_perfect_line_is_one(self):
        value = FactorEngine({"close": [1.0, 2.0, 3.0, 4.0, 5.0]}).evaluate_last(
            "Rsquare($close, 5)"
        )
        self.assertAlmostEqual(value, 1.0)

    def test_resi_of_perfect_line_is_zero(self):
        value = FactorEngine({"close": [1.0, 2.0, 3.0, 4.0, 5.0]}).evaluate_last(
            "Resi($close, 5)"
        )
        self.assertAlmostEqual(value, 0.0, places=9)

    def test_corr_of_identical_series_is_one(self):
        value = FactorEngine(
            {"close": [1.0, 3.0, 2.0, 5.0, 4.0], "volume": [1.0, 3.0, 2.0, 5.0, 4.0]}
        ).evaluate_last("Corr($close, $volume, 5)")
        self.assertAlmostEqual(value, 1.0)

    def test_corr_of_constant_series_is_none(self):
        """常数序列相关系数无定义 —— 必须返回 None，不能拿 0 冒充。"""
        value = FactorEngine(
            {"close": [2.0, 2.0, 2.0, 2.0], "volume": [1.0, 2.0, 3.0, 4.0]}
        ).evaluate_last("Corr($close, $volume, 4)")
        self.assertIsNone(value)

    def test_ema_matches_recursive_definition(self):
        data = [1.0, 2.0, 3.0]
        alpha = 2 / (3 + 1)
        expected = data[0]
        for value in data[1:]:
            expected = alpha * value + (1 - alpha) * expected
        self.assertAlmostEqual(
            FactorEngine({"close": data}).evaluate_last("EMA($close, 3)"), expected
        )


class TestMissingValues(unittest.TestCase):
    def test_insufficient_window_returns_none(self):
        """窗口比数据长时必须是 None —— 宁可缺失，不可臆造。"""
        self.assertIsNone(FactorEngine({"close": [1.0, 2.0]}).evaluate_last("Mean($close, 10)"))

    def test_none_is_contagious(self):
        got = FactorEngine({"close": [1.0, None, 3.0]}).evaluate("$close*2").data
        self.assertEqual(got[0], 2.0)
        self.assertIsNone(got[1])

    def test_window_containing_none_is_none(self):
        value = FactorEngine({"close": [1.0, None, 3.0]}).evaluate_last("Mean($close, 3)")
        self.assertIsNone(value)

    def test_division_by_zero_is_none_not_crash(self):
        value = FactorEngine({"close": [1.0], "open": [0.0]}).evaluate_last("$close/$open")
        self.assertIsNone(value)

    def test_nan_inf_are_normalised_to_none(self):
        # 1e308*10 会溢出成 inf，必须被收敛成 None
        value = FactorEngine({"close": [1e308, 1e308]}).evaluate_last("$close*$close")
        self.assertIsNone(value)


class TestAlpha158Expressions(unittest.TestCase):
    """真实 Alpha158 表达式必须能跑通且给出有限值。"""

    EXPRS = (
        "($close-$open)/$open",
        "($high-$low)/$open",
        "($close-$open)/($high-$low+1e-12)",
        "($high-Greater($open, $close))/$open",
        "(Less($open, $close)-$low)/($high-$low+1e-12)",
        "(2*$close-$high-$low)/$open",
        "Ref($close, 5)/$close",
        "Mean($close, 5)/$close",
        "Std($close, 5)/$close",
        "Slope($close, 5)/$close",
        "Rsquare($close, 5)",
        "Resi($close, 5)/$close",
        "Max($high, 5)/$close",
        "Quantile($close, 5, 0.8)/$close",
        "Rank($close, 5)",
        "($close-Min($low, 5))/(Max($high, 5)-Min($low, 5)+1e-12)",
        "IdxMax($high, 5)/5",
        "(IdxMax($high, 5)-IdxMin($low, 5))/5",
        "Corr($close, Log($volume+1), 5)",
        "Corr($close/Ref($close,1), Log($volume/Ref($volume, 1)+1), 5)",
        "Mean($close>Ref($close, 1), 5)-Mean($close<Ref($close, 1), 5)",
        "Sum(Greater($close-Ref($close, 1), 0), 5)/(Sum(Abs($close-Ref($close, 1)), 5)+1e-12)",
        "Mean($volume, 5)/($volume+1e-12)",
        "Std($volume, 5)/($volume+1e-12)",
        "Std(Abs($close/Ref($close, 1)-1)*$volume, 5)"
        "/(Mean(Abs($close/Ref($close, 1)-1)*$volume, 5)+1e-12)",
        "(Sum(Greater($volume-Ref($volume, 1), 0), 5)"
        "-Sum(Greater(Ref($volume, 1)-$volume, 0), 5))"
        "/(Sum(Abs($volume-Ref($volume, 1)), 5)+1e-12)",
    )

    def test_all_expressions_evaluate_to_finite(self):
        eng = engine()
        for expr in self.EXPRS:
            with self.subTest(expr=expr):
                value = eng.evaluate_last(expr)
                self.assertIsNotNone(value, f"{expr} 求值为 None")
                self.assertTrue(math.isfinite(value), f"{expr} 求值非有限：{value}")


class TestSecurity(unittest.TestCase):
    """表达式来自 GitHub，按不可信输入处理。"""

    BAD = (
        "__import__('os').system('id')",
        "$close.__class__.__mro__",
        "open('/etc/passwd').read()",
        "[x for x in range(10)]",
        "lambda: 1",
        "Foo($close, 3)",
        "eval('1+1')",
        "$close if True else $open",
        "{'a': 1}",
        "Mean($close, n=3)",
    )

    def test_dangerous_expressions_are_rejected(self):
        eng = engine()
        for expr in self.BAD:
            with self.subTest(expr=expr):
                with self.assertRaises(ExprError):
                    eng.evaluate(expr)

    def test_unknown_variable_rejected(self):
        with self.assertRaises(ExprError):
            compile_expr("$close + secret")

    def test_evaluate_last_swallows_errors(self):
        """evaluate_last 用于批量求值，坏表达式返回 None 而不是炸掉整轮。"""
        self.assertIsNone(engine().evaluate_last("Foo($close, 1)"))

    def test_syntax_error_rejected(self):
        with self.assertRaises(ExprError):
            compile_expr("$close +")


class TestEngineContract(unittest.TestCase):
    def test_mismatched_field_lengths_rejected(self):
        with self.assertRaises(ExprError):
            FactorEngine({"close": [1.0, 2.0], "open": [1.0]})

    def test_empty_fields_rejected(self):
        with self.assertRaises(ExprError):
            FactorEngine({})

    def test_evaluate_many_returns_named_dict(self):
        got = engine().evaluate_many([("A", "$close"), ("B", "Foo($close,1)")])
        self.assertAlmostEqual(got["A"], 12.8)
        self.assertIsNone(got["B"])

    def test_scalar_expression_broadcasts(self):
        self.assertAlmostEqual(engine().evaluate_last("1+1"), 2.0)

    def test_vec_length_mismatch_raises(self):
        with self.assertRaises(ExprError):
            Vec([1.0, 2.0]) + Vec([1.0])


if __name__ == "__main__":
    unittest.main()
