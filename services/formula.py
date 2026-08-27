"""主场系统全部数值公式（配置驱动，纯函数，无 I/O）。

所有阈值/系数均来自 config.defaults.DEFAULT_CONFIG / _conf_schema.json，
管理员可通过 /主场设置 调整而不改代码。
"""

import random
import re

from ..config.defaults import parse_json_object, parse_float_list

# 子设施固定键名
FACILITY_COMMERCIAL = "commercial"
FACILITY_BROADCAST = "broadcast"
FACILITY_PITCH = "pitch"
FACILITY_YOUTH = "youth"
FACILITY_MEDICAL = "medical"

# 天气键（对应 weather_ranges / weather_probabilities）
WX_SUNNY = "晴"
WX_CLOUDY = "多云"
WX_RAIN = "雨"
WX_SNOW = "雪"

DEFAULT_COMPETITION = "联赛"

_WEEKDAY_CHARS = ("一", "二", "三", "四", "五", "六", "日")

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_WEEK_TOKEN_RE = re.compile(r"^[Ww]?(\d+)$")


def weekday_name(day_no) -> str:
    """星期单字：1→一 … 7→日；非法返回空。"""
    try:
        idx = int(day_no) - 1
    except (TypeError, ValueError):
        return ""
    if 0 <= idx < len(_WEEKDAY_CHARS):
        return _WEEKDAY_CHARS[idx]
    return ""


def norm_time(raw) -> str:
    """开球时间归一化为 HH:MM；非法抛 ValueError。"""
    m = _TIME_RE.match(str(raw).strip())
    if not m:
        raise ValueError(f"时间需为 HH:MM: {raw}")
    h, minute = int(m.group(1)), int(m.group(2))
    if h > 23 or minute > 59:
        raise ValueError(f"时间非法: {raw}")
    return f"{h:02d}:{minute:02d}"


def parse_schedule_fields(extra) -> tuple[int | None, int | None, str | None]:
    """解析日程字段序列：周 天 时间（顺序固定，非法抛 ValueError）。

    周：W12 或 纯数字；天：D6 或 纯数字 1-7；时间：HH:MM。
    仅一个字段且为 HH:MM 时视为只有时间；D 前缀可跳过周直接写天。
    """
    tokens = [str(t).strip() for t in extra if str(t).strip()]
    if not tokens:
        return None, None, None
    if len(tokens) == 1 and _TIME_RE.match(tokens[0]):
        return None, None, norm_time(tokens[0])

    week = day = time = None
    i = 0
    t0 = tokens[0]
    if t0[:1].lower() == "d" and t0[1:].isdigit():
        day = int(t0[1:])
        i = 1
    else:
        m = _WEEK_TOKEN_RE.match(t0)
        if not m:
            raise ValueError(f"周需为 W+数字 或数字: {t0}")
        week = int(m.group(1))
        i = 1
        if i < len(tokens) and tokens[i][:1].lower() == "d" and tokens[i][1:].isdigit():
            day = int(tokens[i][1:])
            i += 1
    if i < len(tokens):
        t = tokens[i]
        if _TIME_RE.match(t):
            time = norm_time(t)
            i += 1
        elif day is None and _WEEK_TOKEN_RE.match(t):
            val = int(_WEEK_TOKEN_RE.match(t).group(1))
            if not (1 <= val <= 7):
                raise ValueError(f"天需为 1-7: {t}")
            day = val
            i += 1
        else:
            raise ValueError(f"无法解析的日程字段: {t}")
    if i < len(tokens):
        t = tokens[i]
        if _TIME_RE.match(t):
            time = norm_time(t)
            i += 1
        else:
            raise ValueError(f"无法解析的日程字段: {t}")
    if i < len(tokens):
        raise ValueError(f"多余字段: {' '.join(tokens[i:])}")
    if week is not None and week < 1:
        raise ValueError(f"周需为正数: {week}")
    if day is not None and not (1 <= day <= 7):
        raise ValueError(f"天需为 1-7: {day}")
    return week, day, time

DEFAULT_COMPETITION_ALIASES = {
    "次级联赛": ["次级", "次"],
    "顶级联赛": ["顶级", "顶", "超级"],
    "冠军杯": ["冠军杯", "冠军", "冠", "小组", "欧冠", "杯"],
}

_RESULT_PTS = {"W": 3, "D": 1, "L": 0}

# 比分：常规 (h-a) 可选点球 (PK h-a)，分隔符支持连字符/半角/全角冒号
_SCORE_RE = re.compile(
    r"^\s*(\d+)\s*[-:：]\s*(\d+)(?:\s*[Pp][Kk]\s*(\d+)\s*[-:：]\s*(\d+))?\s*$"
)


