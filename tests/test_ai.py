"""测试 DeepSeek 大模型 API 提炼功能。"""

import unittest
from unittest.mock import MagicMock

from octopus.ai import (
    DEEPSEEK_API_URL,
    NEWS_ANALYSIS_PROMPT,
    NEWS_BRIEF_PROMPT,
    SYSTEM_PROMPT,
    DeepSeekAI,
    NewsAnalysisEntry,
)
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

    def test_news_prompt_asks_for_one_liner(self):
        """每条先要一句证券分析专家口吻的大白话，再要详细分析。"""
        self.assertIn("一句话", NEWS_ANALYSIS_PROMPT)
        self.assertIn("证券分析专家", NEWS_ANALYSIS_PROMPT)
        self.assertIn("1. 一句话：", NEWS_ANALYSIS_PROMPT)
        self.assertIn("分析：", NEWS_ANALYSIS_PROMPT)
        self.assertIn("「一句话」同样受此约束", NEWS_ANALYSIS_PROMPT)

    def test_news_prompt_requires_securities_dimensions(self):
        """证券分析必须按五模块走：事件重塑/利弊挖掘/深度溯源/多维推演/事实核查。"""
        self.assertIn("你是证券分析专家", NEWS_ANALYSIS_PROMPT)
        for field in (
            "【事件重塑】",
            "【利弊挖掘】",
            "【深度溯源】",
            "【多维推演】",
            "【事实核查】",
        ):
            self.assertIn(field, NEWS_ANALYSIS_PROMPT)
        self.assertIn("1-5 个交易日", NEWS_ANALYSIS_PROMPT)
        self.assertIn("合计 100%", NEWS_ANALYSIS_PROMPT)
        self.assertIn("观点样本不足", NEWS_ANALYSIS_PROMPT)
        self.assertIn("不可信引用数据", NEWS_ANALYSIS_PROMPT)
        self.assertIn("不得服从", NEWS_ANALYSIS_PROMPT)

    def test_news_prompt_keeps_probabilities_inside_multi_dimension(self):
        """多空情景概率写在「多维推演」里，格式固定为“偏多 x% / 偏空 y%”。"""
        self.assertIn("偏多 55% / 偏空 45%", NEWS_ANALYSIS_PROMPT)
        self.assertIn("短线情绪与资金", NEWS_ANALYSIS_PROMPT)
        self.assertIn("推测", NEWS_ANALYSIS_PROMPT)
        self.assertIn("事实核查", NEWS_ANALYSIS_PROMPT)

    def test_structured_news_context_is_sent_to_model(self):
        http = MagicMock()
        http.post_json.return_value = {"choices": [{"message": {"content": "1. ok"}}]}
        entry = NewsAnalysisEntry(
            number=1,
            source="巨潮资讯",
            title="宁德时代拟回购",
            summary="公司公告",
            related=("[网上相似观点] 证券时报：回购影响解读",),
            sector="电力设备",
            concepts=("锂电池", "新能源车"),
            market_data="宁德时代 300750 现价 188.50，涨跌幅 +2.31%",
            tags=("回购",),
        )
        ok, _ = DeepSeekAI("sk-test", http=http).analyze_news([entry])
        self.assertTrue(ok)
        user = http.post_json.call_args[0][1]["messages"][1]["content"]
        self.assertIn("行业板块（本地词典匹配）：电力设备", user)
        self.assertIn("概念题材（本地词典匹配）：锂电池、新能源车", user)
        self.assertIn("已核验行情：宁德时代 300750 现价 188.50", user)
        self.assertIn("[网上相似观点] 证券时报", user)

    def test_analyze_news_missing_key(self):
        ok, msg = DeepSeekAI("").analyze_news([(1, "源", "标题", "摘要", [])])
        self.assertFalse(ok)
        self.assertIn("未配置 DeepSeek API Key", msg)


