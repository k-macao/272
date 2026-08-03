"""PushPlus 微信推送.

PushPlus 单条消息正文有长度上限（约 20 万字符，但微信端渲染
超过 4~5 万就会被截断/卡顿），所以这里做了长度守卫：
超长时按源分组切分成多条依次发送。
"""

from __future__ import annotations

import logging
import time

from .http import FetchError, Http

log = logging.getLogger(__name__)

PUSHPLUS_URL = "https://www.pushplus.plus/send"
PUSHPLUS_URL_FALLBACK = "http://www.pushplus.plus/send"

MAX_CONTENT = 40000


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
        if dry_run:
            log.info("[dry-run] 跳过推送：%s（正文 %d 字符）", title, len(content))
            return True

        if len(content) > MAX_CONTENT:
            log.warning("正文 %d 字符超过上限，将被截断", len(content))
            content = content[: MAX_CONTENT - 200] + "</div><div>……内容过长已截断</div>"

        base_payload = {
            "token": self.token,
            "title": title,
            "content": content,
            "template": "html",
            "channel": self.channel,
        }

        topics_to_send = self.topics or [None]  # None means send to self (no topic)

        success_count = 0
        last_error = ""
        for topic in topics_to_send:
            payload = dict(base_payload)
            if topic:
                payload["topic"] = topic

            sent_ok = False
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
                # 业务错误（token 失效等）重试无意义
                if code in (400, 401, 403, 500):
                    break

            if not sent_ok:
                log.error("推送失败 (topic=%s)：%s", topic or "self", last_error)

        if success_count > 0:
            return True
        return False
