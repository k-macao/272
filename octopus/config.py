"""配置加载：默认值 < 配置文件 < 环境变量."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yml"

DEFAULTS: dict[str, Any] = {
    # 抓取间隔 30 分钟，窗口放宽到 180 分钟，配合去重避免边界漏推
    "window_minutes": 180,
    "interval_minutes": 30,
    # 条数上限一律 0 = 不限：一条推送包含全部通过时间校验的抓取内容。
    # 代码兜底默认也遵循这个语义；想限量时在 config.yml 里填正整数。
    "max_items_per_source": 0,
    "max_items_total": 0,
    "push_when_empty": True,
    "state_file": "state/seen.json",
    "timeout": 15,
    "retries": 2,
    "sources": {},
    "disabled_sources": [],
}


@dataclass
class Config:
    window_minutes: int = 180
    interval_minutes: int = 30
    max_items_per_source: int = 0  # 0 = 单源不限条数
    max_items_total: int = 0       # 0 = 整条推送不限总条数
    push_when_empty: bool = True
    state_file: str = "state/seen.json"
    timeout: float = 15.0
    retries: int = 2
    pushplus_token: str = ""
    pushplus_topics: list[str] = field(default_factory=list)
    sources: dict[str, dict] = field(default_factory=dict)
    disabled_sources: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        data = dict(DEFAULTS)
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if cfg_path.exists():
            data.update(_read_yaml(cfg_path))

        # 环境变量优先级最高（GitHub Actions Secrets 走这里）
        env_map = {
            "window_minutes": ("OCTOPUS_WINDOW_MINUTES", int),
            "interval_minutes": ("OCTOPUS_INTERVAL_MINUTES", int),
            "max_items_per_source": ("OCTOPUS_MAX_PER_SOURCE", int),
            "max_items_total": ("OCTOPUS_MAX_TOTAL", int),
            "state_file": ("OCTOPUS_STATE_FILE", str),
            "timeout": ("OCTOPUS_TIMEOUT", float),
        }
        for key, (env, caster) in env_map.items():
            raw = os.getenv(env)
            if raw:
                try:
                    data[key] = caster(raw)
                except ValueError:
                    pass

        empty = os.getenv("OCTOPUS_PUSH_WHEN_EMPTY")
        if empty is not None:
            data["push_when_empty"] = empty.strip().lower() in ("1", "true", "yes", "on")

        disabled = os.getenv("OCTOPUS_DISABLED_SOURCES", "")
        if disabled:
            data["disabled_sources"] = [s.strip() for s in disabled.split(",") if s.strip()]

        return cls(
            window_minutes=int(data["window_minutes"]),
            interval_minutes=int(data["interval_minutes"]),
            max_items_per_source=int(data["max_items_per_source"]),
            max_items_total=int(data["max_items_total"]),
            push_when_empty=bool(data["push_when_empty"]),
            state_file=str(data["state_file"]),
            timeout=float(data["timeout"]),
            retries=int(data["retries"]),
            pushplus_token=os.getenv("PUSHPLUS_TOKEN", str(data.get("pushplus_token", ""))).strip(),
            pushplus_topics=Config._load_pushplus_topics(data),
            sources=dict(data.get("sources") or {}),
            disabled_sources=_as_list(data.get("disabled_sources")),
        )

    def for_source(self, name: str) -> dict:
        cfg = dict(self.sources.get(name) or {})
        cfg.setdefault("limit", self.max_items_per_source)
        return cfg

    @staticmethod
    def _load_pushplus_topics(data: dict) -> list[str]:
        """Load pushplus_topics from env or config (supports comma separated or list)."""
        env_topics = os.getenv("PUSHPLUS_TOPICS", "")
        if env_topics:
            return [t.strip() for t in env_topics.split(",") if t.strip()]

        # fallback to legacy single topic
        legacy = os.getenv("PUSHPLUS_TOPIC", str(data.get("pushplus_topic", ""))).strip()
        if legacy:
            return [legacy]

        raw = data.get("pushplus_topics") or data.get("pushplus_topic", "")
        if isinstance(raw, str):
            if raw:
                return [t.strip() for t in raw.split(",") if t.strip()]
            return []
        if isinstance(raw, (list, tuple)):
            return [str(t).strip() for t in raw if str(t).strip()]
        return []


def _as_list(value: Any) -> list[str]:
    """把配置值稳妥地转成字符串列表.

    防御 "datayes,hibor" 或 "[]" 这类字符串被 list() 逐字符拆开的坑。
    """
    if not value:
        return []
    if isinstance(value, str):
        return [piece.strip() for piece in value.split(",") if piece.strip()]
    return [str(piece).strip() for piece in value if str(piece).strip()]


def _read_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore

        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except ImportError:
        return _read_simple_yaml(path)
    except Exception:  # noqa: BLE001 - 配置坏了就用默认值，不该炸掉任务
        return {}


def _read_simple_yaml(path: Path) -> dict:
    """PyYAML 缺失时的极简回退：只解析顶层 key: value（含内联列表）。"""
    result: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].rstrip()
        if not line or line.startswith(" ") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            continue
        if value.startswith("[") and value.endswith("]"):
            # 内联列表 [] / [a, b]；空列表必须解析成 []，
            # 否则会被当成字符串 "[]" 再被逐字符拆开
            inner = value[1:-1].strip()
            result[key] = (
                [piece.strip().strip("'\"") for piece in inner.split(",") if piece.strip()]
                if inner
                else []
            )
        elif value.lower() in ("true", "false"):
            result[key] = value.lower() == "true"
        elif value.lstrip("-").isdigit():
            result[key] = int(value)
        else:
            result[key] = value.strip("'\"")
    return result
