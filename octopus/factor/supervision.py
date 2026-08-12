"""A 股市场监督管理视角：监管动态抓取 + 监管风险画像。

「A股市场监督管理」这一层做两件事：

1. **监管动态**（本模块）：抓取与主题相关的监管类信息 ——
   交易所问询函/关注函、证监会立案调查与行政处罚、股票异常波动公告、
   风险警示与停复牌。这些是分析一个题材时绕不开的合规底色：
   一个板块涨得再好，若成分股正被立案调查，报告必须说出来。

2. **合规审查**（compliance.py）：约束我们自己的输出，
   确保不出现荐股、收益承诺等违反《证券法》《发布证券研究报告暂行规定》
   的表述。

时间校验沿用项目核心原则：公告时间解析不出来的条目直接丢弃，
未来时间丢弃，只保留窗口内（默认 30 天）的监管信息。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..http import FetchError, Http
from ..models import TimeQuality
from ..timeutil import CN_TZ, is_future, now, parse

log = logging.getLogger(__name__)

EASTMONEY_ANN_API = "https://np-anotice-stock.eastmoney.com/api/security/ann"
EASTMONEY_SEARCH_API = "https://search-api-web.eastmoney.com/search/jsonp"

#: 监管关注度分级：命中即视为监管事件，权重越高越严重
REGULATORY_RULES: tuple[tuple[str, int, str], ...] = (
    ("立案调查", 100, "立案调查"),
    ("立案", 95, "立案调查"),
    ("行政处罚", 90, "行政处罚"),
    ("处罚决定", 88, "行政处罚"),
    ("市场禁入", 92, "行政处罚"),
    ("被证监会", 85, "监管措施"),
    ("监管措施", 80, "监管措施"),
    ("警示函", 78, "监管措施"),
    ("责令改正", 76, "监管措施"),
    ("通报批评", 74, "纪律处分"),
    ("公开谴责", 74, "纪律处分"),
    ("纪律处分", 72, "纪律处分"),
    ("问询函", 70, "问询关注"),
    ("关注函", 68, "问询关注"),
    ("监管函", 68, "问询关注"),
    ("年报问询", 66, "问询关注"),
    ("股票交易异常波动", 60, "异常波动"),
    ("异常波动", 58, "异常波动"),
    ("严重异常波动", 65, "异常波动"),
    ("风险提示", 55, "风险提示"),
    ("退市风险", 85, "退市风险"),
    ("其他风险警示", 80, "退市风险"),
    ("暂停上市", 88, "退市风险"),
    ("终止上市", 90, "退市风险"),
    ("停牌", 50, "停复牌"),
    ("核查", 52, "问询关注"),
    ("自律监管", 70, "纪律处分"),
)

#: 主题层面的监管政策关键词（用于判断该主题是否处在政策监管风口）
POLICY_WORDS: tuple[str, ...] = (
    "证监会", "交易所", "沪深交易所", "北交所", "国务院", "人民银行", "金融监管总局",
    "新规", "征求意见", "管理办法", "实施细则", "指引", "自律规则", "退市新规",
    "程序化交易", "量化交易", "融券", "转融通", "减持新规", "分红", "回购增持",
    "信息披露", "内幕交易", "操纵市场", "财务造假", "严监管",
)


@dataclass
class RegulatoryEvent:
    """一条监管事件。"""

    title: str
    url: str
    published_at: datetime
    time_quality: TimeQuality
    category: str          # 立案调查 / 问询关注 / 异常波动 ...
    severity: int          # 严重度 0-100
    stock: str = ""
    code: str = ""
    raw_time: str = ""

    @property
    def age_days(self) -> int:
        return max(0, (now() - self.published_at).days)


@dataclass
class SupervisionReport:
    """主题对应的监管画像。"""

    events: list[RegulatoryEvent] = field(default_factory=list)
    scanned: int = 0
    window_days: int = 30
    dropped_no_time: int = 0
    dropped_stale: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    focus: list[RegulatoryEvent] = field(default_factory=list)
    """与本次分析标的/主题直接相关的事件，由 pipeline 回填。

    风险等级优先看这批 —— 全市场每天都有问询函，那是背景噪音；
    真正要紧的是"我们正在分析的这些标的"有没有被监管点名。
    """

    @staticmethod
    def level_of(events: list[RegulatoryEvent]) -> str:
        if not events:
            return "低"
        top = max(e.severity for e in events)
        if top >= 85:
            return "高"
        if top >= 65:
            return "中"
        return "偏低"

    @property
    def risk_level(self) -> str:
        """本主题的监管风险等级 —— 报告顶部的醒目结论。

        以"与分析标的直接相关的事件"为准；没有相关事件时为低，
        全市场的背景性监管动态不会把某个主题的风险等级抬上去。
        """
        return self.level_of(self.focus)

    @property
    def by_category(self) -> dict[str, list[RegulatoryEvent]]:
        out: dict[str, list[RegulatoryEvent]] = {}
        for event in self.events:
            out.setdefault(event.category, []).append(event)
        return out

    def for_codes(self, codes: set[str]) -> list[RegulatoryEvent]:
        """涉及给定股票代码的监管事件（分析标的的直接合规风险）。"""
        return [e for e in self.events if e.code and e.code in codes]

    def for_keywords(self, keywords: list[str]) -> list[RegulatoryEvent]:
        """标题或公司名命中主题关键词的监管事件。"""
        words = [k for k in (keywords or []) if k and len(k) >= 2]
        if not words:
            return []
        return [
            e for e in self.events
            if any(w in e.title or (e.stock and w in e.stock) for w in words)
        ]

    def summary_line(self) -> str:
        if self.error:
            return f"监管动态抓取失败：{self.error}"
        if not self.events:
            return f"近 {self.window_days} 天未检出监管类公告（已扫描 {self.scanned} 条）"
        if self.focus:
            cats = "、".join(
                f"{k}{len(v)}条"
                for k, v in _group(self.focus).items()
            )
            return (
                f"分析标的涉及监管事件 {len(self.focus)} 条（{cats}），"
                f"风险等级：{self.risk_level}；"
                f"同期全市场监管公告 {len(self.events)} 条"
            )
        return (
            f"分析标的近 {self.window_days} 天未涉及监管事件（风险等级：低）；"
            f"同期全市场监管公告 {len(self.events)} 条，作为背景参考"
        )


def _group(events: list[RegulatoryEvent]) -> dict[str, list[RegulatoryEvent]]:
    out: dict[str, list[RegulatoryEvent]] = {}
    for event in events:
        out.setdefault(event.category, []).append(event)
    return out


def classify_event(title: str) -> tuple[str, int]:
    """判断标题属于哪类监管事件、严重度多少。返回 ("", 0) 表示非监管事件。"""
    best_cat, best_sev = "", 0
    for word, severity, category in REGULATORY_RULES:
        if word in title and severity > best_sev:
            best_cat, best_sev = category, severity
    return best_cat, best_sev


class SupervisionSource:
    """监管动态抓取。

    主用东方财富公告中心（覆盖沪深两市全部公告，含问询函回复、
    立案调查、异常波动等监管类公告），按标题关键词筛出监管事件。
    """

    def __init__(self, http: Http, *, window_days: int = 30) -> None:
        self.http = http
        self.window_days = window_days

    # ------------------------------------------------------------------
    def collect(
        self,
        keywords: list[str] | None = None,
        codes: set[str] | None = None,
        *,
        ref: datetime | None = None,
        pages: int = 2,
    ) -> SupervisionReport:
        """抓取监管公告。

        keywords 命中标题、codes 命中股票代码，两者任一命中即保留；
        **都不给则保留全部监管类公告** —— 这是默认用法：先把窗口内的监管
        事件全量取回，再由 pipeline 按"是否涉及分析标的"分层展示。
        原因是抓取与行情是并发的，取数时还不知道最终选中哪些个股。
        """
        ref = ref or now()
        report = SupervisionReport(window_days=self.window_days)
        rows: list[dict] = []
        try:
            for page in range(1, max(1, pages) + 1):
                rows.extend(self._announcements(page))
        except FetchError as exc:
            report.error = str(exc)
            log.warning("监管动态抓取失败：%s", exc)
            return report

        report.scanned = len(rows)
        cutoff = ref - timedelta(days=self.window_days)
        keywords = [k for k in (keywords or []) if k]
        codes = codes or set()

        seen_urls: set[str] = set()
        for row in rows:
            title = str(row.get("title") or "").strip()
            if not title:
                continue

            category, severity = classify_event(title)
            if not category:
                continue

            published, quality, raw = parse(row.get("display_time") or row.get("eiTime"))
            if published is None or quality is TimeQuality.MISSING:
                report.dropped_no_time += 1
                continue
            if is_future(published, ref=ref):
                report.dropped_no_time += 1
                continue
            if published < cutoff:
                report.dropped_stale += 1
                continue

            codes_field = row.get("codes") or []
            stock_code = str((codes_field[0] or {}).get("stock_code") or "") if codes_field else ""
            stock_name = str((codes_field[0] or {}).get("short_name") or "") if codes_field else ""

            if keywords or codes:
                hit_code = bool(stock_code and stock_code in codes)
                hit_kw = any(k in title or (stock_name and k in stock_name) for k in keywords)
                if not (hit_code or hit_kw):
                    continue

            art = str(row.get("art_code") or "")
            url = (
                f"https://data.eastmoney.com/notices/detail/{stock_code}/{art}.html"
                if stock_code and art
                else "https://data.eastmoney.com/notices/"
            )
            if url in seen_urls and url != "https://data.eastmoney.com/notices/":
                continue
            seen_urls.add(url)

            report.events.append(
                RegulatoryEvent(
                    title=title if stock_name and stock_name in title else f"{stock_name} {title}".strip(),
                    url=url,
                    published_at=published,
                    time_quality=quality,
                    category=category,
                    severity=severity,
                    stock=stock_name,
                    code=stock_code,
                    raw_time=raw,
                )
            )

        report.events.sort(key=lambda e: (-e.severity, -e.published_at.timestamp()))
        log.info("监管动态：扫描 %d 条公告，命中 %d 条监管事件", report.scanned, len(report.events))
        return report

    # ------------------------------------------------------------------
    def _announcements(self, page: int) -> list[dict]:
        data = self.http.json(
            EASTMONEY_ANN_API,
            params={
                "sr": -1,
                "page_size": 100,
                "page_index": page,
                "ann_type": "A",
                "client_source": "web",
                "f_node": 0,
                "s_node": 0,
            },
            headers={"Referer": "https://data.eastmoney.com/notices/"},
        )
        rows = ((data or {}).get("data") or {}).get("list") or []
        if not rows and page == 1:
            raise FetchError("东财公告接口返回空列表")
        return list(rows)


def policy_context(topic: str) -> list[str]:
    """主题里出现的监管政策关键词 —— 提示这个题材本身是否政策敏感。"""
    text = topic or ""
    return [word for word in POLICY_WORDS if word in text]


def freshness_note(events: list[RegulatoryEvent], *, ref: datetime | None = None) -> str:
    """监管信息的时间校验说明。"""
    if not events:
        return "无监管事件，无需时间校验说明"
    base = ref or now()
    newest = max(e.published_at for e in events)
    delta = (base - newest).total_seconds() / 3600
    when = f"{newest.astimezone(CN_TZ):%Y-%m-%d %H:%M}"
    if delta < 24:
        return f"最新一条监管信息发布于 {when}（{int(delta)} 小时前），已通过时间校验"
    return f"最新一条监管信息发布于 {when}（{int(delta / 24)} 天前），已通过时间校验"
