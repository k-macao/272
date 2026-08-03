"""章鱼 AI · A股情报抓取智能体.

每 30 分钟抓取一次十大金融信息源的最新内容，严格校验时间戳，
整合后推送到微信 PushPlus。
"""

from __future__ import annotations

from .agent import Agent
from .ai import ZhipuAI
from .config import Config

__version__ = "1.0.0"
__author__ = "章鱼 AI"
__all__ = ["Agent", "Config", "ZhipuAI"]
