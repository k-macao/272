"""证券之星 —— 盘中异动快报 / 要闻.

滚动页 https://stock.stockstar.com/list/6095.shtml 每条都带
"2026-07-27 10:06:10" 的精确时间前缀，是少见的高质量时间源，
适合做涨跌停触发的实时监控。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..models import Item
from ..timeutil import parse
from .base import Source

SCROLL_URL = "https://stock.stockstar.com/list/6095.shtml"

_TIME_PREFIX = re.compile(r"(20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
# "异动快报：xxx（600000）7月27日9点58分触及涨停板" 里的重复前缀
_DUP_PREFIX = re.compile(r"^异动快报[:：]\s*")


class StockstarSource(Source):
    name = "stockstar"
    label = "证券之星"
    homepage = "https://stock.stockstar.com"
    allow_date_only = False

    def collect(self) -> list[Item]:
        html = self.http.text(SCROLL_URL, encoding="gb18030")
        soup = BeautifulSoup(html, "lxml")

        items: list[Item] = []
        seen_titles: set[str] = set()

        for li in soup.select("li"):
            text = li.get_text(" ", strip=True)
            match = _TIME_PREFIX.search(text)
            if not match:
                continue
            link = li.find("a")
            if not link:
                continue

            published, quality, raw = parse(match.group(1))
            if published is None:
                continue

            title = link.get_text(strip=True)
            if not title:
                continue

            # 站点会把同一条异动发两遍（带/不带"异动快报："前缀），归一后去重
            normalized = _DUP_PREFIX.sub("", title)
            if normalized in seen_titles:
                continue
            seen_titles.add(normalized)

            href = str(link.get("href") or "")
            if href and not href.startswith("http"):
                href = f"{self.homepage}/{href.lstrip('/')}"

            items.append(
                self.make_item(
                    title=normalized,
                    url=href or SCROLL_URL,
                    summary="",
                    published_at=published,
                    time_quality=quality,
                    raw_time=raw,
                    tags=_tags(normalized),
                    extra={"kind": "盘中异动"},
                )
            )
        return items


def _tags(title: str) -> list[str]:
    tags = []
    if "涨停" in title:
        tags.append("涨停")
    if "跌停" in title:
        tags.append("跌停")
    if "龙虎榜" in title:
        tags.append("龙虎榜")
    return tags