def result_from_score(text) -> str | None:
    """由比分推导主队赛果：2-1→W、1-2→L、1-1→D；常规平且有点球时按点球判。

    支持 2-1 / 2:1 / 2：1 / 0-0PK2-4 / 0-0PK4-2；无法解析返回 None。
    """
    m = _SCORE_RE.match(str(text or ""))
    if not m:
        return None
    h, a = int(m.group(1)), int(m.group(2))
    if h > a:
        return "W"
    if h < a:
        return "L"
    ph, pa = m.group(3), m.group(4)
    if ph is not None and pa is not None:
        if int(ph) > int(pa):
            return "W"
        if int(ph) < int(pa):
            return "L"
    return "D"


def competition_aliases(cfg: dict) -> dict:
    aliases = parse_json_object(cfg.get("competition_aliases", {}))
    return aliases if aliases else DEFAULT_COMPETITION_ALIASES


def split_competition(cfg: dict, s: str) -> tuple[str, str]:
    """按轮次前缀识别赛事，返回 (赛事, 剥离前缀后的剩余)；无前缀命中为联赛。

    与 parse_round_token 共用前缀表；不要求剩余部分含数字。
    """
    aliases = competition_aliases(cfg)
    for name, prefixes in aliases.items():
        for p in prefixes:
            p = str(p)
            if s.startswith(p):
                return name, s[len(p):]
    return DEFAULT_COMPETITION, s


def parse_round_token(cfg: dict, token) -> tuple[str, int]:
    """轮次支持文字，按前缀识别赛事：如「顶级9」「次级11」「冠军3」「小组赛第3轮」。

    返回 (赛事, 轮次号)；无前缀命中则赛事为「联赛」，轮次取其中的数字。
    """
    s = str(token or "").strip()
    if not s:
        raise ValueError("轮次不能为空")
    comp, rest = split_competition(cfg, s)
    digits = re.sub(r"\D", "", rest)
    if not digits:
        raise ValueError(f"轮次需包含数字: {token}")
    round_no = int(digits)
    if round_no < 1:
        raise ValueError(f"轮次需为正数: {token}")
    return comp, round_no


def tier_config(cfg: dict, tier: int) -> dict:
    """读取等级表配置（JSON 字符串 -> dict）。"""
    table = parse_json_object(cfg.get("tier_table", {}))
    entry = table.get(str(tier))
    if not isinstance(entry, dict):
        raise ValueError(f"等级 {tier} 不存在于等级表配置")
    return entry


def tier_attend_coef(cfg: dict, tier: int) -> float:
    return float(tier_config(cfg, tier).get("attend_coef", 1.0))


def tier_maintenance(cfg: dict, tier: int, capacity: int, home_matches: int) -> float:
    """半赛季维护费 = 基础 + 每万座每场费率 × 容量(万) × 场次（S9 公式）。"""
    t = tier_config(cfg, tier)
    base = float(t.get("base_maintenance", 0.0))
    rate = float(t.get("per_10k_rate", 0.0))
    return base + rate * (capacity / 10000.0) * home_matches


def tier_upgrade_cost(cfg: dict, tier: int) -> float:
    """升到 tier 级所需的单级升级费。"""
    return float(tier_config(cfg, tier).get("upgrade_cost", 0.0))


def weather_ranges(cfg: dict) -> dict:
    return parse_json_object(cfg.get("weather_ranges", {}))


def weather_probabilities(cfg: dict) -> dict:
    return parse_json_object(cfg.get("weather_probabilities", {}))


def roll_weather(cfg: dict) -> str:
    """按配置概率掷一种天气。"""
    probs = weather_probabilities(cfg)
    items = list(probs.items())
    total = sum(float(v) for _, v in items)
    if total <= 0:
        return WX_CLOUDY
    r = random.uniform(0, total)
    acc = 0.0
    for name, p in items:
        acc += float(p)
        if r <= acc:
            return name
    return items[-1][0] if items else WX_CLOUDY


def weather_coef(cfg: dict, weather: str | None) -> float:
    """天气系数：S8 表比例拉伸到 0.75-1.25，区间内均匀。"""
    ranges = weather_ranges(cfg)
    bounds = ranges.get(weather or "")
    if not bounds:
        return 1.0
    lo, hi = float(bounds[0]), float(bounds[1])
    if hi <= lo:
        return lo
    return random.uniform(lo, hi)


def form_coef_table(cfg: dict) -> dict:
    return parse_json_object(cfg.get("form_coef_table", {}))


