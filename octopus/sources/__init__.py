"""十个情报源的注册表."""

from __future__ import annotations

from .base import Source
from .cninfo import CninfoSource
from .datayes import DatayesSource
from .eastmoney import EastmoneySource
from .ifind import IFindSource
from .iwencai import IWenCaiSource
from .jisilu import JisiluSource
from .research import HiborSource, MybbondSource
from .stats import StatsSource
from .stockstar import StockstarSource

#: 推送里的展示顺序 —— 按"盘口时效性"从强到弱排列
REGISTRY: dict[str, type[Source]] = {
    "iwencai": IWenCaiSource,
    "eastmoney": EastmoneySource,
    "stockstar": StockstarSource,
    "cninfo": CninfoSource,
    "ifind": IFindSource,
    "jisilu": JisiluSource,
    "mybbond": MybbondSource,
    "hibor": HiborSource,
    "stats": StatsSource,
    "datayes": DatayesSource,
}

__all__ = ["REGISTRY", "Source"]
