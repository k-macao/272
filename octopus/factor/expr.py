"""qlib 因子表达式求值引擎（纯标准库实现）。

microsoft/qlib 的因子是一串表达式字符串，例如：

    "($close-$open)/$open"
    "Mean($close, 20)/$close"
    "Corr($close, Log($volume+1), 30)"
    "Std(Abs($close/Ref($close, 1)-1)*$volume, 10)/(Mean(...)+1e-12)"

qlib 本体依赖 numpy/pandas/cython，装不动也没必要装 —— 我们只需要在
几百根日线上算出因子的**最新值**。所以这里自己实现一套等价语义的
滚动算子，输入输出都是 list[float | None]，缺失位置一律 None（不臆造）。

安全性：表达式来自 GitHub 上的公开仓库，仍按不可信输入处理 ——
先用 ast 解析并做节点白名单校验，只允许算术/比较/函数调用，
禁止属性访问、下标、lambda、推导式等一切可能逃逸的语法。
"""

from __future__ import annotations

import ast
import math
import re
from typing import Callable, Iterable, Sequence

Number = float | int
Series = list[float | None]

#: 行情字段名 -> 表达式里的 $ 变量
PRICE_FIELDS = ("open", "high", "low", "close", "volume", "vwap", "amount", "factor")

_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_VAR_PREFIX = "QF_"


class ExprError(ValueError):
    """表达式不合法或无法求值。"""


# ---------------------------------------------------------------------------
# 向量类型：对 list[float|None] 的逐元素运算，None 具有传染性
# ---------------------------------------------------------------------------
class Vec:
    """一列时间序列值。长度固定，缺失值用 None 表示。"""

    __slots__ = ("data",)

    def __init__(self, data: Iterable[float | None]) -> None:
        self.data = list(data)

    def __len__(self) -> int:
        return len(self.data)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Vec({self.data[:3]}...{self.data[-1:]}, n={len(self.data)})"

    # --- 逐元素运算 ---------------------------------------------------
    def _binary(self, other: "Vec | Number", fn: Callable[[float, float], float]) -> "Vec":
        if isinstance(other, Vec):
            if len(other) != len(self):
                raise ExprError("参与运算的序列长度不一致")
            pairs = zip(self.data, other.data)
        else:
            rhs = float(other)
            pairs = ((v, rhs) for v in self.data)
        out: Series = []
        for left, right in pairs:
            if left is None or right is None:
                out.append(None)
                continue
            try:
                out.append(float(fn(float(left), float(right))))
            except (ZeroDivisionError, ValueError, OverflowError):
                out.append(None)
        return Vec(out)

    def __add__(self, other):
        return self._binary(other, lambda a, b: a + b)

    __radd__ = __add__

    def __sub__(self, other):
        return self._binary(other, lambda a, b: a - b)

    def __rsub__(self, other):
        return _scalar_vec(other, len(self))._binary(self, lambda a, b: a - b)

    def __mul__(self, other):
        return self._binary(other, lambda a, b: a * b)

    __rmul__ = __mul__

    def __truediv__(self, other):
        return self._binary(other, _safe_div)

    def __rtruediv__(self, other):
        return _scalar_vec(other, len(self))._binary(self, _safe_div)

    def __pow__(self, other):
        return self._binary(other, lambda a, b: a ** b)

    def __neg__(self):
        return Vec([None if v is None else -v for v in self.data])

    def __pos__(self):
        return Vec(list(self.data))

    # 比较：qlib 里 ($close>Ref($close,1)) 会参与 Mean 求和，返回 1.0/0.0
    def __gt__(self, other):
        return self._binary(other, lambda a, b: 1.0 if a > b else 0.0)

    def __lt__(self, other):
        return self._binary(other, lambda a, b: 1.0 if a < b else 0.0)

    def __ge__(self, other):
        return self._binary(other, lambda a, b: 1.0 if a >= b else 0.0)

    def __le__(self, other):
        return self._binary(other, lambda a, b: 1.0 if a <= b else 0.0)

    def __eq__(self, other):  # type: ignore[override]
        return self._binary(other, lambda a, b: 1.0 if a == b else 0.0)

    def __ne__(self, other):  # type: ignore[override]
        return self._binary(other, lambda a, b: 1.0 if a != b else 0.0)

    __hash__ = None  # type: ignore[assignment]

    # --- 取值 -----------------------------------------------------------
    def last(self) -> float | None:
        return self.data[-1] if self.data else None