def form_pts(results: list) -> int:
    """近 3 场 Pts（胜3平1负0），results 为最近几场的 result 字符串列表。

    取消（C）不计入，也不占「近 3 场」名额。
    """
    pts = 0
    seen = 0
    for r in results:
        key = str(r).strip().upper()
        if key == "C":
            continue
        pts += _RESULT_PTS.get(key, 0)
        seen += 1
        if seen >= 3:
            break
    return pts


def form_coef(cfg: dict, pts: int) -> float:
    """近 3 场 Pts → 上座系数（S8 表）。"""
    table = form_coef_table(cfg)
    key = str(min(max(pts, 0), 9))
    return float(table.get(key, 1.0))


NEUTRAL_FORM_PTS = 4


def effective_form_pts(results: list) -> int:
    """近 3 场 Pts，非取消场次不足 3 场时按中性（S8 默认 Pts=4 → 系数 1.0）。"""
    played = sum(1 for r in results if str(r).strip().upper() != "C")
    return form_pts(results) if played >= 3 else NEUTRAL_FORM_PTS


def opponent_coef(home_influence: float, away_influence: float) -> float:
    """对手系数 = 1 + 5%×客队影响力/主队影响力（S8 真实拉力）。"""
    if home_influence <= 0:
        return 1.0
    ratio = max(away_influence, 0.0) / home_influence
    return 1.0 + 0.05 * ratio


def attendance_multiplier(cfg: dict, tier: int) -> float:
    """上座倍数 = 基准 × (1 + 每级系数×等级)，0 级 = 基准（开局不变）。"""
    base = float(cfg.get("attendance_multiplier_base", 4.0))
    per_tier = float(cfg.get("attendance_multiplier_per_tier", 0.35))
    return base * (1.0 + per_tier * tier)


def fans_target_table(cfg: dict) -> list[tuple[float, float]]:
    """死忠目标阶梯：[(带上限, 斜率), ...]，上限 0 表示末段开放（高影响力趋于饱和）。

    解析失败或缺失时回退为单段线性 (0, fans_per_influence)。
    """
    bands: list[tuple[float, float]] = []
    try:
        data = parse_json_object(cfg.get("fans_target_table"))
        for band in data.get("bands", []):
            bands.append(
                (float(band.get("max_influence", 0) or 0),
                 float(band.get("slope", 0)))
            )
    except (TypeError, ValueError):
        bands = []
    if not bands:
        return [(0.0, float(cfg.get("fans_per_influence", 20.0)))]
    return bands


def diehard_target(cfg: dict, influence: float) -> float:
    """死忠目标：影响力-死忠阶梯分段线性，逐带累计，斜率递减。"""
    target = 0.0
    lower = 0.0
    for limit, slope in fans_target_table(cfg):
        upper = influence if limit <= 0 else min(influence, limit)
        if upper > lower:
            target += (upper - lower) * slope
        if limit <= 0 or influence <= limit:
            break
        lower = limit
    return target


def attendance(
    cfg: dict,
    fans: float,
    capacity: int,
    tier: int,
    weather: str | None,
    form_pts_value: int,
    home_influence: float,
    away_influence: float,
    next_attendance_mod: float = 1.0,
) -> int:
    """计算实际上座 = min(容量, 死忠 × 上座倍数 × 等级系数 × 战绩 × 天气 × 对手 × 扰动)。

    扰动 ±3%，调用方可用 random 直接注入（本函数固定为确定性种子之外的随机）。
    """
    multiplier = attendance_multiplier(cfg, tier)
    tier_coef = tier_attend_coef(cfg, tier)
    form = form_coef(cfg, form_pts_value)
    wx = weather_coef(cfg, weather)
    opp = opponent_coef(home_influence, away_influence)
    perturbation = random.uniform(0.97, 1.03)
    demand = fans * multiplier * tier_coef * form * wx * opp * perturbation * next_attendance_mod
    return int(min(capacity, max(0, demand)))


def match_revenues(
    cfg: dict, attendance_num: int, commercial_level: int, broadcast_level: int
) -> tuple[float, float, float]:
    """单场收入：票房 + 商业 + 转播（M）。"""
    attendance_wan = attendance_num / 10000.0
    ticket = attendance_wan * float(cfg.get("ticket_revenue_per_10k", 1.5))
    commercial = attendance_wan * float(cfg.get("commercial_per_10k_per_level", 0.1)) * commercial_level
    broadcast = float(cfg.get("broadcast_per_match_per_level", 0.3)) * broadcast_level
    return round(ticket, 4), round(commercial, 4), round(broadcast, 4)


def expansion_cost(cfg: dict, old_capacity: int, new_capacity: int) -> float:
    """扩建费用 = 差量座位 × 单价（0.1M/100座）。"""
    delta = max(0, new_capacity - old_capacity)
    return delta / 100.0 * float(cfg.get("expansion_cost_per_100", 0.1))


