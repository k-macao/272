"""测试 DeepSeek 大模型 API 提炼功能。"""

import unittest
from unittest.mock import MagicMock

from octopus.ai import SYSTEM_PROMPT, NEWS_ANALYSIS_PROMPT, DEEPSEEK_API_URL, DeepSeekAI
from octopus.http import FetchError


class TestDeepSeekAI(unittest.TestCase):
    def test_missing_api_key_returns_false(self):
        client = DeepSeekAI("")
        ok, msg = client.analyze("测试主题", "测试内容")
        self.assertFalse(ok)
        self.assertIn("未配置 DeepSeek API Key", msg)

    def test_empty_content_returns_false(self):
        client = DeepSeekAI("test-api-key")
        ok, msg = client.analyze("测试主题", "")
        self.assertFalse(ok)
        self.assertIn("输入内容为空", msg)

    def test_successful_analyze(self):
        http = MagicMock()
        http.post_json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "【主题分类】：行业景气\n【核心结论】：板块向上\n【关键信息】：1. 数据改善"
                    }
                }
            ]
        }
        client = DeepSeekAI("test-key.secret", model="deepseek-v4-flash", http=http)
        ok, res = client.analyze("AI算力", "半导体订单大幅增长……")

        self.assertTrue(ok)
        self.assertIn("【主题分类】：行业景气", res)
        self.assertIn("【核心结论】：板块向上", res)

        # 检查 post_json 参数
        http.post_json.assert_called_once()
        url, payload = http.post_json.call_args[0]
        headers = http.post_json.call_args[1]["headers"]

        self.assertEqual(url, DEEPSEEK_API_URL)
        self.assertEqual(headers["Authorization"], "Bearer test-key.secret")
        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertEqual(payload["messages"][0]["content"], SYSTEM_PROMPT)
        self.assertIn("AI算力", payload["messages"][1]["content"])

    def test_api_failure(self):
        http = MagicMock()
        http.post_json.side_effect = FetchError("请求超时")
        client = DeepSeekAI("test-key", http=http)

        ok, msg = client.analyze("主题", "内容")
        self.assertFalse(ok)
        self.assertIn("DeepSeek API 调用异常", msg)

    def test_analyze_news_success(self):
        http = MagicMock()
        http.post_json.return_value = {
            "choices": [{"message": {"content": "1. 宁德时代拟回购，证券时报报道一致。\n2. 嘉美包装封板，单一来源。"}}]
        }
        client = DeepSeekAI("sk-test", model="deepseek-v4-flash", http=http)
        ok, text = client.analyze_news(
            [
                (1, "巨潮资讯", "宁德时代拟回购400亿", "公司公告", ["证券时报（Google News）：宁德时代披露回购（09-07 10:12）"]),
                (2, "问财·同花顺", "嘉美包装封板", "", []),
            ]
        )
        self.assertTrue(ok)
        self.assertIn("宁德时代拟回购", text)
        url, payload = http.post_json.call_args[0]
        self.assertEqual(url, DEEPSEEK_API_URL)
        self.assertEqual(payload["messages"][0]["content"], NEWS_ANALYSIS_PROMPT)
        user = payload["messages"][1]["content"]
        self.assertIn("宁德时代拟回购400亿", user)
        self.assertIn("证券时报（Google News）：宁德时代披露回购", user)
        self.assertIn("（无，目前仅此一个来源）", user)  # 单一来源要明说，不让模型脑补

    def test_news_prompt_forbids_overclaim(self):
        self.assertIn("单一来源", NEWS_ANALYSIS_PROMPT)
        self.assertIn("严禁编造", NEWS_ANALYSIS_PROMPT)
        self.assertIn("不做买卖建议", NEWS_ANALYSIS_PROMPT)

    def test_analyze_news_missing_key(self):
        ok, msg = DeepSeekAI("").analyze_news([(1, "源", "标题", "摘要", [])])
        self.assertFalse(ok)
        self.assertIn("未配置 DeepSeek API Key", msg)


if __name__ == "__main__":
    unittest.main()
