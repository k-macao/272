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

# 主题因子分析：把「事实清单」交给大模型解读，模型只负责组织语言与归因，
# 不负责编造数字 —— 所有数值都由本地因子引擎算好后传入。
THEME_SYSTEM_PROMPT = """你是一位资深的 A 股量化研究员兼合规风控专员（章鱼 AI · 因子分析引擎）。
用户会给你一份**已经计算完成**的结构化事实清单，内容包括：分析标的、
基于 microsoft/qlib 开源 Alpha158 因子模型算出的多维因子读数、以及 A 股市场监督管理动态。

你的任务是把这些事实解读成一份专业、克制、可直接阅读的研究简报。

【硬性要求】
1. 只能使用清单中提供的数字与事实，**严禁编造任何数据、股票代码、机构观点或政策原文**；
   清单里没有的信息，就不要提及。
2. 必须引用具体因子读数来支撑判断（例如"20日量价相关性 +0.52"），不要只说空话。
3. 必须单独用一段说明监管与合规风险，如实反映清单里的监管事件。
4. {compliance_rules}

【输出格式】严格按以下五个模块输出，不要写开场白和客套话：
【主题定位】：一句话说明该主题对应的板块/标的范围与当前市场位置
【因子解读】：分维度解读因子读数，指出相互印证或彼此矛盾之处（4-6 点）
【核心结论】：2-3 句话概括因子层面呈现的整体状态，措辞中性、不做方向性劝导
【监管视角】：结合监管事件与政策敏感度，说明该主题的合规风险与需要关注的监管口径
【风险提示】：3-4 点客观风险，包括因子模型本身的局限性"""


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
        return self._chat(SYSTEM_PROMPT, user_prompt, temperature=0.3)

    # ------------------------------------------------------------------
    def analyze_theme(self, topic: str, facts: str) -> tuple[bool, str]:
        """基于「已算好的事实清单」生成主题因子分析报告。

        与 analyze() 的区别：这里的输入是本地因子引擎 + 监管抓取产出的
        结构化事实，大模型只做解读与措辞，不接触原始数据，也就无从编造。

        返回 (ok, 报告正文或错误信息)。
        """
        if not self.api_key:
            return False, "未配置 DeepSeek API Key"
        facts = (facts or "").strip()
        if not facts:
            return False, "事实清单为空"

        from .factor.compliance import AI_COMPLIANCE_RULES

        system = THEME_SYSTEM_PROMPT.format(compliance_rules=AI_COMPLIANCE_RULES)
        user_prompt = (
            f"请基于以下事实清单，为主题「{(topic or '未指定').strip()}」撰写分析报告。\n\n"
            f"{facts[:12000]}"
        )
        return self._chat(system, user_prompt, temperature=0.4, max_tokens=2000)

    # ------------------------------------------------------------------
    def _chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> tuple[bool, str]:
        """统一的对话调用与错误收敛。"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

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
