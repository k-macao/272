"""核心数据模型."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TimeQuality(str, Enum):
    """时间戳质量分级 —— 决定条目能否进入推送.

    EXACT    源直接给出精确到分/秒的发布时间，最可信。
    DATE     源只给到日期（无时分），按当日 00:00 处理，属于弱时间。
    DERIVED  时间由相对描述（如 "46分钟前"）推算而来。
    MISSING  完全没有时间信息 —— 默认丢弃，绝不冒充新内容。
    """

    EXACT = "exact"
    DATE = "date"
    DERIVED = "derived"
    MISSING = "missing"


@dataclass
class Item:
    """一条抓取到的情报条目."""

    source: str
    """源标识，如 'cninfo'。"""

    source_label: str
    """源中文名，如 '巨潮资讯'。"""

    title: str

    url: str = ""

    summary: str = ""

    published_at: datetime | None = None
    """发布时间，必须是 tz-aware（Asia/Shanghai）。"""

    time_quality: TimeQuality = TimeQuality.MISSING

    raw_time: str = ""
    """源站原始时间字符串，便于排查解析问题。"""

    tags: list[str] = field(default_factory=list)

    extra: dict[str, Any] = field(default_factory=dict)
    """源特有的结构化字段（涨跌幅、评级、溢价率等）。"""

    def dedupe_key(self) -> str:
        """去重键.

        优先用 URL（最稳定）；无 URL 时退化到 源+标题 的哈希。
        标题里的易变部分（如实时价格）由各源在构造时自行剔除。
        """
        basis = self.url.strip() or f"{self.source}::{_normalize(self.title)}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_label": self.source_label,
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "time_quality": self.time_quality.value,
            "raw_time": self.raw_time,
            "tags": list(self.tags),
            "extra": dict(self.extra),
        }


@dataclass
class SourceResult:
    """单个源一次抓取的结果与健康状况."""

    source: str
    source_label: str
    items: list[Item] = field(default_factory=list)
    ok: bool = True
    error: str = ""
    degraded: str = ""
    """降级说明，例如主接口失败改用备用接口。"""

    fetched: int = 0
    """源返回的原始条目数（过滤前）。"""

    dropped_no_time: int = 0
    dropped_future: int = 0
    dropped_stale: int = 0
    dropped_seen: int = 0
    elapsed_ms: int = 0

    @property
    def kept(self) -> int:
        return len(self.items)

    def summary_line(self) -> str:
        if not self.ok:
            return f"{self.source_label}: 失败({self.error[:60]})"
        bits = [f"抓{self.fetched}", f"留{self.kept}"]
        if self.dropped_stale:
            bits.append(f"过期{self.dropped_stale}")
        if self.dropped_seen:
            bits.append(f"重复{self.dropped_seen}")
        if self.dropped_no_time:
            bits.append(f"无时间{self.dropped_no_time}")
        if self.dropped_future:
            bits.append(f"未来时间{self.dropped_future}")
        return f"{self.source_label}: " + "/".join(bits)


_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS.sub(" ", text or "").strip().lower()
