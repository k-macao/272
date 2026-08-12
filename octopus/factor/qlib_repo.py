"""从 GitHub 拉取开源量化因子模型定义（microsoft/qlib）。

「用 AI 调用 GitHub 上的因子模型」这一步落在这里：不臆造因子公式，
而是实时从 qlib 仓库把 `qlib/contrib/data/loader.py` 里的 Alpha158
因子定义抓下来，解析出 (因子名, 表达式) 清单，交给本地表达式引擎计算。

为什么解析源码而不是 pip install qlib：
  - qlib 依赖 numpy/pandas/cython/redis，CI 里装一次两三分钟，收益为零；
  - 我们只需要「因子公式」这份知识，公式就写在 loader.py 里，是稳定的；
  - 解析出的是纯字符串表达式，由 expr.py 的白名单引擎求值，不执行仓库代码。

三级取数策略（每一级都会如实标注在报告里，绝不假装数据来自原站）：
  1. GitHub API contents 接口（带 commit sha 与提交时间，可溯源）
  2. raw.githubusercontent.com 直连
  3. 本地内置快照 BUILTIN_ALPHA158（离线兜底，标注为「内置快照」）
"""

from __future__ import annotations

import ast
import base64
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..http import FetchError, Http
from ..timeutil import parse

log = logging.getLogger(__name__)

REPO = "microsoft/qlib"
LOADER_PATH = "qlib/contrib/data/loader.py"
GITHUB_API = f"https://api.github.com/repos/{REPO}/contents/{LOADER_PATH}"
GITHUB_COMMITS = f"https://api.github.com/repos/{REPO}/commits"
GITHUB_RAW = f"https://raw.githubusercontent.com/{REPO}/main/{LOADER_PATH}"
REPO_URL = f"https://github.com/{REPO}"
LOADER_URL = f"{REPO_URL}/blob/main/{LOADER_PATH}"


@dataclass
class Factor:
    """一个因子：名字 + qlib 表达式 + 归类 + 人话解释。"""

    name: str
    expr: str
    group: str = "其他"
    desc: str = ""

    def as_pair(self) -> tuple[str, str]:
        return self.name, self.expr


@dataclass
class FactorModel:
    """一份从 GitHub 取回的因子模型定义。"""

    factors: list[Factor] = field(default_factory=list)
    source: str = ""           # github-api / github-raw / builtin
    repo: str = REPO
    url: str = LOADER_URL
    commit_sha: str = ""
    commit_time: datetime | None = None
    degraded: str = ""         # 降级说明，会如实写进推送
    fetched_count: int = 0     # 从源码里解析出的因子总数

    @property
    def provenance(self) -> str:
        """报告里展示的「因子来源」一行。"""
        if self.source == "builtin":
            return f"{self.repo} · Alpha158（内置快照，未联网）"
        sha = self.commit_sha[:7] if self.commit_sha else "latest"
        when = f"，{self.commit_time:%Y-%m-%d}" if self.commit_time else ""
        via = {
            "github-api": "GitHub API 实时拉取",
            "github-raw": "raw.githubusercontent 实时拉取",
            "cache": "本地缓存，上次自 GitHub 拉取",
        }.get(self.source, self.source)
        return f"{self.repo}@{sha}{when} · Alpha158（{via}）"

    def by_group(self) -> dict[str, list[Factor]]:
        out: dict[str, list[Factor]] = {}
        for factor in self.factors:
            out.setdefault(factor.group, []).append(factor)
        return out


# ---------------------------------------------------------------------------
# 因子分组与中文释义 —— 让报告能说人话，而不是甩一堆 KMID/WVMA30
# ---------------------------------------------------------------------------
GROUP_RULES: tuple[tuple[str, str], ...] = (
    (r"^K(MID|LEN|UP|LOW|SFT)", "K线形态"),
    (r"^(ROC|MA|BETA|RSQR|RESI)\d", "趋势动量"),
    (r"^(STD|MAX|MIN|QTLU|QTLD|RANK|RSV|IMAX|IMIN|IMXD)\d", "波动与位置"),
    (r"^(CNTP|CNTN|CNTD|SUMP|SUMN|SUMD)\d", "涨跌强弱"),
    (r"^(VMA|VSTD|WVMA|VSUMP|VSUMN|VSUMD|VOLUME)\d", "量能结构"),
    (r"^(CORR|CORD)\d", "量价配合"),
    (r"^(OPEN|HIGH|LOW|CLOSE|VWAP)\d", "价格基准"),
)

