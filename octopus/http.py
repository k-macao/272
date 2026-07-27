"""带重试、超时、UA 伪装的 HTTP 客户端.

财经站点普遍对 UA / Referer 敏感，且偶发 5xx，所以统一在这里处理，
各源只关心解析逻辑。
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class FetchError(RuntimeError):
    """网络层不可恢复的失败（重试耗尽）。"""


class Http:
    def __init__(
        self,
        *,
        timeout: float = 15.0,
        retries: int = 2,
        backoff: float = 1.5,
        user_agent: str = DEFAULT_UA,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
            }
        )

    # ------------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        encoding: str | None = None,
    ) -> requests.Response:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.get(
                    url, params=params, headers=headers, timeout=self.timeout
                )
                if resp.status_code >= 500:
                    raise FetchError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                if encoding:
                    resp.encoding = encoding
                elif resp.encoding in (None, "ISO-8859-1"):
                    resp.encoding = resp.apparent_encoding or "utf-8"
                return resp
            except Exception as exc:  # noqa: BLE001 - 统一收敛为 FetchError
                last = exc
                if attempt < self.retries:
                    delay = self.backoff ** attempt + random.uniform(0, 0.4)
                    log.debug("GET %s 第%d次失败(%s)，%.1fs 后重试", url, attempt + 1, exc, delay)
                    time.sleep(delay)
        raise FetchError(f"GET {url} 失败: {last}") from last

    # ------------------------------------------------------------------
    def json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        strip_jsonp: bool = False,
    ) -> Any:
        resp = self.get(url, params=params, headers=headers)
        text = resp.text.strip()
        if strip_jsonp:
            text = _unwrap_jsonp(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise FetchError(f"{url} 返回的不是合法 JSON: {text[:120]}") from exc

    def text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        encoding: str | None = None,
    ) -> str:
        return self.get(url, params=params, headers=headers, encoding=encoding).text

    def post_form(
        self,
        url: str,
        data: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.post(url, data=data, headers=headers, timeout=self.timeout)
                resp.raise_for_status()
                return resp
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < self.retries:
                    time.sleep(self.backoff ** attempt)
        raise FetchError(f"POST {url} 失败: {last}") from last

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < self.retries:
                    time.sleep(self.backoff ** attempt)
        raise FetchError(f"POST {url} 失败: {last}") from last

    def close(self) -> None:
        self.session.close()


def _unwrap_jsonp(text: str) -> str:
    """剥掉 JSONP 外壳： cb({...}) -> {...}，也兼容 var x={...};"""
    if text.startswith("var "):
        eq = text.find("=")
        if eq != -1:
            text = text[eq + 1 :].strip().rstrip(";")
    start = text.find("(")
    end = text.rfind(")")
    if start != -1 and end > start and not text.lstrip().startswith(("{", "[")):
        return text[start + 1 : end]
    return text
