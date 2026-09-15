"""情报条目增强：给每条新闻补上现价、找同一新闻的不同源头，再写 AI 分析。

流程（渲染推送前、不影响时间校验与去重）：

1. 从 extra / 标题 / 摘要里提取 A 股或转债代码（宁可少提，不可臆造）
2. 批量取现价：东财 ulist 优先，失败或漏报的代码再降级 Yahoo Finance
3. 多源印证（crossref）：先在本轮十个源之间互相匹配同一事件，再对最值得
   核实的若干条做外部新闻检索（Google/Bing News RSS，仅保留带可验证发布
   时间且标题对得上的结果）
4. AI 分析：配置了 DeepSeek 则把「本条 + 其它源头报道」交给模型，写
   ① 开头一句「人话」结论（投资专家口吻、大白话、不做方向判断）
   ② 事件要点 / 多源印证 / 关注点；否则或调用失败时用规则化文本，并如实标注，
   绝不假装用了 AI

任何一步失败只让该条缺少现价/印证/改用规则分析，不会丢条目、不会拖垮整轮推送。
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

#: 模型输出里的段落标记：「一句话：…」+「分析：…」。分析段有时直接写「事件要点：」，
#: 两种都要能切开；模型没写「一句话」时按老格式整段当分析处理。
_HEADLINE_MARK = re.compile(
    r"^(?:【\s*)?(?:一句话点评|投资专家一句话|一句人话|一句话|人话|白话|专家点评|点评|结论)"
    r"(?:\s*】)?\s*[:：]\s*"
)
_ANALYSIS_MARK = re.compile(r"(?:AI\s*)?分析\s*[:：]\s*|事件要点\s*[:：]\s*")
_SENTENCE_END = re.compile(r"[。！？!?]")

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

_BANNED_ANALYSIS = ("买入", "卖出", "目标价", "立即建仓", "稳赚", "必涨", "马上买", "建议加仓", "建议减仓")
#: 单一来源的条目，模型不得声称已获多方证实
_OVERCLAIM = ("多方证实", "多家媒体证实", "多源证实", "已获证实", "多家权威媒体")
ANALYSIS_LIMIT = 160
HEADLINE_LIMIT = 48
AI_CHUNK_SIZE = 6
AI_WORKERS = 3

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


@dataclass(frozen=True)
class NewsReport:
    """模型对一条情报的两段产出：开头的一句人话 + 详细分析。"""

    headline: str = ""
    analysis: str = ""


def enrich_news(
    items: Iterable[Item],
    *,
    http: Http | None = None,
    api_key: str = "",
    model: str = "deepseek-v4-flash",
    crossref_mode: str = "auto",
    crossref_max_items: int = 12,
    crossref_timeout: float = 6.0,
    crossref_max_gap_hours: float = 36.0,
    ref=None,
) -> dict[str, int]:
    """就地给条目补现价、多源印证、一句人话与 AI 分析。返回计数，便于日志。"""
    bag = [it for it in items if it is not None]
    stats = {
        "items": len(bag), "quotes": 0, "ai": 0, "rule": 0,
        "headline_ai": 0, "headline_rule": 0,
        "linked": 0, "searched": 0, "external_hits": 0,
    }
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

    # 多源印证：先本轮跨源匹配（离线），再外部检索（在线、可关）
    from . import crossref

    try:
        stats["linked"] = crossref.link_batch(bag)
    except Exception as exc:  # noqa: BLE001 - 印证失败不影响推送
        log.info("本轮跨源匹配失败（不影响推送）：%s", exc)
    try:
        search_stats = crossref.search_external(
            bag,
            http=http,
            mode=crossref_mode,
            max_items=crossref_max_items,
            timeout=crossref_timeout,
            max_gap_hours=crossref_max_gap_hours,
            ref=ref,
        )
        stats["searched"] = search_stats.searched
        stats["external_hits"] = search_stats.hits
        if search_stats.disabled:
            log.info("外部新闻检索本轮熔断：%s", "、".join(search_stats.disabled))
    except Exception as exc:  # noqa: BLE001
        log.info("外部新闻检索失败（不影响推送）：%s", exc)

    _fill_analysis(bag, http=http, api_key=api_key, model=model)
    stats["ai"] = sum(1 for it in bag if it.ai_analysis_from_model)
    stats["rule"] = sum(1 for it in bag if it.ai_analysis and not it.ai_analysis_from_model)
    stats["headline_ai"] = sum(1 for it in bag if it.ai_headline_from_model)
    stats["headline_rule"] = sum(
        1 for it in bag if it.ai_headline and not it.ai_headline_from_model
    )
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
# AI 分析（DeepSeek）/ 规则化分析（降级）
# ---------------------------------------------------------------------------
def rule_analysis(item: Item) -> str:
    """规则化分析：多源印证情况 + 关注点。

    不调用大模型，只把已知事实组织成一段话，卡片上标「分析」而非「AI」。
    标题与源摘要已经在卡片上，这里不再复述，只补「其它源头怎么说」和「该核对什么」。
    """
    from . import crossref

    corroboration = crossref.corroboration_summary(item)
    focus = _rule_focus(item)
    return "".join(p for p in (corroboration + "。", focus) if p)


#: 事件类别 -> 一句人话（投资专家口吻，但不做方向判断、不给建议）。
#: 未配置大模型或调用失败时用它兜底，卡片上标「一句话」而不是「AI 一句话」。
RULE_HEADLINES: tuple[tuple[frozenset[str], str], ...] = (
    (
        frozenset({"监管", "风险警示"}),
        "监管已经盯上这家公司，处罚或退市风险要按公告口径算清，别只看股价反应。",
    ),
    (frozenset({"问询"}), "交易所发函等于要公司把话说清楚，回复公告才是重点。"),
    (frozenset({"回购"}), "公司自己掏真金白银买回股份，能买多少、买多久要看公告条款。"),
    (frozenset({"增持"}), "股东或高管自己掏钱加仓，态度比研报实在，规模和期限决定分量。"),
    (frozenset({"减持"}), "股东要减持或限售股解禁，短期多了一股卖压，量级和节奏是关键。"),
    (frozenset({"分红"}), "现金分红是把利润真发到手上，能不能持续要看盈利和现金流。"),
    (frozenset({"业绩"}), "业绩数字最硬，但得跟上一期和市场预期比，才知道算好还是算坏。"),
    (frozenset({"订单"}), "拿到订单说明有生意，但从签约到确认收入还有距离，金额口径看公告。"),
    (frozenset({"再融资"}), "再融资是向市场要钱，摊薄多少、钱投到哪里，是两笔要分开算的账。"),
    (
        frozenset({"并购重组"}),
        "并购重组改的是公司结构，成不成、溢价多少还要过监管和股东这一关。",
    ),
    (frozenset({"转债"}), "转债跟着正股走，还要盯转股价和赎回条款，两头都可能变。"),
    (frozenset({"评级"}), "研报观点只代表那一家机构，先看它的假设和数据口径。"),
    (frozenset({"宏观数据"}), "宏观数据是全局变量，关键看实际值和市场原先的预期差多少。"),
    (frozenset({"货币政策"}), "资金面松紧直接影响估值，看落地规模和后续操作能不能接上。"),
    (
        frozenset({"涨停", "跌停", "异动", "拉升", "回撤"}),
        "盘中涨跌是结果不是原因，得找到对应的消息或公告才解释得通。",
    ),
    (frozenset({"资金"}), "资金流向只说明当天谁在买卖，一天的数字还谈不上趋势。"),
    (frozenset({"人事"}), "换人改变的是预期，真正的变化还得等经营数据说话。"),
    (frozenset({"诉讼"}), "诉讼没判决前都是不确定项，先看涉案金额占公司体量多少。"),
    (frozenset({"停复牌"}), "停牌期间价格会一次性重新定价，复牌前后波动通常被放大。"),
    (frozenset({"上市"}), "发行与上市影响的是供给和情绪，公司基本面还得单独看。"),
)

_RULE_HEADLINE_FALLBACK = "先把这条消息说了什么、由谁披露看清楚，再判断它有多重。"


def rule_headline(item: Item) -> str:
    """规则化的一句人话：不调用大模型，只按事件类型给出中性的大白话结论。"""
    from .crossref import event_tags

    tags = set(event_tags(f"{item.title} {item.summary}"))
    for group, text in RULE_HEADLINES:
        if tags & group:
            return text
    return _RULE_HEADLINE_FALLBACK


def _rule_focus(item: Item) -> str:
    from .crossref import event_tags

    tags = set(event_tags(f"{item.title} {item.summary}"))
    kind = str((item.extra or {}).get("kind") or "")
    if tags & {"监管", "问询", "风险警示"}:
        return "关注监管口径与公司后续回复公告。"
    if tags & {"回购", "增持", "减持", "分红", "再融资", "并购重组", "停复牌", "人事", "诉讼"}:
        return "以交易所披露的公告原文为准，留意后续进展公告。"
    if tags & {"业绩", "订单"}:
        return "关注定期报告与公告口径是否一致。"
    if tags & {"宏观数据", "货币政策"}:
        return "以统计局/央行正式发布口径为准。"
    if tags & {"涨停", "跌停", "异动", "拉升", "回撤"} or kind in ("盘中异动", "涨停", "快讯"):
        return "盘中信号时效性强，留意是否有对应公告或消息面解释。"
    if tags & {"转债"}:
        return "留意转债条款公告与正股走势的对应关系。"
    if tags & {"评级"} or kind in ("研报", "投研", "机构预期", "宏观研报", "行业研报"):
        return "研报观点仅代表发布机构，留意其数据口径与假设。"
    return "留意后续是否有官方渠道的进一步披露。"


def clip_headline(text: str, limit: int = HEADLINE_LIMIT) -> str:
    """整理模型输出的「一句人话」：取第一句、去掉标记与引号，超长省略。"""
    text = _WS.sub(" ", (text or "").replace("\n", " ")).strip()
    text = _HEADLINE_MARK.sub("", text.strip("\"'“”"), count=1).strip()
    end = _SENTENCE_END.search(text)
    if end:
        text = text[: end.end()].strip()
    if len(text) > limit:
        return text[:limit].rstrip("，,;；、 ") + "…"
    if text and text[-1] not in "。！？!?…":
        return text + "。"
    return text


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


def clip_analysis(text: str, limit: int = ANALYSIS_LIMIT) -> str:
    """整理模型输出的一段分析：合并空白、去掉编号残留，超长在句号处截断。"""
    text = _WS.sub(" ", (text or "").replace("\n", " ")).strip()
    text = text.strip("\"'“”")
    text = re.sub(r"^(?:分析|AI分析|AI 分析)[:：]\s*", "", text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("。", "；", ";", "！", "？"):
        idx = cut.rfind(sep)
        if idx >= limit // 2:
            return cut[: idx + 1]
    return cut.rstrip("，,;；、 ") + "…"


def _group_numbered(text: str) -> dict[int, str]:
    """把「1. xxx」编号列表拆成 {序号: 原文}。

    容错 1、/ 1: / 【1】 / 1) 等写法，且允许一条内容跨多行（模型常把
    「一句话」与「分析」分成两行写）。
    """
    line_re = re.compile(
        r"^\s*(?:[#\-*]+\s*)?(?:【\s*)?(\d{1,3})\s*(?:】|[.．、:：)）])\s*(.*?)\s*$"
    )
    out: dict[int, list[str]] = {}
    current: int | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = line_re.match(line)
        if match:
            current = int(match.group(1))
            out.setdefault(current, [])
            if match.group(2):
                out[current].append(match.group(2))
            continue
        if current is not None:
            out[current].append(line)
    return {
        num: _WS.sub(" ", " ".join(parts)).strip() for num, parts in out.items() if parts
    }


def split_headline_and_analysis(raw: str) -> tuple[str, str]:
    """把一条模型产出拆成（一句话, 分析）。

    模型没写「一句话」标记时，一句话为空串，由规则化一句话兜底，
    整段仍按分析处理 —— 老格式的输出不会因为新增字段而失效。
    """
    text = _WS.sub(" ", (raw or "").replace("\n", " ")).strip()
    if not _HEADLINE_MARK.match(text):
        return "", clip_analysis(text)
    rest = _HEADLINE_MARK.sub("", text, count=1)
    marker = _ANALYSIS_MARK.search(rest)
    if marker is None:
        return clip_headline(rest), ""
    return clip_headline(rest[: marker.start()]), clip_analysis(rest[marker.end() :])


def parse_news_reports(text: str) -> dict[int, NewsReport]:
    """解析「1. 一句话：… / 分析：…」编号列表，按编号返回一句话与分析。"""
    result: dict[int, NewsReport] = {}
    for num, raw in _group_numbered(text).items():
        headline, analysis = split_headline_and_analysis(raw)
        if headline or analysis:
            result[num] = NewsReport(headline=headline, analysis=analysis)
    return result


def parse_news_analyses(text: str) -> dict[int, str]:
    """解析「1. xxx」编号列表里的分析部分（一句话见 parse_news_reports）。"""
    return {
        num: report.analysis
        for num, report in parse_news_reports(text).items()
        if report.analysis
    }


def _acceptable(item: Item, text: str) -> bool:
    """一句话与分析共用的合规红线。

    荐股类措辞一律拒收；没有任何其它源头却声称「已获多方证实」的也拒收 ——
    拒收后保留规则化文本，宁可朴素也不越线。
    """
    if not text or any(w in text for w in _BANNED_ANALYSIS):
        return False
    return bool(item.related) or not any(w in text for w in _OVERCLAIM)


def _fill_analysis(
    items: list[Item],
    *,
    http: Http | None,
    api_key: str,
    model: str,
) -> None:
    """先铺规则化兜底（一句话 + 分析），再用大模型覆盖能覆盖的部分。

    两段各自过同一套合规红线；被拒收的那段保留规则化文本，不假装是 AI。
    """
    from . import crossref

    for item in items:
        if not item.ai_analysis:
            item.ai_analysis = rule_analysis(item)
            item.ai_analysis_from_model = False
        if not item.ai_headline:
            item.ai_headline = rule_headline(item)
            item.ai_headline_from_model = False

    if not (api_key or "").strip() or http is None:
        return

    from concurrent.futures import ThreadPoolExecutor

    from .ai import DeepSeekAI

    client = DeepSeekAI(api_key, model=model or "deepseek-v4-flash", http=http)
    # 每条现在带着「其它源头报道」进 prompt，单次输入/输出都比以前的一句总结长，
    # 所以块切小一点；块与块之间并发，整体耗时不比以前差。
    chunk_size = AI_CHUNK_SIZE
    chunks = [items[start : start + chunk_size] for start in range(0, len(items), chunk_size)]

    def run_chunk(chunk: list[Item]) -> None:
        entries = []
        for i, it in enumerate(chunk):
            others = []
            for rel in it.related[:4]:
                when = f"（{rel.published_at:%m-%d %H:%M}）" if rel.published_at else ""
                flag = "" if rel.relation == "same_event" else "[同标的，未必同一事件]"
                others.append(f"{crossref.describe_related(rel)}：{rel.title}{when}{flag}")
            entries.append((i + 1, it.source_label, it.title, it.summary, others))
        try:
            ok, text = client.analyze_news(entries)
        except Exception as exc:  # noqa: BLE001
            log.info("AI 分析调用失败，保留规则化分析：%s", exc)
            return
        if not ok:
            log.info("AI 分析未成功：%s", text)
            return
        mapping = parse_news_reports(text)
        for i, item in enumerate(chunk):
            report = mapping.get(i + 1)
            if report is None:
                continue
            if _acceptable(item, report.analysis):
                item.ai_analysis = report.analysis
                item.ai_analysis_from_model = True
            if _acceptable(item, report.headline):
                item.ai_headline = report.headline
                item.ai_headline_from_model = True

    if len(chunks) == 1:
        run_chunk(chunks[0])
        return
    with ThreadPoolExecutor(max_workers=min(AI_WORKERS, len(chunks))) as pool:
        list(pool.map(run_chunk, chunks))
