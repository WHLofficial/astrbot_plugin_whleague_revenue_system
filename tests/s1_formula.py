"""天气与轮次解析公式测试：区间锚点、概率归一、轮次 token 解析。"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrbot_plugin_whleague_revenue_system.config.defaults import DEFAULT_CONFIG  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services import formula  # noqa: E402


def _cfg():
    return dict(DEFAULT_CONFIG)


async def test_weather_ranges():
    """天气系数区间：四档天气各有 [lo, hi]，lo<=hi。"""
    cfg = _cfg()
    ranges = formula.weather_ranges(cfg)
    assert set(ranges) == {"晴", "多云", "雨", "雪"}, ranges
    for name, bounds in ranges.items():
        lo, hi = float(bounds[0]), float(bounds[1])
        assert lo <= hi, (name, bounds)


async def test_weather_coef_within_range():
    """weather_coef 落在对应天气区间内；未知天气回落 1.0。"""
    cfg = _cfg()
    ranges = formula.weather_ranges(cfg)
    for name, bounds in ranges.items():
        lo, hi = float(bounds[0]), float(bounds[1])
        for _ in range(50):
            c = formula.weather_coef(cfg, name)
            assert lo <= c <= hi, (name, c, bounds)
    assert formula.weather_coef(cfg, None) == 1.0
    assert formula.weather_coef(cfg, "未知天气") == 1.0


async def test_roll_weather_with_coef():
    """roll_weather_with_coef：天气在四档内、系数 4 位小数且落在对应区间。"""
    cfg = _cfg()
    ranges = formula.weather_ranges(cfg)
    for _ in range(50):
        weather, coef = formula.roll_weather_with_coef(cfg)
        assert weather in ("晴", "多云", "雨", "雪"), weather
        lo, hi = float(ranges[weather][0]), float(ranges[weather][1])
        assert lo <= coef <= hi, (weather, coef)
        assert coef == round(coef, 4), coef


async def test_parse_round_token():
    """轮次 token：前缀识别赛事 + 数字即轮次；无前缀=None（全部赛事）；非法报错。"""
    cfg = _cfg()
    assert formula.parse_round_token(cfg, "顶级9") == ("顶级联赛", 9)
    assert formula.parse_round_token(cfg, "次级11") == ("次级联赛", 11)
    assert formula.parse_round_token(cfg, "冠军3") == ("冠军杯", 3)
    # 「联赛」是顶级联赛的旧前缀（v2 迁移文本），「联赛12」按顶级联赛解析
    assert formula.parse_round_token(cfg, "联赛12") == ("顶级联赛", 12)
    # 纯数字 = 全部赛事（帮助文案口径），不再回落旧文本「联赛」
    assert formula.parse_round_token(cfg, "5") == (None, 5)
    try:
        formula.parse_round_token(cfg, "顶级")
        assert False, "无数字应报错"
    except ValueError:
        pass


async def test_split_competition():
    """split_competition：前缀剥离；无前缀命中为 None（全部赛事）。"""
    cfg = _cfg()
    assert formula.split_competition(cfg, "顶级9") == ("顶级联赛", "9")
    assert formula.split_competition(cfg, "冠军杯小组第3轮") == ("冠军杯", "小组第3轮")
    assert formula.split_competition(cfg, "12") == (None, "12")


async def test_competition_texts_alias():
    """库内文本等价集：顶级联赛查询同时命中 v2 迁移行「联赛」；其余赛事自映射。"""
    assert formula.competition_texts("顶级联赛") == ("顶级联赛", "联赛")
    assert formula.competition_texts("次级联赛") == ("次级联赛",)
    assert formula.competition_texts("冠军杯") == ("冠军杯",)
    assert formula.competition_texts(None) == ()
    # 写出方向归一（club 天气推送只认三类标准名）
    assert formula.normalize_competition("联赛") == "顶级联赛"
    assert formula.normalize_competition("顶级联赛") == "顶级联赛"
    assert formula.normalize_competition("冠军杯") == "冠军杯"
