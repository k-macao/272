"""PushPlus 微信推送。"""

from __future__ import annotations

import logging
import re
import time

from .http import FetchError, Http

log = logging.getLogger(__name__)

PUSHPLUS_URL = "https://www.pushplus.plus/send"
PUSHPLUS_URL_FALLBACK = "http://www.pushplus.plus/send"

MAX_CONTENT = 100000
HTML_BLOCK_SEPARATOR = "<!--octopus:block-->"

_TAG_NAME = re.compile(r"^<\s*/?\s*([a-zA-Z][\w:-]*)")
_TOKEN = re.compile(r"<!--[\s\S]*?-->|<![^>]*>|<[^>]*>|[^<]+")
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def _trim_entity(text: str) -> str:
    """避免截在 ``&amp;`` 这类 HTML 实体中间。"""
    amp = text.rfind("&")
    semi = text.rfind(";")
    return text[:amp] if amp > semi else text


def _truncate_valid_html(content: str, max_content: int) -> str:
    """无卡片边界时保守截断，并补齐已经打开的 HTML 标签。"""
    if len(content) <= max_content:
        return content

    out: list[str] = []
    size = 0
    stack: list[str] = []

    def closing_size(names: list[str]) -> int:
        return sum(len(name) + 3 for name in names)

    for token in _TOKEN.findall(content):
        if token.startswith("<!--"):
            continue
        match = _TAG_NAME.match(token)
        if match:
            name = match.group(1).lower()
            if token.lstrip().startswith("</"):
                new_stack = list(stack)
                if name in new_stack:
                    reverse_index = new_stack[::-1].index(name)
                    del new_stack[len(new_stack) - reverse_index - 1 :]
                if size + len(token) + closing_size(new_stack) > max_content:
                    break
                out.append(token)
                size += len(token)
                stack = new_stack
                continue
            is_void = name in _VOID_TAGS or token.rstrip().endswith("/>")
            new_stack = stack if is_void else stack + [name]
            if size + len(token) + closing_size(new_stack) > max_content:
                break
            out.append(token)
            size += len(token)
            stack = new_stack
            continue

        reserve = closing_size(stack)
        available = max_content - size - reserve
        if available <= 0:
            break
        if len(token) <= available:
            out.append(token)
            size += len(token)
            continue
        piece = _trim_entity(token[:available])
        out.append(piece)
        size += len(piece)
        break

    for name in reversed(stack):
        closing = f"</{name}>"
        if size + len(closing) > max_content:
            break
        out.append(closing)
        size += len(closing)
    return "".join(out)


def limit_html_content(content: str, max_content: int = MAX_CONTENT) -> str:
    """输出一条不超过上限的完整 HTML 正文。"""
    content = content or ""
    if HTML_BLOCK_SEPARATOR not in content:
        return _truncate_valid_html(content, max_content)

    opening_end = content.find(">")
    closing_start = content.rfind("</div>")
    if opening_end < 0 or closing_start <= opening_end:
        return _truncate_valid_html(
            content.replace(HTML_BLOCK_SEPARATOR, ""), max_content
        )

    opening = content[: opening_end + 1]
    closing = content[closing_start:]
    inner = content[opening_end + 1 : closing_start]
    blocks = [block for block in inner.split(HTML_BLOCK_SEPARATOR) if block]
    selected: list[str] = []
    size = len(opening) + len(closing)
    for block in blocks:
        if size + len(block) > max_content:
            break
        selected.append(block)
        size += len(block)

    if selected:
        return opening + "".join(selected) + closing
    return _truncate_valid_html(
        opening + (blocks[0] if blocks else "") + closing,
        max_content,
    )


def split_html_pages(content: str, max_content: int = MAX_CONTENT) -> list[str]:
    """兼容旧调用；正文固定为一条。"""
    return [limit_html_content(content, max_content)]


class PushPlus:
    def __init__(self, http: Http, token: str, *, topics: list[str] | str = "", channel: str = "wechat") -> None:
        if not token:
            raise ValueError("PushPlus token 不能为空")
        self.http = http
        self.token = token
        if isinstance(topics, str):
            self.topics = [topics] if topics else []
        else:
            self.topics = [t for t in (topics or []) if t]
        self.channel = channel

    def send(self, title: str, content: str, *, dry_run: bool = False) -> bool:
        content = limit_html_content(content)
        if dry_run:
            log.info("[dry-run] 跳过推送：%s（正文 %d 字符）", title, len(content))
            return True

        topics_to_send = self.topics or [None]
        success_count = 0
        for topic in topics_to_send:
            payload = {
                "token": self.token,
                "title": title,
                "content": content,
                "template": "html",
                "channel": self.channel,
            }
            if topic:
                payload["topic"] = topic

            sent_ok = False
            last_error = ""
            for url in (PUSHPLUS_URL, PUSHPLUS_URL_FALLBACK):
                try:
                    data = self.http.post_json(url, payload)
                except FetchError as exc:
                    last_error = str(exc)
                    time.sleep(1.0)
                    continue

                code = (data or {}).get("code")
                if code == 200:
                    log.info("推送成功：%s (topic=%s)", title, topic or "self")
                    sent_ok = True
                    success_count += 1
                    break
                last_error = f"code={code} msg={(data or {}).get('msg')}"
                log.error("PushPlus 返回异常：%s", last_error)
                if code in (400, 401, 403, 500):
                    break

            if not sent_ok:
                log.error("推送失败 (topic=%s)：%s", topic or "self", last_error)

        return success_count > 0
