"""同花顺 iFinD —— 板块涨跌 / 北向资金 / 两融 / 概念热度.

iFinD 网页端主体需要登录，这里用同花顺与东财的公开行情接口拼出
等价的"市场资金情绪"快照：
  - 行业板块涨跌幅 Top（东财 clist 接口，含主力净流入与领涨股）
  - 沪深港通实时净流入（东财 kamt 接口）

这类"快照"没有条目级发布时间，用行情接口返回的分钟刻度作为
DERIVED 时间戳，并按 30 分钟粒度对齐 —— 既满足"时间可验证"，
又保证同一时段不会重复推送。
"""

from __future__ import annotations

from datetime import timedelta

from ..http import FetchError
from ..models import Item, TimeQuality
from ..timeutil import now
from .base import Source

BOARD_API = "https://push2.eastmoney.com/api/qt/clist/get"
HSGT_API = "https://push2.eastmoney.com/api/qt/kamt.rtmin/get"
UT = "b2884a393a59ad64002292a3e90d46a5"


class IFindSource(Source):
    name = "ifind"
    label = "iFinD·资金情绪"
    homepage = "https://ifind.10jqka.com.cn"
    allow_date_only = False

    def collect(self) -> list[Item]:
        ref = now()
        if not _in_trading_hours(ref):
            return []

        slot = _align_slot(ref, int(self.config.get("slot_minutes", 30)))
        items: list[Item] = []

        try:
            items.extend(self._boards(slot))
        except FetchError:
            pass
        try:
            items.extend(self._northbound(slot))
        except FetchError:
            pass
        return items

    # ------------------------------------------------------------------
    def _boards(self, slot) -> list[Item]:
        data = self.http.json(
            BOARD_API,
            params={
                "pn": 1,
                "pz": 10,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": "m:90 t:2",
                "fields": "f3,f12,f14,f62,f104,f105,f128,f136",
                "ut": UT,
            },
            headers={"Referer": "https://quote.eastmoney.com/center/boardlist.html"},
        )
        rows = ((data or {}).get("data") or {}).get("diff") or []
        if not rows:
            return []

        top = rows[: int(self.config.get("board_top", 6))]
        lines = []
        for row in top:
            name = row.get("f14")
            chg = row.get("f3")
            inflow = row.get("f62")
            leader = row.get("f128")
            piece = f"{name} {chg:+.2f}%" if isinstance(chg, (int, float)) else str(name)
            if isinstance(inflow, (int, float)):
                piece += f"（主力{inflow / 1e8:+.2f}亿"
                piece += f"，领涨{leader}）" if leader else "）"
            lines.append(piece)

        first = top[0]
        return [
            self.make_item(
                title=f"板块热力 {slot:%H:%M} · 领涨 {first.get('f14')} {first.get('f3', 0):+.2f}%",
                url="https://quote.eastmoney.com/center/boardlist.html#industry_board",
                summary=" / ".join(lines),
                published_at=slot,
                time_quality=TimeQuality.DERIVED,
                raw_time=f"{slot:%Y-%m-%d %H:%M} 行情快照",
                tags=["板块热力"],
                extra={"kind": "板块热力", "boards": lines},
            )
        ]

    # ------------------------------------------------------------------
    def _northbound(self, slot) -> list[Item]:
        data = self.http.json(
            HSGT_API,
            params={"fields1": "f1,f3", "fields2": "f51,f52,f54,f56", "ut": UT},
            headers={"Referer": "https://data.eastmoney.com/hsgt/index.html"},
        )
        series = ((data or {}).get("data") or {}).get("s2n") or []
        latest = None
        for line in series:
            parts = str(line).split(",")
            if len(parts) < 2 or parts[1] in ("-", ""):
                continue
            latest = parts
        if not latest:
            return []

        try:
            net = float(latest[1])
        except (TypeError, ValueError):
            return []

        # 全 0 说明当天通道未开或数据未更新，不推
        if net == 0:
            return []

        direction = "净流入" if net > 0 else "净流出"
        return [
            self.make_item(
                title=f"北向资金 {latest[0]} 累计{direction} {abs(net) / 10000:.2f}亿",
                url="https://data.eastmoney.com/hsgt/index.html",
                summary=f"沪深股通合计{direction} {abs(net) / 10000:.2f} 亿元",
                published_at=slot,
                time_quality=TimeQuality.DERIVED,
                raw_time=f"{slot:%Y-%m-%d} {latest[0]} 分时",
                tags=["北向资金"],
                extra={"kind": "北向资金", "net_wan": net},
            )
        ]


def _align_slot(dt, minutes: int):
    """把时间对齐到 N 分钟刻度，作为快照的版本号。"""
    return dt.replace(minute=(dt.minute // minutes) * minutes, second=0, microsecond=0)


def _in_trading_hours(dt) -> bool:
    if dt.weekday() >= 5:
        return False
    minutes = dt.hour * 60 + dt.minute
    return (9 * 60 + 30) <= minutes <= (15 * 60 + 10)