DESC_RULES: tuple[tuple[str, str], ...] = (
    ("KMID", "实体涨跌幅：收盘相对开盘的偏离，衡量当日多空控盘力度"),
    ("KLEN", "K线振幅：最高最低价差相对开盘价，衡量日内波动强度"),
    ("KUP", "上影线长度：冲高回落程度，偏大提示上方抛压"),
    ("KLOW", "下影线长度：探底回升程度，偏大提示下方承接"),
    ("KSFT", "K线重心偏移：收盘价在当日区间中的位置"),
    ("ROC", "价格变化率：N 日前收盘 / 最新收盘，>1 表示区间内下跌"),
    ("MA", "均线偏离：N 日均价 / 最新价，<1 表示价格站上均线"),
    ("STD", "价格波动率：N 日收盘标准差 / 最新价，越大越不稳"),
    ("BETA", "价格斜率：N 日线性回归斜率，正值代表趋势向上"),
    ("RSQR", "趋势线性度：N 日回归 R²，越接近 1 趋势越干净"),
    ("RESI", "趋势残差：最新价偏离回归线的幅度，衡量短期超买超卖"),
    ("MAX", "阶段高点比：N 日最高价 / 最新价，接近 1 表示逼近前高"),
    ("MIN", "阶段低点比：N 日最低价 / 最新价，接近 1 表示逼近前低"),
    ("QTLU", "上分位比：N 日收盘 80% 分位 / 最新价"),
    ("QTLD", "下分位比：N 日收盘 20% 分位 / 最新价"),
    ("RANK", "价格分位：最新价在 N 日内的分位数，越高越强势"),
    ("RSV", "随机指标 RSV：最新价在 N 日高低区间中的相对位置"),
    ("IMAX", "距最高价天数（归一化）：Aroon 上行分量"),
    ("IMIN", "距最低价天数（归一化）：Aroon 下行分量"),
    ("IMXD", "高低点时序差：正值提示下行动能占优"),
    ("CORR", "量价相关性：收盘价与对数成交量的 N 日相关系数"),
    ("CORD", "涨跌—量变相关性：价格变动率与成交量变动率的相关系数"),
    ("CNTP", "上涨天数占比：N 日内收阳比例"),
    ("CNTN", "下跌天数占比：N 日内收阴比例"),
    ("CNTD", "涨跌天数差：上涨占比减下跌占比"),
    ("SUMP", "上涨动能占比：类 RSI，N 日涨幅之和 / 总波动"),
    ("SUMN", "下跌动能占比：N 日跌幅之和 / 总波动"),
    ("SUMD", "净动能：涨跌动能之差，正值多头占优"),
    ("VMA", "量能均值比：N 日均量 / 最新量，<1 表示当前放量"),
    ("VSTD", "成交量波动率：N 日量能标准差 / 最新量"),
    ("WVMA", "量加权价波动：成交量加权的价格波动离散度"),
    ("VSUMP", "放量占比：N 日量增之和 / 量变总和"),
    ("VSUMN", "缩量占比：N 日量减之和 / 量变总和"),
    ("VSUMD", "量能净变化：放量减缩量，正值资金流入迹象"),
    ("VWAP", "成交均价相对收盘价"),
    ("VOLUME", "成交量相对基准"),
    ("OPEN", "开盘价相对收盘价"),
    ("HIGH", "最高价相对收盘价"),
    ("LOW", "最低价相对收盘价"),
    ("CLOSE", "收盘价基准"),
)