def _safe_div(a: float, b: float) -> float:
    if b == 0:
        raise ZeroDivisionError
    return a / b


def _scalar_vec(value: Number, length: int) -> Vec:
    return Vec([float(value)] * length)


def _as_vec(value: "Vec | Number", length: int) -> Vec:
    return value if isinstance(value, Vec) else _scalar_vec(value, length)


def _window(n: object) -> int:
    try:
        size = int(n)
    except (TypeError, ValueError) as exc:
        raise ExprError(f"窗口参数不是整数：{n!r}") from exc
    if size <= 0:
        raise ExprError(f"窗口必须为正整数，收到 {size}")
    return size


def _slice(data: Series, idx: int, size: int) -> list[float] | None:
    """取 [idx-size+1, idx] 的完整窗口；有缺失或不足则返回 None。"""
    start = idx - size + 1
    if start < 0:
        return None
    chunk = data[start : idx + 1]
    if any(v is None for v in chunk):
        return None
    return [float(v) for v in chunk]  # type: ignore[arg-type]


def _rolling(x: Vec, size: int, fn: Callable[[list[float]], float | None]) -> Vec:
    out: Series = []
    for idx in range(len(x)):
        chunk = _slice(x.data, idx, size)
        if chunk is None:
            out.append(None)
            continue
        try:
            out.append(_finite(fn(chunk)))
        except (ZeroDivisionError, ValueError, OverflowError):
            out.append(None)
    return Vec(out)


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return None
    return value


# ---------------------------------------------------------------------------
# 算子 —— 语义对齐 qlib/data/ops.py
# ---------------------------------------------------------------------------
def op_ref(x: Vec, n) -> Vec:
    size = int(n)
    if size == 0:
        return Vec(list(x.data))
    if size < 0:
        raise ExprError("Ref 的偏移必须 >= 0")
    if size >= len(x):
        return Vec([None] * len(x))
    return Vec([None] * size + x.data[:-size])


def op_mean(x: Vec, n) -> Vec:
    return _rolling(x, _window(n), lambda c: sum(c) / len(c))


def op_sum(x: Vec, n) -> Vec:
    return _rolling(x, _window(n), sum)


def op_std(x: Vec, n) -> Vec:
    def _std(c: list[float]) -> float | None:
        if len(c) < 2:
            return None
        mu = sum(c) / len(c)
        return math.sqrt(sum((v - mu) ** 2 for v in c) / (len(c) - 1))  # pandas ddof=1

    return _rolling(x, _window(n), _std)


def op_var(x: Vec, n) -> Vec:
    std = op_std(x, n)
    return Vec([None if v is None else v * v for v in std.data])


def op_max(x: Vec, n) -> Vec:
    return _rolling(x, _window(n), max)


def op_min(x: Vec, n) -> Vec:
    return _rolling(x, _window(n), min)


def op_med(x: Vec, n) -> Vec:
    def _med(c: list[float]) -> float:
        s = sorted(c)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

    return _rolling(x, _window(n), _med)


def op_mad(x: Vec, n) -> Vec:
    def _mad(c: list[float]) -> float:
        mu = sum(c) / len(c)
        return sum(abs(v - mu) for v in c) / len(c)

    return _rolling(x, _window(n), _mad)


def op_quantile(x: Vec, n, q) -> Vec:
    ratio = float(q)

    def _q(c: list[float]) -> float:
        s = sorted(c)
        if len(s) == 1:
            return s[0]
        pos = ratio * (len(s) - 1)  # pandas 线性插值
        low = math.floor(pos)
        high = math.ceil(pos)
        if low == high:
            return s[int(pos)]
        return s[low] + (s[high] - s[low]) * (pos - low)

    return _rolling(x, _window(n), _q)


def op_rank(x: Vec, n) -> Vec:
    """当前值在过去 n 个值中的分位（pandas rolling rank pct=True 语义）。"""

    def _rank(c: list[float]) -> float:
        cur = c[-1]
        le = sum(1 for v in c if v <= cur)
        return le / len(c)

    return _rolling(x, _window(n), _rank)


def op_idxmax(x: Vec, n) -> Vec:
    def _idx(c: list[float]) -> float:
        return float(c.index(max(c)) + 1)  # qlib: argmax + 1

    return _rolling(x, _window(n), _idx)


def op_idxmin(x: Vec, n) -> Vec:
    def _idx(c: list[float]) -> float:
        return float(c.index(min(c)) + 1)

    return _rolling(x, _window(n), _idx)


