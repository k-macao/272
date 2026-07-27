"""抓自线上的真实响应片段（已裁剪条数，字段结构原样保留）。"""

# --- 同花顺涨停池 -----------------------------------------------------------
LIMIT_UP_POOL = {
    "status_code": 0,
    "data": {
        "page": {"limit": 10, "total": 53, "count": 6, "page": 1},
        "info": [
            {
                "open_num": None,
                "first_limit_up_time": "1785116229",
                "last_limit_up_time": "1785116229",
                "code": "002969",
                "limit_up_type": "换手板",
                "order_volume": 3.27996e7,
                "limit_up_suc_rate": 0.75,
                "market_id": 33,
                "change_rate": 9.9808,
                "turnover_rate": 1.5737,
                "reason_type": "中报预增+饮料包装+实控人变更",
                "order_amount": 3.7588342e8,
                "high_days": "首板",
                "name": "嘉美包装",
                "latest": 11.46,
            },
            {
                "open_num": 7,
                "first_limit_up_time": "1785116319",
                "last_limit_up_time": "1785117003",
                "code": "002900",
                "limit_up_type": "换手板",
                "change_rate": 10.0,
                "turnover_rate": 14.7139,
                "reason_type": "创新药+化学制药+中报减亏",
                "order_amount": 8.0333748e7,
                "high_days": "4天3板",
                "name": "哈三联",
                "latest": 12.54,
            },
        ],
        "limit_up_count": {"today": {"num": 53, "rate": 0.841, "open_num": 10}},
        "date": "20260727",
    },
}

# --- 同花顺个股人气榜 -------------------------------------------------------
HOT_STOCKS = {
    "status_code": 0,
    "data": {
        "stock_list": [
            {
                "market": 17,
                "code": "688825",
                "rise_and_fall": 454.157,
                "name": "长鑫科技",
                "tag": {"concept_tag": ["新股与次新股", "融资融券"]},
                "order": 1,
            },
            {
                "market": 17,
                "code": "601606",
                "rise_and_fall": 10.0156,
                "name": "长城军工",
                "tag": {"concept_tag": ["兵装重组概念", "国企改革"], "popularity_tag": "3天2板"},
                "order": 2,
            },
        ]
    },
}

# --- 东财公告中心（注意 display_time 的冒号毫秒） -----------------------------
EASTMONEY_ANN = {
    "data": {
        "list": [
            {
                "art_code": "AN202607271827362101",
                "codes": [{"short_name": "*ST步森", "stock_code": "002569"}],
                "columns": [{"column_name": "独立董事提名人声明"}],
                "display_time": "2026-07-27 07:53:06:817",
                "eiTime": "2026-07-27 07:56:29:000",
                "notice_date": "2026-07-27 00:00:00",
                "title": "*ST步森:独立董事提名人声明与承诺-林善浪",
            },
            {
                "art_code": "AN202607271827362108",
                "codes": [{"short_name": "安克创新", "stock_code": "300866"}],
                "columns": [{"column_name": "其他"}],
                "display_time": "2026-07-27 07:54:39:463",
                "title": "安克创新:关于公司境外上市股份(H股)调入港股通标的证券名单的公告",
            },
        ]
    },
    "success": 1,
}

# --- 东财 7x24 快讯（var ajaxResult={...} 包裹） ------------------------------
EASTMONEY_KUAIXUN_RAW = """var ajaxResult={"rc":1,"me":"","LivesList":[
{"id":"202607273821610750","url_w":"http://finance.eastmoney.com/a/202607273821610750.html",
"title":"CPO概念快速拉升 汇绿生态涨停",
"digest":"CPO概念快速拉升，汇绿生态涨停，源杰科技、光迅科技跟涨。",
"showtime":"2026-07-27 10:00:57","ordertime":"2026-07-27 10:00:57"},
{"id":"202607273821590287","url_w":"http://finance.eastmoney.com/a/202607273821590287.html",
"title":"创业板指涨逾2% 上涨个股近4800只",
"digest":"【创业板指涨逾2% 上涨个股近4800只】指数走强，创业板指拉升涨逾2%，沪指涨0.49%。",
"showtime":"2026-07-27 09:57:39","ordertime":"2026-07-27 09:57:39"}
],"PageCount":5}"""

# --- 集思录转债（last_time 无日期，只有 10:05:51） ----------------------------
JISILU_CB = {
    "page": 1,
    "rows": [
        {
            "id": "110074",
            "cell": {
                "bond_id": "110074",
                "bond_nm": "Z精达转",
                "price": 221.843,
                "increase_rt": 1.99,
                "stock_nm": "精达股份",
                "premium_rt": -0.11,
                "dblow": 221.73,
                "last_time": "10:05:50",
                "qstatus": "00",
                "redeem_dt": "2026-07-27",
                "real_force_redeem_price": "101.8959",
                "icons": {"R": "最后交易日 2026年7月27日\r\n赎回价 101.8959元/张"},
            },
        },
        {
            "id": "123188",
            "cell": {
                "bond_id": "123188",
                "bond_nm": "水羊转债",
                "price": 135.353,
                "increase_rt": 7.31,
                "stock_nm": "水羊股份",
                "premium_rt": -0.08,
                "dblow": 132.48,
                "last_time": "10:05:03",
                "qstatus": "00",
                "redeem_dt": None,
            },
        },
        {
            "id": "118074",
            "cell": {
                "bond_id": "118074",
                "bond_nm": "特宝转债",
                "price": 100,
                "increase_rt": 0,
                "stock_nm": "特宝生物",
                "premium_rt": -0.61,
                "dblow": 99.39,
                "last_time": None,  # 待上市，没有成交时间 -> 应被丢弃
                "qstatus": "00",
                "redeem_dt": None,
            },
        },
    ],
}

