import json
import math
import os

PLUGIN_VERSION = "3.0.0"
"""插件版本号，与 metadata.yaml 保持一致（tests/s17_framework_compat 校验）。"""

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_conf_schema.json"
)

_TYPE_DEFAULTS = {
    "int": 0,
    "float": 0.0,
    "bool": False,
    "string": "",
    "list": [],
}

_TYPE_MAP = {
    "int": int,
    "float": float,
    "bool": bool,
    "string": str,
    "list": str,
}


def _load_schema() -> dict:
    if not os.path.exists(_SCHEMA_PATH):
        raise RuntimeError(f"缺少插件配置 schema 文件: {_SCHEMA_PATH}")
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    for key, meta in schema.items():
        if meta.get("type") not in _TYPE_DEFAULTS:
            raise RuntimeError(f"配置项 {key} 的类型 {meta.get('type')} 不受支持")
    return schema


_SCHEMA = _load_schema()

DEFAULT_CONFIG = {
    key: meta.get("default", _TYPE_DEFAULTS[meta["type"]])
    for key, meta in _SCHEMA.items()
}

TYPE_MAP = {key: _TYPE_MAP[meta["type"]] for key, meta in _SCHEMA.items()}

_LIST_KEYS = tuple(key for key, meta in _SCHEMA.items() if meta["type"] == "list")

# 整数配置业务上限（超出拒绝）；未列出的整数键不设上限
_INT_UPPER_BOUNDS = {
    "backup_keep_count": 10_000,
}

_INT_LOWER_BOUNDS = {
    "backup_keep_count": 1,
}

_TIME_KEYS = ("backup_time",)

_JSON_STRING_KEYS = (
    "weather_probabilities",
    "weather_ranges",
    "competition_aliases",
)


def _validate_json_object(raw: str, key: str) -> str:
    try:
        data = json.loads(str(raw))
    except (ValueError, TypeError):
        raise ValueError(f"{key} 需为 JSON 字符串")
    if not isinstance(data, dict):
        raise ValueError(f"{key} 需为 JSON 对象")
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def parse_group_list(raw):
    """将配置中的群白名单解析为列表，兼容 JSON 数组或逗号分隔文本。"""
    if isinstance(raw, (list, tuple)):
        return [str(g) for g in raw if str(g).strip()]
    if not isinstance(raw, str):
        return []
    s = raw.strip()
    if not s:
        return []
    try:
        data = json.loads(s)
        if isinstance(data, list):
            return [str(g) for g in data if str(g).strip()]
    except json.JSONDecodeError:
        pass
    return [g.strip() for g in s.split(",") if g.strip()]


def parse_json_object(raw) -> dict:
    """将 JSON 字符串配置解析为对象（服务层使用）。"""
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(str(raw or "{}"))
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def validate_and_cast(key: str, raw: str):
    """校验并转换管理员通过 WebUI/配置传入的配置值。"""
    if key not in DEFAULT_CONFIG:
        raise ValueError(f"未知配置项: {key}")

    if key in _JSON_STRING_KEYS:
        return _validate_json_object(raw, key)

    if key in _LIST_KEYS:
        return parse_group_list(raw)

    if key in _TIME_KEYS:
        import re

        m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", raw.strip())
        if not m:
            raise ValueError(f"{key} 需为 HH:MM 格式")
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"

    t = TYPE_MAP.get(key, str)
    if t is bool:
        low = raw.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
        raise ValueError(f"配置 {key} 需为布尔值 (true/false/1/0)")
    if t is int:
        try:
            parsed = int(raw.strip())
        except ValueError:
            raise ValueError(f"配置 {key} 需为整数")
        lower = _INT_LOWER_BOUNDS.get(key)
        if lower is not None and parsed < lower:
            raise ValueError(f"配置 {key} 不能小于 {lower}")
        upper = _INT_UPPER_BOUNDS.get(key)
        if upper is not None and parsed > upper:
            raise ValueError(f"配置 {key} 不能大于 {upper}")
        return parsed
    if t is float:
        try:
            parsed = float(raw.strip())
        except ValueError:
            raise ValueError(f"配置 {key} 需为数字")
        if not math.isfinite(parsed):
            # NaN 与任何区间比较恒 False，会绕过下面的范围检查
            raise ValueError(f"配置 {key} 需为有限数字")
        return parsed
    return raw.strip()
