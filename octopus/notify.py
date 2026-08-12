"""PushPlus 微信推送。

微信端对超长 HTML 的容错很差。渲染层会在顶层卡片之间插入不可见边界；
超过建议长度时，本模块只沿这些边界拆成多条完整消息，绝不从标签中间硬截断。
"""

from __future__ import annotations

import logging
import time

from .http import FetchError, Http

log = logging.getLogger(__name__)

PUSHPLUS_URL = "https://www.pushplus.plus/send"
PUSHPLUS_URL_FALLBACK = "http://www.pushplus.plus/send"

# 这不是 PushPlus API 的硬上限，而是微信内置浏览器保持流畅的建议单页长度。
MAX_CONTENT = 40000
HTML_BLOCK_SEPARATOR = "<!--octopus:block-->"


def split_html_pages(content: str, max_content: int = MAX_CONTENT) -> list[str]:
    """沿渲染器提供的卡片边界拆分 HTML，并让每一页都有完整外层标签。

    对没有边界的第三方 HTML 宁可完整发送，也不做破坏标签结构的盲目截断。
    单张卡片本身超过建议长度时同样完整保留。
    """
    content = content or ""
    if HTML_BLOCK_SEPARATOR not in content:
        return [content]

    # `_document()` 产出的内容一定是一个外层 div。这里仍做防御检查，避免
    # 输入形态变化时生成无效 HTML。
    opening_end = content.find(">")
    closing_start = content.rfind("</div>")
    if opening_end < 0 or closing_start <= opening_end:
        return [content.replace(HTML_BLOCK_SEPARATOR, "")]

    opening = content[: opening_end + 1]
    closing = content[closing_start:]
    inner = content[opening_end + 1 : closing_start]
    blocks = [block for block in inner.split(HTML_BLOCK_SEPARATOR) if block]
    if not blocks:
        return [opening + closing]

    budget = max(1, max_content - len(opening) - len(closing))
    pages: list[str] = []
    current: list[str] = []
    current_size = 0

    for block in blocks:
        block_size = len(block)
        if current and current_size + block_size > budget:
            pages.append(opening + "".join(current) + closing)
            current = []
            current_size = 0
        if block_size > budget:
            # 单卡过长时先落掉前面的卡，再把此卡作为一张完整的大页。
            if current:
                pages.append(opening + "".join(current) + closing)
                current = []
                current_size = 0
            pages.append(opening + block + closing)
            continue
        current.append(block)
        current_size += block_size

    if current:
        pages.append(opening + "".join(current) + closing)
    return pages or [opening + closing]


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

    # ------------------------------------------------------------------
    def send(self, title: str, content: str, *, dry_run: bool = False) -> bool:
        pages = split_html_pages(content)
        if dry_run:
            log.info(
                "[dry-run] 跳过推送：%s（正文 %d 字符，%d 页）",
                title,
                len(content),
                len(pages),
            )
            return True

        if len(pages) > 1:
            log.info("正文 %d 字符，已按完整卡片整理为 %d 条推送", len(content), len(pages))
        elif len(pages[0]) > MAX_CONTENT:
            log.warning(
                "正文含一张 %d 字符的超长卡片；为避免破坏 HTML，保持完整发送",
                len(pages[0]),
            )

        topics_to_send = self.topics or [None]  # None 表示个人推送
        complete_topics = 0

        for topic in topics_to_send:
            topic_ok = True
            for page_index, page in enumerate(pages, 1):
                page_title = (
                    f"{title}（{page_index}/{len(pages)}）" if len(pages) > 1 else title
                )
                payload = {
                    "token": self.token,
                    "title": page_title,
                    "content": page,
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
                        log.info(
                            "推送成功：%s (topic=%s)",
                            page_title,
                            topic or "self",
                        )
                        sent_ok = True
                        break
                    last_error = f"code={code} msg={(data or {}).get('msg')}"
                    log.error("PushPlus 返回异常：%s", last_error)
                    # 业务错误（token 失效等）重试备用域名无意义。
                    if code in (400, 401, 403, 500):
                        break

                if not sent_ok:
                    log.error(
                        "推送失败 (topic=%s, page=%d/%d)：%s",
                        topic or "self",
                        page_index,
                        len(pages),
                        last_error,
                    )
                    topic_ok = False
                    break

            if topic_ok:
                complete_topics += 1

        return complete_topics > 0
