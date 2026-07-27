"""券商研报聚合 —— 迈博汇金(mybbond) + 慧博投研(hibor).

迈博汇金站点在多数网络环境下不可达/需登录，因此：
  MybbondSource  主用迈博接口，失败自动降级到东方财富研报中心
                 （同为"全市场券商研报聚合"，字段含机构/评级/行业）
  HiborSource    直接解析慧博宏观经济列表页（自上而下的板块逻辑）

时间校验：研报只精确到日期，属于 DATE 质量 —— 两个源都开启
allow_date_only，但窗口过滤照常生效（3 小时窗口内只会放行当天的）。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..http import FetchError
from ..models import Item, TimeQuality
from ..timeutil import CN_TZ, now, parse
from .base import Source

MYBBOND_API = "https://www.mybbond.com/api/research/list"
EASTMONEY_REPORT_API = "https://reportapi.eastmoney.com/report/list"
HIBOR_MACRO = "http://www.hibor.com.cn/microns_13.html"
HIBOR_INDUSTRY = "http://www.hibor.com.cn/microns_2.html"


class MybbondSource(Source):
    """迈博汇金：券商研报聚合、行业深度、机构一致预期。"""

    name = "mybbond"
    label = "迈博汇金·研报"
    homepage = "https://www.mybbond.com"
    allow_date_only = True
    date_only_hour = 8

    def collect(self) -> list[Item]:
        try:
            return self._from_mybbond()
        except (FetchError, ValueError, KeyError):
            return self._from_eastmoney()

    # ------------------------------------------------------------------
    def _from_mybbond(self) -> list[Item]:
        data = self.http.json(
            MYBBOND_API,
            params={"page": 1, "pageSize": 30},
            headers={"Referer": self.homepage},
        )
        rows = (data or {}).get("data") or (data or {}).get("list") or []
        if not isinstance(rows, list) or not rows:
            raise FetchError("迈博汇金返回空列表")
        items: list[Item] = []
        for row in rows:
            published, quality, raw = parse(
                row.get("publishDate") or row.get("date") or row.get("createTime")
            )
            if published is None:
                continue
            title = str(row.get("title") or "").strip()
            org = str(row.get("orgName") or row.get("org") or "").strip()
            items.append(
                self.make_item(
                    title=f"{org}·{title}" if org else title,
                    url=str(row.get("url") or self.homepage),
                    summary=str(row.get("summary") or "")[:160],
                    published_at=_align(published, quality, self.date_only_hour),
                    time_quality=quality,
                    raw_time=raw,
                    tags=[t for t in [row.get("industry"), row.get("rating")] if t][:2],
                    extra={"org": org, "kind": "研报", "via": "mybbond"},
                )
            )
        return items

    # ------------------------------------------------------------------
    def _from_eastmoney(self) -> list[Item]:
        today = now()
        begin = (today.replace(hour=0, minute=0) - _days(3)).strftime("%Y-%m-%d")
        data = self.http.json(
            EASTMONEY_REPORT_API,
            params={
                "industryCode": "*",
                "pageSize": 40,
                "industry": "*",
                "rating": "",
                "ratingChange": "",
                "beginTime": begin,
                "endTime": today.strftime("%Y-%m-%d"),
                "pageNo": 1,
                "qType": 1,
            },
            headers={"Referer": "https://data.eastmoney.com/report/"},
        )
        rows = (data or {}).get("data") or []
        items: list[Item] = []
        for row in rows:
            published, quality, raw = _parse_publish_date(row.get("publishDate"))
            if published is None:
                continue
            title = str(row.get("title") or "").strip()
            org = str(row.get("orgSName") or row.get("orgName") or "").strip()
            industry = str(row.get("industryName") or "").strip()
            rating = str(row.get("emRatingName") or "").strip()
            info_code = str(row.get("infoCode") or "")
            items.append(
                self.make_item(
                    title=f"{org}·{title}" if org else title,
                    url=f"https://data.eastmoney.com/report/info/{info_code}.html"
                    if info_code
                    else "https://data.eastmoney.com/report/",
                    summary=" ".join(x for x in [industry, rating, f"{row.get('attachPages', '')}页"] if x),
                    published_at=_align(published, quality, self.date_only_hour),
                    time_quality=quality,
                    raw_time=raw,
                    tags=[x for x in (industry, rating) if x],
                    extra={
                        "org": org,
                        "industry": industry,
                        "rating": rating,
                        "kind": "研报",
                        "via": "eastmoney",
                    },
                )
            )
        return items


class HiborSource(Source):
    """慧博投研：宏观政策、行业景气度、产业链数据。"""

    name = "hibor"
    label = "慧博投研"
    homepage = "http://www.hibor.com.cn"
    allow_date_only = True
    date_only_hour = 8

    def collect(self) -> list[Item]:
        items: list[Item] = []
        errors: list[str] = []
        for url, kind in ((HIBOR_MACRO, "宏观"), (HIBOR_INDUSTRY, "行业")):
            try:
                items.extend(self._parse_list(url, kind))
            except FetchError as exc:
                errors.append(str(exc))
        # 两个列表页都挂了才算失败；只挂一个仍可正常产出
        if not items and len(errors) == 2:
            raise FetchError(f"慧博列表页全部不可达: {errors[0]}")
        return items

    # ------------------------------------------------------------------
    def _parse_list(self, url: str, kind: str) -> list[Item]:
        html = self.http.text(url, encoding="gb18030")
        soup = BeautifulSoup(html, "lxml")
        items: list[Item] = []

        for link in soup.select('a[href*="/data/"]'):
            title = link.get_text(strip=True)
            if len(title) < 8:
                continue
            href = str(link.get("href") or "")
            if not href.startswith("http"):
                href = f"{self.homepage}/{href.lstrip('/')}"

            # 日期在同一表格块的后续文本里，形如 2026-07-27分享者：xxx
            block = link.find_parent("table") or link.find_parent("tr") or link.parent
            text = block.get_text(" ", strip=True) if block else ""
            match = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
            if not match:
                continue
            published, quality, raw = parse(match.group(1))
            if published is None:
                continue

            summary = ""
            snippet = re.search(r"\[详细\]", text)
            if snippet:
                summary = text[: snippet.start()][-160:]

            items.append(
                self.make_item(
                    title=title,
                    url=href,
                    summary=summary.strip(),
                    published_at=_align(published, quality, self.date_only_hour),
                    time_quality=quality,
                    raw_time=raw,
                    tags=[kind],
                    extra={"kind": f"{kind}研报"},
                )
            )
        return items


def _parse_publish_date(value):
    """研报接口的 publishDate 形如 '2026-07-27 00:00:00.000'.

    00:00:00 并不是真的"零点发布"，而是该源只精确到天。
    如实降级成 DATE 质量，避免把日期当成精确时刻去做窗口判断。
    """
    text = str(value or "").split(".")[0].strip()
    if not text:
        return None, TimeQuality.MISSING, ""
    dt, quality, raw = parse(text)
    if dt is not None and quality is TimeQuality.EXACT and (dt.hour, dt.minute, dt.second) == (0, 0, 0):
        quality = TimeQuality.DATE
    return dt, quality, raw


def _align(dt, quality, hour: int):
    """把只有日期的时间戳对齐到当天的发布时点，避免全部堆在 00:00。"""
    if quality is TimeQuality.DATE:
        return dt.replace(hour=hour, minute=0, second=0, microsecond=0)
    return dt


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)
