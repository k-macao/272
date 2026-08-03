"""手动主题分析推送：一对一语义、渲染、Agent 推送、网页服务与 CLI 入口（全程离线）。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import octopus.agent as agent_mod
from octopus.agent import Agent
from octopus.config import Config
from octopus.render import render_manual, render_manual_title
from octopus.timeutil import CN_TZ

REF = datetime(2026, 7, 27, 10, 30, 0, tzinfo=CN_TZ)


class RecordingPush:
    """替换真实 PushPlus：记录构造参数与调用，永远返回成功。"""

    sent: list[tuple[str, str]] = []
    instances: list[tuple] = []

    def __init__(self, *args, **kwargs):
        RecordingPush.instances.append((args, kwargs))

    def send(self, title, content, dry_run=False):
        RecordingPush.sent.append((title, content))
        return True


class TestRenderManual(unittest.TestCase):
    def test_renders_topic_and_content(self):
        html = render_manual(
            "机器人板块分析",
            "今日机器人板块放量上涨。\n\n关注减速器与执行器方向。",
            ref=REF,
        )
        self.assertIn("章鱼 AI · 主题分析", html)
        self.assertIn("▍机器人板块分析", html)
        self.assertIn("今日机器人板块放量上涨。<br><br>关注减速器与执行器方向。", html)
        self.assertIn("发布时间 2026-07-27 10:30", html)
        self.assertIn("人工录入", html)

    def test_escapes_html(self):
        html = render_manual("测试", "<script>alert(1)</script>\n&<>", ref=REF)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&amp;&lt;&gt;", html)

    def test_no_topic_falls_back(self):
        html = render_manual("", "纯内容文本", ref=REF)
        self.assertIn("AI 分析内容", html)
        self.assertIn("纯内容文本", html)


class TestRenderManualTitle(unittest.TestCase):
    def test_title_with_topic(self):
        t = render_manual_title("机器人板块分析", REF)
        self.assertIn("章鱼AI 07-27 10:30", t)
        self.assertIn("AI分析", t)
        self.assertIn("机器人板块分析", t)

    def test_title_without_topic(self):
        self.assertTrue(render_manual_title("", REF).endswith("主题"))

    def test_title_truncates_long_topic(self):
        t = render_manual_title("很" * 30, REF)
        self.assertIn("…", t)
        self.assertLess(len(t), len("章鱼AI 07-27 10:30 · AI分析 · ") + 30)


class AgentPushTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self._push_backup = agent_mod.PushPlus
        agent_mod.PushPlus = RecordingPush
        RecordingPush.sent = []
        RecordingPush.instances = []

    def tearDown(self):
        agent_mod.PushPlus = self._push_backup
        self.tmp.cleanup()

    def _agent(self, **overrides):
        cfg = Config(
            window_minutes=180,
            max_items_total=0,
            state_file="state/seen.json",
            pushplus_token="test-token",
        )
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return Agent(cfg, base_dir=self.base)


class TestAgentManualPush(AgentPushTestCase):
    def test_push_manual_sends_once(self):
        agent = self._agent()
        report = agent.push_manual("机器人板块", "第一段。\n\n第二段。", ref=REF)

        self.assertTrue(report.pushed)
        self.assertEqual(report.total, 1)
        self.assertEqual(report.groups, [])
        self.assertEqual(len(RecordingPush.sent), 1)
        title, html = RecordingPush.sent[0]
        self.assertIn("机器人板块", title)
        self.assertIn("第一段。", html)

    def test_manual_push_is_one_to_one_ignores_topics(self):
        """一对一：即使配置了群组 topic，手动推送也不携带 topic。"""
        agent = self._agent(pushplus_topics=["oai.1", "group2"])
        agent.push_manual("主题", "内容", ref=REF)

        self.assertEqual(len(RecordingPush.instances), 1)
        _, kwargs = RecordingPush.instances[0]
        self.assertEqual(kwargs["topics"], [])

    def test_push_manual_dry_run_no_real_send(self):
        agent = self._agent()
        report = agent.push_manual("主题", "内容", dry_run=True, ref=REF)
        self.assertTrue(report.pushed)
        self.assertEqual(len(RecordingPush.sent), 1)


class TestManualWeb(unittest.TestCase):
    """独立网页服务：页面、预览、一对一推送、令牌保护。"""

    def setUp(self):
        import octopus.webui as webui_mod

        self.webui = webui_mod
        self._push_backup = agent_mod.PushPlus
        agent_mod.PushPlus = RecordingPush
        RecordingPush.sent = []
        RecordingPush.instances = []

        cfg = Config(
            window_minutes=180,
            max_items_total=0,
            state_file="state/seen.json",
            pushplus_token="test-token",
        )
        agent = Agent(cfg, base_dir=Path(tempfile.mkdtemp()))
        handler = type("TestBoundHandler", (webui_mod.ManualWebHandler,), {"agent": agent})
        self.httpd = webui_mod.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        agent_mod.PushPlus = self._push_backup

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_page_served(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as resp:
            html = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("章鱼 AI · 手动主题推送", html)
        self.assertIn("一对一推送", html)
        self.assertIn("var AUTH = false;", html)  # 未配置令牌时 auth 标记为 false

    def test_preview_endpoint(self):
        code, data = self._post("/preview", {"topic": "主题", "content": "内容<p>&"})
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        self.assertIn("内容&lt;p&gt;&amp;", data["html"])

    def test_push_endpoint_one_to_one(self):
        code, data = self._post("/push", {"topic": "主题", "content": "内容"})
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(len(RecordingPush.instances), 1)
        _, kwargs = RecordingPush.instances[0]
        self.assertEqual(kwargs["topics"], [])

    def test_push_endpoint_rejects_empty_content(self):
        code, data = self._post("/push", {"topic": "主题", "content": "  "})
        self.assertEqual(code, 400)
        self.assertFalse(data["ok"])


class TestManualWebToken(unittest.TestCase):
    def test_token_required_when_configured(self):
        import octopus.webui as webui_mod

        original = webui_mod.TOKEN
        webui_mod.TOKEN = "s3cret"
        agent_mod.PushPlus = RecordingPush
        RecordingPush.instances = []
        try:
            cfg = Config(state_file="state/seen.json", pushplus_token="t")
            agent = Agent(cfg, base_dir=Path(tempfile.mkdtemp()))
            handler = type("TestTokenHandler", (webui_mod.ManualWebHandler,), {"agent": agent})
            httpd = webui_mod.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                def post(payload):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/push",
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    try:
                        with urllib.request.urlopen(req) as resp:
                            return resp.status, json.loads(resp.read().decode("utf-8"))
                    except urllib.error.HTTPError as exc:
                        return exc.code, json.loads(exc.read().decode("utf-8"))

                code, data = post({"topic": "t", "content": "c"})
                self.assertEqual(code, 401)
                self.assertFalse(data["ok"])

                code, data = post({"topic": "t", "content": "c", "token": "s3cret"})
                self.assertEqual(code, 200)
                self.assertTrue(data["ok"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
        finally:
            webui_mod.TOKEN = original


class TestManualCli(unittest.TestCase):
    def test_cli_manual_content_writes_preview(self):
        import main as main_mod

        code = main_mod.main([
            "--manual", "--topic", "主题A", "--content", "内容B",
            "--dry-run", "--config", "/nonexistent/config.yml",
        ])
        self.assertEqual(code, 0)

        preview = main_mod.BASE_DIR / "preview.html"
        self.assertTrue(preview.exists())
        text = preview.read_text(encoding="utf-8")
        self.assertIn("主题A", text)
        self.assertIn("内容B", text)
        self.assertIn("章鱼 AI · 主题分析", text)

    def test_cli_manual_topic_implies_manual(self):
        import main as main_mod

        code = main_mod.main([
            "--topic", "只有主题", "--content", "内容",
            "--dry-run", "--config", "/nonexistent/config.yml",
        ])
        self.assertEqual(code, 0)

    def test_cli_manual_empty_content_rejected(self):
        import main as main_mod

        code = main_mod.main([
            "--manual", "--topic", "主题", "--content", "  ",
            "--dry-run", "--config", "/nonexistent/config.yml",
        ])
        self.assertEqual(code, 2)

    def test_cli_manual_reads_content_from_stdin(self):
        import io
        import main as main_mod

        old_stdin = sys.stdin
        sys.stdin = io.StringIO("来自标准输入的内容\n第二行")
        try:
            code = main_mod.main([
                "--manual", "--topic", "主题B",
                "--dry-run", "--config", "/nonexistent/config.yml",
            ])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(code, 0)
        text = (main_mod.BASE_DIR / "preview.html").read_text(encoding="utf-8")
        self.assertIn("来自标准输入的内容<br>第二行", text)


class TestManualDeepSeekAI(unittest.TestCase):
    def test_push_manual_with_deepseek_api(self):
        from unittest.mock import MagicMock
        from octopus.agent import Agent
        from octopus.config import Config

        cfg = Config(
            state_file="state/seen.json",
            pushplus_token="test-token",
            deepseek_api_key="deepseek-secret-key",
            deepseek_model="deepseek-v4-flash",
        )
        agent = Agent(cfg)
        agent.http = MagicMock()
        # mock http.post_json 为两种接口返回不同结果：
        # 1) DeepSeek 提炼 API -> {"choices": [{"message": {"content": "【核心提炼】：这是大模型的总结"}}]}
        # 2) PushPlus 推送 API -> {"code": 200, "msg": "ok"}
        def mock_post_json(url, payload, **kwargs):
            if "deepseek.com" in url:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": "【分类标签】：AI概念\n【核心结论】：看好发展"
                            }
                        }
                    ]
                }
            return {"code": 200, "msg": "ok"}

        agent.http.post_json.side_effect = mock_post_json

        report = agent.push_manual("AI测试", "一大段复杂的分析内容……")
        self.assertTrue(report.pushed)
        self.assertIn("✨ DeepSeek AI 智能提炼", report.html)
        self.assertIn("【分类标签】：AI概念", report.html)
        self.assertIn("一大段复杂的分析内容……", report.html)

    def test_preview_manual_with_deepseek_api(self):
        from unittest.mock import MagicMock
        from octopus.agent import Agent
        from octopus.config import Config

        cfg = Config(
            state_file="state/seen.json",
            deepseek_api_key="deepseek-secret-key",
            deepseek_model="deepseek-v4-flash",
        )
        agent = Agent(cfg)
        agent.http = MagicMock()
        agent.http.post_json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "【分类标签】：半导体\n【核心结论】：复苏预期强"
                    }
                }
            ]
        }
        html = agent.preview_manual("半导体行业", "研报正文：订单显著上升")
        self.assertIn("✨ DeepSeek AI 智能提炼", html)
        self.assertIn("【分类标签】：半导体", html)
        self.assertIn("研报正文：订单显著上升", html)


if __name__ == "__main__":
    unittest.main()
