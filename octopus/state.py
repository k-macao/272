"""已推送条目的持久化去重.

30 分钟跑一次、时间窗口 3 小时，必然出现重叠 —— 没有状态就会反复推同一条。
状态文件是个 JSON： {dedupe_key: 首次见到的 ISO 时间}，
超过保留期的键会被自动清掉，防止无限增长。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Iterable

from .timeutil import CN_TZ, now

log = logging.getLogger(__name__)

DEFAULT_RETENTION_HOURS = 72
MAX_KEYS = 20000


class SeenStore:
    def __init__(self, path: str | Path, retention_hours: int = DEFAULT_RETENTION_HOURS) -> None:
        self.path = Path(path)
        self.retention = timedelta(hours=retention_hours)
        self._seen: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("状态文件 %s 损坏，按空处理: %s", self.path, exc)
            return
        if isinstance(raw, dict):
            self._seen = {str(k): str(v) for k, v in raw.get("seen", raw).items()}
        self._prune()

    def _prune(self) -> None:
        cutoff = now() - self.retention
        kept: dict[str, str] = {}
        for key, iso in self._seen.items():
            try:
                from datetime import datetime

                ts = datetime.fromisoformat(iso)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=CN_TZ)
            except ValueError:
                continue
            if ts >= cutoff:
                kept[key] = iso
        if len(kept) > MAX_KEYS:  # 极端情况下按时间截断
            kept = dict(sorted(kept.items(), key=lambda kv: kv[1], reverse=True)[:MAX_KEYS])
        self._seen = kept

    # ------------------------------------------------------------------
    def has(self, key: str) -> bool:
        return key in self._seen

    def add(self, key: str) -> None:
        self._seen.setdefault(key, now().isoformat(timespec="seconds"))

    def add_many(self, keys: Iterable[str]) -> None:
        for key in keys:
            self.add(key)

    def __len__(self) -> int:
        return len(self._seen)

    # ------------------------------------------------------------------
    def save(self) -> None:
        """原子写入，避免任务被中断时留下半截文件。"""
        self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": now().isoformat(timespec="seconds"), "seen": self._seen}
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=0)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
