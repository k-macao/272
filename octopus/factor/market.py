"""A 股行情数据层：主题 -> 板块 -> 成分股 -> 日线序列。

数据源可切换（config `factor_market_source`）：
  - eastmoney：东方财富公开行情接口（国内机器，与项目里 ifind/cninfo 等
    源同一套口径）—— 板块/主力资金/换手率最全
  - yahoo：Yahoo Finance（国外可直连的免费源，免注册无 Key）—— 板块用
    内置概念词典，涨跌幅/成交额由最新行情推算并如实标注
  - auto：东财优先，任一环节失败自动降级到 Yahoo

具体抓取逻辑在 providers.py；本模块只负责「按配置选源 + 兜底降级 +
标的组装」。时间校验沿用项目的核心原则：K 线的日期由接口给出
（YYYY-MM-DD），解析不出来的整根丢弃；最新一根 K 线的日期会带进报告，
读者能自己判断数据新鲜度。绝不用"抓取时刻"冒充"行情时刻"。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from ..http import FetchError, Http
from ..timeutil import CN_TZ, now, parse

log = logging.getLogger(__name__)

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

    code: str          # 东财 BK0475；词典源下为板块名
    name: str
    change: float = 0.0
    main_inflow: float = 0.0   # 主力净流入（元）；Yahoo 源无此数据
    leader: str = ""
    kind: str = "概念"
    matched_by: str = ""       # 命中主题的关键词，用于报告里说明匹配依据
    change_derived: bool = False  # 涨跌幅是否由成分股行情推算（Yahoo 源）


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
    """多源行情门面：按配置选 Provider，逐个尝试、失败降级。

    - `source="eastmoney"`：只用东财（默认，兼容旧行为）
    - `source="yahoo"`：只用 Yahoo Finance（国外免费源）
    - `source="auto"`：东财优先，任一环节失败自动切 Yahoo

    所有方法失败都抛 FetchError，由 pipeline 决定降级；每次成功调用会
    记录实际生效的 Provider，供报告如实标注数据来源。
    """

    def __init__(self, http: Http, *, source: str = "eastmoney", config: dict | None = None) -> None:
        from .providers import EastmoneyProvider, YahooProvider  # 延迟导入，避免循环依赖

        self.http = http
        self.config = config or {}
        self.source = (source or "eastmoney").strip().lower()
        if self.source not in ("auto", "eastmoney", "yahoo"):
            raise ValueError(
                f"未知的行情数据源：{self.source!r}（可选 auto / eastmoney / yahoo）"
            )
        self._providers: list = []
        if self.source in ("auto", "eastmoney"):
            self._providers.append(EastmoneyProvider(http))
        if self.source in ("auto", "yahoo"):
            self._providers.append(YahooProvider(http))
        self.used: list[str] = []      # 本轮实际用过的 Provider 名（保序去重）

    #: Provider 内部名 -> 报告展示名
    DISPLAY = {"eastmoney": "东方财富行情接口", "yahoo": "Yahoo Finance（国外免费源）"}

    def source_note(self) -> str:
        """实际生效的行情数据源说明（报告里如实标注）。"""
        return "、".join(self.DISPLAY.get(n, n) for n in self.used) or "未获取到行情"

    # ------------------------------------------------------------------
    def _try(self, method: str, *args, **kwargs):
        """按顺序尝试各 Provider，全部失败时抛汇总的 FetchError。"""
        errors: list[str] = []
        for provider in self._providers:
            try:
                result = getattr(provider, method)(*args, **kwargs)
            except FetchError as exc:
                errors.append(f"{provider.name}: {exc}")
                log.info("行情源 %s 的 %s 失败：%s", provider.name, method, exc)
                continue
            if provider.name not in self.used:
                self.used.append(provider.name)
            return result
        raise FetchError("；".join(errors))

    # ------------------------------------------------------------------
    def boards(self, kind: str = "concept") -> list[Board]:
        """全部概念（或行业）板块列表。"""
        return self._try("boards", kind)

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
    def board_members(self, board: Board | str, top: int = 8) -> list[tuple[str, str, dict]]:
        """板块成分股，按成交额降序取前 N。返回 [(secid, 名称, 行情字段)]。

        兼容两种传参：Board 对象，或板块代码字符串（东财 BKxxxx）。
        """
        if isinstance(board, str):
            board = Board(code=board, name=board)
        return self._try("board_members", board, top)

    # ------------------------------------------------------------------
    def top_amount_stocks(self, top: int = 8) -> list[tuple[str, str, dict]]:
        """全市场成交额前 N（板块匹配失败时的降级口径）。"""
        return self._try("top_amount_stocks", top)

    # ------------------------------------------------------------------
    def kline(self, secid: str, *, limit: int = 250) -> list[Bar]:
        """日线序列（前复权），最早在前、最新在后。"""
        return self._try("kline", secid, limit=limit)

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
