"""端到端：多源并发 -> 时间校验 -> 去重 -> 渲染 -> 推送（全程离线）."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from octopus.agent import Agent
from octopus.config import Config
from octopus.models import Item, TimeQuality
from octopus.notify import PushPlus
from octopus.sources import REGISTRY
from octopus.sources.base import Source
from octopus.timeutil import CN_TZ

REF = datetime(2026, 7, 27, 10, 30, 0, tzinfo=CN_TZ)


class StubSource(Source):
    """产出可控条目的假源。"""

    name = "stub"
    label = "假源"
    titles: list[str] = []
    should_fail = False

    def collect(self):
        if self.should_fail:
            raise RuntimeError("模拟接口故障")
        return [
            Item(
                source=self.name,
                source_label=self.label,
                title=t,
                url=f"https://example.com/{self.name}/{i}",
                published_at=REF - timedelta(minutes=5 * (i + 1)),
                time_quality=TimeQuality.EXACT,
            )
            for i, t in enumerate(self.titles)
        ]


def make_source(source_name, label, titles, fail=False):
    return type(
        f"Stub_{source_name}",
        (StubSource,),
        {"name": source_name, "label": label, "titles": titles, "should_fail": fail},
    )


class RecordingPush:
    """替换掉真实 PushPlus，记录调用。"""

    sent: list[tuple[str, str]] = []

    def __init__(self, *args, **kwargs):
        pass

    def send(self, title, content, dry_run=False):
        RecordingPush.sent.append((title, content))
        return True


class FailingPush(RecordingPush):
    def send(self, title, content, dry_run=False):
        RecordingPush.sent.append((title, content))
        return False


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self._registry_backup = dict(REGISTRY)
        REGISTRY.clear()
        RecordingPush.sent = []

    def tearDown(self):
        REGISTRY.clear()
        REGISTRY.update(self._registry_backup)
        self.tmp.cleanup()

    def _config(self, **overrides):
        cfg = Config(
            window_minutes=180,
            max_items_per_source=5,
            max_items_total=50,
            state_file="state/seen.json",
            pushplus_token="test-token",
        )
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    def _agent(self, cfg, push_cls=RecordingPush):
        import octopus.agent as mod

        mod.PushPlus = push_cls
        return Agent(cfg, base_dir=self.base)


class TestAgentFlow(AgentTestCase):
    def test_collects_from_all_sources(self):
        REGISTRY["a"] = make_source("a", "源A", ["A新闻1", "A新闻2"])
        REGISTRY["b"] = make_source("b", "源B", ["B新闻1"])

        agent = self._agent(self._config())
        report = agent.run_once(ref=REF)

        self.assertEqual(report.total, 3)
        self.assertTrue(report.pushed)
        self.assertEqual(len(RecordingPush.sent), 1)

    def test_second_run_dedupes_everything(self):
        """同样的内容第二轮不能重复推送。"""
        REGISTRY["a"] = make_source("a", "源A", ["同一条新闻"])
        cfg = self._config()

        first = self._agent(cfg).run_once(ref=REF)
        self.assertEqual(first.total, 1)

        second = self._agent(cfg).run_once(ref=REF + timedelta(minutes=30))
        self.assertEqual(second.total, 0)

    def test_new_item_still_pushed_after_dedupe(self):
        REGISTRY["a"] = make_source("a", "源A", ["旧闻"])
        cfg = self._config()
        self._agent(cfg).run_once(ref=REF)

        REGISTRY["a"] = make_source("a", "源A", ["旧闻", "新料"])
        second = self._agent(cfg).run_once(ref=REF + timedelta(minutes=30))
        self.assertEqual(second.total, 1)
        self.assertEqual(second.groups[0][1][0].title, "新料")

    def test_failed_source_does_not_break_run(self):
        REGISTRY["ok"] = make_source("ok", "正常源", ["有效新闻"])
        REGISTRY["bad"] = make_source("bad", "故障源", [], fail=True)

        report = self._agent(self._config()).run_once(ref=REF)
        self.assertEqual(report.total, 1)
        self.assertTrue(report.pushed)
        self.assertEqual(len(report.failures), 1)
        self.assertEqual(report.failures[0].source_label, "故障源")
        self.assertIn("故障源", report.html)

    def test_all_sources_failing_still_pushes_status(self):
        REGISTRY["bad1"] = make_source("bad1", "故障源1", [], fail=True)
        REGISTRY["bad2"] = make_source("bad2", "故障源2", [], fail=True)

        report = self._agent(self._config(push_when_empty=True)).run_once(ref=REF)
        self.assertEqual(report.total, 0)
        self.assertTrue(report.pushed)
        self.assertIn("本轮无新增内容", report.html)

    def test_empty_silent_mode(self):
        REGISTRY["a"] = make_source("a", "源A", [])
        report = self._agent(self._config(push_when_empty=False)).run_once(ref=REF)
        self.assertFalse(report.pushed)
        self.assertEqual(len(RecordingPush.sent), 0)

    def test_empty_push_mode_sends_heartbeat(self):
        REGISTRY["a"] = make_source("a", "源A", [])
        report = self._agent(self._config(push_when_empty=True)).run_once(ref=REF)
        self.assertTrue(report.pushed)
        self.assertIn("本轮无新增", report.title)

    def test_failed_push_does_not_mark_items_seen(self):
        """推送失败时不能记账，否则内容永久丢失。"""
        REGISTRY["a"] = make_source("a", "源A", ["重要新闻"])
        cfg = self._config()

        first = self._agent(cfg, push_cls=FailingPush).run_once(ref=REF)
        self.assertEqual(first.total, 1)
        self.assertFalse(first.pushed)

        # 下一轮应该还能推出来
        second = self._agent(cfg, push_cls=RecordingPush).run_once(ref=REF + timedelta(minutes=30))
        self.assertEqual(second.total, 1)
        self.assertTrue(second.pushed)

    def test_global_cap_enforced(self):
        REGISTRY["a"] = make_source("a", "源A", [f"新闻{i}" for i in range(10)])
        REGISTRY["b"] = make_source("b", "源B", [f"消息{i}" for i in range(10)])

        cfg = self._config(max_items_per_source=10, max_items_total=12)
        report = self._agent(cfg).run_once(ref=REF)
        self.assertEqual(report.total, 12)

    def test_zero_caps_push_everything(self):
        """上限置 0 = 不限：一条推送包含全部抓取内容，一条不漏。"""
        REGISTRY["a"] = make_source("a", "源A", [f"新闻{i}" for i in range(15)])
        REGISTRY["b"] = make_source("b", "源B", [f"消息{i}" for i in range(12)])

        cfg = self._config(max_items_per_source=0, max_items_total=0)
        report = self._agent(cfg).run_once(ref=REF)

        self.assertEqual(report.total, 27)
        self.assertTrue(report.pushed)
        self.assertEqual(len(RecordingPush.sent), 1)  # 仍然只有一条推送
        titles = {i.title for _, items in report.groups for i in items}
        self.assertIn("新闻14", titles)
        self.assertIn("消息11", titles)

    def test_per_source_limit_zero_means_unlimited(self):
        """源级 limit=0 时不截断，全部通过时间校验的条目都保留。"""
        REGISTRY["a"] = make_source("a", "源A", [f"新闻{i}" for i in range(20)])

        cfg = self._config(max_items_per_source=3, max_items_total=0)
        cfg.sources = {"a": {"limit": 0}}  # 该源显式覆盖为不限
        report = self._agent(cfg).run_once(ref=REF)
        self.assertEqual(report.total, 20)

    def test_disabled_source_skipped(self):
        REGISTRY["a"] = make_source("a", "源A", ["A的新闻"])
        REGISTRY["b"] = make_source("b", "源B", ["B的新闻"])

        cfg = self._config(disabled_sources=["b"])
        report = self._agent(cfg).run_once(ref=REF)
        self.assertEqual(report.total, 1)
        self.assertNotIn("源B", report.html)

    def test_dry_run_does_not_push(self):
        REGISTRY["a"] = make_source("a", "源A", ["新闻"])
        import octopus.agent as mod

        mod.PushPlus = PushPlus  # 用真实类，但 dry_run 会短路掉网络
        agent = Agent(self._config(), base_dir=self.base)
        report = agent.run_once(ref=REF, dry_run=True)
        self.assertTrue(report.pushed)  # dry-run 视为成功
        self.assertIn("源A", report.html)

    def test_html_output_is_styled(self):
        REGISTRY["a"] = make_source("a", "源A", ["宁德时代回购"])
        report = self._agent(self._config()).run_once(ref=REF)
        self.assertIn("#eceff3", report.html)  # 浅灰底
        self.assertIn("#12305c", report.html)  # 深蓝字
        self.assertIn("宁德时代回购", report.html)

    def test_state_persisted_to_disk(self):
        REGISTRY["a"] = make_source("a", "源A", ["会被记住的新闻"])
        self._agent(self._config()).run_once(ref=REF)
        self.assertTrue((self.base / "state" / "seen.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestConfigParsing(unittest.TestCase):
    """回归：配置解析的两个真实 bug。"""

    def test_empty_inline_list_not_split_into_chars(self):
        """`disabled_sources: []` 曾被当成字符串拆成 ['[', ']']，
        导致日志出现「已禁用的源：[, ]」。"""
        from octopus.config import _read_simple_yaml

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.yml"
            p.write_text("disabled_sources: []\nwindow_minutes: 180\n", encoding="utf-8")
            self.assertEqual(_read_simple_yaml(p)["disabled_sources"], [])

    def test_inline_list_with_values(self):
        from octopus.config import _read_simple_yaml

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.yml"
            p.write_text("disabled_sources: [datayes, hibor]\n", encoding="utf-8")
            self.assertEqual(_read_simple_yaml(p)["disabled_sources"], ["datayes", "hibor"])

    def test_as_list_guards_against_string(self):
        from octopus.config import _as_list

        self.assertEqual(_as_list("datayes,hibor"), ["datayes", "hibor"])
        self.assertEqual(_as_list("[]"), ["[]"])   # 不再逐字符拆开
        self.assertEqual(_as_list(None), [])
        self.assertEqual(_as_list([]), [])

    def test_real_config_yml_loads_clean(self):
        from octopus.config import Config

        cfg = Config.load()
        self.assertEqual(cfg.disabled_sources, [])
        self.assertEqual(cfg.window_minutes, 180)


class TestMultiEndpointFailure(unittest.TestCase):
    """回归：多端点源在全部端点失败时必须报错，不能装成「本轮无新数据」。"""

    def _http(self, exc):
        class H:
            def text(self, url, **kw):
                raise exc
            def json(self, url, **kw):
                raise exc
        return H()

    def test_stats_raises_when_all_rss_down(self):
        from octopus.http import FetchError
        from octopus.sources.stats import StatsSource

        src = StatsSource(self._http(FetchError("网络不可达")), {})
        with self.assertRaises(FetchError):
            src.collect()
        result = src.run(window_minutes=180, seen=None, ref=REF)
        self.assertFalse(result.ok)  # 如实报失败

    def test_hibor_raises_when_all_pages_down(self):
        from octopus.http import FetchError
        from octopus.sources.research import HiborSource

        src = HiborSource(self._http(FetchError("网络不可达")), {})
        result = src.run(window_minutes=180, seen=None, ref=REF)
        self.assertFalse(result.ok)

    def test_partial_failure_still_returns_items(self):
        """只有一个端点挂掉时，另一个照常产出。"""
        from octopus.http import FetchError
        from octopus.sources.stats import StatsSource
        from tests.fixtures import samples

        class H:
            def text(self, url, **kw):
                if "sjjd" in url:
                    raise FetchError("解读频道挂了")
                return samples.STATS_RSS

        items = StatsSource(H(), {}).collect()
        self.assertTrue(items)