#: 解析 qlib 因子定义时允许出现的属性（方法）名白名单。
#: 源码里只用到 dict.get 与 str.lower/upper 这类纯函数，
#: 其余（尤其是任何 dunder）一律拒绝，杜绝沙箱逃逸。
_SAFE_ATTRS = frozenset({"get", "lower", "upper", "append", "extend", "keys", "values", "items"})

#: 报告默认关注的核心因子（按 A 股短中期研判的实用性挑选）
CORE_FACTORS: tuple[str, ...] = (
    "KMID", "KLEN", "KUP", "KLOW", "KSFT",
    "ROC5", "ROC10", "ROC20", "ROC60",
    "MA5", "MA10", "MA20", "MA60",
    "STD5", "STD20", "STD60",
    "BETA20", "BETA60", "RSQR20", "RSQR60", "RESI20",
    "MAX20", "MIN20", "QTLU20", "QTLD20", "RANK20", "RANK60", "RSV20",
    "IMAX20", "IMIN20", "IMXD20",
    "CORR10", "CORR20", "CORR60", "CORD20",
    "CNTP20", "CNTN20", "CNTD20",
    "SUMP20", "SUMN20", "SUMD20", "SUMD60",
    "VMA5", "VMA20", "VSTD20", "WVMA20", "VSUMP20", "VSUMD20",
)


def classify(name: str) -> str:
    for pattern, group in GROUP_RULES:
        if re.match(pattern, name):
            return group
    return "其他"


def describe(name: str) -> str:
    base = re.sub(r"\d+$", "", name)
    window = re.search(r"(\d+)$", name)
    for key, text in DESC_RULES:
        if base == key or base.rstrip("2") == key:
            if window:
                return text.replace("N 日", f"{window.group(1)} 日")
            return text
    return ""


