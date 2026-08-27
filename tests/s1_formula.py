"""数值锚点测试：开局四档影响力、等级倍数阶梯、天气区间、系数表、非对称掉粉。"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.config.defaults import DEFAULT_CONFIG  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services import formula  # noqa: E402

INFLUENCES = {
    "min": (88.55, 2302),
    "median": (126.41, 3261),
    "mean": (134.68, 3443),
    "max": (214.42, 4773),
}
CAPACITY0 = 12000


def _cfg():
    return dict(DEFAULT_CONFIG)


async def test_diehard_target():
    """饱和阶梯：26/22/15/12 三断点 120/160/200，逐带分段线性累计。"""
    cfg = _cfg()
    for name, (infl, fans) in INFLUENCES.items():
        got = formula.diehard_target(cfg, infl)
        assert abs(got - fans) < 1, f"{name}: target {got} != {fans}"
    # 锚点：断点处取带内斜率满额
    anchors = {
        100: 2600, 120: 3120, 126.41: 3261.02, 160: 4000,
        200: 4600, 214.42: 4773.04, 248: 5176, 277.28: 5527.36, 300: 5800,
    }
    for infl, exp in anchors.items():
        got = formula.diehard_target(cfg, infl)
        assert abs(got - exp) < 1e-9, f"{infl}: {got} != {exp}"
    # 跨带连续（断点两侧差 < 1）
    for bp in (120, 160, 200):
        left = formula.diehard_target(cfg, bp - 0.01)
        right = formula.diehard_target(cfg, bp + 0.01)
        assert abs(left - right) < 1.0, f"断点 {bp} 不连续: {left} vs {right}"
    # 回退：表非法/缺失 → 线性 死忠系数×影响力
    cfg2 = dict(cfg)
    cfg2["fans_target_table"] = "{bad json"
    assert abs(formula.diehard_target(cfg2, 150) - 3000) < 1e-9
    cfg3 = dict(cfg)
    cfg3.pop("fans_target_table")
    assert abs(formula.diehard_target(cfg3, 100) - 2000) < 1e-9
    assert formula.diehard_target(cfg, 214.42) <= float(cfg["fans_cap"]), "死忠目标应低于上限"


async def test_attendance_multiplier_ladder():
    cfg = _cfg()
    expected = [4.0, 5.4, 6.8, 8.2, 9.6]
    for tier, exp in enumerate(expected):
        got = formula.attendance_multiplier(cfg, tier)
        assert abs(got - exp) < 1e-9, f"tier {tier}: {got} != {exp}"


async def test_opening_attendance_four_brackets():
    """开局 0 级 1.2 万座：弱队（阶梯 2302）约八成上座，中游与头部坐满。"""
    cfg = _cfg()
    import random

    random.seed(2026)
    for name, (infl, fans) in INFLUENCES.items():
        infl_fans = formula.diehard_target(cfg, infl)
        att = formula.attendance(
            cfg, fans=infl_fans, capacity=CAPACITY0, tier=0,
            weather=None, form_pts_value=4,
            home_influence=infl, away_influence=infl,
        )
        if name == "min":
            assert 9000 <= att <= 10200, f"min: {att}"
        else:
            assert att == CAPACITY0, f"{name} 应坐满: {att}"


async def test_weather_ranges():
    cfg = _cfg()
    import json
    import random

    random.seed(7)
    table = json.loads(cfg["weather_ranges"])
    for wx in table:
        bounds = table[wx]
        coef = formula.weather_coef(cfg, wx)
        assert float(bounds[0]) <= coef <= float(bounds[1]), f"{wx}: {coef}"
    # 区间跨 0.75-1.25
    allvals = [v for bounds in table.values() for v in bounds]
    assert min(allvals) == 0.75, "最低应为 0.75"
    assert max(allvals) == 1.25, "最高应为 1.25"


async def test_form_coef_table():
    cfg = _cfg()
    assert formula.form_coef(cfg, 0) == 0.7
    assert formula.form_coef(cfg, 4) == 1.0
    assert formula.form_coef(cfg, 7) == 1.19
    assert formula.form_coef(cfg, 9) == 1.25
    assert formula.form_coef(cfg, 99) == 1.25  # 钳制
    assert formula.form_pts(["W", "W", "D"]) == 7
    assert formula.form_pts(["L", "L", "D"]) == 1


async def test_extreme_scenarios():
    """连败 × 雪天极端场景：需求应显著压低但仍非负。"""
    cfg = _cfg()
    import random

    random.seed(3)
    fans = 4288  # 顶级队
    att = formula.attendance(
        cfg, fans=fans, capacity=100000, tier=4,
        weather="雪", form_pts_value=0,
        home_influence=214.42, away_influence=88.55,
    )
    demand_base = fans * formula.attendance_multiplier(cfg, 4) * formula.tier_attend_coef(cfg, 4)
    assert att < demand_base * 0.55, f"极端场景应显著低于基础需求: {att} vs {demand_base}"
    assert att >= 0


async def test_maintenance_s9():
    cfg = _cfg()
    m = formula.tier_maintenance(cfg, 0, 12000, 6)
    assert abs(m - (2.0 + 0.8 * 1.2 * 6)) < 1e-9, m
    m4 = formula.tier_maintenance(cfg, 4, 100000, 6)
    assert abs(m4 - (14.0 + 0.2 * 10 * 6)) < 1e-9, m4


async def test_match_revenues():
    cfg = _cfg()
    ticket, commercial, broadcast = formula.match_revenues(cfg, 21000, 3, 3)
    assert abs(ticket - 3.15) < 1e-6, ticket
    assert abs(commercial - 0.63) < 1e-6, commercial
    assert abs(broadcast - 0.9) < 1e-6, broadcast


async def test_expansion_and_facility_costs():
    cfg = _cfg()
    cost = formula.expansion_cost(cfg, 12000, 25000)
    assert abs(cost - 13.0) < 1e-9, cost  # 13000座 / 100 × 0.1
    assert formula.facility_cost_to_level(cfg, 0) == 0
    assert formula.facility_cost_to_level(cfg, 1) == 3.0
    assert formula.facility_cost_to_level(cfg, 3) == 16.0
    assert formula.facility_cost_to_level(cfg, 5) == 44.0


async def test_asymmetric_fans_evolution():
    cfg = _cfg()
    # 满座两侧对称：都是 50%
    grow = formula.fans_grow_coef(cfg, 1.0)
    drop = formula.fans_drop_coef(cfg, 1.0)
    assert abs(grow - 0.5) < 1e-9
    assert abs(drop - 0.5) < 1e-9
    # 55% 上座率：涨 41%（0.5×0.82），掉 68%（0.5×1.36）
    grow55 = formula.fans_grow_coef(cfg, 0.55)
    drop55 = formula.fans_drop_coef(cfg, 0.55)
    assert abs(grow55 - 0.41) < 1e-9, grow55
    assert abs(drop55 - 0.68) < 1e-9, drop55
    # 示例：fans 4288 → target 3000，DropCoef 0.68（Pts 中性）
    fans = formula.evolve_fans(cfg, fans=4288, target=3000, attend_rate=0.55, form_pts_value=4)
    assert 3400 <= fans <= 3430, fans


async def test_activity_income():
    cfg = _cfg()
    import random

    random.seed(11)
    r = formula.activity_income(cfg, "esports")
    assert r["income"] >= 2.0 and r["income"] <= 4.0
    r2 = formula.activity_income(cfg, "open_day")
    assert abs(r2["income"] - 0.5) < 1e-9
    r3 = formula.activity_income(cfg, "idle")
    assert r3["income"] == 0.0


async def test_naming_fee():
    cfg = _cfg()
    fee = formula.naming_fee(cfg, 35000, 3261, 1.0)
    # 公式保留 3 位小数
    assert abs(fee - 1.589) < 1e-6, fee


async def test_parse_round_token():
    cfg = _cfg()
    cases = [
        ("1", "联赛", 1),
        ("第9轮", "联赛", 9),
        ("顶级9", "顶级联赛", 9),
        ("顶3", "顶级联赛", 3),
        ("超级联赛第2轮", "顶级联赛", 2),
        ("次级11", "次级联赛", 11),
        ("次第5轮", "次级联赛", 5),
        ("冠军3", "冠军杯", 3),
        ("冠军杯第4轮", "冠军杯", 4),
        ("小组赛第3轮", "冠军杯", 3),
        ("欧冠第4轮", "冠军杯", 4),
    ]
    for token, comp, rnd in cases:
        got_comp, got_rnd = formula.parse_round_token(cfg, token)
        assert (got_comp, got_rnd) == (comp, rnd), f"{token}: {got_comp},{got_rnd}"
    for bad in ("", "abc", "顶级", "第轮"):
        try:
            formula.parse_round_token(cfg, bad)
            assert False, f"应拒绝 {bad}"
        except ValueError:
            pass


async def test_split_competition():
    """前缀剥离：识别赛事并返回剩余（不要求剩余含数字，供命名轮次使用）。"""
    cfg = _cfg()
    cases = [
        ("1", "联赛", "1"),
        ("顶级9", "顶级联赛", "9"),
        ("顶级", "顶级联赛", ""),
        ("顶3", "顶级联赛", "3"),
        ("次级11", "次级联赛", "11"),
        ("小组赛", "冠军杯", "赛"),
        ("abc", "联赛", "abc"),
        ("", "联赛", ""),
    ]
    for token, comp, rest in cases:
        got_comp, got_rest = formula.split_competition(cfg, token)
        assert (got_comp, got_rest) == (comp, rest), f"{token}: {got_comp},{got_rest}"


async def test_result_from_score():
    cases = [
        ("2-1", "W"), ("1-2", "L"), ("1-1", "D"),
        ("2:1", "W"), ("2：1", "W"),
        ("0-0PK2-4", "L"), ("0-0PK4-2", "W"), ("0-0PK2-2", "D"),
        ("1-1PK0-0", "D"),
        ("0-0PK", None), ("abc", None), ("2", None), ("2-1-3", None), ("", None),
    ]
    for text, expect in cases:
        got = formula.result_from_score(text)
        assert got == expect, f"{text}: {got!r} vs {expect!r}"


async def test_form_pts_skips_cancelled():
    """取消（C）不计入积分，也不占「近 3 场」名额。"""
    assert formula.form_pts(["W", "W", "D"]) == 7
    assert formula.form_pts(["C", "W"]) == 3
    assert formula.form_pts(["W", "C", "W", "W"]) == 9  # 前 3 场非取消 = W,W,W
    assert formula.form_pts(["C", "C", "C"]) == 0


async def test_effective_form_pts():
    """非取消场次不足 3 场一律按中性 4 分；满 3 场恢复实际积分。"""
    assert formula.NEUTRAL_FORM_PTS == 4
    assert formula.effective_form_pts([]) == 4
    assert formula.effective_form_pts(["W"]) == 4
    assert formula.effective_form_pts(["L", "D"]) == 4  # 实际仅 1 分也强制中性
    assert formula.effective_form_pts(["C", "W"]) == 4
    assert formula.effective_form_pts(["C", "C"]) == 4
    assert formula.effective_form_pts(["W", "W"]) == 4
    # 满 3 场后走真实路径（取消不占名额照常跳过）
    assert formula.effective_form_pts(["W", "L", "L"]) == 3
    assert formula.effective_form_pts(["W", "W", "D"]) == 7
    assert formula.effective_form_pts(["C", "L", "L", "W"]) == 3


def run_all():
    import asyncio

    failures = 0
    for name in sorted([n for n in dir() if n.startswith("test_")]):
        fn = globals()[name]
        try:
            asyncio.run(fn())
            print(f"  PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"s1_formula: {failures} failed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)