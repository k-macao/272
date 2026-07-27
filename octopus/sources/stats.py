"""国家统计局 —— 官方宏观数据发布.

统计局提供标准 RSS（https://www.stats.gov.cn/sj/zxfb/rss.xml），
pubDate 精确到秒（如 2026-07-27 09:30:01），是最规范的时间源之一。
数据发布节奏是每月固定几次，所以窗口内多数轮次为空 —— 这是正常的，
一旦 PMI/CPI/社融出来，第一时间就能推到。
"""

from __future__ import annotations

import re

from ..http import FetchError
from ..models import Item
from ..timeutil import parse
from .base import Source

RSS_LATEST = "https://www.stats.gov.cn/sj/zxfb/rss.xml"
RSS_INTERPRET = "https://www.stats.gov.cn/sj/sjjd/rss.xml"

_ITEM = re.compile(r"<item>(.*?)</item>", re.S | re.I)
_TITLE = re.compile(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", re.S | re.I)
_LINK = re.compile(r"<link>\s*(.*?)\s*</link>", re.S | re.I)
_PUBDATE = re.compile(r"<pubDate>\s*(.*?)\s*</pubDate>", re.S | re.I)

# 影响大盘周期判断的关键指标，命中后打标便于一眼识别
KEY_INDICATORS = (
    "采购经理指数", "PMI", "居民消费价格", "CPI", "工业生产者出厂价格", "PPI",
    "国内生产总值", "GDP", "工业增加值", "固定资产投资", "社会消费品零售",
    "房地产", "工业企业利润", "就业", "能源生产",
)


class StatsSource(Source):
    name = "stats"
    label = "国家统计局"
    homepage = "https://www.stats.gov.cn"
    allow_date_only = True
    date_only_hour = 9

    def collect(self) -> list[Item]:
        items: list[Item] = []
        errors: list[str] = []
        for url, kind in ((RSS_LATEST, "数据发布"), (RSS_INTERPRET, "数据解读")):
            try:
                items.extend(self._parse_rss(url, kind))
            except FetchError as exc:
                errors.append(str(exc))
        # 两个 RSS 都取不到 —— 这是真失败，不能伪装成"本轮没有新数据"
        if not items and len(errors) == 2:
            raise FetchError(f"统计局 RSS 全部不可达: {errors[0]}")
        return items

    # ------------------------------------------------------------------
    def _parse_rss(self, url: str, kind: str) -> list[Item]:
        xml = self.http.text(url, encoding="utf-8")
        items: list[Item] = []
        for block in _ITEM.findall(xml)[:30]:
            title_m = _TITLE.search(block)
            if not title_m:
                continue
            title = title_m.group(1).strip()
            if not title:
                continue

            date_m = _PUBDATE.search(block)
            published, quality, raw = parse(date_m.group(1) if date_m else "")
            if published is None:
                continue

            link_m = _LINK.search(block)
            link = (link_m.group(1).strip() if link_m else "") or f"{self.homepage}/sj/zxfb/"

            items.append(
                self.make_item(
                    title=title,
                    url=link,
                    summary="",
                    published_at=published,
                    time_quality=quality,
                    raw_time=raw,
                    tags=[kind] + [k for k in KEY_INDICATORS if k in title][:2],
                    extra={"kind": kind, "official": True},
                )
            )
        return items
