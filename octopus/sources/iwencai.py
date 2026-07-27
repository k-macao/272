"""问财 / 同花顺 —— 智能选股、涨停池、个股热度榜.

问财网页版对非浏览器请求有 Nginx 拦截（需要 hexin-v 签名 Cookie），
所以这里走同花顺开放数据接口取等价信号：
  1. 涨停池（data.10jqka.com.cn）—— 相当于问财"今日涨停"选股结果
  2. 个股人气榜（dq.10jqka.com.cn）—— 板块热力/题材热度

时间校验：涨停池给出 first_limit_up_time（Unix 秒），是真实盘中时间戳；
热度榜没有条目级时间，用交易时段的整点刷新时刻作为 DERIVED 时间，
且仅在开市时段推送，避免非交易时段刷屏。
"""

from __future__ import annotations

from ..models import Item, TimeQuality
from ..timeutil import CN_TZ, now, parse
from .base import Source

LIMIT_UP_API = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
HOT_STOCK_API = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"

_FIELDS = (
    "199112,10,9001,330323,330324,330325,9002,330329,"
    "133971,133970,1968584,3475914,9003,9004"
)


class IWenCaiSource(Source):
    name = "iwencai"
    label = "问财·同花顺"
    homepage = "https://iwencai.10jqka.com.cn"
    allow_date_only = False

    def collect(self) -> list[Item]:
        items: list[Item] = []
        items.extend(self._limit_up_pool())
        items.extend(self._hot_stocks())
        return items

    # ------------------------------------------------------------------
    def _limit_up_pool(self) -> list[Item]:
        """涨停池：每只票带精确的封板时间戳，是最硬的盘中信号。"""
        data = self.http.json(
            LIMIT_UP_API,
            params={"page": 1, "limit": 40, "field": _FIELDS},
            headers={"Referer": "https://data.10jqka.com.cn/datacenterph/limitup/limtupInfo.html"},
        )
        payload = (data or {}).get("data") or {}
        rows = payload.get("info") or []
        stats = payload.get("limit_up_count") or {}
        today = stats.get("today") or {}

        items: list[Item] = []
        for row in rows:
            ts = row.get("last_limit_up_time") or row.get("first_limit_up_time")
            published, quality, raw = parse(ts)
            if published is None:
                continue

            code = str(row.get("code") or "")
            stock = str(row.get("name") or "")
            reason = str(row.get("reason_type") or "").strip()
            high_days = str(row.get("high_days") or "").strip()
            change = row.get("change_rate")
            turnover = row.get("turnover_rate")

            title_bits = [f"{stock}({code}) 封板"]
            if high_days:
                title_bits.append(high_days)
            if reason:
                title_bits.append(reason)
            title = " · ".join(title_bits)

            detail = []
            if isinstance(change, (int, float)):
                detail.append(f"涨幅 {change:.2f}%")
            if isinstance(turnover, (int, float)):
                detail.append(f"换手 {turnover:.2f}%")
            amount = row.get("order_amount")
            if isinstance(amount, (int, float)) and amount:
                detail.append(f"封单 {amount / 1e8:.2f}亿")
            if row.get("open_num"):
                detail.append(f"开板 {row['open_num']} 次")

            items.append(
                self.make_item(
                    title=title,
                    url=f"https://stockpage.10jqka.com.cn/{code}/",
                    summary="，".join(detail),
                    published_at=published,
                    time_quality=quality,
                    raw_time=raw,
                    tags=[t for t in reason.split("+") if t][:3],
                    extra={
                        "code": code,
                        "stock": stock,
                        "change_rate": change,
                        "limit_up_total": today.get("num"),
                        "limit_up_rate": today.get("rate"),
                        "kind": "涨停",
                    },
                )
            )
        return items

    # ------------------------------------------------------------------
    def _hot_stocks(self) -> list[Item]:
        """人气榜 Top N：反映资金/散户注意力，按小时刷新。"""
        data = self.http.json(
            HOT_STOCK_API,
            params={"stock_type": "a", "type": "hour", "list_type": "normal"},
            headers={"Referer": "https://dq.10jqka.com.cn/"},
        )
        rows = ((data or {}).get("data") or {}).get("stock_list") or []
        if not rows:
            return []

        # 榜单本身没有条目时间，用"当前整点"作为版本号：
        # 同一小时内重复抓取会命中去重，不会重复推送。
        ref = now()
        slot = ref.replace(minute=0, second=0, microsecond=0)
        if not _in_trading_hours(ref):
            return []

        top = rows[: int(self.config.get("hot_top", 8))]
        names = []
        for row in top:
            name = str(row.get("name") or "")
            code = str(row.get("code") or "")
            chg = row.get("rise_and_fall")
            if isinstance(chg, (int, float)):
                names.append(f"{name}({code}) {chg:+.2f}%")
            else:
                names.append(f"{name}({code})")

        tags: list[str] = []
        for row in top:
            for tag in ((row.get("tag") or {}).get("concept_tag") or []):
                if tag not in tags:
                    tags.append(tag)

        first = top[0] if top else {}
        return [
            self.make_item(
                title=f"个股人气榜 {slot:%H:00} · 领先 {first.get('name', '—')}",
                url="https://dq.10jqka.com.cn/",
                summary=" / ".join(names),
                published_at=slot,
                time_quality=TimeQuality.DERIVED,
                raw_time=f"{slot:%Y-%m-%d %H:00} 榜单刷新",
                tags=tags[:5],
                extra={"kind": "热度榜", "top": names},
            )
        ]


def _in_trading_hours(dt) -> bool:
    """A 股交易时段（含集合竞价与收盘后 30 分钟余温）。"""
    if dt.weekday() >= 5:
        return False
    minutes = dt.hour * 60 + dt.minute
    return (9 * 60 + 15) <= minutes <= (15 * 60 + 30)