def op_corr(x: Vec, y: Vec, n) -> Vec:
    size = _window(n)
    length = len(x)
    other = _as_vec(y, length)
    out: Series = []
    for idx in range(length):
        left = _slice(x.data, idx, size)
        right = _slice(other.data, idx, size)
        if left is None or right is None or len(left) < 2:
            out.append(None)
            continue
        out.append(_finite(_pearson(left, right)))
    return Vec(out)


def op_cov(x: Vec, y: Vec, n) -> Vec:
    size = _window(n)
    other = _as_vec(y, len(x))
    out: Series = []
    for idx in range(len(x)):
        left = _slice(x.data, idx, size)
        right = _slice(other.data, idx, size)
        if left is None or right is None or len(left) < 2:
            out.append(None)
            continue
        mx = sum(left) / len(left)
        my = sum(right) / len(right)
        cov = sum((a - mx) * (b - my) for a, b in zip(left, right)) / (len(left) - 1)
        out.append(_finite(cov))
    return Vec(out)


def _pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    ma = sum(a) / n
    mb = sum(b) / n
    va = sum((v - ma) ** 2 for v in a)
    vb = sum((v - mb) ** 2 for v in b)
    if va <= 0 or vb <= 0:
        return None  # 常数序列相关系数无定义，宁可缺失
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(va * vb)


def _linreg(c: list[float]) -> tuple[float, float]:
    """对 y=c、x=0..n-1 做最小二乘，返回 (斜率, 截距)。"""
    n = len(c)
    mx = (n - 1) / 2
    my = sum(c) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    sxy = sum((i - mx) * (v - my) for i, v in enumerate(c))
    slope = sxy / sxx if sxx else 0.0
    return slope, my - slope * mx


def op_slope(x: Vec, n) -> Vec:
    return _rolling(x, _window(n), lambda c: _linreg(c)[0])


def op_rsquare(x: Vec, n) -> Vec:
    def _r2(c: list[float]) -> float | None:
        slope, intercept = _linreg(c)
        my = sum(c) / len(c)
        sst = sum((v - my) ** 2 for v in c)
        if sst <= 0:
            return None
        sse = sum((v - (slope * i + intercept)) ** 2 for i, v in enumerate(c))
        return 1.0 - sse / sst

    return _rolling(x, _window(n), _r2)


def op_resi(x: Vec, n) -> Vec:
    """最新一点相对回归直线的残差（qlib Resi 取窗口最后一点残差）。"""

    def _resi(c: list[float]) -> float:
        slope, intercept = _linreg(c)
        i = len(c) - 1
        return c[i] - (slope * i + intercept)

    return _rolling(x, _window(n), _resi)


def op_delta(x: Vec, n) -> Vec:
    return x - op_ref(x, n)


def op_ema(x: Vec, n) -> Vec:
    size = _window(n)
    alpha = 2.0 / (size + 1)
    out: Series = []
    prev: float | None = None
    for value in x.data:
        if value is None:
            out.append(None)
            continue
        prev = value if prev is None else alpha * value + (1 - alpha) * prev
        out.append(prev)
    return Vec(out)


def op_wma(x: Vec, n) -> Vec:
    def _wma(c: list[float]) -> float:
        weights = range(1, len(c) + 1)
        return sum(v * w for v, w in zip(c, weights)) / sum(weights)

    return _rolling(x, _window(n), _wma)


def _elementwise(a, b, fn: Callable[[float, float], float], length: int) -> Vec:
    return _as_vec(a, length)._binary(_as_vec(b, length), fn)


def op_greater(a, b) -> Vec:
    length = len(a) if isinstance(a, Vec) else len(b)
    return _elementwise(a, b, lambda x, y: x if x > y else y, length)


def op_less(a, b) -> Vec:
    length = len(a) if isinstance(a, Vec) else len(b)
    return _elementwise(a, b, lambda x, y: x if x < y else y, length)


def op_abs(x) -> Vec:
    if not isinstance(x, Vec):
        return abs(float(x))  # type: ignore[return-value]
    return Vec([None if v is None else abs(v) for v in x.data])


def op_log(x) -> Vec:
    if not isinstance(x, Vec):
        return math.log(float(x))  # type: ignore[return-value]
    return Vec([None if v is None or v <= 0 else math.log(v) for v in x.data])


