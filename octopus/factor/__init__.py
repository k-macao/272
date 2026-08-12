"""量化因子分析子系统。

输入一个主题 -> 从 GitHub 拉取因子模型定义（microsoft/qlib 的 Alpha158）
-> 在真实 A 股行情上计算因子 -> 叠加 A 股市场监督管理视角（监管动态 + 合规审查）
-> 交给大模型生成分析报告 -> 一对一推送到 PushPlus。

各模块职责：
    expr.py        qlib 因子表达式的纯 Python 求值引擎（不依赖 numpy/pandas）
    qlib_repo.py   从 GitHub 实时拉取 qlib 因子定义，附带本地缓存兜底
    market.py      A 股行情：主题 -> 板块匹配 -> 成分股 -> 日线序列
    supervision.py A 股市场监督管理动态（问询函/立案/处罚/异常波动），带时间校验
    compliance.py  输出端合规审查：违规荐股表述检测与中性化改写
    pipeline.py    全流程编排（含无大模型时的规则化降级解读）
"""

from __future__ import annotations

__all__ = [
    "expr",
    "qlib_repo",
    "market",
    "supervision",
    "compliance",
    "pipeline",
]
