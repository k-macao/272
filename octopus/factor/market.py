"""A 股行情数据层：主题 -> 板块 -> 成分股 -> 日线序列。

数据全部来自东方财富公开行情接口（与项目里 ifind/cninfo 等源同一套口径）：
  - 概念/行业板块列表   push2 clist（fs=m:90 t:3 / m:90 t:2）
  - 板块成分股           push2 clist（fs=b:BKxxxx）
  - 个股/指数日线        push2his kline（klt=101 日线，fqt=1 前复权）

时间校验沿用项目的核心原则：K 线的日期由接口给出（YYYY-MM-DD），
解析不出来的整根丢弃；最新一根 K 线的日期会带进报告，读者能自己判断
数据新鲜度。绝不用"抓取时刻"冒充"行情时刻"。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from ..http import FetchError, Http
from ..timeutil import CN_TZ, now, parse

log = logging.getLogger(__name__)

CLIST_API = "https://push2.eastmoney.com/api/qt/clist/get"
KLINE_API = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
UT = "b2884a393a59ad64002292a3e90d46a5"

#: 板块类型：t:3 概念板块，t:2 行业板块
CONCEPT_FS = "m:90 t:3"
INDUSTRY_FS = "m:90 t:2"

#: 大盘基准指数（secid: 市场.代码，1=沪 0=深）
BENCHMARKS: tuple[tuple[str, str, str], ...] = (
    ("1.000001", "上证指数", "SH000001"),
    ("0.399001", "深证成指", "SZ399001"),
    ("0.399006", "创业板指", "SZ399006"),
    ("1.000300", "沪深300", "SH000300"),
)

#: 主题里出现这些词时，直接锁定对应的宽基指数视角
INDEX_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("上证", "沪指", "大盘", "A股", "两市", "市场"), "1.000001"),
    (("深证", "深成指"), "0.399001"),
    (("创业板",), "0.399006"),
    (("沪深300", "300"), "1.000300"),
)

_CLEAN_TOPIC = re.compile(r"[，。、,.\s]+")
#: 主题里的通用词，匹配板块时要剔除，否则"分析""行情"会误命中板块名
STOPWORDS = frozenset(
    {
        "分析", "研判", "行情", "板块", "概念", "题材", "个股", "股票", "市场",
        "走势", "复盘", "研究", "报告", "投资", "机会", "风险", "今日", "近期",
        "最新", "怎么样", "如何", "点评", "监管", "监督", "管理", "因子", "量化",
        "的", "了", "与", "和", "及",
    }
)


@dataclass
class Bar:
    """一根日线。"""

    day: date
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float = 0.0
    turnover: float = 0.0   # 换手率 %
    change: float = 0.0     # 涨跌幅 %

    @property
    def vwap(self) -> float:
        """成交均价：金额/成交量（手 -> 股要 ×100）。缺数据时退化为收盘价。"""
        if self.amount > 0 and self.volume > 0:
            return self.amount / (self.volume * 100.0)
        return self.close


@dataclass
class Instrument:
    """一个分析标的（个股或指数）及其日线序列。"""

    code: str
    name: str
    secid: str
    bars: list[Bar] = field(default_factory=list)
    is_index: bool = False
    change: float = 0.0       # 最新涨跌幅 %
    turnover: float = 0.0     # 最新换手率 %
    amount: float = 0.0       # 最新成交额（元）
    factors: dict[str, float | None] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def last_day(self) -> date | None:
        return self.bars[-1].day if self.bars else None

    @property
    def last_close(self) -> float | None:
        return self.bars[-1].close if self.bars else None

    def fields(self) -> dict[str, list[float | None]]:
        """转成因子引擎需要的字段序列。"""
        return {
            "open": [b.open for b in self.bars],
            "high": [b.high for b in self.bars],
            "low": [b.low for b in self.bars],
            "close": [b.close for b in self.bars],
            "volume": [b.volume for b in self.bars],
            "amount": [b.amount for b in self.bars],
            "vwap": [b.vwap for b in self.bars],
        }

    def ret(self, days: int) -> float | None:
        """近 N 日涨跌幅（%）。"""
        if len(self.bars) <= days:
            return None
        past = self.bars[-days - 1].close
        if not past:
            return None
        return (self.bars[-1].close / past - 1.0) * 100.0


@dataclass
class Board:
    """一个概念/行业板块。"""

    code: str          # BK0475
    name: str
    change: float = 0.0
    main_inflow: float = 0.0   # 主力净流入（元）
    leader: str = ""
    kind: str = "概念"
    matched_by: str = ""       # 命中主题的关键词，用于报告里说明匹配依据


@dataclass
class MarketSnapshot:
    """一次分析所需的全部行情素材。"""

    topic: str
    board: Board | None = None
    boards_considered: list[Board] = field(default_factory=list)
    stocks: list[Instrument] = field(default_factory=list)
    benchmarks: list[Instrument] = field(default_factory=list)
    universe_note: str = ""     # 标的选取口径说明，如实写进报告
    data_date: date | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def all_instruments(self) -> list[Instrument]:
        return list(self.benchmarks) + list(self.stocks)


# ---------------------------------------------------------------------------
def tokenize(topic: str) -> list[str]:
    """把主题拆成用于板块匹配的关键词（长词优先）。"""
    text = _CLEAN_TOPIC.sub(" ", (topic or "").strip())
    words: list[str] = []
    for chunk in text.split():
        chunk = chunk.strip()
        if not chunk or chunk in STOPWORDS:
            continue
        words.append(chunk)
        # 中文长词再切出 2-4 字的子串，提高与板块名的命中率
        if len(chunk) > 4 and re.search(r"[\u4e00-\u9fff]", chunk):
            for size in (4, 3, 2):
                for i in range(len(chunk) - size + 1):
                    piece = chunk[i : i + size]
                    if piece not in STOPWORDS:
                        words.append(piece)
    # 去重保序，长的排前面（更精确）
    seen: set[str] = set()
    uniq = [w for w in words if not (w in seen or seen.add(w))]
    return sorted(uniq, key=len, reverse=True)


class MarketData:
    """东财行情抓取。所有方法失败都抛 FetchError，由 pipeline 决定降级。"""

    def __init__(self, http: Http, *, config: dict | None = None) -> None:
        self.http = http
        self.config = config or {}

    # ------------------------------------------------------------------
    def boards(self, kind: str = "concept") -> list[Board]:
        """全部概念（或行业）板块列表。"""
        fs = CONCEPT_FS if kind == "concept" else INDUSTRY_FS
        data = self.http.json(
            CLIST_API,
            params={
                "pn": 1,
                "pz": 500,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": fs,
                "fields": "f2,f3,f12,f14,f62,f128,f136",
                "ut": UT,
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
    def match_board(self, topic: str) -> tuple[Board | None, list[Board]]:
        """把主题匹配到最合适的板块。返回 (最佳板块, 候选列表)。"""
        keywords = tokenize(topic)
        if not keywords:
            return None, []

        pool: list[Board] = []
        for kind in ("concept", "industry"):
            try:
                pool.extend(self.boards(kind))
            except FetchError as exc:
                log.warning("取%s板块失败：%s", kind, exc)
        if not pool:
            return None, []

        scored: list[tuple[float, Board]] = []
        for board in pool:
            best_score = 0.0
            best_kw = ""
            for kw in keywords:
                if kw == board.name:
                    score = 100.0 + len(kw)
                elif kw in board.name:
                    score = 60.0 + len(kw) * 2 - (len(board.name) - len(kw)) * 0.5
                elif board.name in kw and len(board.name) >= 2:
                    score = 50.0 + len(board.name) * 2
                else:
                    continue
                if score > best_score:
                    best_score, best_kw = score, kw
            if best_score > 0:
                board.matched_by = best_kw
                scored.append((best_score, board))

        if not scored:
            return None, []
        scored.sort(key=lambda p: (-p[0], -abs(p[1].change)))
        candidates = [b for _, b in scored[:5]]
        return candidates[0], candidates

    # ------------------------------------------------------------------
    def board_members(self, board_code: str, top: int = 8) -> list[tuple[str, str, dict]]:
        """板块成分股，按成交额降序取前 N。返回 [(secid, 名称, 行情字段)]。"""
        data = self.http.json(
            CLIST_API,
            params={
                "pn": 1,
                "pz": max(top * 3, 30),
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f6",  # 按成交额排序
                "fs": f"b:{board_code}",
                "fields": "f2,f3,f6,f8,f12,f13,f14",
                "ut": UT,
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
                "pn": 1,
                "pz": max(top * 3, 30),
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f6",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f2,f3,f6,f8,f12,f13,f14",
                "ut": UT,
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

    # ------------------------------------------------------------------
    def load_instrument(
        self,
        secid: str,
        name: str,
        *,
        limit: int = 250,
        is_index: bool = False,
        quote: dict | None = None,
    ) -> Instrument:
        bars = self.kline(secid, limit=limit)
        code = secid.split(".", 1)[-1]
        inst = Instrument(code=code, name=name, secid=secid, bars=bars, is_index=is_index)
        quote = quote or {}
        inst.change = _num(quote.get("f3")) or (bars[-1].change if bars else 0.0)
        inst.turnover = _num(quote.get("f8")) or 0.0
        inst.amount = _num(quote.get("f6")) or 0.0
        return inst


# ---------------------------------------------------------------------------
def _parse_kline(line: str) -> Bar | None:
    """解析东财 K 线串：日期,开,收,高,低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率。"""
    parts = line.split(",")
    if len(parts) < 6:
        return None
    published, _, _ = parse(parts[0])
    if published is None:
        return None
    try:
        return Bar(
            day=published.date(),
            open=float(parts[1]),
            close=float(parts[2]),
            high=float(parts[3]),
            low=float(parts[4]),
            volume=float(parts[5]),
            amount=float(parts[6]) if len(parts) > 6 and parts[6] not in ("", "-") else 0.0,
            change=float(parts[8]) if len(parts) > 8 and parts[8] not in ("", "-") else 0.0,
            turnover=float(parts[10]) if len(parts) > 10 and parts[10] not in ("", "-") else 0.0,
        )
    except (TypeError, ValueError):
        return None


def _num(value: object) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


_RISKY = re.compile(r"(ST|退|\*)")


def _is_risky_name(name: str) -> bool:
    return bool(_RISKY.search(name.upper().replace(" ", "")))


def data_freshness(day: date | None, *, ref: datetime | None = None) -> str:
    """行情日期的新鲜度说明 —— 让读者一眼知道数据截至何时。"""
    if day is None:
        return "行情日期未知"
    base = (ref or now()).astimezone(CN_TZ).date()
    delta = (base - day).days
    if delta <= 0:
        return f"{day:%Y-%m-%d}（今日）"
    if delta == 1:
        return f"{day:%Y-%m-%d}（上一交易日）"
    return f"{day:%Y-%m-%d}（{delta} 天前）"
