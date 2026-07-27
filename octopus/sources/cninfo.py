"""巨潮资讯 —— 上市公司公告（证监会指定披露平台）.

巨潮官网的 hisAnnouncement 接口需要 POST + 严格的 Referer，
在部分网络环境下会被 WAF 拦截；因此实现为：
  主用：巨潮 POST 接口（权威源头）
  备用：东方财富公告中心 API（同源数据，字段更规整）
两者都给出精确到秒的公告时间，时间校验可靠。
"""

from __future__ import annotations

from ..http import FetchError
from ..models import Item
from ..timeutil import parse
from .base import Source

CNINFO_API = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
EASTMONEY_ANN_API = "https://np-anotice-stock.eastmoney.com/api/security/ann"

# 值得盘前盘中关注的公告类型关键词（用于打标，不做硬过滤）
HOT_KEYWORDS = (
    "业绩预增", "业绩预告", "业绩快报", "回购", "增持", "减持", "中标",
    "重大合同", "重组", "收购", "停牌", "复牌", "问询函", "关注函",
    "立案", "分红", "解禁", "定增", "可转债",
)


class CninfoSource(Source):
    name = "cninfo"
    label = "巨潮资讯"
    homepage = "http://www.cninfo.com.cn"
    allow_date_only = False

    def collect(self) -> list[Item]:
        try:
            return self._from_cninfo()
        except FetchError:
            return self._from_eastmoney()

    # ------------------------------------------------------------------
    def _from_cninfo(self) -> list[Item]:
        payload = {
            "pageNum": 1,
            "pageSize": 40,
            "column": "szse",
            "tabName": "fulltext",
            "plate": "",
            "stock": "",
            "searchkey": "",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": "",
            "sortName": "time",
            "sortType": "desc",
            "isHLtitle": "true",
        }
        data = self.http.post_json(
            CNINFO_API,
            payload,
            headers={
                "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        rows = (data or {}).get("announcements") or []
        if not rows:
            raise FetchError("巨潮返回空公告列表")

        items: list[Item] = []
        for row in rows:
            published, quality, raw = parse(row.get("announcementTime"))
            if published is None:
                continue
            title = str(row.get("announcementTitle") or "").replace("<em>", "").replace("</em>", "")
            code = str(row.get("secCode") or "")
            stock = str(row.get("secName") or "")
            adjunct = str(row.get("adjunctUrl") or "")
            url = f"http://static.cninfo.com.cn/{adjunct}" if adjunct else self.homepage
            items.append(
                self.make_item(
                    title=f"{stock}({code}) {title}" if stock else title,
                    url=url,
                    summary="",
                    published_at=published,
                    time_quality=quality,
                    raw_time=raw,
                    tags=_hot_tags(title),
                    extra={"code": code, "stock": stock, "kind": "公告", "via": "cninfo"},
                )
            )
        return items

    # ------------------------------------------------------------------
    def _from_eastmoney(self) -> list[Item]:
        data = self.http.json(
            EASTMONEY_ANN_API,
            params={
                "sr": -1,
                "page_size": 40,
                "page_index": 1,
                "ann_type": "A",
                "client_source": "web",
                "f_node": 0,
                "s_node": 0,
            },
            headers={"Referer": "https://data.eastmoney.com/notices/"},
        )
        rows = ((data or {}).get("data") or {}).get("list") or []
        items: list[Item] = []
        for row in rows:
            # display_time 形如 2026-07-27 07:53:06:817（毫秒用冒号），timeutil 已兼容
            published, quality, raw = parse(row.get("display_time") or row.get("eiTime"))
            if published is None:
                continue
            title = str(row.get("title") or "")
            codes = row.get("codes") or []
            code = str(codes[0].get("stock_code")) if codes else ""
            stock = str(codes[0].get("short_name")) if codes else ""
            art = str(row.get("art_code") or "")
            columns = [c.get("column_name") for c in (row.get("columns") or []) if c.get("column_name")]
            items.append(
                self.make_item(
                    title=title if stock and stock in title else (f"{stock} {title}".strip()),
                    url=f"https://data.eastmoney.com/notices/detail/{code}/{art}.html"
                    if code and art
                    else "https://data.eastmoney.com/notices/",
                    summary="",
                    published_at=published,
                    time_quality=quality,
                    raw_time=raw,
                    tags=(_hot_tags(title) + columns)[:4],
                    extra={
                        "code": code,
                        "stock": stock,
                        "kind": "公告",
                        "via": "eastmoney",
                    },
                )
            )
        return items


def _hot_tags(title: str) -> list[str]:
    return [kw for kw in HOT_KEYWORDS if kw in title][:3]