# --- 国家统计局 RSS ---------------------------------------------------------
STATS_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>数据发布</title>
<item>
<title><![CDATA[2026年1—6月份全国规模以上工业企业利润增长18.7%]]></title>
<link>https://www.stats.gov.cn/sj/zxfb/202607/t20260727_1964194.html</link>
<pubDate>2026-07-27 09:30:01</pubDate>
</item>
<item>
<title><![CDATA[2026年7月中旬流通领域重要生产资料市场价格变动情况]]></title>
<link>https://www.stats.gov.cn/sj/zxfb/202607/t20260723_1964185.html</link>
<pubDate>2026-07-24 09:30:00</pubDate>
</item>
</channel></rss>"""

# --- 证券之星盘中异动（HTML，同一条会出现带/不带"异动快报："两个版本） ---------
STOCKSTAR_HTML = """<html><body><div class="list">
<ul>
<li>2026-07-27 10:06:10 <a href="/RB2026072700005169.shtml">异动快报：宏和科技（603256）7月27日9点58分触及涨停板</a></li>
<li>2026-07-27 10:05:15 <a href="/RB2026072700005126.shtml">宏和科技（603256）7月27日9点58分触及涨停板</a></li>
<li>2026-07-27 10:05:59 <a href="/RB2026072700005162.shtml">异动快报：长城军工（601606）7月27日10点2分触及涨停板</a></li>
<li>2026-07-27 09:46:51 <a href="/RB2026072700004018.shtml">异动快报：快意电梯（002774）7月27日9点42分触及跌停板</a></li>
<li><a href="/no-time.shtml">没有时间戳的条目</a></li>
</ul></div></body></html>"""

# --- 东财研报中心 -----------------------------------------------------------
EASTMONEY_REPORT = {
    "hits": 312,
    "data": [
        {
            "title": "家电行业周报：家电持仓降至低位，配置价值渐显",
            "orgSName": "华源证券",
            "publishDate": "2026-07-27 00:00:00.000",
            "infoCode": "AP202607271827375033",
            "industryName": "其他家电Ⅱ",
            "emRatingName": "增持",
            "attachPages": 13,
        },
        {
            "title": "煤炭行业周报：旺季需求支撑动力煤高位运行",
            "orgSName": "开源证券",
            "publishDate": "2026-07-27 00:00:00.000",
            "infoCode": "AP202607271827363849",
            "industryName": "煤炭开采",
            "emRatingName": "持有",
            "attachPages": 27,
        },
    ],
}

# --- 慧博投研宏观列表（GB18030 HTML） ----------------------------------------
HIBOR_HTML = """<html><body>
<table><tr><td>
<a href="/data/f486fe590f569815ff2b1101f4829030.html">中航证券-2026年6月及上半年金融数据点评：市场主体内在融资需求仍有待修复-260716</a>
</td></tr>
<tr><td>报告摘要 上半年社融同比回落，融资结构分化格局延续 [详细]</td></tr>
<tr><td>2026-07-27分享者：lsy****na作者：刘庆东评级：页数：9 页</td></tr>
</table>
<table><tr><td>
<a href="/data/8964e0acb6e748225cbaef5b278de644.html">国信证券-多资产周报：美债收益率持续冲高-260726</a>
</td></tr>
<tr><td>美债收益率持续冲高。7月中旬以来，10年期美债收益率从4.40%升至4.68% [详细]</td></tr>
<tr><td>2026-07-26分享者：me***i作者：邵兴宇评级：页数：10 页</td></tr>
</table>
</body></html>"""

# --- 东财板块行情 -----------------------------------------------------------
EASTMONEY_BOARDS = {
    "rc": 0,
    "data": {
        "total": 496,
        "diff": [
            {"f3": 9.33, "f12": "BK1458", "f14": "个护小家电", "f62": 17364441.0, "f128": "倍益康"},
            {"f3": 6.25, "f12": "BK1342", "f14": "房地产综合服务", "f62": 4478808.0, "f128": "珠江股份"},
            {"f3": 5.84, "f12": "BK1462", "f14": "玻纤制造", "f62": 1169846800.0, "f128": "宏和科技"},
        ],
    },
}

EASTMONEY_HSGT = {
    "rc": 0,
    "data": {
        "s2n": [
            "9:30,120.50,60.20,60.30",
            "9:31,340.80,180.10,160.70",
            "9:32,-,-,-",
        ],
        "s2nDate": "07-27",
    },
}
