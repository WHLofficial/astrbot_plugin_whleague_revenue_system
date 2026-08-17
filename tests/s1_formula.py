"""数值锚点测试：开局四档影响力、等级倍数阶梯、天气区间、系数表、非对称掉粉。"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.config.defaults import DEFAULT_CONFIG  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services import formula  # noqa: E402

INFLUENCES = {
    "min": (88.55, 1771),
    "median": (126.41, 2528),
    "mean": (134.68, 2694),
    "max": (214.42, 4288),
}
CAPACITY0 = 12000


def _cfg():
    return dict(DEFAULT_CONFIG)


async def test_diehard_target():
    cfg = _cfg()
    for name, (infl, fans) in INFLUENCES.items():
        got = formula.diehard_target(cfg, infl)
        assert abs(got - fans) < 1, f"{name}: target {got} != {fans}"
    assert formula.diehard_target(cfg, 214.42) <= float(cfg["fans_cap"]), "死忠目标应低于上限"


async def test_attendance_multiplier_ladder():
    cfg = _cfg()
    expected = [4.0, 5.4, 6.8, 8.2, 9.6]
    for tier, exp in enumerate(expected):
        got = formula.attendance_multiplier(cfg, tier)
        assert abs(got - exp) < 1e-9, f"tier {tier}: {got} != {exp}"


async def test_opening_attendance_four_brackets():
    """开局 0 级 1.2 万座：弱队半满、中游 8 成多、头部坐满。"""
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
            assert 6300 <= att <= 8000, f"min: {att}"
        elif name == "median":
            assert 9300 <= att <= 11100, f"median: {att}"
        elif name == "mean":
            assert 9900 <= att <= 11800, f"mean: {att}"
        else:
            assert att == CAPACITY0, f"max 应坐满: {att}"


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
    # 卖核例子：214 → 150，w1 ≈ 3412（DropCoef 0.68，Pts 中性）
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
    fee = formula.naming_fee(cfg, 35000, 2528, 1.0)
    # 公式保留 3 位小数
    assert abs(fee - 1.58) < 1e-6, fee


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