def op_sign(x: Vec) -> Vec:
    return Vec([None if v is None else (1.0 if v > 0 else (-1.0 if v < 0 else 0.0)) for v in x.data])


def op_if(cond: Vec, left, right) -> Vec:
    length = len(cond)
    a = _as_vec(left, length)
    b = _as_vec(right, length)
    out: Series = []
    for flag, x, y in zip(cond.data, a.data, b.data):
        if flag is None:
            out.append(None)
        else:
            out.append(x if flag else y)
    return Vec(out)


FUNCTIONS: dict[str, Callable] = {
    "Ref": op_ref,
    "Mean": op_mean,
    "Sum": op_sum,
    "Std": op_std,
    "Var": op_var,
    "Max": op_max,
    "Min": op_min,
    "Med": op_med,
    "Mad": op_mad,
    "Quantile": op_quantile,
    "Rank": op_rank,
    "IdxMax": op_idxmax,
    "IdxMin": op_idxmin,
    "Corr": op_corr,
    "Cov": op_cov,
    "Slope": op_slope,
    "Rsquare": op_rsquare,
    "Resi": op_resi,
    "Delta": op_delta,
    "EMA": op_ema,
    "WMA": op_wma,
    "Greater": op_greater,
    "Less": op_less,
    "Abs": op_abs,
    "Log": op_log,
    "Sign": op_sign,
    "If": op_if,
}

#: ast 节点白名单 —— 只放行纯算术表达式
_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Gt,
    ast.Lt,
    ast.GtE,
    ast.LtE,
    ast.Eq,
    ast.NotEq,
)


def _to_python(expr: str) -> str:
    """把 qlib 表达式翻译成受限 Python 表达式：$close -> QF_close。"""
    return _DOLLAR.sub(lambda m: _VAR_PREFIX + m.group(1), expr)


def compile_expr(expr: str) -> ast.Expression:
    """解析并做安全校验，返回可 eval 的 AST。"""
    source = _to_python(expr)
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"表达式语法错误：{expr}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExprError(f"表达式包含不允许的语法 {type(node).__name__}：{expr}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                name = getattr(node.func, "id", "?")
                raise ExprError(f"未知算子 {name}：{expr}")
            if node.keywords:
                raise ExprError(f"算子不支持关键字参数：{expr}")
        if isinstance(node, ast.Name) and not (
            node.id in FUNCTIONS or node.id.startswith(_VAR_PREFIX)
        ):
            raise ExprError(f"未知变量 {node.id}：{expr}")
    return tree


class FactorEngine:
    """在一段行情序列上对 qlib 表达式求值。

    >>> engine = FactorEngine({"close": [1, 2, 3], "open": [1, 1, 1]})
    >>> engine.evaluate_last("($close-$open)/$open")
    2.0
    """

    def __init__(self, fields: dict[str, Sequence[float | None]]) -> None:
        if not fields:
            raise ExprError("行情字段为空")
        lengths = {len(v) for v in fields.values()}
        if len(lengths) != 1:
            raise ExprError("各行情字段长度不一致")
        self.length = lengths.pop()
        self.env: dict[str, object] = {
            _VAR_PREFIX + name: Vec(values) for name, values in fields.items()
        }
        self.env.update(FUNCTIONS)
        self._cache: dict[str, ast.Expression] = {}

    # ------------------------------------------------------------------
    def evaluate(self, expr: str) -> Vec:
        tree = self._cache.get(expr)
        if tree is None:
            tree = compile_expr(expr)
            self._cache[expr] = tree
        code = compile(tree, filename="<factor>", mode="eval")
        try:
            value = eval(code, {"__builtins__": {}}, dict(self.env))  # noqa: S307 - 已白名单校验
        except ExprError:
            raise
        except Exception as exc:  # noqa: BLE001 - 求值异常统一收敛
            raise ExprError(f"表达式求值失败 {expr}: {exc}") from exc
        if not isinstance(value, Vec):
            value = _scalar_vec(float(value), self.length)
        return value

    def evaluate_last(self, expr: str) -> float | None:
        """只要最新一期的因子值（横截面分析用）。"""
        try:
            return _finite(self.evaluate(expr).last())
        except ExprError:
            return None

    def evaluate_many(self, pairs: Iterable[tuple[str, str]]) -> dict[str, float | None]:
        """批量求值 [(因子名, 表达式)] -> {因子名: 最新值}。"""
        out: dict[str, float | None] = {}
        for name, expr in pairs:
            out[name] = self.evaluate_last(expr)
        return out
