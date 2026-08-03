"""DeepSeek 大模型接口 —— 用于手动主题推送时对输入内容进行深度提炼、分类和摘要.

直接支持 DeepSeek OpenAI 兼容接口（https://api.deepseek.com/chat/completions）。
当前官方模型为 ``deepseek-v4-flash`` / ``deepseek-v4-pro``；为兼容已有部署，
模型不可用时可按配置尝试候选模型，但鉴权、余额、限流和网络错误不会盲目重试其它模型。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from .http import Http

log = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
# V4 是当前首选；旧别名已进入退役阶段，只作为最后的兼容尝试，不能保证仍可用。
DEFAULT_FALLBACK_MODELS = (
    "deepseek-v4-pro",
    "deepseek-chat",
    "deepseek-reasoner",
)

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
        model: str = DEFAULT_MODEL,
        fallback_models: Iterable[str] | str | None = None,
        thinking: str | bool = "enabled",
        http: Http | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.model = (model or DEFAULT_MODEL).strip()
        self.fallback_models = self._normalise_models(
            DEFAULT_FALLBACK_MODELS if fallback_models is None else fallback_models
        )
        self.thinking = self._normalise_thinking(thinking)
        self.http = http or Http(timeout=20.0, retries=1)
        # 供上游在发生降级后准确标记实际使用的模型。
        self.last_model = self.model
        self.last_error = ""

    # ------------------------------------------------------------------
    def analyze(self, topic: str, content: str) -> tuple[bool, str]:
        """对用户录入的主题和内容执行大模型智能提炼、分类和摘要。

        返回 (ok: bool, result_or_err_message: str)。仅在明确的“模型不存在/不支持”
        错误时切换候选模型；API Key、余额、限流、网络故障等错误直接返回，避免重复
        扣费或把真正的根因掩盖掉。
        """
        if not self.api_key:
            log.warning("未配置 DeepSeek API Key (DEEPSEEK_API_KEY)，跳过大模型提炼")
            return False, "未配置 DeepSeek API Key"

        topic_str = (topic or "（未命名主题）").strip()
        content_str = (content or "").strip()
        if not content_str:
            return False, "输入内容为空"

        user_prompt = f"主题：{topic_str}\n\n内容：\n{content_str[:6000]}"
        candidates = self._candidate_models()
        last_error = ""

        for index, model in enumerate(candidates):
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = self._build_payload(model, user_prompt)
            log.info("调用 DeepSeek API (%s)%s", model, "" if index == 0 else "（降级候选）")

            try:
                data = self.http.post_json(DEEPSEEK_API_URL, payload, headers=headers)
            except Exception as exc:  # noqa: BLE001 - 网络及业务错误由上游平滑降级
                last_error = f"DeepSeek API 调用异常: {exc}"
                if index + 1 < len(candidates) and self._is_model_error(exc):
                    log.warning("模型 %s 不可用，尝试下一个候选模型：%s", model, candidates[index + 1])
                    continue
                break

            choices = (data or {}).get("choices") or []
            if choices:
                reply = str(choices[0].get("message", {}).get("content", "")).strip()
                if reply:
                    self.last_model = model
                    self.last_error = ""
                    return True, reply
                last_error = "大模型未生成有效结果"
                break

            # 有些网关会以 HTTP 200 返回 error JSON，不能只报“结构异常”。
            error = (data or {}).get("error") or {}
            if isinstance(error, dict):
                detail = error.get("message") or error.get("code") or ""
            else:
                detail = str(error)
            last_error = (
                f"DeepSeek API 返回错误：{detail}"
                if detail
                else f"DeepSeek API 返回结构异常：{data}"
            )
            if index + 1 < len(candidates) and self._is_model_error_text(last_error):
                log.warning("模型 %s 返回不支持/不存在，尝试下一个候选模型：%s", model, candidates[index + 1])
                continue
            break

        self.last_error = last_error or "DeepSeek API 调用失败"
        log.warning(self.last_error)
        return False, self.last_error

    def _candidate_models(self) -> list[str]:
        """去重并保持主模型优先，允许配置为空以关闭降级。"""
        result: list[str] = []
        for model in (self.model, *self.fallback_models):
            model = (model or "").strip()
            if model and model not in result:
                result.append(model)
        return result or [DEFAULT_MODEL]

    def _build_payload(self, model: str, user_prompt: str) -> dict[str, Any]:
        """生成兼容 V4 与旧别名的请求体。

        V4 默认开启 thinking；这里显式传参，避免不同网关/SDK 对默认值解释不一致。
        旧别名不传 V4 专属字段，作为最后的兼容尝试。
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        }
        if model.startswith("deepseek-v4-"):
            payload["thinking"] = {"type": self.thinking}
            if self.thinking == "enabled":
                payload["reasoning_effort"] = "high"
            else:
                payload["temperature"] = 0.3
        else:
            payload["temperature"] = 0.3
        return payload

    @staticmethod
    def _normalise_models(models: Iterable[str] | str) -> tuple[str, ...]:
        if isinstance(models, str):
            values = models.split(",")
        else:
            values = models
        return tuple(str(value).strip() for value in values if str(value).strip())

    @staticmethod
    def _normalise_thinking(value: str | bool) -> str:
        if isinstance(value, bool):
            return "enabled" if value else "disabled"
        return "disabled" if str(value).strip().lower() in {"0", "false", "no", "off", "disabled"} else "enabled"

    @classmethod
    def _is_model_error(cls, exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        # 鉴权、余额、限流和服务器错误与模型名无关，不应换模型重复请求。
        if status in {401, 402, 403, 408, 429} or (isinstance(status, int) and status >= 500):
            return False
        return cls._is_model_error_text(str(exc))

    @staticmethod
    def _is_model_error_text(text: str) -> bool:
        lowered = (text or "").lower()
        model_words = ("model", "模型")
        reason_words = (
            "not found",
            "does not exist",
            "unknown",
            "unsupported",
            "invalid",
            "不存在",
            "不支持",
            "无效",
        )
        return any(word in lowered for word in model_words) and any(
            word in lowered for word in reason_words
        )
