import math
import re

_MAX_TEXT_LENGTH = 50
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_text(text: str, max_length: int = _MAX_TEXT_LENGTH) -> str:
    """剥离控制字符并截断，防止构造多行伪造消息。"""
    if not text:
        return ""
    return _CTRL_RE.sub("", str(text)).strip()[:max_length]


def parse_int(raw: str, min_val: int | None = None, max_val: int | None = None) -> int:
    try:
        val = int(str(raw).strip())
    except (ValueError, TypeError):
        raise ValueError(f"Invalid integer: {raw}")
    if min_val is not None and val < min_val:
        raise ValueError(f"Value {val} is below minimum {min_val}")
    if max_val is not None and val > max_val:
        raise ValueError(f"Value {val} exceeds maximum {max_val}")
    return val


def parse_float(raw: str, min_val: float | None = None, max_val: float | None = None) -> float:
    try:
        val = float(str(raw).strip())
    except (ValueError, TypeError):
        raise ValueError(f"Invalid number: {raw}")
    if not math.isfinite(val):
        # NaN 与任何区间比较恒 False，会绕过下面的范围检查
        raise ValueError(f"Invalid number: {raw}")
    if min_val is not None and val < min_val:
        raise ValueError(f"Value {val} is below minimum {min_val}")
    if max_val is not None and val > max_val:
        raise ValueError(f"Value {val} exceeds maximum {max_val}")
    return val


def parse_qq(raw: str) -> str:
    cleaned = str(raw).strip().lstrip("@")
    if not cleaned.isdigit():
        raise ValueError(f"Invalid QQ number: {raw}")
    return cleaned


def parse_qq_arg(raw: str) -> str | None:
    """从 @用户 / @昵称(QQ) / 昵称(QQ) / [CQ:at,qq=...] 形式中提取 QQ 号。"""
    s = str(raw).strip()
    if not s:
        return None
    m = re.search(r"\[CQ:at,qq=(\d+)\]", s)
    if m:
        return m.group(1)
    m = re.search(r"\((\d+)\)", s)
    if m:
        return m.group(1)
    if s.startswith("@"):
        m = re.match(r"^@(\d+)$", s)
        if m:
            return m.group(1)
    return None


_CIRCLE_NO = {"①": 1, "②": 2, "③": 3, "④": 4}


def parse_choice_no(raw: str) -> int:
    """解析随机事件选项号：支持 1/2/3/4 与 ①②③④。"""
    raw = str(raw).strip()
    if raw in _CIRCLE_NO:
        return _CIRCLE_NO[raw]
    return int(raw)


def format_m(value: float) -> str:
    """金额显示：去掉多余的尾零（最多 2 位小数）。"""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_int(value) -> str:
    """整数显示：加千分位。"""
    return f"{int(value):,}"