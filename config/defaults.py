import json
import os

PLUGIN_VERSION = "1.5.0"
"""插件版本号，与 metadata.yaml 保持一致。"""

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

# 小数类配置的允许区间（闭区间）
_FLOAT_RANGES = {
    "default_influence": (1.0, 1000.0),
    "fans_per_influence": (0.1, 1000.0),
    "fans_grow_rate": (0.0, 1.0),
    "fans_grow_heat_base": (0.0, 1.0),
    "fans_grow_heat_span": (0.0, 1.0),
    "fans_drop_rate": (0.0, 1.0),
    "fans_drop_heat_extra": (0.0, 2.0),
    "attendance_multiplier_base": (0.1, 100.0),
    "attendance_multiplier_per_tier": (0.0, 2.0),
    "ticket_revenue_per_10k": (0.0, 100.0),
    "commercial_per_10k_per_level": (0.0, 10.0),
    "broadcast_per_match_per_level": (0.0, 10.0),
    "expansion_cost_per_100": (0.0, 10.0),
    "naming_base": (0.0, 100.0),
    "naming_per_capacity_wan": (0.0, 10.0),
    "naming_per_fans_wan": (0.0, 10.0),
    "naming_terminate_penalty": (0.0, 1.0),
    "naming_fans_drop_threshold": (0.0, 1.0),
    "naming_terminate_probability": (0.0, 1.0),
    "event_hit_probability": (0.0, 1.0),
    "import_max_file_size_mb": (0.1, 500.0),
    "event_money_clamp": (0.0, 1000.0),
    "event_fans_clamp": (0.0, 1.0),
    "event_maintenance_clamp": (0.0, 1000.0),
    "rename_fee": (0.0, 1000.0),
    "start_funds": (0.0, 1e9),
    "build_credit_ratio": (0.0, 1.0),
    "llm_timeout_seconds": (1, 300),
}

# 整数配置业务上限（超出拒绝）；未列出的整数键不设上限
_INT_UPPER_BOUNDS = {
    "fans_cap": 10_000_000,
    "naming_windows": 1000,
    "activity_slots": 20,
    "max_open_tier": 4,
    "llm_max_calls": 100,
    "backup_keep_count": 10_000,
}

_INT_LOWER_BOUNDS = {
    "fans_cap": 1,
    "naming_windows": 1,
    "activity_slots": 0,
    "max_open_tier": 0,
    "llm_max_calls": 0,
    "backup_keep_count": 1,
}

_TIME_KEYS = ("backup_time",)

_JSON_STRING_KEYS = (
    "tier_table",
    "weather_probabilities",
    "weather_ranges",
    "form_coef_table",
    "facility_effects",
    "activity_config",
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


def parse_float_list(raw) -> list[float]:
    """解析逗号分隔的小数列表（子设施升级费用）。"""
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    out = []
    for p in parts:
        try:
            out.append(float(p))
        except ValueError:
            raise ValueError(f"含非法数字: {p}")
    return out


def validate_and_cast(key: str, raw: str):
    """校验并转换管理员通过 /主场设置 传入的配置值。"""
    if key not in DEFAULT_CONFIG:
        raise ValueError(f"未知配置项: {key}")

    if key in _JSON_STRING_KEYS:
        return _validate_json_object(raw, key)

    if key == "facility_upgrade_costs":
        values = parse_float_list(raw)
        if len(values) < 1 or len(values) > 20:
            raise ValueError("facility_upgrade_costs 需为 1-20 个逗号分隔的小数")
        if any(v <= 0 for v in values):
            raise ValueError("facility_upgrade_costs 每级费用需为正数")
        return ",".join(f"{v:g}" for v in values)

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
        lo, hi = _FLOAT_RANGES.get(key, (None, None))
        if lo is not None and parsed < lo:
            raise ValueError(f"配置 {key} 不能小于 {lo}")
        if hi is not None and parsed > hi:
            raise ValueError(f"配置 {key} 不能大于 {hi}")
        return parsed
    return raw.strip()
