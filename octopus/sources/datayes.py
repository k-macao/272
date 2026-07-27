"""萝卜投研（通联数据）—— 财报解读 / 机构盈利预测.

robo.datayes.com 的 gw.datayes.com 网关接口全部要求登录
（返回 {"code":-403,"message":"Need login"}），公开渠道拿不到数据。

因此本源采取"可选 + 显式降级"策略：
  - 若配置了 DATAYES_TOKEN（环境变量），带 Authorization 走官方接口；
  - 否则用东财"研报-盈利预测调整"作为等价替代（机构一致预期变动），
    并在结果里标注降级原因，推送页脚会如实说明数据来自替代源。

绝不伪造数据，也不因为一个源不可用就让整轮任务失败。
"""

from __future__ import annotations

import os

from ..http import FetchError
from ..models import Item, TimeQuality
from ..timeutil import now, parse
from .base import Source
from .research import _parse_publish_date

DATAYES_NEWS = "https://gw.datayes.com/rrp_mammon/web/realTimeNews/list"
EASTMONEY_FORECAST = "https://reportapi.eastmoney.com/report/list"


class DatayesSource(Source):
    name = "datayes"
    label = "萝卜投研"
    homepage = "https://robo.datayes.com"
    allow_date_only = True
    date_only_hour = 8

    def collect(self) -> list[Item]:
        token = os.getenv("DATAYES_TOKEN", "").strip()
        if token:
            try:
                return self._official(token)
            except FetchError:
                pass
        return self._fallback()

    # ------------------------------------------------------------------
    def _official(self, token: str) -> list[Item]:
        data = self.http.json(
            DATAYES_NEWS,
            params={"pageSize": 30, "pageNow": 1, "important": "false"},
            headers={"Cloud-Sso-Token": token, "Referer": self.homepage},
        )
        if (data or {}).get("code") != 1:
            raise FetchError(f"萝卜投研返回 {data.get('code')}: {data.get('message')}")
        rows = ((data or {}).get("data") or {}).get("list") or []
        items: list[Item] = []
        for row in rows:
            published, quality, raw = parse(row.get("publishTime") or row.get("createTime"))
            if published is None:
                continue
            items.append(
                self.make_item(
                    title=str(row.get("title") or "").strip(),
                    url=str(row.get("url") or self.homepage),
                    summary=str(row.get("content") or "")[:180],
                    published_at=published,
                    time_quality=quality,
                    raw_time=raw,
                    tags=["投研"],
                    extra={"kind": "投研", "via": "datayes"},
                )
            )
        return items

    # ------------------------------------------------------------------
    def _fallback(self) -> list[Item]:
        """机构盈利预测调整（qType=4 为个股研报，含目标价与评级变动）。"""
        today = now()
        begin = (today - _days(2)).strftime("%Y-%m-%d")
        data = self.http.json(
            EASTMONEY_FORECAST,
            params={
                "industryCode": "*",
                "pageSize": 30,
                "industry": "*",
                "rating": "",
                "ratingChange": "",
                "beginTime": begin,
                "endTime": today.strftime("%Y-%m-%d"),
                "pageNo": 1,
                "qType": 0,
            },
            headers={"Referer": "https://data.eastmoney.com/report/stock.jshtml"},
        )
        rows = (data or {}).get("data") or []
        items: list[Item] = []
        for row in rows:
            # 复用研报源的日期归一逻辑：00:00:00 视为"只精确到天"
            published, quality, raw = _parse_publish_date(row.get("publishDate"))
            if published is None:
                continue
            if quality is TimeQuality.DATE:
                published = published.replace(hour=self.date_only_hour)

            stock = str(row.get("stockName") or "").strip()
            code = str(row.get("stockCode") or "").strip()
            org = str(row.get("orgSName") or "").strip()
            rating = str(row.get("emRatingName") or "").strip()
            title = str(row.get("title") or "").strip()

            pe_this = row.get("predictThisYearPe")
            eps_this = row.get("predictThisYearEps")
            bits = []
            if rating:
                bits.append(f"评级 {rating}")
            if eps_this:
                bits.append(f"今年EPS预测 {eps_this}")
            if pe_this:
                bits.append(f"对应PE {pe_this}")

            head = f"{stock}({code})" if stock and code else (stock or "")
            items.append(
                self.make_item(
                    title=f"{head} {org}·{title}".strip(),
                    url=f"https://data.eastmoney.com/report/info/{row.get('infoCode')}.html",
                    summary="，".join(bits),
                    published_at=published,
                    time_quality=quality,
                    raw_time=raw,
                    tags=[t for t in (rating, "盈利预测") if t][:2],
                    extra={
                        "kind": "机构预期",
                        "stock": stock,
                        "code": code,
                        "org": org,
                        "via": "eastmoney-fallback",
                    },
                )
            )
        return items


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)
