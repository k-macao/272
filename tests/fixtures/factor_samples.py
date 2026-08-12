"""主题因子分析用的离线样本：东财行情/公告接口的真实字段结构。

行情序列是程序生成的（真实接口每天都在变，写死会让测试随时间失效），
但**字段名、嵌套层级、数值格式**全部按东财真实响应保留，
保证解析逻辑一旦写错就会被测出来。
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from octopus.timeutil import CN_TZ

#: 固定的参考时刻，让所有断言可复现
REF = datetime(2026, 8, 12, 15, 30, 0, tzinfo=CN_TZ)


def make_klines(
    count: int = 250,
    *,
    start: float = 10.0,
    drift: float = 0.001,
    vol: float = 0.02,
    seed: int = 1,
    end_day: date | None = None,
) -> list[str]:
    """生成东财 kline 格式的字符串列表。

    真实格式（fields2=f51..f61）：
        日期,开,收,高,低,成交量(手),成交额(元),振幅,涨跌幅,涨跌额,换手率
    """
    rnd = random.Random(seed)
    end_day = end_day or REF.date()
    # 从后往前推算交易日（跳过周末）
    days: list[date] = []
    cursor = end_day
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()

    out: list[str] = []
    price = start
    base_volume = rnd.uniform(3e5, 2e6)
    for day in days:
        prev = price
        price = max(0.5, price * (1 + rnd.gauss(drift, vol)))
        open_ = prev * (1 + rnd.gauss(0, 0.006))
        close = price
        high = max(open_, close) * (1 + abs(rnd.gauss(0, 0.007)))
        low = min(open_, close) * (1 - abs(rnd.gauss(0, 0.007)))
        volume = base_volume * (1 + abs(rnd.gauss(0, 0.4)))
        amount = volume * 100 * (open_ + close + high + low) / 4
        change = (close / prev - 1) * 100
        out.append(
            f"{day:%Y-%m-%d},{open_:.2f},{close:.2f},{high:.2f},{low:.2f},"
            f"{volume:.0f},{amount:.0f},{(high - low) / prev * 100:.2f},"
            f"{change:.2f},{close - prev:.2f},{abs(rnd.gauss(2, 1)):.2f}"
        )
    return out


#: 概念板块列表（push2 clist，fs=m:90 t:3）
CONCEPT_BOARDS = {
    "rc": 0,
    "data": {
        "total": 4,
        "diff": [
            {"f3": 5.83, "f12": "BK0896", "f14": "人形机器人", "f62": 1.24e9, "f128": "三花智控"},
            {"f3": 4.02, "f12": "BK1030", "f14": "减速器", "f62": 4.2e8, "f128": "绿的谐波"},
            {"f3": 1.20, "f12": "BK0475", "f14": "半导体", "f62": 8.8e8, "f128": "中芯国际"},
            {"f3": -0.90, "f12": "BK0900", "f14": "储能", "f62": -2.2e8, "f128": "阳光电源"},
        ],
    },
}

#: 行业板块列表（fs=m:90 t:2）
INDUSTRY_BOARDS = {
    "rc": 0,
    "data": {
        "total": 2,
        "diff": [
            {"f3": 1.90, "f12": "BK0545", "f14": "专用设备", "f62": 2.4e8, "f128": "三花智控"},
            {"f3": 0.80, "f12": "BK0459", "f14": "电子元件", "f62": 1.5e8, "f128": "立讯精密"},
        ],
    },
}

#: 板块成分股（fs=b:BK0896），f13 是市场标识（0=深 1=沪）
BOARD_MEMBERS = {
    "rc": 0,
    "data": {
        "total": 5,
        "diff": [
            {"f2": 68.31, "f3": 4.10, "f6": 2.9e9, "f8": 3.2, "f12": "300124", "f13": 0, "f14": "汇川技术"},
            {"f2": 25.40, "f3": 6.70, "f6": 3.4e9, "f8": 5.1, "f12": "002050", "f13": 1, "f14": "三花智控"},
            {"f2": 41.22, "f3": 5.50, "f6": 1.2e9, "f8": 6.4, "f12": "688017", "f13": 0, "f14": "绿的谐波"},
            # ST 股必须被过滤掉，不进分析样本
            {"f2": 3.11, "f3": 1.00, "f6": 1.0e7, "f8": 2.0, "f12": "002569", "f13": 0, "f14": "ST步森"},
            {"f2": 18.90, "f3": 3.90, "f6": 1.7e9, "f8": 4.8, "f12": "002472", "f13": 0, "f14": "双环传动"},
        ],
    },
}

#: 全市场成交额榜（板块匹配失败时的降级口径）
TOP_AMOUNT = {
    "rc": 0,
    "data": {
        "total": 2,
        "diff": [
            {"f2": 1680.0, "f3": 1.10, "f6": 9.9e9, "f8": 0.6, "f12": "600519", "f13": 1, "f14": "贵州茅台"},
            {"f2": 35.6, "f3": 2.30, "f6": 8.1e9, "f8": 2.1, "f12": "000858", "f13": 0, "f14": "五粮液"},
        ],
    },
}


def _ann(title: str, code: str, name: str, when: datetime, idx: int) -> dict:
    """东财公告条目：display_time 的毫秒用冒号分隔（非标准格式）。"""
    return {
        "art_code": f"AN{idx:05d}",
        "title": title,
        "codes": [{"stock_code": code, "short_name": name}],
        "display_time": f"{when:%Y-%m-%d %H:%M:%S}:123",
        "columns": [{"column_name": "公司治理"}],
    }


#: 监管公告样本：涵盖各严重度、非监管公告、超窗口公告、无时间公告
ANNOUNCEMENTS = {
    "data": {
        "list": [
            _ann("关于对汇川技术股份有限公司的问询函", "300124", "汇川技术", REF - timedelta(days=2), 1),
            _ann("三花智控：股票交易异常波动公告", "002050", "三花智控", REF - timedelta(days=1, hours=2), 2),
            _ann("绿的谐波：收到深圳证券交易所关注函", "688017", "绿的谐波", REF - timedelta(days=5), 3),
            _ann("某某科技：收到中国证监会立案告知书", "600001", "某某科技", REF - timedelta(days=3), 4),
            # 非监管类公告 —— 必须被过滤
            _ann("双环传动：2026年半年度业绩预增公告", "002472", "双环传动", REF - timedelta(days=4), 5),
            # 超出 30 天窗口 —— 必须被丢弃
            _ann("旧闻股份：收到警示函", "600002", "旧闻股份", REF - timedelta(days=95), 6),
            # 无时间戳 —— 必须被丢弃，绝不臆造时间
            {
                "art_code": "AN00007",
                "title": "无时间公司：收到监管函",
                "codes": [{"stock_code": "600003", "short_name": "无时间"}],
                "display_time": "",
            },
            # 未来时间 —— 脏数据，必须被丢弃
            _ann("未来股份：收到问询函", "600004", "未来股份", REF + timedelta(days=2), 8),
        ]
    }
}
