"""多源印证：给每条情报寻找「同一新闻在其它源头的报道」。

AI 分析不能只看一条标题就下评论 —— 先找到同一事件的不同源头，再评论。
分两步，任何一步失败都只让该条少几条印证，不丢条目、不拖垮推送：

1. **本轮跨源匹配（离线）**：十个抓取源同一轮抓回来的条目里，找出报道
   同一事件的其它源（例如巨潮的回购公告 ↔ 证券之星的异动快报 ↔ 东财快讯）。
   判定依据是「共同标的 + 共同事件词」或标题字符二元组高度重合，不用大模型。
2. **外部新闻检索（在线，可关）**：用 Google News / Bing News 的 RSS 检索
   同一事件在证券时报、财联社、新浪财经等媒体上的报道。检索结果同样过时间
   校验：没有发布时间、时间在未来、与原条目间隔过久的一律丢弃；标题与原
   条目对不上的也丢弃（搜索引擎的噪音不算印证）。

原则与主流程一致：**宁可少列，不可凑数**。找不到就如实标「单一来源」。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable
from urllib.parse import quote_plus, urlsplit

from .http import FetchError, Http
from .models import Item, RelatedNews
from .timeutil import is_future, now, parse

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 事件词典：同一事件在不同源头的措辞不同，但事件类别是稳定的
# ---------------------------------------------------------------------------
EVENT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("回购", ("回购",)),
    ("增持", ("增持",)),
    ("减持", ("减持", "解禁", "限售股上市")),
    ("分红", ("分红", "派息", "利润分配", "现金红利", "转增")),
    ("业绩", ("业绩预告", "业绩快报", "预增", "预减", "预盈", "预亏", "净利润", "营收", "年报", "半年报", "季报")),
    ("订单", ("中标", "签订", "合同", "订单", "框架协议", "战略合作")),
    ("涨停", ("涨停", "封板", "封死", "连板", "一字板")),
    ("跌停", ("跌停",)),
    ("异动", ("异动", "异常波动", "大宗交易", "龙虎榜")),
    ("停复牌", ("停牌", "复牌")),
    ("问询", ("问询函", "关注函", "监管函", "监管工作函")),
    ("监管", ("立案", "调查", "处罚", "警示函", "责令改正", "纪律处分", "公开谴责", "市场禁入")),
    ("再融资", ("定增", "定向增发", "非公开发行", "配股", "可转债发行", "发行股份")),
    ("并购重组", ("并购", "重组", "收购", "出售资产", "股权转让", "控制权", "要约收购")),
    ("上市", ("IPO", "首发", "上市首日", "申购", "过会", "注册生效", "招股")),
    ("转债", ("强赎", "赎回", "转股价", "下修", "不下修")),
    ("评级", ("评级", "首次覆盖", "目标价", "上调", "下调", "维持")),
    ("货币政策", ("降息", "降准", "LPR", "MLF", "逆回购", "公开市场")),
    ("宏观数据", ("PMI", "CPI", "PPI", "GDP", "社融", "M2", "采购经理指数", "居民消费价格", "工业增加值", "固定资产投资", "社会消费品零售", "工业企业利润", "进出口")),
    ("拉升", ("拉升", "走强", "领涨", "大涨", "涨超", "冲高", "创新高", "翻红")),
    ("回撤", ("跳水", "大跌", "跌超", "走弱", "领跌", "创新低")),
    ("资金", ("北向", "外资", "主力资金", "净流入", "净流出", "融资余额")),
    ("人事", ("辞职", "聘任", "离任", "董事长", "总经理变更", "高管变动")),
    ("诉讼", ("诉讼", "仲裁", "起诉")),
    ("风险警示", ("ST", "退市", "风险警示", "终止上市")),
)

#: 快照类条目（榜单/热力图）不是「新闻」，既不当匹配对象也不做外部检索
SNAPSHOT_KINDS: frozenset[str] = frozenset({"板块热力", "北向资金", "热度榜", "双低榜"})

#: 外部检索的引擎
ENGINES: tuple[str, ...] = ("google", "bing")
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
BING_NEWS_RSS = "https://www.bing.com/news/search"
ENGINE_LABELS = {"google": "Google News", "bing": "Bing News"}

#: 判定阈值。overlap = 共同字符二元组 / 较短标题的二元组数（对长短差异宽容）
SAME_EVENT_OVERLAP = 0.45      # 无共同标的时，仅凭标题重合判定同一事件
TICKER_EVENT_OVERLAP = 0.30    # 有共同标的时，标题重合到这个程度也算同一事件
EXTERNAL_OVERLAP = 0.40        # 外部检索结果与原条目的标题重合下限
MIN_SHARED_BIGRAMS = 3
MAX_RELATED_PER_ITEM = 4
MAX_SUBJECT_GAP = timedelta(hours=3)

_PUNCT = re.compile(r"[\s\u3000，,。．.、；;：:！!？?“”\"'‘’（）()【】\[\]《》<>〈〉「」『』—\-–_|｜/\\·•…~～*#@&%+=]+")
_CODE_IN_TEXT = re.compile(r"(?<!\d)\d{6}(?:\.(?:SH|SZ|SS|BJ))?(?!\d)", re.I)
_TITLE_TAIL_SOURCE = re.compile(r"\s+[-–—|｜]\s+([^-–—|｜]{2,24})$")
_XML_TAG = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# 文本特征
# ---------------------------------------------------------------------------
def event_tags(text: str) -> list[str]:
    """标题/摘要里出现的事件类别（去重、保持词典顺序）。"""
    blob = text or ""
    found: list[str] = []
    for label, words in EVENT_GROUPS:
        if any(w in blob for w in words):
            found.append(label)
    return found


#: 同一指标的官方全称与媒体简称 —— 统计局说「居民消费价格」，媒体说「CPI」，
#: 比对前先归一，否则同一份数据在两边永远对不上。
_ALIASES: tuple[tuple[str, str], ...] = (
    ("居民消费价格指数", "cpi"),
    ("居民消费价格", "cpi"),
    ("工业生产者出厂价格指数", "ppi"),
    ("工业生产者出厂价格", "ppi"),
    ("制造业采购经理指数", "pmi"),
    ("采购经理指数", "pmi"),
    ("国内生产总值", "gdp"),
    ("社会融资规模", "社融"),
    ("广义货币", "m2"),
    ("中期借贷便利", "mlf"),
    ("贷款市场报价利率", "lpr"),
    ("社会消费品零售总额", "社零"),
    ("规模以上工业增加值", "工业增加值"),
    ("月份", "月"),
    ("年份", "年"),
)


def normalize_title(text: str) -> str:
    """去掉标点/空白/来源尾巴，指标别名归一，小写。用于比对与去重。"""
    text = _TITLE_TAIL_SOURCE.sub("", (text or "").strip())
    text = _XML_TAG.sub("", text)
    text = _PUNCT.sub("", text).lower()
    for long, short in _ALIASES:
        text = text.replace(long, short)
    return text


def bigrams(text: str) -> set[str]:
    norm = normalize_title(text)
    if len(norm) < 2:
        return {norm} if norm else set()
    return {norm[i : i + 2] for i in range(len(norm) - 1)}


def title_overlap(a: str, b: str) -> tuple[float, int]:
    """返回 (重合系数, 共同二元组数)。重合系数 = 共同数 / 较短一方的二元组数。"""
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 0.0, 0
    shared = len(ga & gb)
    return shared / min(len(ga), len(gb)), shared


@dataclass
class Signature:
    """一条情报的匹配特征。"""

    codes: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    events: list[str] = field(default_factory=list)
    kind: str = ""

    @property
    def snapshot(self) -> bool:
        return self.kind in SNAPSHOT_KINDS

    @property
    def subjects(self) -> set[str]:
        return self.codes | self.names


def signature(item: Item) -> Signature:
    from .enrich import extract_tickers

    extra = item.extra or {}
    sig = Signature(kind=str(extra.get("kind") or ""))
    for tk in extract_tickers(item):
        sig.codes.add(tk.code)
        if tk.name and len(tk.name) >= 2:
            sig.names.add(tk.name)
    for key in ("stock", "stock_nm", "bond_nm"):
        name = str(extra.get(key) or "").strip()
        if len(name) >= 2:
            sig.names.add(name)
    if item.price_name and len(item.price_name) >= 2:
        sig.names.add(item.price_name)
    sig.events = event_tags(f"{item.title} {item.summary}")
    return sig


# ---------------------------------------------------------------------------
# 第一步：本轮跨源匹配（离线）
# ---------------------------------------------------------------------------
def link_batch(items: list[Item]) -> int:
    """在同一轮的条目之间互相寻找「同一事件的其它源头」。返回建立了印证的条目数。"""
    sigs = [signature(it) for it in items]
    linked = 0
    for i, item in enumerate(items):
        if sigs[i].snapshot:
            continue
        found: list[tuple[int, RelatedNews]] = []
        for j, other in enumerate(items):
            if i == j or other.source == item.source or sigs[j].snapshot:
                continue
            relation = classify_pair(item, sigs[i], other, sigs[j])
            if relation is None:
                continue
            rank = 0 if relation == "same_event" else 1
            found.append(
                (
                    rank,
                    RelatedNews(
                        source_label=other.source_label,
                        title=other.title,
                        url=other.url,
                        published_at=other.published_at,
                        relation=relation,
                        via="batch",
                    ),
                )
            )
        if found:
            found.sort(key=lambda pair: (pair[0], -(pair[1].published_at.timestamp() if pair[1].published_at else 0)))
            _merge_related(item, [r for _, r in found])
            linked += 1
    return linked


def classify_pair(a: Item, sa: Signature, b: Item, sb: Signature) -> str | None:
    """两条不同源的条目是否报道同一事件。返回 same_event / same_subject / None。

    共享标的时，标题重合度只看**去掉标的名/代码之后的剩余部分** ——
    「宁德时代(300750) 回购公告」与「宁德时代(300750) 触及涨停」光凭名字+代码
    就能重合一大截，不能因此算成同一事件。
    """
    shared_subject = bool(sa.subjects & sb.subjects)
    shared_event = bool(set(sa.events) & set(sb.events))

    if shared_subject:
        if shared_event:
            return "same_event"
        subjects = sa.subjects | sb.subjects
        overlap, shared = title_overlap(strip_subjects(a.title, subjects), strip_subjects(b.title, subjects))
        if overlap >= TICKER_EVENT_OVERLAP and shared >= MIN_SHARED_BIGRAMS:
            return "same_event"
        if _close_in_time(a, b):
            return "same_subject"
        return None

    overlap, shared = title_overlap(a.title, b.title)
    if overlap >= SAME_EVENT_OVERLAP and shared >= max(MIN_SHARED_BIGRAMS, 4):
        return "same_event"
    return None


def strip_subjects(title: str, subjects: Iterable[str]) -> str:
    """去掉标题里的标的名与代码，只留事件描述。"""
    text = title or ""
    for subject in sorted({s for s in subjects if s}, key=len, reverse=True):
        text = text.replace(subject, " ")
    text = _CODE_IN_TEXT.sub(" ", text)
    return text


def _close_in_time(a: Item, b: Item) -> bool:
    if a.published_at is None or b.published_at is None:
        return False
    return abs(a.published_at - b.published_at) <= MAX_SUBJECT_GAP


def _merge_related(item: Item, incoming: Iterable[RelatedNews]) -> None:
    """去重合并，同一事件排前面，总数封顶。"""
    seen_urls = {r.url for r in item.related if r.url}
    seen_titles = {normalize_title(r.title) for r in item.related}
    for rel in incoming:
        key = normalize_title(rel.title)
        if (rel.url and rel.url in seen_urls) or key in seen_titles:
            continue
        item.related.append(rel)
        if rel.url:
            seen_urls.add(rel.url)
        seen_titles.add(key)
    item.related.sort(key=lambda r: (0 if r.relation == "same_event" else 1, 0 if r.via == "batch" else 1))
    del item.related[MAX_RELATED_PER_ITEM:]


# ---------------------------------------------------------------------------
# 第二步：外部新闻检索（在线）
# ---------------------------------------------------------------------------
@dataclass
class SearchStats:
    searched: int = 0
    hits: int = 0
    items_with_hits: int = 0
    engine_errors: dict[str, int] = field(default_factory=dict)
    disabled: list[str] = field(default_factory=list)

    def line(self) -> str:
        bits = [f"外部检索 {self.searched} 条", f"命中 {self.hits} 篇/{self.items_with_hits} 条"]
        if self.disabled:
            bits.append("已熔断 " + "、".join(ENGINE_LABELS.get(e, e) for e in self.disabled))
        return "，".join(bits)


class _Breaker:
    """按引擎熔断：连续两次网络失败就认为本轮该引擎不可达，不再浪费时间。"""

    def __init__(self, enabled: bool, threshold: int = 2) -> None:
        self.enabled = enabled
        self.threshold = threshold
        self._fails: dict[str, int] = {}
        self._open: set[str] = set()
        self._lock = threading.Lock()

    def allow(self, engine: str) -> bool:
        with self._lock:
            return engine not in self._open

    def ok(self, engine: str) -> None:
        with self._lock:
            self._fails[engine] = 0

    def fail(self, engine: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._fails[engine] = self._fails.get(engine, 0) + 1
            if self._fails[engine] >= self.threshold:
                self._open.add(engine)

    @property
    def open(self) -> list[str]:
        with self._lock:
            return sorted(self._open)


def search_external(
    items: list[Item],
    *,
    http: Http | None,
    mode: str = "auto",
    max_items: int = 12,
    timeout: float = 6.0,
    max_gap_hours: float = 36.0,
    engines: Iterable[str] = ENGINES,
    ref: datetime | None = None,
    workers: int = 4,
    budget_seconds: float = 40.0,
) -> SearchStats:
    """对最值得印证的若干条做外部检索，把结果挂到 item.related。"""
    stats = SearchStats()
    mode = (mode or "auto").strip().lower()
    engines = [e for e in engines if e in ENGINES]
    if http is None or mode in ("off", "none", "0", "false") or not engines or max_items <= 0:
        return stats

    ref = ref or now()
    candidates = pick_candidates(items, max_items)
    if not candidates:
        return stats

    breaker = _Breaker(enabled=(mode != "on"))
    deadline = time.monotonic() + budget_seconds
    stats_lock = threading.Lock()

    def work(item: Item) -> tuple[Item, list[RelatedNews], bool]:
        query = build_query(item)
        if not query:
            return item, [], False
        attempted = False
        found: list[RelatedNews] = []
        for engine in engines:
            if time.monotonic() > deadline or not breaker.allow(engine):
                continue
            attempted = True
            try:
                rows = fetch_engine(http, engine, query, timeout=timeout)
            except FetchError as exc:
                breaker.fail(engine)
                with stats_lock:
                    stats.engine_errors[engine] = stats.engine_errors.get(engine, 0) + 1
                log.debug("[crossref] %s 检索失败（%s）：%s", engine, query, exc)
                continue
            breaker.ok(engine)
            found.extend(filter_results(item, rows, engine, ref=ref, max_gap_hours=max_gap_hours))
        return item, found, attempted

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(candidates)))) as pool:
        futures = [pool.submit(work, it) for it in candidates]
        for future in as_completed(futures):
            try:
                item, found, attempted = future.result()
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它
                log.debug("[crossref] 检索线程异常：%s", exc)
                continue
            if attempted:
                item.related_searched = True
                stats.searched += 1
            if found:
                before = len(item.related)
                _merge_related(item, found)
                added = len(item.related) - before
                if added:
                    stats.hits += added
                    stats.items_with_hits += 1
    stats.disabled = breaker.open
    return stats


def pick_candidates(items: list[Item], max_items: int) -> list[Item]:
    """挑最值得花网络请求去印证的条目：有标的、有事件词、且本轮尚无其它源印证的优先。"""
    scored: list[tuple[int, int, Item]] = []
    for idx, item in enumerate(items):
        sig = signature(item)
        if sig.snapshot or not (item.title or "").strip():
            continue
        score = 0
        if sig.subjects:
            score += 2
        if sig.events:
            score += 1
        if not any(r.relation == "same_event" for r in item.related):
            score += 2
        if sig.kind in ("公告", "快讯", "盘中异动", "涨停", "数据发布", "数据解读"):
            score += 1
        scored.append((-score, idx, item))
    scored.sort()
    return [it for _, _, it in scored[:max_items]]


def build_query(item: Item) -> str:
    """检索词：标的名 + 事件词 最稳；否则用清洗后的标题。"""
    sig = signature(item)
    name = ""
    for cand in sorted(sig.names, key=len, reverse=True):
        if 2 <= len(cand) <= 8 and not cand.isdigit():
            name = cand
            break
    events = [e for e in sig.events if e not in ("拉升", "回撤", "资金", "评级")]
    if name and events:
        return f"{name} {events[0]}"
    if name and sig.events:
        return f"{name} {sig.events[0]}"
    title = _CODE_IN_TEXT.sub(" ", item.title or "")
    title = re.sub(r"[（(][^）)]{0,12}[）)]", " ", title)
    title = title.split("｜", 1)[-1]
    title = _PUNCT.sub(" ", title).strip()
    title = re.sub(r"\s+", " ", title)
    if name and name not in title:
        title = f"{name} {title}"
    return title[:30].strip()


def fetch_engine(http: Http, engine: str, query: str, *, timeout: float) -> list[dict]:
    """拉一个引擎的 RSS 并解析为 [{title, url, source, pubdate}]。网络错误抛 FetchError。"""
    if engine == "google":
        url = (
            f"{GOOGLE_NEWS_RSS}?q={quote_plus(query + ' when:2d')}"
            f"&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
        )
        headers = {"Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8"}
    elif engine == "bing":
        url = f"{BING_NEWS_RSS}?q={quote_plus(query)}&format=RSS&setlang=zh-hans&cc=cn"
        headers = {"Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8"}
    else:
        raise FetchError(f"未知引擎 {engine}")
    text = http.text(url, headers=headers, encoding="utf-8", timeout=timeout, retries=0)
    return parse_rss(text)


def parse_rss(text: str) -> list[dict]:
    """解析 RSS 2.0：标题、链接、发布时间、来源。ElementTree 失败时退化为正则。"""
    rows: list[dict] = []
    text = (text or "").strip()
    if not text:
        return rows
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(text.encode("utf-8"))
        for node in root.iter():
            if _local(node.tag) != "item":
                continue
            row = {"title": "", "url": "", "source": "", "pubdate": ""}
            for child in node:
                name = _local(child.tag).lower()
                value = (child.text or "").strip()
                if name == "title":
                    row["title"] = value
                elif name == "link":
                    row["url"] = value or (child.attrib.get("href") or "")
                elif name == "pubdate":
                    row["pubdate"] = value
                elif name == "source":
                    row["source"] = value
                    row.setdefault("source_url", child.attrib.get("url", ""))
            if row["title"]:
                rows.append(row)
        return rows
    except Exception:  # noqa: BLE001 - 退化到正则
        pass

    for block in re.findall(r"<item>(.*?)</item>", text, re.S | re.I):
        def pick(tag: str) -> str:
            m = re.search(rf"<(?:\w+:)?{tag}[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</(?:\w+:)?{tag}>", block, re.S | re.I)
            return (m.group(1).strip() if m else "")

        title = pick("title")
        if title:
            rows.append({"title": title, "url": pick("link"), "source": pick("source"), "pubdate": pick("pubDate")})
    return rows


def _local(tag: object) -> str:
    tag = str(tag)
    return tag.rsplit("}", 1)[-1]


def filter_results(
    item: Item,
    rows: list[dict],
    engine: str,
    *,
    ref: datetime,
    max_gap_hours: float,
) -> list[RelatedNews]:
    """把搜索结果收敛成可信的印证：有时间、时间合理、标题对得上、不是同一家源头。"""
    sig = signature(item)
    out: list[RelatedNews] = []
    max_gap = timedelta(hours=max_gap_hours)
    own_title_key = normalize_title(item.title)
    for row in rows:
        raw_title = str(row.get("title") or "").strip()
        title, tail_source = split_source_from_title(raw_title)
        publisher = str(row.get("source") or tail_source or "").strip()
        if not title:
            continue

        published, _quality, _raw = parse(row.get("pubdate"), ref=ref)
        if published is None or is_future(published, ref=ref):
            continue  # 没有可验证的发布时间 —— 不能当印证
        if item.published_at is not None and abs(published - item.published_at) > max_gap:
            continue

        if same_outlet(item, publisher, str(row.get("url") or "")):
            continue  # 同一家源头不算「不同源头」
        if normalize_title(title) == own_title_key and not publisher:
            continue

        if not is_relevant(item, sig, title):
            continue

        out.append(
            RelatedNews(
                source_label=publisher or ENGINE_LABELS.get(engine, engine),
                title=title,
                url=str(row.get("url") or ""),
                published_at=published,
                relation="same_event",
                via=engine,
            )
        )
    return out


def is_relevant(item: Item, sig: Signature, title: str) -> bool:
    """检索结果是否真的在说同一件事。

    已知标的名时，结果标题必须出现该标的（搜「宁德时代 回购」搜出「比亚迪回购」
    不算印证），再看事件词或去掉标的后的剩余标题是否重合；
    不知道标的时，只能靠整题重合度，阈值更高。
    """
    if sig.names:
        has_subject = any(name in title for name in sig.names if len(name) >= 2) or any(
            code in title for code in sig.codes
        )
        if not has_subject:
            return False
        other_events = set(event_tags(title))
        if sig.events and (other_events & set(sig.events)):
            return True
        overlap, shared = title_overlap(
            strip_subjects(item.title, sig.subjects), strip_subjects(title, sig.subjects)
        )
        return overlap >= TICKER_EVENT_OVERLAP and shared >= MIN_SHARED_BIGRAMS

    overlap, shared = title_overlap(
        strip_subjects(item.title, sig.codes), strip_subjects(title, sig.codes)
    )
    return overlap >= EXTERNAL_OVERLAP and shared >= MIN_SHARED_BIGRAMS


def split_source_from_title(title: str) -> tuple[str, str]:
    """Google News 的标题形如「宁德时代回购进展 - 证券时报」，把来源拆出来。"""
    m = _TITLE_TAIL_SOURCE.search(title or "")
    if not m:
        return (title or "").strip(), ""
    return title[: m.start()].strip(), m.group(1).strip()


#: 抓取源 -> 该源在外部媒体里的叫法/域名，用于排除「同一家源头」
_OUTLET_HINTS: dict[str, tuple[str, ...]] = {
    "eastmoney": ("东方财富", "eastmoney"),
    "stockstar": ("证券之星", "stockstar"),
    "cninfo": ("巨潮", "cninfo"),
    "iwencai": ("同花顺", "问财", "10jqka", "iwencai"),
    "jisilu": ("集思录", "jisilu"),
    "stats": ("国家统计局", "统计局", "stats.gov"),
    "mybbond": ("迈博汇金", "mybbond"),
    "hibor": ("慧博", "hibor"),
    "datayes": ("萝卜投研", "datayes"),
    "ifind": ("同花顺", "iFinD", "10jqka"),
}


def same_outlet(item: Item, publisher: str, url: str) -> bool:
    hints = _OUTLET_HINTS.get(item.source, ())
    label_core = re.sub(r"(网|资讯|投研|·.*)$", "", item.source_label or "")
    pool = list(hints) + ([label_core] if len(label_core) >= 2 else [])
    host = urlsplit(url).netloc.lower() if url else ""
    blob = f"{publisher} {host}".lower()
    return any(h.lower() in blob for h in pool if h)


# ---------------------------------------------------------------------------
# 给渲染/规则分析用的描述
# ---------------------------------------------------------------------------
def describe_related(rel: RelatedNews) -> str:
    """「证券时报（Google News）」/「证券之星」。"""
    if rel.via == "batch":
        return rel.source_label
    return f"{rel.source_label}（{ENGINE_LABELS.get(rel.via, rel.via)}）"


def corroboration_summary(item: Item) -> str:
    """一句话说明印证情况，供规则化分析与日志使用。"""
    same = [r for r in item.related if r.relation == "same_event"]
    subj = [r for r in item.related if r.relation == "same_subject"]
    if same:
        names = "、".join(dict.fromkeys(describe_related(r) for r in same))
        return f"另有 {len(same)} 个源头报道同一事件（{names}），可交叉核对"
    if subj:
        names = "、".join(dict.fromkeys(describe_related(r) for r in subj))
        return f"本轮另见 {len(subj)} 条同标的消息（{names}），是否同一事件待核对"
    if item.related_searched:
        return "本轮其它源与外部检索均未见同一事件的其它报道，暂属单一来源"
    return "本轮其它源未见同一事件报道（未做外部检索），暂属单一来源"
