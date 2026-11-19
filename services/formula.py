"""天气与轮次解析公式（配置驱动，纯函数，无 I/O）。

v3 只保留展示插件用到的部分：天气抽取/系数固化、轮次 token 解析、星期展示。
所有阈值/系数来自 config.defaults.DEFAULT_CONFIG / _conf_schema.json。
"""

import random
import re

from ..config.defaults import parse_json_object

# 天气键（对应 weather_ranges / weather_probabilities）
WX_SUNNY = "晴"
WX_CLOUDY = "多云"
WX_RAIN = "雨"
WX_SNOW = "雪"

_WEEKDAY_CHARS = ("一", "二", "三", "四", "五", "六", "日")


def weekday_name(day_no) -> str:
    """星期单字：1→一 … 7→日；非法返回空。"""
    try:
        idx = int(day_no) - 1
    except (TypeError, ValueError):
        return ""
    if 0 <= idx < len(_WEEKDAY_CHARS):
        return _WEEKDAY_CHARS[idx]
    return ""


DEFAULT_COMPETITION_ALIASES = {
    "次级联赛": ["次级", "次"],
    "顶级联赛": ["顶级", "顶", "超级", "联赛"],
    "冠军杯": ["冠军杯", "冠军", "冠", "小组", "欧冠", "杯"],
}

# 库内 competition 文本的等价集：v15 以前迁移行存「联赛」（= tournament_id 1），
# v3 写路径统一「顶级联赛」——读侧按集合匹配，两种文本都命中（设计 §2 纯数字=全部赛事）。
_COMPETITION_TEXT_ALIASES = {
    "顶级联赛": ("顶级联赛", "联赛"),
}
# 单向归一（写出方向，如 club 天气推送的 competition 只认三类标准名）
_COMPETITION_TEXT_NORMALIZE = {"联赛": "顶级联赛"}


def normalize_competition(text: str) -> str:
    """库内 competition 文本归一：「联赛」→「顶级联赛」，其余原样。"""
    return _COMPETITION_TEXT_NORMALIZE.get(str(text), str(text))


def competition_texts(competition: str | None) -> tuple[str, ...]:
    """某赛事在库内的全部等价文本；None（全部赛事）返回空元组（调用方不加过滤）。"""
    if not competition:
        return ()
    return _COMPETITION_TEXT_ALIASES.get(competition, (competition,))


def competition_aliases(cfg: dict) -> dict:
    aliases = parse_json_object(cfg.get("competition_aliases", {}))
    return aliases if aliases else DEFAULT_COMPETITION_ALIASES


def split_competition(cfg: dict, s: str) -> tuple[str | None, str]:
    """按轮次前缀识别赛事，返回 (赛事, 剥离前缀后的剩余)；无前缀命中为 None（全部赛事）。"""
    aliases = competition_aliases(cfg)
    for name, prefixes in aliases.items():
        for p in prefixes:
            p = str(p)
            if s.startswith(p):
                return name, s[len(p):]
    return None, s


def parse_round_token(cfg: dict, token) -> tuple[str | None, int]:
    """轮次支持文字，按前缀识别赛事：如「顶级9」「次级11」「冠军3」「联赛12」。

    返回 (赛事, 轮次号)；无前缀命中则赛事为 None（= 全部赛事，与帮助文案一致），
    轮次取其中的数字。
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


def roll_weather_with_coef(cfg: dict) -> tuple[str, float]:
    """掷天气并抽定系数（一次抽定、落库固化，平台推送同源）。

    返回 (weather, coef)：coef 固定 round(.,4) 存储，跨端比对不因浮点尾数漂移。
    """
    weather = roll_weather(cfg)
    return weather, round(weather_coef(cfg, weather), 4)
