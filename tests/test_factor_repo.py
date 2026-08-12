"""GitHub 因子模型拉取：源码解析、降级链、缓存、安全（全程离线）。"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from octopus.factor import qlib_repo
from octopus.factor.expr import FactorEngine
from octopus.factor.qlib_repo import (
    QlibFactorRepo,
    builtin_model,
    classify,
    describe,
    parse_alpha158,
)
from octopus.http import FetchError

# --- 一份**结构等同真实 loader.py** 的最小源码 ------------------------------
# 保留 Alpha158DL.get_feature_config 的全部语法特征：默认参数 config、
# 内部函数 use()、列表推导、% 格式化、字符串 .lower()/.upper()。
FAKE_LOADER = '''
from qlib.data.dataset.loader import QlibDataLoader


class Alpha360DL(QlibDataLoader):
    @staticmethod
    def get_feature_config():
        return ["$close"], ["CLOSE0"]


class Alpha158DL(QlibDataLoader):
    """Dataloader to get Alpha158"""

    @staticmethod
    def get_feature_config(
        config={
            "kbar": {},
            "price": {"windows": [0], "feature": ["OPEN", "HIGH"]},
            "rolling": {},
        }
    ):
        """create factors from config"""
        fields = []
        names = []
        if "kbar" in config:
            fields += ["($close-$open)/$open", "($high-$low)/$open"]
            names += ["KMID", "KLEN"]
        if "price" in config:
            windows = config["price"].get("windows", range(5))
            feature = config["price"].get("feature", ["OPEN", "HIGH"])
            for field in feature:
                field = field.lower()
                fields += ["Ref($%s, %d)/$close" % (field, d) if d != 0 else "$%s/$close" % field for d in windows]
                names += [field.upper() + str(d) for d in windows]
        if "rolling" in config:
            windows = config["rolling"].get("windows", [5, 10])
            include = config["rolling"].get("include", None)
            exclude = config["rolling"].get("exclude", [])

            def use(x):
                return x not in exclude and (include is None or x in include)

            if use("ROC"):
                fields += ["Ref($close, %d)/$close" % d for d in windows]
                names += ["ROC%d" % d for d in windows]
            if use("MA"):
                fields += ["Mean($close, %d)/$close" % d for d in windows]
                names += ["MA%d" % d for d in windows]
        return fields, names
'''


class TestParseAlpha158(unittest.TestCase):
    def test_parses_names_and_expressions(self):
        factors = parse_alpha158(FAKE_LOADER)
        table = {f.name: f.expr for f in factors}
        self.assertEqual(table["KMID"], "($close-$open)/$open")
        self.assertEqual(table["KLEN"], "($high-$low)/$open")
        self.assertEqual(table["ROC5"], "Ref($close, 5)/$close")
        self.assertEqual(table["MA10"], "Mean($close, 10)/$close")

    def test_handles_lower_upper_and_comprehensions(self):
        """price 分支用了 field.lower()/.upper() 与列表推导，必须能跑。"""
        names = {f.name for f in parse_alpha158(FAKE_LOADER)}
        self.assertIn("OPEN0", names)
        self.assertIn("HIGH0", names)

    def test_inner_use_function_not_broken_by_return_rewrite(self):
        """内部函数 use() 的 return 不能被改写，否则会提前捕获到 bool。"""
        factors = parse_alpha158(FAKE_LOADER)
        self.assertGreater(len(factors), 4)
        self.assertTrue(any(f.name.startswith("ROC") for f in factors))

    def test_config_override_forces_full_price_features(self):
        """解析时用的是我们自己的 config（含 LOW/VWAP），不是源码默认值。

        这保证无论 qlib 上游怎么改默认参数，我们取到的因子口径是稳定的。
        """
        names = {f.name for f in parse_alpha158(FAKE_LOADER)}
        self.assertIn("LOW0", names)
        self.assertIn("VWAP0", names)

    def test_expressions_are_evaluable(self):
        """解析出来的表达式必须能被本地引擎求值 —— 端到端契约。"""
        factors = parse_alpha158(FAKE_LOADER)
        eng = FactorEngine(
            {
                "close": [10.0 + i * 0.1 for i in range(30)],
                "open": [10.0 + i * 0.09 for i in range(30)],
                "high": [10.2 + i * 0.1 for i in range(30)],
                "low": [9.8 + i * 0.1 for i in range(30)],
                "volume": [1000.0 + i for i in range(30)],
                "vwap": [10.05 + i * 0.1 for i in range(30)],
            }
        )
        for factor in factors:
            with self.subTest(name=factor.name):
                self.assertIsNotNone(eng.evaluate_last(factor.expr))

    def test_missing_class_raises(self):
        with self.assertRaises(FetchError):
            parse_alpha158("class Other:\n    pass\n")

    def test_syntax_error_raises(self):
        with self.assertRaises(FetchError):
            parse_alpha158("def broken(:\n")

    def test_rejects_import_inside_function(self):
        """源码被篡改成含 import 时必须拒绝执行。"""
        evil = FAKE_LOADER.replace(
            "        fields = []", "        import os\n        fields = []"
        )
        with self.assertRaises(FetchError):
            parse_alpha158(evil)

    def test_rejects_dunder_attribute(self):
        evil = FAKE_LOADER.replace(
            "        fields = []", "        fields = [].__class__()\n"
        )
        with self.assertRaises(FetchError):
            parse_alpha158(evil)

    def test_mismatched_lengths_raise(self):
        evil = FAKE_LOADER.replace('names += ["KMID", "KLEN"]', 'names += ["KMID"]')
        with self.assertRaises(FetchError):
            parse_alpha158(evil)


class TestClassification(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(classify("KMID"), "K线形态")
        self.assertEqual(classify("MA20"), "趋势动量")
        self.assertEqual(classify("STD20"), "波动与位置")
        self.assertEqual(classify("CORR20"), "量价配合")
        self.assertEqual(classify("VMA5"), "量能结构")
        self.assertEqual(classify("SUMP20"), "涨跌强弱")

    def test_description_injects_window(self):
        self.assertIn("20 日", describe("MA20"))
        self.assertTrue(describe("KMID"))

    def test_unknown_name_is_tolerated(self):
        self.assertEqual(classify("ZZZ9"), "其他")
        self.assertEqual(describe("ZZZ9"), "")


class FakeHttp:
    """可编排的假 Http：按 URL 关键字返回预设值或抛错。"""

    def __init__(self, *, api=None, raw=None, commits=None, fail_api=False, fail_raw=False):
        self.api = api
        self.raw = raw
        self.commits = commits if commits is not None else []
        self.fail_api = fail_api
        self.fail_raw = fail_raw
        self.calls: list[str] = []

    def json(self, url, params=None, headers=None, strip_jsonp=False):
        self.calls.append(url)
        if "commits" in url:
            return self.commits
        if self.fail_api:
            raise FetchError("api down")
        return self.api

    def text(self, url, params=None, headers=None, encoding=None):
        self.calls.append(url)
        if self.fail_raw:
            raise FetchError("raw down")
        return self.raw


def _api_payload(source: str) -> dict:
    return {
        "sha": "blobsha123",
        "content": base64.b64encode(source.encode("utf-8")).decode("ascii"),
        "encoding": "base64",
    }


COMMITS = [
    {
        "sha": "a7d5a9b500de5df053e32abf00f6a679546636eb",
        "commit": {"committer": {"date": "2024-07-05T07:44:16Z"}},
    }
]


class TestRepoLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_loads_from_github_api(self):
        http = FakeHttp(api=_api_payload(FAKE_LOADER), commits=COMMITS)
        model = QlibFactorRepo(http, cache_dir=self.cache).load()

        self.assertEqual(model.source, "github-api")
        self.assertFalse(model.degraded)
        self.assertTrue(model.factors)
        self.assertTrue(model.commit_sha.startswith("a7d5a9b"))
        self.assertIn("a7d5a9b", model.provenance)
        self.assertIn("GitHub API", model.provenance)

    def test_commit_time_converted_to_beijing(self):
        """GitHub 给 UTC，报告展示北京时间 —— 07:44 UTC = 15:44 北京。"""
        http = FakeHttp(api=_api_payload(FAKE_LOADER), commits=COMMITS)
        model = QlibFactorRepo(http, cache_dir=self.cache).load()
        self.assertIsNotNone(model.commit_time)
        self.assertEqual(model.commit_time.hour, 15)

    def test_falls_back_to_raw_when_api_fails(self):
        http = FakeHttp(fail_api=True, raw=FAKE_LOADER)
        model = QlibFactorRepo(http, cache_dir=self.cache).load()

        self.assertEqual(model.source, "github-raw")
        self.assertTrue(model.degraded)
        self.assertTrue(model.factors)

    def test_falls_back_to_cache_when_network_dead(self):
        # 先成功一次写入缓存
        good = FakeHttp(api=_api_payload(FAKE_LOADER), commits=COMMITS)
        QlibFactorRepo(good, cache_dir=self.cache).load()
        self.assertTrue((self.cache / "qlib_alpha158.json").exists())

        dead = FakeHttp(fail_api=True, fail_raw=True)
        model = QlibFactorRepo(dead, cache_dir=self.cache).load()

        self.assertEqual(model.source, "cache")
        self.assertIn("缓存", model.degraded)
        self.assertTrue(model.factors)

    def test_falls_back_to_builtin_when_all_dead(self):
        dead = FakeHttp(fail_api=True, fail_raw=True)
        model = QlibFactorRepo(dead, cache_dir=self.cache).load()

        self.assertEqual(model.source, "builtin")
        self.assertIn("内置", model.degraded)
        self.assertEqual(len(model.factors), 158)

    def test_degradation_is_always_disclosed(self):
        """降级必须如实标注，不能假装数据来自 GitHub。"""
        dead = FakeHttp(fail_api=True, fail_raw=True)
        model = QlibFactorRepo(dead, cache_dir=None).load()
        self.assertTrue(model.degraded)
        self.assertIn("内置快照", model.provenance)
        self.assertNotIn("实时拉取", model.provenance)

    def test_corrupt_cache_is_ignored(self):
        (self.cache / "qlib_alpha158.json").write_text("{not json", encoding="utf-8")
        dead = FakeHttp(fail_api=True, fail_raw=True)
        model = QlibFactorRepo(dead, cache_dir=self.cache).load()
        self.assertEqual(model.source, "builtin")

    def test_bad_source_falls_through_to_builtin(self):
        """GitHub 返回了内容但解析不出因子时，也要降级而不是抛错。"""
        http = FakeHttp(api=_api_payload("class Nope: pass"), raw="also nope")
        model = QlibFactorRepo(http, cache_dir=None).load()
        self.assertEqual(model.source, "builtin")

    def test_token_is_sent_as_bearer(self):
        captured = {}

        class TokenHttp(FakeHttp):
            def json(self, url, params=None, headers=None, strip_jsonp=False):
                captured.update(headers or {})
                return super().json(url, params=params, headers=headers)

        http = TokenHttp(api=_api_payload(FAKE_LOADER), commits=COMMITS)
        QlibFactorRepo(http, cache_dir=None, token="ghp_secret").load()
        self.assertEqual(captured.get("Authorization"), "Bearer ghp_secret")


class TestBuiltinModel(unittest.TestCase):
    def test_has_158_factors(self):
        self.assertEqual(len(builtin_model().factors), 158)

    def test_names_are_unique(self):
        names = [f.name for f in builtin_model().factors]
        self.assertEqual(len(names), len(set(names)))

    def test_matches_real_alpha158_shape(self):
        """内置快照必须覆盖 Alpha158 的全部因子族。"""
        names = {f.name for f in builtin_model().factors}
        for expected in ("KMID", "KLEN", "ROC60", "MA60", "STD60", "CORR60",
                         "SUMD60", "VMA60", "WVMA60", "VSUMD60", "RSV30", "IMXD10"):
            self.assertIn(expected, names)

    def test_all_builtin_expressions_are_evaluable(self):
        eng = FactorEngine(
            {
                "close": [10.0 + (i % 7) * 0.3 + i * 0.05 for i in range(120)],
                "open": [10.0 + (i % 5) * 0.2 + i * 0.05 for i in range(120)],
                "high": [10.6 + (i % 7) * 0.3 + i * 0.05 for i in range(120)],
                "low": [9.4 + (i % 5) * 0.2 + i * 0.05 for i in range(120)],
                "volume": [1000.0 + (i % 11) * 60 for i in range(120)],
                "vwap": [10.1 + (i % 6) * 0.25 + i * 0.05 for i in range(120)],
            }
        )
        missing = [
            f.name for f in builtin_model().factors if eng.evaluate_last(f.expr) is None
        ]
        self.assertEqual(missing, [], f"这些内置因子算不出值：{missing[:10]}")


if __name__ == "__main__":
    unittest.main()
