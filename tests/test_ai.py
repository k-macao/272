"""测试 DeepSeek 大模型 API 提炼功能。"""

import unittest
from unittest.mock import MagicMock

from octopus.ai import SYSTEM_PROMPT, DEEPSEEK_API_URL, DeepSeekAI
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


if __name__ == "__main__":
    unittest.main()
