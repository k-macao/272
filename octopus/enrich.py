"""情报条目增强：补现价、匹配板块概念、检索同题报道/相似观点，再写证券分析。

流程（渲染推送前、不影响时间校验与去重）：

1. 从 extra / 标题 / 摘要里提取 A 股或转债代码（宁可少提，不可臆造）
2. 批量取现价：东财 ulist 优先，失败或漏报的代码再降级 Yahoo Finance
3. 多源检索（crossref）：先在本轮十个源之间匹配同一事件，再对最值得核实的
   若干条做外部新闻/观点检索（Google/Bing News RSS），收集同题报道与带可验证
   时间的相似观点；只有公开标题/摘要时绝不假装读过全文
4. 证券 AI 分析：配置了 DeepSeek 则把「事件 + 行情 + 板块/概念匹配 + 网上
   交叉材料」交给模型，写 ① 开头一句人话结论 ② 板块、概念、相似观点、
   看多/看空概率与传导逻辑；否则或调用失败时使用规则化情景分析，并如实标注，
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
#: 没有同一事件事实报道作印证时，模型不得声称已获多方证实
_OVERCLAIM = ("多方证实", "多家媒体证实", "多源证实", "已获证实", "多家权威媒体")
# 六字段证券分析需要容纳板块、概念、观点、多空概率和完整传导链；仍设硬上限
# 防止单条模型输出失控挤爆微信卡片。
ANALYSIS_LIMIT = 520
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
class MarketContext:
    """由源字段与本地 A 股词典匹配出的行业/概念上下文，不调用大模型猜测。"""

    sector: str = ""
    concepts: tuple[str, ...] = ()


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
    """就地补现价、多源观点、一句人话与证券分析。返回计数，便于日志。"""
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

    # 多源材料：先本轮跨源匹配（离线），再检索同题报道/网上观点（在线、可关）
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
            log.info("外部报道/观点检索本轮熔断：%s", "、".join(search_stats.disabled))
    except Exception as exc:  # noqa: BLE001
        log.info("外部报道/观点检索失败（不影响推送）：%s", exc)

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


def market_context(item: Item) -> MarketContext:
    """用源字段 + 内置成分词典识别行业板块和概念，识别不到就留空。

    词典只用于归类，不代表公司当前仍属于某个动态指数，也不会把结果包装成
    交易所/数据商的实时分类。模型收到的字段会明确标为“本地词典匹配”。
    """
    from .crossref import event_tags
    from .factor.concepts import CONCEPTS, INDUSTRIES

    tickers = extract_tickers(item)
    codes = {ticker.code for ticker in tickers}
    extra = item.extra or {}
    source_industry = str(
        extra.get("industry") or extra.get("industry_name") or extra.get("sector") or ""
    ).strip()
    if source_industry in ("*", "--", "-"):
        source_industry = ""
    tags = [str(tag).strip() for tag in (item.tags or []) if str(tag).strip()]
    blob = " ".join(
        [item.title or "", item.summary or "", source_industry, *tags]
    )

    def membership(table: dict[str, list[tuple[str, str]]], name: str) -> bool:
        return any(code in codes for code, _stock in table.get(name, ()))

    industry_scores: list[tuple[int, int, str]] = []
    for order, name in enumerate(INDUSTRIES):
        score = 0
        if source_industry == name:
            score = 120
        elif name in tags:
            score = 110
        elif name in blob:
            score = 90
        elif membership(INDUSTRIES, name):
            score = 40
        if score:
            industry_scores.append((-score, order, name))
    industry_scores.sort()
    sector = industry_scores[0][2] if industry_scores else source_industry

    concept_scores: list[tuple[int, int, str]] = []
    for order, name in enumerate(CONCEPTS):
        score = 0
        if name in tags:
            score = 120
        elif name in blob:
            score = 100
        elif membership(CONCEPTS, name):
            score = 40
        if score:
            concept_scores.append((-score, order, name))
    concept_scores.sort()
    concepts = tuple(name for _score, _order, name in concept_scores[:3])

    # 宏观新闻没有单一申万行业；明确写成全市场宏观影响，比硬套某一行业诚实。
    events = set(event_tags(f"{item.title} {item.summary}"))
    if not sector and events & {"宏观数据", "货币政策"}:
        sector = "全市场（宏观）"
    return MarketContext(sector=sector, concepts=concepts)


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
    """无大模型时的结构化证券情景分析。

    百分比是透明的事件类型规则权重，不伪装成统计模型；板块/概念来自本地词典，
    网上观点只引用已检索到的公开标题。卡片因此仍完整展示用户要求的六个字段，
    但标签保持「分析」而非「AI 分析」。
    """
    from . import crossref

    context = market_context(item)
    sector = context.sector or "未识别"
    concepts = "、".join(context.concepts) or "未识别"
    views = [rel for rel in item.related if rel.relation == "similar_viewpoint"]
    if views:
        samples = "；".join(
            f"{crossref.describe_related(rel)}：{clip_brief(rel.title, 34)}"
            for rel in views[:3]
        )
        if len({rel.source_label for rel in views}) < 2:
            samples += "；观点样本不足（少于 2 个不同来源）"
        if not any(rel.relation == "same_event" for rel in item.related):
            samples += "；事件事实目前仍仅见原始来源"
        viewpoint = samples
    else:
        viewpoint = crossref.corroboration_summary(item)
        viewpoint += "；未检出可独立比较的网上观点，观点样本不足"

    bull, bear, bull_reason, bear_reason, chain = _rule_scenario(item)
    focus = _rule_focus(item).rstrip("。")
    return (
        f"【板块】{sector}；【概念】{concepts}；"
        f"【相似观点】{viewpoint}；"
        f"【看多】{bull}%：{bull_reason}；"
        f"【看空】{bear}%：{bear_reason}；"
        f"【逻辑】{chain}；验证点：{focus}。"
        f"（规则情景权重，非统计预测）。"
    )


#: 事件类别 -> (看多概率, 看多理由, 看空理由, 传导链)。概率只表示短线事件影响权重。
_RULE_SCENARIOS: tuple[tuple[frozenset[str], int, str, str, str], ...] = (
    (frozenset({"风险警示"}), 15, "若风险处置快于预期，情绪可能短暂修复", "退市或持续经营不确定性会抬高风险折价", "风险警示 → 风险偏好下降 → 个股估值承压"),
    (frozenset({"监管"}), 20, "调查结果若影响有限，不确定性有望收敛", "处罚与合规成本可能影响经营和估值", "监管事件 → 合规与经营不确定性上升 → 风险折价扩大"),
    (frozenset({"问询"}), 35, "充分回复可消除部分信息疑虑", "回复不及预期可能继续压制风险偏好", "交易所问询 → 信息透明度接受检验 → 估值风险重定价"),
    (frozenset({"减持"}), 30, "减持规模较小或提前结束可缓解供给压力", "新增股份供给可能形成阶段性卖压", "减持计划 → 流通筹码增加 → 短线供需承压"),
    (frozenset({"诉讼"}), 30, "涉案影响有限时风险可能逐步出清", "潜在赔付与经营扰动会增加不确定性", "诉讼进展 → 现金流与经营风险变化 → 估值折价调整"),
    (frozenset({"回购"}), 62, "真金白银回购可改善筹码预期并传递管理层信心", "规模、价格上限或执行进度不及预期会削弱信号", "回购计划 → 流通筹码与信心变化 → 个股风险偏好调整"),
    (frozenset({"增持"}), 65, "股东或高管投入资金可增强信心信号", "增持规模偏小或执行不足时象征意义大于实质", "增持计划 → 内部人信号与筹码变化 → 个股预期调整"),
    (frozenset({"分红"}), 60, "现金回报可增强股东回报预期", "盈利或现金流不足会削弱分红持续性", "分红方案 → 现金回报预期变化 → 高股息估值偏好调整"),
    (frozenset({"订单"}), 63, "新增订单可能改善收入可见度", "签约到收入确认仍有执行、毛利和回款风险", "订单落地 → 收入与产能预期变化 → 公司及产业链预期调整"),
    (frozenset({"业绩"}), 50, "若数据高于可比口径或预期，盈利预期可能上修", "若增长质量或持续性不足，估值可能承压", "业绩披露 → 盈利与现金流预期重估 → 个股及板块定价变化"),
    (frozenset({"并购重组"}), 58, "协同与资产注入预期可能提升成长想象空间", "审批、估值、整合和业绩承诺均存在不确定性", "并购方案 → 资产与盈利结构预期变化 → 估值重定价"),
    (frozenset({"再融资"}), 42, "募资投向若回报清晰，可能增强长期产能或现金实力", "股份摊薄与项目回报不确定性可能压制短线估值", "再融资 → 股本与资金用途变化 → 每股收益及估值调整"),
    (frozenset({"货币政策"}), 58, "流动性边际改善通常有利于市场风险偏好", "落地规模不及预期或传导受阻会限制效果", "政策操作 → 资金价格与流动性变化 → 全市场估值偏好调整"),
    (frozenset({"宏观数据"}), 50, "数据改善可能抬升增长和盈利预期", "数据走弱或与预期偏离可能压低风险偏好", "宏观数据 → 增长与政策预期变化 → 行业盈利和估值重估"),
    (frozenset({"涨停", "拉升"}), 57, "强势价格行为显示短线资金关注度较高", "缺少基本面催化时拥挤交易与回撤风险同步上升", "价格异动 → 资金关注与筹码拥挤 → 短线波动放大"),
    (frozenset({"跌停", "回撤"}), 35, "若无新增基本面利空，超跌后可能出现情绪修复", "弱势价格行为可能反映资金撤离或未披露风险", "价格异动 → 风险偏好与筹码供需恶化 → 短线波动放大"),
)


def _rule_scenario(item: Item) -> tuple[int, int, str, str, str]:
    from .crossref import event_tags

    tags = set(event_tags(f"{item.title} {item.summary}"))
    for group, bull, bull_reason, bear_reason, chain in _RULE_SCENARIOS:
        if tags & group:
            return bull, 100 - bull, bull_reason, bear_reason, chain
    return (
        50,
        50,
        "现有事实尚不足以确认正向盈利或供需变化",
        "信息未经充分交叉验证，仍有口径与后续进展风险",
        "新闻披露 → 市场预期变化 → 等待经营数据或官方信息验证",
    )


#: 事件类别 -> 一句人话（投资专家口吻，但不做操作建议）。
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


_SECURITY_FIELDS = ("【板块】", "【概念】", "【相似观点】", "【看多】", "【看空】", "【逻辑】")


def _probabilities_valid(text: str) -> bool:
    """新六字段格式必须字段齐全、多空合计 100；无字段的历史格式继续兼容。"""
    present = [field in text for field in _SECURITY_FIELDS]
    if not any(present):
        return True
    if not all(present):
        return False
    bull = re.search(r"【看多】\s*(\d{1,3})\s*[%％]", text)
    bear = re.search(r"【看空】\s*(\d{1,3})\s*[%％]", text)
    if bull is None or bear is None:
        return False
    values = int(bull.group(1)), int(bear.group(1))
    return all(0 <= value <= 100 for value in values) and sum(values) == 100


def _acceptable(item: Item, text: str) -> bool:
    """一句话与分析共用的合规/概率红线。

    荐股类措辞一律拒收；没有同一事件的其它事实报道却声称「已获多方证实」也拒收；
    （相似观点和同标的消息不能冒充事实印证。）新格式若六字段不完整，或多空
    概率不合计 100，同样拒收。回退时保留规则化文本，
    宁可朴素也不越线。
    """
    if not text or any(w in text for w in _BANNED_ANALYSIS):
        return False
    if not _probabilities_valid(text):
        return False
    fact_corroborated = any(rel.relation == "same_event" for rel in item.related)
    return fact_corroborated or not any(w in text for w in _OVERCLAIM)


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

    from .ai import DeepSeekAI, NewsAnalysisEntry

    client = DeepSeekAI(api_key, model=model or "deepseek-v4-flash", http=http)
    # 每条会携带行情、板块概念和最多六条网上材料，输出也扩展为六字段；继续分块
    # 并发，既控制单次上下文，又不让全量情报串行等待。
    chunk_size = AI_CHUNK_SIZE
    chunks = [items[start : start + chunk_size] for start in range(0, len(items), chunk_size)]

    def run_chunk(chunk: list[Item]) -> None:
        entries: list[NewsAnalysisEntry] = []
        for i, it in enumerate(chunk):
            materials: list[str] = []
            for rel in it.related[:6]:
                when = f"（{rel.published_at:%m-%d %H:%M}）" if rel.published_at else ""
                kind = {
                    "same_event": "同一事件报道",
                    "similar_viewpoint": "网上相似观点",
                    "same_subject": "同标的消息，未必同一事件",
                }.get(rel.relation, "待核对材料")
                similarity = (
                    f"，规则相关度 {rel.similarity:.0%}" if rel.similarity is not None else ""
                )
                material = (
                    f"[{kind}{similarity}] {crossref.describe_related(rel)}：{rel.title}{when}"
                )
                if rel.summary:
                    material += f"；搜索公开摘要：{rel.summary}"
                materials.append(material)

            context = market_context(it)
            market_data = ""
            if it.last_price is not None:
                name_code = " ".join(
                    part for part in ((it.price_name or "").strip(), (it.price_code or "").strip())
                    if part
                )
                change = "" if it.price_change is None else f"，涨跌幅 {it.price_change:+.2f}%"
                market_data = f"{name_code + ' ' if name_code else ''}现价 {it.last_price:.2f}{change}"
            entries.append(
                NewsAnalysisEntry(
                    number=i + 1,
                    source=it.source_label,
                    title=it.title,
                    summary=it.summary,
                    related=tuple(materials),
                    sector=context.sector,
                    concepts=context.concepts,
                    market_data=market_data,
                    tags=tuple(str(tag) for tag in it.tags[:6]),
                )
            )
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
