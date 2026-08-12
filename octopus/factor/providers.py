"""主题因子分析的行情数据源：东财（国内）与 Yahoo Finance（国外免费）。

数据源选择由配置 `factor_market_source` 决定：
  - eastmoney ：只用东方财富公开接口（国内机器，板块/资金流最全）
  - yahoo     ：只用 Yahoo Finance（国外机器可直连，免费、免注册、无 Key）
  - auto      ：东财优先，任一环节失败自动降级到 Yahoo

两个 Provider 暴露同一套方法（boards / board_members / top_amount_stocks /
kline），由 market.MarketData 按顺序尝试。口径差异如实标注：

  - Yahoo 没有板块概念列表与主力资金流：板块用内置概念词典
    （concepts.py），涨跌幅由成分股最新价推算，净流入留空；
  - Yahoo 日线为不复权原始价 + 复权因子：复权价 = 原始价 × 复权系数，
    与 yfinance 的 auto_adjust 同一口径；
  - Yahoo 无换手率：该字段为 0，报告里不展示。

时间校验沿用项目核心原则：日期解析不出来的整根丢弃；最新一根 K 线的
日期带进报告，让读者自行判断数据新鲜度。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Protocol

from ..http import FetchError, Http
from ..timeutil import CN_TZ
from .concepts import (
    BOARDS,
    CONCEPTS,
    CORE_UNIVERSE,
    INDUSTRIES,
    board_kind,
    secid_to_code,
    secid_to_yahoo_symbol,
    to_secid,
    to_yahoo_symbol,
)
from .market import Bar, Board, _is_risky_name, _num, _parse_kline

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 东财接口
# ---------------------------------------------------------------------------
CLIST_API = "https://push2.eastmoney.com/api/qt/clist/get"
KLINE_API = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
UT = "b2884a393a59ad64002292a3e90d46a5"

CONCEPT_FS = "m:90 t:3"
INDUSTRY_FS = "m:90 t:2"

# ---------------------------------------------------------------------------
# Yahoo Finance 接口（免费、免注册）
# ---------------------------------------------------------------------------
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_SPARK = "https://query1.finance.yahoo.com/v8/finance/spark"
YAHOO_QUOTE = "https://query1.finance.yahoo.com/v7/finance/quote"

#: Yahoo 批量报价单次最多带多少个代码（大了容易 400）
YAHOO_QUOTE_CHUNK = 100


class MarketProvider(Protocol):
    """行情 Provider 的统一接口。所有失败抛 FetchError。"""

    name: str

    def boards(self, kind: str = "concept") -> list[Board]: ...

    def board_members(self, board: Board, top: int = 8) -> list[tuple[str, str, dict]]: ...

    def top_amount_stocks(self, top: int = 8) -> list[tuple[str, str, dict]]: ...

    def kline(self, secid: str, *, limit: int = 250) -> list[Bar]: ...


# ---------------------------------------------------------------------------
class EastmoneyProvider:
    """东方财富公开行情接口（国内直连，板块/主力资金/换手率齐全）。"""

    name = "eastmoney"
    display = "东方财富行情接口"

    def __init__(self, http: Http) -> None:
        self.http = http

    # ------------------------------------------------------------------
    def boards(self, kind: str = "concept") -> list[Board]:
        fs = CONCEPT_FS if kind == "concept" else INDUSTRY_FS
        data = self.http.json(
            CLIST_API,
            params={
                "pn": 1, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f3", "fs": fs,
                "fields": "f2,f3,f12,f14,f62,f128,f136", "ut": UT,
            },
            headers={"Referer": "https://quote.eastmoney.com/center/boardlist.html"},
        )
        rows = ((data or {}).get("data") or {}).get("diff") or []
        if isinstance(rows, dict):  # 东财有时返回 {"0": {...}} 形式
            rows = list(rows.values())
        out: list[Board] = []
        for row in rows:
            code = str(row.get("f12") or "")
            name = str(row.get("f14") or "")
            if not code or not name:
                continue
            out.append(
                Board(
                    code=code,
                    name=name,
                    change=_num(row.get("f3")) or 0.0,
                    main_inflow=_num(row.get("f62")) or 0.0,
                    leader=str(row.get("f128") or ""),
                    kind="概念" if kind == "concept" else "行业",
                )
            )
        if not out:
            raise FetchError(f"东财{kind}板块列表为空")
        return out

    # ------------------------------------------------------------------
    def board_members(self, board: Board, top: int = 8) -> list[tuple[str, str, dict]]:
        """板块成分股，按成交额降序取前 N。返回 [(secid, 名称, 行情字段)]。"""
        data = self.http.json(
            CLIST_API,
            params={
                "pn": 1, "pz": max(top * 3, 30), "po": 1, "np": 1, "fltt": 2,
                "invt": 2, "fid": "f6",  # 按成交额排序
                "fs": f"b:{board.code}",
                "fields": "f2,f3,f6,f8,f12,f13,f14", "ut": UT,
            },
            headers={"Referer": "https://quote.eastmoney.com/center/boardlist.html"},
        )
        rows = ((data or {}).get("data") or {}).get("diff") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        out: list[tuple[str, str, dict]] = []
        for row in rows:
            code = str(row.get("f12") or "")
            name = str(row.get("f14") or "")
            market = row.get("f13")
            if not code or not name or market is None:
                continue
            if _is_risky_name(name):
                continue  # ST/退市风险股不纳入分析样本
            out.append((f"{market}.{code}", name, row))
            if len(out) >= top:
                break
        return out

    # ------------------------------------------------------------------
    def top_amount_stocks(self, top: int = 8) -> list[tuple[str, str, dict]]:
        """全市场成交额前 N（板块匹配失败时的降级口径）。"""
        data = self.http.json(
            CLIST_API,
            params={
                "pn": 1, "pz": max(top * 3, 30), "po": 1, "np": 1, "fltt": 2,
                "invt": 2, "fid": "f6",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f2,f3,f6,f8,f12,f13,f14", "ut": UT,
            },
            headers={"Referer": "https://quote.eastmoney.com/center/gridlist.html"},
        )
        rows = ((data or {}).get("data") or {}).get("diff") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        out: list[tuple[str, str, dict]] = []
        for row in rows:
            code = str(row.get("f12") or "")
            name = str(row.get("f14") or "")
            market = row.get("f13")
            if not code or not name or market is None or _is_risky_name(name):
                continue
            out.append((f"{market}.{code}", name, row))
            if len(out) >= top:
                break
        if not out:
            raise FetchError("全市场成交额榜为空")
        return out

    # ------------------------------------------------------------------
    def kline(self, secid: str, *, limit: int = 250) -> list[Bar]:
        """日线序列（前复权），最早在前、最新在后。"""
        data = self.http.json(
            KLINE_API,
            params={
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": 101,   # 日线
                "fqt": 1,     # 前复权
                "end": "20500101",
                "lmt": limit,
                "ut": UT,
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
        )
        payload = (data or {}).get("data") or {}
        lines = payload.get("klines") or []
        bars: list[Bar] = []
        for line in lines:
            bar = _parse_kline(str(line))
            if bar is not None:      # 时间/数值解析不出来的整根丢弃，绝不臆造
                bars.append(bar)
        if not bars:
            raise FetchError(f"{secid} 日线为空")
        return bars


# ---------------------------------------------------------------------------
class YahooProvider:
    """Yahoo Finance —— 国外可直连的免费 A 股数据源。

    - 日线：v8 chart 接口（1d 间隔），原始价 + 复权因子 -> 前复权序列
    - 板块：内置概念词典（concepts.py），涨跌幅由成分股最新价**推算**
    - 成交额：收盘价 × 成交量**推算**，用于板块内排序
    - 换手率 / 主力净流入：Yahoo 不提供，置空并如实标注
    """

    name = "yahoo"
    display = "Yahoo Finance（国外免费源）"

    def __init__(self, http: Http) -> None:
        self.http = http
        self._headers = {"Referer": "https://finance.yahoo.com/"}

    # ------------------------------------------------------------------
    def boards(self, kind: str = "concept") -> list[Board]:
        table = CONCEPTS if kind == "concept" else INDUSTRIES
        if not table:
            raise FetchError(f"内置词典无{kind}板块")
        out: list[Board] = []
        for idx, name in enumerate(table):
            out.append(
                Board(
                    code=name,           # 词典板块用名字当 code，board_members 按名字查
                    name=name,
                    change=0.0,          # 动态涨跌幅在 board_members 后回填（推算）
                    main_inflow=0.0,     # Yahoo 无主力资金数据
                    kind="概念" if kind == "concept" else "行业",
                    change_derived=True,
                )
            )
            if idx >= 300:
                break
        return out

    # ------------------------------------------------------------------
    def board_members(self, board: Board, top: int = 8) -> list[tuple[str, str, dict]]:
        table = BOARDS.get(board.name or board.code)
        if not table:
            raise FetchError(f"内置词典未收录板块「{board.name or board.code}」")
        members = [(code, name) for code, name in table if not _is_risky_name(name)]
        ranked = self._rank_by_amount(members, top=top)
        # 回填板块涨跌幅/领涨股：由成分股最新行情**推算**（如实标注）
        changes = [q.get("f3") for _, _, q in ranked if q.get("f3") is not None]
        if changes:
            board.change = sum(changes) / len(changes)
        if ranked:
            board.leader = max(ranked, key=lambda r: r[2].get("f3") or -999.0)[1]
        return ranked

    # ------------------------------------------------------------------
    def top_amount_stocks(self, top: int = 8) -> list[tuple[str, str, dict]]:
        members = [(c, n) for c, n in CORE_UNIVERSE if not _is_risky_name(n)]
        return self._rank_by_amount(members, top=top)

    # ------------------------------------------------------------------
    def _rank_by_amount(
        self, members: list[tuple[str, str]], top: int
    ) -> list[tuple[str, str, dict]]:
        """给一组股票批量取 Yahoo 行情，按「价格×成交量」推算成交额排序。"""
        if not members:
            raise FetchError("候选个股列表为空")
        quotes = self._quotes([c for c, _ in members])
        ranked: list[tuple[float, str, str, dict]] = []
        for code, name in members:
            q = quotes.get(code) or {}
            price = _num(q.get("price"))
            volume = _num(q.get("volume"))
            amount = (price * volume) if (price and volume) else 0.0
            quote = {
                "f2": price,                       # 最新价
                "f3": q.get("change_pct"),         # 涨跌幅 %
                "f6": amount or None,              # 成交额（推算）
                "f8": None,                        # 换手率：Yahoo 无
                "f12": code,
                "f13": 1 if code[0] in "69" else 0,
                "f14": name,
            }
            ranked.append((amount, code, name, quote))
        ranked.sort(key=lambda r: -r[0])
        return [(to_secid(code), name, quote) for _, code, name, quote in ranked[:top]]

    # ------------------------------------------------------------------
    def kline(self, secid: str, *, limit: int = 250) -> list[Bar]:
        symbol = secid_to_yahoo_symbol(secid)
        # 250 个交易日 ≈ 1 个自然年；按需取更长的区间再截尾
        days = max(120, int(limit * 1.8))
        period2 = int(datetime.now().timestamp()) + 86400
        period1 = period2 - days * 86400
        data = self.http.json(
            YAHOO_CHART.format(symbol=symbol),
            params={
                "period1": period1,
                "period2": period2,
                "interval": "1d",
                "events": "history",
            },
            headers=self._headers,
        )
        result = (((data or {}).get("chart") or {}).get("result") or [None])[0]
        if result is None:
            err = (((data or {}).get("chart") or {}).get("error") or {})
            raise FetchError(f"Yahoo {symbol} 无行情数据：{err.get('description') or err}")
        bars = _parse_yahoo_chart(result)
        if not bars:
            raise FetchError(f"Yahoo {symbol} 日线解析为空")
        return bars[-limit:]

    # ------------------------------------------------------------------
    def _quotes(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        """批量取最新价/涨跌幅/成交量。spark 接口优先，v7 quote 兜底。"""
        symbols = [to_yahoo_symbol(c) for c in codes]
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(symbols), YAHOO_QUOTE_CHUNK):
            chunk = symbols[i : i + YAHOO_QUOTE_CHUNK]
            try:
                data = self.http.json(
                    YAHOO_SPARK,
                    params={
                        "symbols": ",".join(chunk),
                        "range": "1d",
                        "interval": "1d",
                    },
                    headers=self._headers,
                )
                out.update(self._parse_spark(data))
            except FetchError:
                out.update(self._parse_quote_v7(chunk))
        return out

    # ------------------------------------------------------------------
    def _parse_spark(self, data: dict) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        results = (((data or {}).get("spark") or {}).get("result")) or []
        for item in results:
            symbol = str(item.get("symbol") or "")
            code = secid_to_code(symbol)
            meta = ((item.get("response") or [{}])[0] or {}).get("meta") or {}
            price = _num(meta.get("regularMarketPrice"))
            prev = _num(meta.get("chartPreviousClose")) or _num(meta.get("previousClose"))
            volume = _num(meta.get("regularMarketVolume"))
            change_pct = None
            if price and prev:
                change_pct = (price / prev - 1.0) * 100.0
            if price is None:
                # meta 缺失时退而从收盘序列取最后一根
                quotes = ((item.get("response") or [{}])[0] or {}).get("indicators", {}).get("quote") or []
                closes = (quotes[0].get("close") or []) if quotes else []
                vols = (quotes[0].get("volume") or []) if quotes else []
                price = _last(closes)
                volume = _last(vols)
            out[code] = {"price": price, "volume": volume, "change_pct": change_pct}
        if not out:
            raise FetchError("Yahoo spark 报价为空")
        return out

    # ------------------------------------------------------------------
    def _parse_quote_v7(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        data = self.http.json(
            YAHOO_QUOTE,
            params={"symbols": ",".join(symbols)},
            headers=self._headers,
        )
        rows = (((data or {}).get("quoteResponse") or {}).get("result")) or []
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            code = secid_to_code(str(row.get("symbol") or ""))
            out[code] = {
                "price": _num(row.get("regularMarketPrice")),
                "volume": _num(row.get("regularMarketVolume")),
                "change_pct": _num(row.get("regularMarketChangePercent")),
            }
        if not out:
            raise FetchError("Yahoo quote 报价为空")
        return out


def _last(seq: list) -> Any:
    return seq[-1] if seq else None


def _parse_yahoo_chart(result: dict) -> list[Bar]:
    """把 Yahoo v8 chart 的 result 解析成 Bar 列表（前复权）。

    复权口径：原始 OHLC 乘以「复权收盘/原始收盘」的比例系数，
    与 yfinance auto_adjust 一致；指数没有复权因子时用原始价。
    任一字段缺失/解析失败的交易日整根丢弃。
    """
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    adj = ((result.get("indicators") or {}).get("adjclose") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    vols = quote.get("volume") or []
    adjcloses = adj.get("adjclose") or []

    bars: list[Bar] = []
    n = len(timestamps)
    for i in range(n):
        ts = timestamps[i]
        o, h, l, c, v = (
            opens[i] if i < len(opens) else None,
            highs[i] if i < len(highs) else None,
            lows[i] if i < len(lows) else None,
            closes[i] if i < len(closes) else None,
            vols[i] if i < len(vols) else None,
        )
        if ts is None or c is None:
            continue
        try:
            day = datetime.fromtimestamp(float(ts), tz=CN_TZ).date()
            raw_close = float(c)
        except (TypeError, ValueError):
            continue
        ratio = 1.0
        if i < len(adjcloses) and adjcloses[i] not in (None, 0) and raw_close:
            ratio = float(adjcloses[i]) / raw_close
        try:
            bar = Bar(
                day=day,
                open=float(o) * ratio if o is not None else raw_close * ratio,
                close=raw_close * ratio,
                high=float(h) * ratio if h is not None else raw_close * ratio,
                low=float(l) * ratio if l is not None else raw_close * ratio,
                volume=float(v) / 100.0 if v is not None else 0.0,  # 股 -> 手
                amount=0.0,      # Yahoo 无成交额，vwap 退化为收盘价
                change=0.0,      # 由 close 序列计算，见下
            )
        except (TypeError, ValueError):
            continue
        bars.append(bar)

    # 涨跌幅由真实收盘价推算（首根无前值 -> 0）
    for i, bar in enumerate(bars):
        if i > 0 and bars[i - 1].close:
            bar.change = (bar.close / bars[i - 1].close - 1.0) * 100.0
    return bars