def facility_upgrade_costs(cfg: dict) -> list[float]:
    return parse_float_list(cfg.get("facility_upgrade_costs", "3,5,8,12,16"))


def facility_cost_to_level(cfg: dict, level: int) -> float:
    """子设施升到 N 级的累计费用。"""
    costs = facility_upgrade_costs(cfg)
    return sum(costs[:level])


def facility_effects(cfg: dict) -> dict:
    return parse_json_object(cfg.get("facility_effects", {}))


def fans_grow_coef(cfg: dict, attend_rate: float) -> float:
    """涨粉系数 = 速率 × (基准 + 跨度×上座率)。"""
    rate = float(cfg.get("fans_grow_rate", 0.5))
    base = float(cfg.get("fans_grow_heat_base", 0.6))
    span = float(cfg.get("fans_grow_heat_span", 0.4))
    return rate * (base + span * attend_rate)


def fans_drop_coef(cfg: dict, attend_rate: float) -> float:
    """掉粉系数 = 速率 × (1 + 加速×(1−上座率))，上座率越差跑得越快。"""
    rate = float(cfg.get("fans_drop_rate", 0.5))
    extra = float(cfg.get("fans_drop_heat_extra", 0.8))
    return rate * (1.0 + extra * (1.0 - attend_rate))


def evolve_fans(
    cfg: dict, fans: float, target: float, attend_rate: float,
    youth_level: int = 0, form_pts_value: int = 4,
) -> float:
    """死忠演化（非对称）。

    涨粉方向：靠拢 = 差额 × 涨粉系数 × (1 + 3%×青训级)
    掉粉方向：靠拢 = 差额 × 掉粉系数
    随后叠加战绩修正（Pts≥7 +5%，≤1 −5%），最后钳到 [0, 上限]。
    """
    cap = float(cfg.get("fans_cap", 10000))
    diff = target - fans
    if diff > 0:
        coef = fans_grow_coef(cfg, attend_rate)
        coef *= 1.0 + 0.03 * youth_level
        new_fans = fans + diff * coef
    else:
        coef = fans_drop_coef(cfg, attend_rate)
        new_fans = fans + diff * coef

    if form_pts_value >= 7:
        new_fans *= 1.05
    elif form_pts_value <= 1:
        new_fans *= 0.95
    return min(max(new_fans, 0.0), cap)


def naming_fee(cfg: dict, capacity: int, fans: float, heat: float) -> float:
    """冠名报价/窗口 = (基准 + 容量系数×容量万 + 死忠系数×死忠万) × 热度。"""
    base = float(cfg.get("naming_base", 0.5))
    per_cap = float(cfg.get("naming_per_capacity_wan", 0.3))
    per_fans = float(cfg.get("naming_per_fans_wan", 0.12))
    return round((base + per_cap * (capacity / 10000.0) + per_fans * (fans / 10000.0)) * heat, 3)


def activity_income(cfg: dict, activity_type: str, pitch_level: int = 0, youth_level: int = 0) -> dict:
    """档期活动结算；返回 {income, extra_maintenance}（演唱会草皮损坏概率受草皮级影响）。"""
    config = parse_json_object(cfg.get("activity_config", {}))
    act = config.get(activity_type, {})
    if not act:
        return {"income": 0.0, "extra_maintenance": 0.0}
    income = float(act.get("income", 0.0))
    if "income_min" in act and "income_max" in act:
        income = random.uniform(float(act["income_min"]), float(act["income_max"]))
    extra = 0.0
    prob = float(act.get("pitch_damage_prob", 0.0))
    if prob > 0:
        effects = facility_effects(cfg)
        pitch = effects.get(FACILITY_PITCH, {})
        reduction = float(pitch.get("damage_reduction_per_level", 0.15))
        prob = max(0.0, prob * (1.0 - reduction * pitch_level))
        if random.random() < prob:
            extra = random.uniform(float(act.get("damage_min", 0.0)), float(act.get("damage_max", 0.0)))
    if activity_type == "concert":
        effects = facility_effects(cfg)
        pitch = effects.get(FACILITY_PITCH, {})
        boost = float(pitch.get("concert_boost_per_level", 0.1))
        income *= 1.0 + boost * pitch_level
    if activity_type == "youth_camp":
        factor = float(act.get("youth_level_factor", 0.0))
        income = income * (1.0 + factor * youth_level)
    return {"income": round(income, 3), "extra_maintenance": round(extra, 3)}


def activity_names(cfg: dict) -> dict[str, str]:
    """活动类型 → 展示名。"""
    config = parse_json_object(cfg.get("activity_config", {}))
    return {k: str(v.get("name", k)) for k, v in config.items()}