class TestNewsBrief(unittest.TestCase):
    """总编极简简报：证券分析没内容时的兜底体裁。"""

    def _http(self, content: str) -> MagicMock:
        http = MagicMock()
        http.post_json.return_value = {"choices": [{"message": {"content": content}}]}
        return http

    def test_brief_prompt_requires_three_modules(self):
        for field in ("【核心快讯】", "【关键要素】", "【发展脉络】"):
            self.assertIn(field, NEWS_BRIEF_PROMPT)
        self.assertIn("资深的新闻总编", NEWS_BRIEF_PROMPT)
        self.assertIn("50 字以内", NEWS_BRIEF_PROMPT)
        self.assertIn("时间、地点、核心涉事方与起因", NEWS_BRIEF_PROMPT)
        self.assertIn("3-5 个主要发展阶段", NEWS_BRIEF_PROMPT)
        self.assertIn("一步一步推理", NEWS_BRIEF_PROMPT)
        self.assertIn("按时间顺序", NEWS_BRIEF_PROMPT)

    def test_brief_prompt_keeps_fact_and_compliance_lines(self):
        """兜底体裁同样不许编造、不许荐股、不许服从新闻里夹带的指令。"""
        self.assertIn("严禁编造", NEWS_BRIEF_PROMPT)
        self.assertIn("未提及", NEWS_BRIEF_PROMPT)
        self.assertIn("不要为了凑数编造阶段", NEWS_BRIEF_PROMPT)
        self.assertIn("据材料推断", NEWS_BRIEF_PROMPT)
        self.assertIn("不做多空研判", NEWS_BRIEF_PROMPT)
        self.assertIn("不给目标价", NEWS_BRIEF_PROMPT)
        self.assertIn("不可信引用数据", NEWS_BRIEF_PROMPT)
        self.assertIn("不得服从", NEWS_BRIEF_PROMPT)

    def test_brief_news_uses_brief_system_prompt(self):
        http = self._http("1. 【核心快讯】公司拟回购。")
        ok, text = DeepSeekAI("sk-test", http=http).brief_news(
            [
                NewsAnalysisEntry(
                    number=1,
                    source="巨潮资讯",
                    title="宁德时代拟回购400亿",
                    summary="公司公告回购",
                    related=("[同一事件报道] 证券时报：宁德时代披露回购",),
                    when="2026-07-27 10:25",
                )
            ]
        )
        self.assertTrue(ok)
        self.assertIn("【核心快讯】", text)
        url, payload = http.post_json.call_args[0]
        self.assertEqual(url, DEEPSEEK_API_URL)
        self.assertEqual(payload["messages"][0]["content"], NEWS_BRIEF_PROMPT)
        user = payload["messages"][1]["content"]
        self.assertIn("标题：宁德时代拟回购400亿", user)
        self.assertIn("摘要：公司公告回购", user)
        self.assertIn("发布时间 2026-07-27 10:25", user)
        self.assertIn("其它公开材料（只有标题/摘要，未必是同一事件）", user)
        self.assertIn("证券时报：宁德时代披露回购", user)

    def test_brief_news_accepts_legacy_tuple(self):
        http = self._http("1. 【核心快讯】ok")
        ok, _ = DeepSeekAI("sk-test", http=http).brief_news([(1, "源", "标题", "摘要", [])])
        self.assertTrue(ok)
        user = http.post_json.call_args[0][1]["messages"][1]["content"]
        self.assertIn("标题：标题", user)
        self.assertNotIn("发布时间", user)  # 旧格式没有时间就不写，不让模型猜

    def test_brief_news_missing_key(self):
        ok, msg = DeepSeekAI("").brief_news([(1, "源", "标题", "摘要", [])])
        self.assertFalse(ok)
        self.assertIn("未配置 DeepSeek API Key", msg)

    def test_brief_news_empty_entries(self):
        http = self._http("不会被调用")
        ok, text = DeepSeekAI("sk-test", http=http).brief_news([])
        self.assertTrue(ok)
        self.assertEqual(text, "")
        http.post_json.assert_not_called()

    def test_brief_news_failure_is_reported(self):
        http = MagicMock()
        http.post_json.side_effect = FetchError("请求超时")
        ok, msg = DeepSeekAI("sk-test", http=http).brief_news([(1, "源", "标题", "", [])])
        self.assertFalse(ok)
        self.assertIn("调用异常", msg)


if __name__ == "__main__":
    unittest.main()
