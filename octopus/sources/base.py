"""源基类：统一抓取 -> 时间校验 -> 去重的流水线.

子类只需实现 `collect()` 返回候选 Item 列表，
时间校验、窗口过滤、去重、异常兜底全部由基类负责，
保证任何一个源挂掉都不会拖垮整轮任务。
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime

from ..http import Http
from ..models import Item, SourceResult, TimeQuality
from ..timeutil import is_future, now, within_window

log = logging.getLogger(__name__)


class Source(ABC):
    """一个情报源."""

    name: str = ""
    label: str = ""
    homepage: str = ""

    #: 该源是否允许只有日期精度的条目进入推送。
    #: 研报/宏观数据这类"按天发布"的源必须放行，否则永远推不出东西；
    #: 快讯/公告类必须收紧，避免把当天早上的旧闻当成新消息。
    allow_date_only: bool = False

    #: 只有日期精度时，视为"发生在当天的这个时刻"（用于排序与窗口判断）。
    date_only_hour: int = 9

    def __init__(self, http: Http, config: dict | None = None) -> None:
        self.http = http
        self.config = config or {}
        # 单源条数上限。0（或负数）表示不限——抓取并通过时间校验的
        # 条目全量进入推送，一条不漏。
        self.limit = int(self.config.get("limit", 0) or 0)

    # ------------------------------------------------------------------
    @abstractmethod
    def collect(self) -> list[Item]:
        """抓取并解析出候选条目（不需要自己做时间过滤）。"""

    # ------------------------------------------------------------------
    def run(self, *, window_minutes: int, seen, ref: datetime | None = None) -> SourceResult:
        """执行一次完整抓取。任何异常都被吞掉并记录在结果里。"""
        ref = ref or now()
        result = SourceResult(source=self.name, source_label=self.label)
        started = time.monotonic()

        try:
            candidates = self.collect()
        except Exception as exc:  # noqa: BLE001 - 单源失败不影响整体
            result.ok = False
            result.error = f"{type(exc).__name__}: {exc}"
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            log.warning("[%s] 抓取失败: %s", self.name, result.error)
            return result

        result.fetched = len(candidates)

        for item in candidates:
            if not item.title.strip():
                continue

            # --- 时间校验：本项目的核心要求 -------------------------------
            if item.published_at is None or item.time_quality is TimeQuality.MISSING:
                result.dropped_no_time += 1
                continue

            if is_future(item.published_at, ref=ref):
                # 源站时钟异常或解析错位，宁可丢弃
                result.dropped_future += 1
                continue

            if item.time_quality is TimeQuality.DATE and not self.allow_date_only:
                result.dropped_no_time += 1
                continue

            if not within_window(item.published_at, window_minutes, ref=ref):
                result.dropped_stale += 1
                continue

            # --- 去重 ---------------------------------------------------
            key = item.dedupe_key()
            if seen is not None and seen.has(key):
                result.dropped_seen += 1
                continue

            result.items.append(item)
            if self.limit > 0 and len(result.items) >= self.limit:
                break

        result.items.sort(key=lambda i: i.published_at or ref, reverse=True)
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        log.info("[%s] %s (%dms)", self.name, result.summary_line(), result.elapsed_ms)
        return result

    # ------------------------------------------------------------------
    def make_item(self, **kwargs) -> Item:
        kwargs.setdefault("source", self.name)
        kwargs.setdefault("source_label", self.label)
        return Item(**kwargs)
