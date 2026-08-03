"""DeepSeek 大模型接口 —— 用于手动主题推送时对输入内容进行深度提炼、分类和摘要.

直接支持 DeepSeek OpenAI 兼容接口（https://api.deepseek.com/chat/completions），
使用 Authorization: Bearer <API_KEY> 鉴权，支持 deepseek-v4-flash 等主流 DeepSeek 模型。
"""

from __future__ import annotations

import logging
from typing import Any

from .http import FetchError, Http

log = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

SYSTEM_PROMPT = """你是一位专业的金融及产业研究分析师和精炼总结专家（章鱼 AI · DeepSeek 大模型提炼引擎）。
请对用户提供的主题与内容进行深度提炼、分类与摘要，输出要求简洁有力、逻辑清晰，便于微信卡片阅读。
请直接输出以下三个核心模块（不需要输出多余的开场白或客套话）：
【主题分类】：给出所属分类（如 宏观政策/行业景气/个股异动/行业研报/通用内容 等）与 3-5 个核心关键词
【核心结论】：用 1-2 句简明扼要的话概括最关键的结论或逻辑
【关键信息提炼】：精炼列举 3-5 点最重要的要点、数据或细节"""


class DeepSeekAI:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "deepseek-v4-flash",
        http: Http | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.model = (model or "deepseek-v4-flash").strip()
        self.http = http or Http(timeout=20.0, retries=1)

    # ------------------------------------------------------------------
    def analyze(self, topic: str, content: str) -> tuple[bool, str]:
        """对用户录入的主题和内容执行大模型智能提炼、分类和摘要。

        返回 (ok: bool, result_or_err_message: str)。
        """
        if not self.api_key:
            log.warning("未配置 DeepSeek API Key (DEEPSEEK_API_KEY)，跳过大模型提炼")
            return False, "未配置 DeepSeek API Key"

        topic_str = (topic or "（未命名主题）").strip()
        content_str = (content or "").strip()
        if not content_str:
            return False, "输入内容为空"

        user_prompt = f"主题：{topic_str}\n\n内容：\n{content_str[:6000]}"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
        }

        try:
            data = self.http.post_json(DEEPSEEK_API_URL, payload, headers=headers)
            choices = (data or {}).get("choices") or []
            if not choices:
                msg = f"DeepSeek API 返回结构异常：{data}"
                log.warning(msg)
                return False, msg
            reply = str(choices[0].get("message", {}).get("content", "")).strip()
            if not reply:
                log.warning("DeepSeek API 返回了空的文本结果")
                return False, "大模型未生成有效结果"
            return True, reply
        except Exception as exc:  # noqa: BLE001 - 捕获网络及业务报错，便于上游平滑降级
            msg = f"DeepSeek API 调用异常: {exc}"
            log.warning(msg)
            return False, msg
