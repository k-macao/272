"""东方财富 —— 7×24 快讯 + 个股人气榜（题材发酵初期信号）.

股吧帖子接口需要设备指纹，且噪音极大；真正有价值的"题材启动初期信号"
其实是东财快讯里的板块异动播报（"XX概念快速拉升 XX涨停"），
它比股吧发帖更早、更结构化，时间戳精确到秒。
"""

from __future__ import annotations

import re

from ..http import FetchError
from ..models import Item
from ..timeutil import parse
from .base import Source

KUAIXUN_API = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_{page_size}_{page}_.html"

# 题材发酵的典型措辞
SIGNAL_WORDS = (
    "拉升", "涨停", "跳水", "异动", "走强", "领涨", "大涨", "翻红",
    "封板", "涨超", "跌超", "创新高", "新高", "冲高",
)


class EastmoneySource(Source):
    name = "eastmoney"
    label = "东方财富"
    homepage = "https://www.eastmoney.com"
    allow_date_only = False

    def collect(self) -> list[Item]:
        rows = self._kuaixun()
        items: list[Item] = []
        for row in rows:
            published, quality, raw = parse(row.get("showtime") or row.get("ordertime"))
            if published is None:
                continue

            title = _strip_bracket(str(row.get("title") or "").strip())
            if not title:
                continue

            digest = _strip_bracket(str(row.get("digest") or row.get("simdigest") or "").strip())
            url = str(row.get("url_w") or row.get("url_unique") or self.homepage)

            items.append(
                self.make_item(
                    title=title,
                    url=url,
                    summary=digest[:180],
                    published_at=published,
                    time_quality=quality,
                    raw_time=raw,
                    tags=_tags(title),
                    extra={"kind": "快讯", "signal": _is_signal(title)},
                )
            )

        # 题材信号排前面，其余按时间
        items.sort(key=lambda i: (not i.extra.get("signal"), -(i.published_at.timestamp())))
        return items

    # ------------------------------------------------------------------
    def _kuaixun(self) -> list[dict]:
        page_size = int(self.config.get("page_size", 30))
        url = KUAIXUN_API.format(page_size=page_size, page=1)
        text = self.http.text(url, encoding="utf-8")
        data = _parse_var_json(text)
        rows = (data or {}).get("LivesList") or []
        if not rows:
            raise FetchError("东财快讯返回空列表")
        return rows


def _parse_var_json(text: str) -> dict:
    """接口返回 `var ajaxResult={...}` 形式。"""
    import json

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise FetchError("东财快讯响应格式异常")
    return json.loads(text[start : end + 1])


_BRACKET = re.compile(r"^【(.*?)】\s*")


def _strip_bracket(text: str) -> str:
    """把【标题】正文 里的方括号去掉，避免推送里嵌套符号过多。"""
    return _BRACKET.sub(r"\1｜", text)


def _is_signal(title: str) -> bool:
    return any(word in title for word in SIGNAL_WORDS)


def _tags(title: str) -> list[str]:
    tags = []
    if "概念" in title or "板块" in title:
        tags.append("题材")
    if any(w in title for w in ("涨停", "封板")):
        tags.append("涨停")
    if any(w in title for w in ("跳水", "跌停", "跌超")):
        tags.append("回撤")
    if "北向" in title or "外资" in title:
        tags.append("资金")
    return tags[:3]
