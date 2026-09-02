"""情报条目增强：给每条新闻补上现价，再写一句总结。

流程（渲染推送前、不影响时间校验与去重）：

1. 从 extra / 标题 / 摘要里提取 A 股或转债代码（宁可少提，不可臆造）
2. 批量取现价：东财 ulist 优先，失败或漏报的代码再降级 Yahoo Finance
3. 一句总结：配置了 DeepSeek 则按条生成；否则或调用失败时用规则化摘要，
   并如实标注，绝不假装用了 AI

任何一步失败只让该条缺少现价/改用规则摘要，不会丢条目、不会拖垮整轮推送。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

from .http import FetchError, Http
from .models import Item

log = logging.getLogger(__name__)

# 东财批量报价。fltt=2 让数值以小数返回，避免还要自己除 100。
ULIST_API = "https://push2.eastmoney.com/api/qt/ulist.np/get"
UT = "b2884a393a59ad64002292a3e90d46a5"
EASTMONEY_CHUNK = 40
YAHOO_QUOTE = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_CHUNK = 50

_CODE_DOT = re.compile(r"(?<!\d)(\d{6})\.(SH|SZ|SS|BJ)(?!\w)", re.I)
_CODE_PAREN = re.compile(r"[（(]\s*(\d{6})\s*[）)]")
_BARE_CODE = re.compile(r"^\d{6}$")
_WS = re.compile(r"\s+")

#: 指数别名 -> (6 位代码, 东财 secid)。必须带市场前缀：000001 既是上证也是平安银行。
INDEX_ALIASES: tuple[tuple[str, str, str], ...] = (
    ("上证指数", "000001", "1.000001"),
    ("上证综指", "000001", "1.000001"),
    ("沪指", "000001", "1.000001"),
    ("深证成指", "399001", "0.399001"),
    ("深成指", "399001", "0.399001"),
    ("创业板指", "399006", "0.399006"),
    ("沪深300", "000300", "1.000300"),
    ("科创50", "000688", "1.000688"),
)

_BANNED_BRIEF = ("买入", "卖出", "目标价", "立即建仓", "稳赚", "必涨", "马上买")

_NAME_INDEX: list[tuple[str, str]] | None = None
_BOARD_NAMES: frozenset[str] | None = None


@dataclass(frozen=True)
class Ticker:
    code: str
    name: str = ""
    secid: str = ""


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    price: float
    change: float | None
    source: str = ""


def enrich_news(
    items: Iterable[Item],
    *,
    http: Http | None = None,
    api_key: str = "",
    model: str = "deepseek-v4-flash",
) -> dict[str, int]:
    """就地给条目补现价与一句总结。返回计数，便于日志。"""
    bag = [it for it in items if it is not None]
    stats = {"items": len(bag), "quotes": 0, "ai": 0, "rule": 0}
    if not bag:
        return stats

    tickers_by_item = [extract_tickers(it) for it in bag]
    unique: dict[str, Ticker] = {}
    for group in tickers_by_item:
        for tk in group:
            unique.setdefault(tk.code, tk)

    quotes: dict[str, Quote] = {}
    if unique and http is not None:
        try:
            quotes = fetch_quotes(http, list(unique.values()))
        except Exception as exc:  # noqa: BLE001 - 行情失败不影响推送
            log.info("批量取现价失败，将仅使用源内价格（如有）：%s", exc)

    for item, tickers in zip(bag, tickers_by_item):
        if _attach_quote(item, tickers, quotes):
            stats["quotes"] += 1

    _fill_briefs(bag, http=http, api_key=api_key, model=model)
    stats["ai"] = sum(1 for it in bag if it.ai_brief_from_model)
    stats["rule"] = sum(1 for it in bag if it.ai_brief and not it.ai_brief_from_model)
    return stats


# ---------------------------------------------------------------------------
# 代码提取
# ---------------------------------------------------------------------------
def extract_tickers(item: Item) -> list[Ticker]:
    """从一条情报里抽出最多 3 个标的。优先 extra / 显式代码，名称匹配最保守。"""
    found: list[Ticker] = []
    seen: set[str] = set()
    extra = item.extra or {}
    title = item.title or ""
    summary = item.summary or ""
    blob = f"{title} {summary}"

    def add(code: str, name: str = "", secid: str = "") -> None:
        code_n = _normalize_code(code)
        if not code_n or code_n in seen:
            return
        seen.add(code_n)
        hint_name = name or str(extra.get("stock") or extra.get("stock_nm") or extra.get("bond_nm") or "")
        secid = secid or code_to_secid(code_n, name=hint_name, title=title)
        found.append(Ticker(code=code_n, name=hint_name, secid=secid))

    for key in ("code", "bond_id", "stock_code"):
        raw = extra.get(key)
        if raw:
            add(str(raw), name=str(extra.get("stock") or extra.get("stock_nm") or extra.get("bond_nm") or ""))

    for match in _CODE_DOT.finditer(blob):
        code, suffix = match.group(1), match.group(2).upper()
        market = "1" if suffix in ("SH", "SS") else "0"
        add(code, secid=f"{market}.{code}")

    for match in _CODE_PAREN.finditer(blob):
        add(match.group(1))

    for alias, code, secid in INDEX_ALIASES:
        if alias in blob:
            add(code, name=alias, secid=secid)

    names, boards = _name_index()
    for name, code in names:
        idx = blob.find(name)
        if idx < 0:
            continue
        after = blob[idx + len(name) : idx + len(name) + 3]
        if after.startswith(("板块", "概念", "行业", "指数")):
            continue
        if name in boards:
            # 「机器人」「黄金」既是板块名也是个股/题材，名称匹配太容易误伤
            continue
        add(code, name=name)
        if len(found) >= 3:
            break

    return found[:3]


def _normalize_code(raw: str) -> str:
    text = (raw or "").strip().upper()
    if not text:
        return ""
    dotted = _CODE_DOT.search(text)
    if dotted:
        return dotted.group(1)
    if _BARE_CODE.match(text):
        return text
    return ""


def code_to_secid(code: str, *, name: str = "", title: str = "") -> str:
    """6 位代码 -> 东财 secid。000001 在「上证」语境下走指数，否则按深市个股。"""
    code = (code or "").strip()
    blob = f"{name}{title}"
    if code == "000001" and any(k in blob for k in ("上证", "沪指", "综指")):
        return "1.000001"
    # 沪市：主板 6、B 股 9、ETF 5、转债 11、科创转债 118、回购 13
    if code.startswith(("5", "6", "9", "11", "13")):
        return f"1.{code}"
    return f"0.{code}"


def code_to_yahoo(code: str, *, secid: str = "") -> str | None:
    if secid:
        left, _, right = secid.partition(".")
        if left == "1":
            return f"{right}.SS"
        if left == "0":
            return f"{right}.SZ"
        if left == "2":
            return f"{right}.BJ"
    code = (code or "").strip()
    if not _BARE_CODE.match(code):
        return None
    if code[0] in "69" or code.startswith("11"):
        return f"{code}.SS"
    if code[0] in "48":
        return f"{code}.BJ"
    return f"{code}.SZ"


def _name_index() -> tuple[list[tuple[str, str]], frozenset[str]]:
    global _NAME_INDEX, _BOARD_NAMES
    if _NAME_INDEX is None:
        from .factor.concepts import BOARDS, CORE_UNIVERSE

        _BOARD_NAMES = frozenset(BOARDS)
        pairs = [(name, code) for code, name in CORE_UNIVERSE if len(name) >= 3]
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        _NAME_INDEX = pairs
    return _NAME_INDEX, _BOARD_NAMES or frozenset()


# ---------------------------------------------------------------------------
# 现价
# ---------------------------------------------------------------------------
def fetch_quotes(http: Http, tickers: list[Ticker]) -> dict[str, Quote]:
    """批量取现价。东财优先，缺的再走 Yahoo；单源失败不抛给上层。"""
    out: dict[str, Quote] = {}
    if not tickers:
        return out
    try:
        out.update(_eastmoney_quotes(http, tickers))
    except FetchError as exc:
        log.info("东财批量报价失败，降级 Yahoo：%s", exc)

    missing = [tk for tk in tickers if tk.code not in out]
    if missing:
        try:
            out.update(_yahoo_quotes(http, missing))
        except FetchError as exc:
            log.info("Yahoo 批量报价失败：%s", exc)
    return out


def _eastmoney_quotes(http: Http, tickers: list[Ticker]) -> dict[str, Quote]:
    by_secid = {tk.secid: tk for tk in tickers if tk.secid}
    out: dict[str, Quote] = {}
    secids = list(by_secid)
    for i in range(0, len(secids), EASTMONEY_CHUNK):
        chunk = secids[i : i + EASTMONEY_CHUNK]
        data = http.json(
            ULIST_API,
            params={
                "fltt": 2,
                "invt": 2,
                "fields": "f2,f3,f12,f14",
                "secids": ",".join(chunk),
                "ut": UT,
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
        )
        rows = ((data or {}).get("data") or {}).get("diff") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        for row in rows:
            code = str(row.get("f12") or "")
            price = _num(row.get("f2"))
            if not code or price is None or price <= 0:
                continue
            name = str(row.get("f14") or "")
            out[code] = Quote(
                code=code,
                name=name,
                price=price,
                change=_num(row.get("f3")),
                source="eastmoney",
            )
    return out


def _yahoo_quotes(http: Http, tickers: list[Ticker]) -> dict[str, Quote]:
    symbols: list[tuple[str, Ticker]] = []
    for tk in tickers:
        symbol = code_to_yahoo(tk.code, secid=tk.secid)
        if symbol:
            symbols.append((symbol, tk))
    out: dict[str, Quote] = {}
    for i in range(0, len(symbols), YAHOO_CHUNK):
        chunk = symbols[i : i + YAHOO_CHUNK]
        data = http.json(
            YAHOO_QUOTE,
            params={"symbols": ",".join(s for s, _ in chunk)},
            headers={"Referer": "https://finance.yahoo.com/"},
        )
        rows = (((data or {}).get("quoteResponse") or {}).get("result")) or []
        by_symbol = {s: tk for s, tk in chunk}
        for row in rows:
            symbol = str(row.get("symbol") or "")
            tk = by_symbol.get(symbol)
            price = _num(row.get("regularMarketPrice"))
            if tk is None or price is None or price <= 0:
                continue
            name = str(row.get("shortName") or tk.name or "")
            out[tk.code] = Quote(
                code=tk.code,
                name=name,
                price=price,
                change=_num(row.get("regularMarketChangePercent")),
                source="yahoo",
            )
    if not out:
        raise FetchError("Yahoo quote 报价为空")
    return out


def _attach_quote(item: Item, tickers: list[Ticker], quotes: dict[str, Quote]) -> bool:
    """把现价写进条目。实时行情优先，源内 price/涨跌幅做降级。"""
    for tk in tickers:
        quote = quotes.get(tk.code)
        if quote is None:
            continue
        item.last_price = quote.price
        item.price_change = quote.change
        item.price_name = quote.name or tk.name
        item.price_code = quote.code
        return True

    extra = item.extra or {}
    fallback = extra.get("price")
    if isinstance(fallback, (int, float)) and fallback > 0:
        item.last_price = float(fallback)
        chg = extra.get("change_rate", extra.get("increase_rt"))
        item.price_change = _num(chg)
        item.price_name = str(
            extra.get("stock")
            or extra.get("bond_nm")
            or extra.get("stock_nm")
            or (tickers[0].name if tickers else "")
        )
        item.price_code = str(extra.get("code") or extra.get("bond_id") or (tickers[0].code if tickers else ""))
        return True
    return False


def _num(value: object) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 一句总结
# ---------------------------------------------------------------------------
def rule_brief(item: Item) -> str:
    """规则化一句话：优先用源摘要的首句，否则压缩标题。"""
    text = (item.summary or "").strip()
    if text:
        clipped = clip_brief(text)
        if clipped:
            return clipped
    return clip_brief(item.title or "")


def clip_brief(text: str, limit: int = 48) -> str:
    """压成单句、限长。不编造，只裁剪。"""
    text = _WS.sub(" ", (text or "").replace("\n", " ")).strip()
    text = text.strip("\"'“”")
    for sep in ("。", "！", "？", ";", "；"):
        if sep in text:
            text = text.split(sep, 1)[0].strip()
            break
    text = text.strip("，,、 ")
    if len(text) > limit:
        text = text[:limit].rstrip("，,;；、 ") + "…"
    return text


def parse_news_briefs(text: str) -> dict[int, str]:
    """解析「1. xxx」编号列表。容错 1、/ 1: / 【1】 等写法。"""
    line_re = re.compile(
        r"^\s*(?:[#\-*]+\s*)?(?:【\s*)?(\d{1,3})\s*(?:】|[.．、:：)）])\s*(.+?)\s*$"
    )
    out: dict[int, str] = {}
    for raw in (text or "").splitlines():
        match = line_re.match(raw.strip())
        if not match:
            continue
        brief = clip_brief(match.group(2))
        if brief:
            out[int(match.group(1))] = brief
    return out


def _fill_briefs(
    items: list[Item],
    *,
    http: Http | None,
    api_key: str,
    model: str,
) -> None:
    for item in items:
        if not item.ai_brief:
            item.ai_brief = rule_brief(item)
            item.ai_brief_from_model = False

    if not (api_key or "").strip() or http is None:
        return

    from .ai import DeepSeekAI

    client = DeepSeekAI(api_key, model=model or "deepseek-v4-flash", http=http)
    chunk_size = 15
    for start in range(0, len(items), chunk_size):
        chunk = items[start : start + chunk_size]
        entries = [
            (i + 1, it.title, it.summary)
            for i, it in enumerate(chunk)
        ]
        try:
            ok, text = client.summarize_news(entries)
        except Exception as exc:  # noqa: BLE001
            log.info("AI 一句总结调用失败，保留规则化摘要：%s", exc)
            continue
        if not ok:
            log.info("AI 一句总结未成功：%s", text)
            continue
        mapping = parse_news_briefs(text)
        for i, item in enumerate(chunk):
            brief = mapping.get(i + 1, "")
            if not brief or any(w in brief for w in _BANNED_BRIEF):
                continue
            item.ai_brief = clip_brief(brief)
            item.ai_brief_from_model = True
