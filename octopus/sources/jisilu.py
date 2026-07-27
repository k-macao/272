"""集思录 —— 可转债 / ETF 折溢价 / 套利信号.

集思录的 webapi 无需登录即可拿到全量转债行情，字段里带 last_time
（最后成交时间，精确到秒），是可靠的时间戳来源。

推送策略：不是把几百只转债全倒出去，而是筛出"当下值得看一眼"的异动：
  - 强赎登记日 / 最后交易日（redeem_dt 命中今天）—— 不看会亏钱的硬提醒
  - 双低值最低的若干只 —— 经典的转债性价比指标
  - 涨跌幅异动（默认 ±5%）—— 盘中真实波动
"""

from __future__ import annotations

from ..models import Item
from ..timeutil import now, parse
from .base import Source

CB_LIST_API = "https://www.jisilu.cn/data/cbnew/cb_list_new/"
CB_LIST_FALLBACK = "https://www.jisilu.cn/webapi/cb/list/"


class JisiluSource(Source):
    name = "jisilu"
    label = "集思录·转债"
    homepage = "https://www.jisilu.cn"
    allow_date_only = False

    def collect(self) -> list[Item]:
        rows = self._load()
        if not rows:
            return []

        move_threshold = float(self.config.get("move_threshold", 5.0))
        dblow_top = int(self.config.get("dblow_top", 5))
        today = now().date()

        items: list[Item] = []
        seen_ids: set[str] = set()

        # --- 1. 强赎/到期提醒（最高优先级） ---------------------------------
        for row in rows:
            redeem = str(row.get("redeem_dt") or "")
            if not redeem:
                continue
            redeem_dt, _, _ = parse(redeem)
            if redeem_dt is None or redeem_dt.date() != today:
                continue
            item = self._build(row, kind="强赎提醒", note=_redeem_note(row))
            if item:
                items.append(item)
                seen_ids.add(str(row.get("bond_id")))

        # --- 2. 盘中大幅异动 ------------------------------------------------
        movers = [
            r
            for r in rows
            if isinstance(r.get("increase_rt"), (int, float))
            and abs(float(r["increase_rt"])) >= move_threshold
            and str(r.get("bond_id")) not in seen_ids
        ]
        movers.sort(key=lambda r: abs(float(r.get("increase_rt") or 0)), reverse=True)
        for row in movers[: int(self.config.get("mover_top", 6))]:
            item = self._build(row, kind="转债异动")
            if item:
                items.append(item)
                seen_ids.add(str(row.get("bond_id")))

        # --- 3. 双低榜（低价+低溢价，中线性价比） -----------------------------
        valued = [
            r
            for r in rows
            if isinstance(r.get("dblow"), (int, float))
            and isinstance(r.get("price"), (int, float))
            and float(r.get("price") or 0) > 0
            and str(r.get("bond_id")) not in seen_ids
            and str(r.get("qstatus")) == "00"  # 正常交易
        ]
        valued.sort(key=lambda r: float(r["dblow"]))
        for row in valued[:dblow_top]:
            item = self._build(row, kind="双低榜")
            if item:
                items.append(item)

        return items

    # ------------------------------------------------------------------
    def _load(self) -> list[dict]:
        headers = {"Referer": "https://www.jisilu.cn/data/cbnew/"}
        try:
            data = self.http.json(CB_LIST_API, params={"___jsl": "LST___t"}, headers=headers)
            rows = [r.get("cell", {}) for r in (data or {}).get("rows", [])]
            if rows:
                return rows
        except Exception:  # noqa: BLE001 - 换备用接口
            pass
        data = self.http.json(CB_LIST_FALLBACK, headers=headers)
        payload = (data or {}).get("data") or []
        return payload if isinstance(payload, list) else []

    # ------------------------------------------------------------------
    def _build(self, row: dict, *, kind: str, note: str = "") -> Item | None:
        last_time = row.get("last_time")
        published, quality, raw = parse(last_time)
        if published is None:
            return None

        bond_id = str(row.get("bond_id") or "")
        bond_nm = str(row.get("bond_nm") or bond_id)
        price = row.get("price")
        chg = row.get("increase_rt")
        premium = row.get("premium_rt")
        dblow = row.get("dblow")
        stock_nm = str(row.get("stock_nm") or "")

        head = f"{bond_nm}({bond_id})"
        if isinstance(chg, (int, float)) and kind == "转债异动":
            head += f" {chg:+.2f}%"
        title = f"{head} · {kind}"

        bits = []
        if isinstance(price, (int, float)):
            bits.append(f"现价 {price:.2f}")
        if isinstance(premium, (int, float)):
            bits.append(f"溢价率 {premium:+.2f}%")
        if isinstance(dblow, (int, float)):
            bits.append(f"双低 {dblow:.2f}")
        if stock_nm:
            bits.append(f"正股 {stock_nm}")
        if note:
            bits.append(note)

        # 标题里不含实时价格，去重键才稳定：同一只债当天只推一次同类事件
        return self.make_item(
            title=title,
            url=f"https://www.jisilu.cn/data/convert_bond_detail/{bond_id}",
            summary="，".join(bits),
            published_at=published,
            time_quality=quality,
            raw_time=str(raw),
            tags=[kind],
            extra={
                "bond_id": bond_id,
                "price": price,
                "premium_rt": premium,
                "dblow": dblow,
                "kind": kind,
            },
        )


def _redeem_note(row: dict) -> str:
    price = row.get("real_force_redeem_price")
    icons = row.get("icons") or {}
    tip = str(icons.get("R") or "").replace("\r\n", " ").replace("\n", " ")
    if tip:
        return tip[:80]
    return f"赎回价 {price}" if price else "今日为强赎登记日"
