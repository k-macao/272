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
from octopus.timeutil import CN_TZ, in_quiet_hours

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

    def test_quiet_hours_pause_entire_round(self):
        """免打扰时段（23:00 后）：不抓、不推、不记账，CI 视角视为成功（total=0）。"""
        REGISTRY["a"] = make_source("a", "源A", ["夜间新闻"])
        cfg = self._config(quiet_start="23:00", quiet_end="07:00")
        agent = self._agent(cfg)

        report = agent.run_once(ref=REF.replace(hour=23, minute=30))

        self.assertEqual(report.skipped, "quiet")
        self.assertEqual(report.total, 0)             # 上层据此退出码为 0
        self.assertFalse(report.pushed)
        self.assertEqual(len(report.results), 0)      # 根本没抓
        self.assertEqual(len(report.groups), 0)
        self.assertEqual(len(RecordingPush.sent), 0)  # 根本没推

        # 次日 07:00 起床后自动恢复正常
        morning = REF.replace(hour=7, minute=0) + timedelta(days=1)
        resumed = agent.run_once(ref=morning)
        self.assertEqual(resumed.skipped, "")
        self.assertEqual(len(resumed.results), 1)     # 正常抓取

    def test_quiet_hours_boundary_points(self):
        """22:59 照常，23:00 整暂停，隔日 06:59 仍暂停，07:00 整恢复。"""
        REGISTRY["a"] = make_source("a", "源A", ["新闻"])
        agent = self._agent(self._config(quiet_start="23:00", quiet_end="07:00"))

        def at(hour, minute, day_offset=0):
            return REF.replace(hour=hour, minute=minute) + timedelta(days=day_offset)

        self.assertEqual(agent.run_once(ref=at(22, 59)).skipped, "")
        self.assertEqual(agent.run_once(ref=at(23, 0)).skipped, "quiet")
        self.assertEqual(agent.run_once(ref=at(0, 30, day_offset=1)).skipped, "quiet")
        self.assertEqual(agent.run_once(ref=at(6, 59, day_offset=1)).skipped, "quiet")
        self.assertEqual(agent.run_once(ref=at(7, 0, day_offset=1)).skipped, "")

    def test_quiet_hours_disabled_by_empty_config(self):
        """两端留空即关闭免打扰——深夜照常跑。"""
        REGISTRY["a"] = make_source("a", "源A", ["夜间新闻"])
        cfg = self._config(quiet_start="", quiet_end="")
        report = self._agent(cfg).run_once(ref=REF.replace(hour=23, minute=30))
        self.assertEqual(report.skipped, "")
        self.assertEqual(len(report.results), 1)

    def test_quiet_hours_dry_run_still_previews(self):
        """dry-run 是人工主动预览，不受免打扰限制。"""
        REGISTRY["a"] = make_source("a", "源A", [])
        cfg = self._config(quiet_start="23:00", quiet_end="07:00")
        report = self._agent(cfg).run_once(ref=REF.replace(hour=23, minute=30), dry_run=True)
        self.assertEqual(report.skipped, "")
        self.assertEqual(len(report.results), 1)  # 照常抓取
        self.assertTrue(report.html)              # 照常生成预览正文


class TestQuietConfigChain(unittest.TestCase):
    """config.yml 里的免打扰配置（引号可省）到实际判定要整条链走通。"""

    def test_quoted_and_unquoted_clock_values(self):
        night = REF.replace(hour=23, minute=30)
        for literal in ("quiet_start: 23:00", 'quiet_start: "23:00"'):
            with self.subTest(literal=literal), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "config.yml"
                path.write_text(f"{literal}\nquiet_end: 07:00\n", encoding="utf-8")
                cfg = Config.load(path)
                self.assertTrue(
                    in_quiet_hours(cfg.quiet_start, cfg.quiet_end, ref=night), literal)
                self.assertFalse(
                    in_quiet_hours(cfg.quiet_start, cfg.quiet_end, ref=REF), literal)

    def test_quiet_hours_env_override(self):
        import os
        from unittest import mock

        env = {"OCTOPUS_QUIET_START": "22:30", "OCTOPUS_QUIET_END": "06:45"}
        with mock.patch.dict(os.environ, env):
            cfg = Config.load(Path("/nonexistent/config.yml"))
        ref = REF.replace(hour=23, minute=0)
        self.assertTrue(in_quiet_hours(cfg.quiet_start, cfg.quiet_end, ref=ref))
        edge = REF.replace(hour=6, minute=50)
        self.assertFalse(in_quiet_hours(cfg.quiet_start, cfg.quiet_end, ref=edge))


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