# ---------------------------------------------------------------------------
# 源码解析：从 loader.py 里跑出 Alpha158DL.get_feature_config 的 (fields, names)
# ---------------------------------------------------------------------------
def parse_alpha158(source: str) -> list[Factor]:
    """解析 loader.py 源码，取出 Alpha158 的因子表达式与名字。

    做法：用 ast 定位 Alpha158DL.get_feature_config，把函数体单独抽出来，
    在一个**没有 builtins、只放行必要函数**的沙箱里执行。函数体本身只有
    字符串拼接与列表推导，没有 import / IO，执行是安全且确定的。
    解析失败抛 FetchError，让上游降级到内置快照。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise FetchError(f"qlib loader.py 语法解析失败: {exc}") from exc

    func: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Alpha158DL":
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == "get_feature_config":
                    func = child
                    break
    if func is None:
        raise FetchError("未在 qlib loader.py 中找到 Alpha158DL.get_feature_config")

    # 校验函数体只包含赋值/增量赋值/if/内部函数定义/return，杜绝意外副作用。
    # 属性访问按**方法名白名单**放行（源码里只有 config.get / field.lower 这类），
    # 任何 dunder 或未知属性一律拒绝，避免通过 __globals__ 之类逃逸。
    for node in ast.walk(func):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.With, ast.Try, ast.Raise)):
            raise FetchError("qlib 因子函数包含意外语句，出于安全放弃解析")
        if isinstance(node, ast.Attribute):
            if node.attr not in _SAFE_ATTRS:
                raise FetchError(f"qlib 因子函数访问了未知属性 .{node.attr}")

    # 默认参数里的 config 决定启用哪些因子族；这里用全量配置，
    # 与 qlib Alpha158 handler 的默认口径一致（kbar + price + rolling）。
    body = ast.Module(body=list(func.body), type_ignores=[])
    scope: dict[str, object] = {
        "config": {
            "kbar": {},
            "price": {"windows": [0], "feature": ["OPEN", "HIGH", "LOW", "VWAP"]},
            "rolling": {},
        }
    }
    safe_builtins = {
        "range": range,
        "len": len,
        "str": str,
        "int": int,
        "float": float,
        "list": list,
        "dict": dict,
        "set": set,
        "tuple": tuple,
        "enumerate": enumerate,
        "zip": zip,
        "sorted": sorted,
    }

    class _Return(Exception):
        def __init__(self, value):
            self.value = value

    # 把 return 换成异常抛出，才能在 exec 里拿到返回值
    result: dict[str, object] = {}

    class _ReturnRewriter(ast.NodeTransformer):
        """把顶层 return 换成 __capture__(...)，好在 exec 里拿到返回值。

        注意不能递归进嵌套函数：qlib 的 get_feature_config 里定义了内部
        函数 use(x)，它的 return 必须原样保留，否则会提前捕获到 bool。
        """

        def visit_FunctionDef(self, node: ast.FunctionDef):  # noqa: N802
            return node  # 不改写嵌套函数体

        def visit_Lambda(self, node: ast.Lambda):  # noqa: N802
            return node

        def visit_Return(self, node: ast.Return):  # noqa: N802
            return ast.copy_location(
                ast.Expr(
                    value=ast.Call(
                        func=ast.Name(id="__capture__", ctx=ast.Load()),
                        args=[node.value] if node.value else [],
                        keywords=[],
                    )
                ),
                node,
            )

    body = _ReturnRewriter().visit(body)
    ast.fix_missing_locations(body)

    def _capture(value):
        result["value"] = value
        raise _Return(value)

    scope["__capture__"] = _capture
    scope["__builtins__"] = safe_builtins
    try:
        # globals 与 locals 必须是同一个字典：函数体里有列表推导式和内部函数
        # （use()），它们只能看到 globals，分开传会报 NameError。
        exec(  # noqa: S102 - 已做 AST 白名单校验，且沙箱无 builtins
            compile(body, filename="<qlib-alpha158>", mode="exec"),
            scope,
        )
    except _Return:
        pass
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"执行 qlib 因子定义失败: {exc}") from exc

    payload = result.get("value")
    if not (isinstance(payload, tuple) and len(payload) == 2):
        raise FetchError("qlib 因子定义返回值结构异常")

    fields, names = payload
    if not fields or len(fields) != len(names):
        raise FetchError(f"qlib 因子字段与名称数量不一致：{len(fields)} vs {len(names)}")

    return [
        Factor(name=str(n), expr=str(f), group=classify(str(n)), desc=describe(str(n)))
        for f, n in zip(fields, names)
    ]


# ---------------------------------------------------------------------------
# 拉取
# ---------------------------------------------------------------------------
class QlibFactorRepo:
    """负责把 GitHub 上的因子定义取回本地。"""

    def __init__(
        self,
        http: Http | None = None,
        *,
        cache_dir: Path | None = None,
        token: str = "",
    ) -> None:
        self.http = http or Http(timeout=20.0, retries=1)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.token = (token or "").strip()

    # ------------------------------------------------------------------
    def load(self, *, use_cache: bool = True) -> FactorModel:
        """按 API -> raw -> 本地缓存 -> 内置快照 的顺序取因子定义。"""
        errors: list[str] = []

        for loader in (self._from_api, self._from_raw):
            try:
                model = loader()
                self._save_cache(model)
                return model
            except Exception as exc:  # noqa: BLE001 - 逐级降级
                errors.append(f"{loader.__name__}: {exc}")
                log.warning("qlib 因子拉取失败（%s），尝试下一路径", exc)

        if use_cache:
            cached = self._from_cache()
            if cached is not None:
                cached.degraded = "GitHub 不可达，使用本地缓存的因子定义"
                log.info("使用本地缓存的 qlib 因子定义（%d 个）", len(cached.factors))
                return cached

        log.warning("GitHub 与缓存均不可用，回落到内置 Alpha158 快照")
        model = builtin_model()
        model.degraded = "GitHub 不可达，使用内置 Alpha158 快照"
        return model

    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _from_api(self) -> FactorModel:
        data = self.http.json(GITHUB_API, headers=self._headers())
        content = (data or {}).get("content") or ""
        if not content:
            raise FetchError("GitHub contents 接口未返回文件内容")
        source = base64.b64decode(content).decode("utf-8", errors="replace")
        factors = parse_alpha158(source)
        sha = str((data or {}).get("sha") or "")
        commit_sha, commit_time = self._latest_commit()
        return FactorModel(
            factors=factors,
            source="github-api",
            commit_sha=commit_sha or sha,
            commit_time=commit_time,
            fetched_count=len(factors),
        )

    def _latest_commit(self) -> tuple[str, datetime | None]:
        """取该文件最近一次提交，用于因子定义的版本溯源（失败不致命）。"""
        try:
            data = self.http.json(
                GITHUB_COMMITS,
                params={"path": LOADER_PATH, "per_page": 1},
                headers=self._headers(),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("取 qlib 提交信息失败：%s", exc)
            return "", None
        if not isinstance(data, list) or not data:
            return "", None
        first = data[0] or {}
        sha = str(first.get("sha") or "")
        raw = ((first.get("commit") or {}).get("committer") or {}).get("date") or ""
        # GitHub 返回 UTC 的 2024-07-05T07:44:16Z，转成东八区
        published, _, _ = parse(str(raw).replace("Z", "").replace("T", " "))
        if published is not None:
            from datetime import timedelta

            published = published + timedelta(hours=8)  # UTC -> 北京时间
        return sha, published

    def _from_raw(self) -> FactorModel:
        source = self.http.text(GITHUB_RAW)
        factors = parse_alpha158(source)
        return FactorModel(
            factors=factors,
            source="github-raw",
            fetched_count=len(factors),
            degraded="GitHub API 不可用，改用 raw 直连（无版本号）",
        )

    # ------------------------------------------------------------------
    def _cache_path(self) -> Path | None:
        return (self.cache_dir / "qlib_alpha158.json") if self.cache_dir else None

    def _save_cache(self, model: FactorModel) -> None:
        path = self._cache_path()
        if not path or not model.factors:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "repo": model.repo,
                "commit_sha": model.commit_sha,
                "commit_time": model.commit_time.isoformat() if model.commit_time else "",
                "source": model.source,
                "factors": [{"name": f.name, "expr": f.expr} for f in model.factors],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.debug("写因子缓存失败：%s", exc)

    def _from_cache(self) -> FactorModel | None:
        path = self._cache_path()
        if not path or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        rows = payload.get("factors") or []
        if not rows:
            return None
        commit_time, _, _ = parse(payload.get("commit_time") or "")
        return FactorModel(
            factors=[
                Factor(
                    name=str(r.get("name")),
                    expr=str(r.get("expr")),
                    group=classify(str(r.get("name"))),
                    desc=describe(str(r.get("name"))),
                )
                for r in rows
                if r.get("name") and r.get("expr")
            ],
            source="cache",
            commit_sha=str(payload.get("commit_sha") or ""),
            commit_time=commit_time,
            fetched_count=len(rows),
        )


# ---------------------------------------------------------------------------
# 内置快照：完全离线时的兜底（等价于 Alpha158 默认配置的核心子集）
# ---------------------------------------------------------------------------
def _builtin_pairs() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = [
        ("KMID", "($close-$open)/$open"),
        ("KLEN", "($high-$low)/$open"),
        ("KMID2", "($close-$open)/($high-$low+1e-12)"),
        ("KUP", "($high-Greater($open, $close))/$open"),
        ("KUP2", "($high-Greater($open, $close))/($high-$low+1e-12)"),
        ("KLOW", "(Less($open, $close)-$low)/$open"),
        ("KLOW2", "(Less($open, $close)-$low)/($high-$low+1e-12)"),
        ("KSFT", "(2*$close-$high-$low)/$open"),
        ("KSFT2", "(2*$close-$high-$low)/($high-$low+1e-12)"),
        ("OPEN0", "$open/$close"),
        ("HIGH0", "$high/$close"),
        ("LOW0", "$low/$close"),
        ("VWAP0", "$vwap/$close"),
    ]
    windows = (5, 10, 20, 30, 60)
    for d in windows:
        pairs += [
            (f"ROC{d}", f"Ref($close, {d})/$close"),
            (f"MA{d}", f"Mean($close, {d})/$close"),
            (f"STD{d}", f"Std($close, {d})/$close"),
            (f"BETA{d}", f"Slope($close, {d})/$close"),
            (f"RSQR{d}", f"Rsquare($close, {d})"),
            (f"RESI{d}", f"Resi($close, {d})/$close"),
            (f"MAX{d}", f"Max($high, {d})/$close"),
            (f"MIN{d}", f"Min($low, {d})/$close"),
            (f"QTLU{d}", f"Quantile($close, {d}, 0.8)/$close"),
            (f"QTLD{d}", f"Quantile($close, {d}, 0.2)/$close"),
            (f"RANK{d}", f"Rank($close, {d})"),
            (f"RSV{d}", f"($close-Min($low, {d}))/(Max($high, {d})-Min($low, {d})+1e-12)"),
            (f"IMAX{d}", f"IdxMax($high, {d})/{d}"),
            (f"IMIN{d}", f"IdxMin($low, {d})/{d}"),
            (f"IMXD{d}", f"(IdxMax($high, {d})-IdxMin($low, {d}))/{d}"),
            (f"CORR{d}", f"Corr($close, Log($volume+1), {d})"),
            (
                f"CORD{d}",
                f"Corr($close/Ref($close,1), Log($volume/Ref($volume, 1)+1), {d})",
            ),
            (f"CNTP{d}", f"Mean($close>Ref($close, 1), {d})"),
            (f"CNTN{d}", f"Mean($close<Ref($close, 1), {d})"),
            (f"CNTD{d}", f"Mean($close>Ref($close, 1), {d})-Mean($close<Ref($close, 1), {d})"),
            (
                f"SUMP{d}",
                f"Sum(Greater($close-Ref($close, 1), 0), {d})"
                f"/(Sum(Abs($close-Ref($close, 1)), {d})+1e-12)",
            ),
            (
                f"SUMN{d}",
                f"Sum(Greater(Ref($close, 1)-$close, 0), {d})"
                f"/(Sum(Abs($close-Ref($close, 1)), {d})+1e-12)",
            ),
            (
                f"SUMD{d}",
                f"(Sum(Greater($close-Ref($close, 1), 0), {d})"
                f"-Sum(Greater(Ref($close, 1)-$close, 0), {d}))"
                f"/(Sum(Abs($close-Ref($close, 1)), {d})+1e-12)",
            ),
            (f"VMA{d}", f"Mean($volume, {d})/($volume+1e-12)"),
            (f"VSTD{d}", f"Std($volume, {d})/($volume+1e-12)"),
            (
                f"WVMA{d}",
                f"Std(Abs($close/Ref($close, 1)-1)*$volume, {d})"
                f"/(Mean(Abs($close/Ref($close, 1)-1)*$volume, {d})+1e-12)",
            ),
            (
                f"VSUMP{d}",
                f"Sum(Greater($volume-Ref($volume, 1), 0), {d})"
                f"/(Sum(Abs($volume-Ref($volume, 1)), {d})+1e-12)",
            ),
            (
                f"VSUMN{d}",
                f"Sum(Greater(Ref($volume, 1)-$volume, 0), {d})"
                f"/(Sum(Abs($volume-Ref($volume, 1)), {d})+1e-12)",
            ),
            (
                f"VSUMD{d}",
                f"(Sum(Greater($volume-Ref($volume, 1), 0), {d})"
                f"-Sum(Greater(Ref($volume, 1)-$volume, 0), {d}))"
                f"/(Sum(Abs($volume-Ref($volume, 1)), {d})+1e-12)",
            ),
        ]
    return pairs


def builtin_model() -> FactorModel:
    factors = [
        Factor(name=n, expr=e, group=classify(n), desc=describe(n)) for n, e in _builtin_pairs()
    ]
    return FactorModel(
        factors=factors,
        source="builtin",
        commit_sha="",
        commit_time=None,
        fetched_count=len(factors),
    )


BUILTIN_ALPHA158 = _builtin_pairs
