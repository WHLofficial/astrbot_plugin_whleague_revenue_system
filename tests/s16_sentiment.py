"""行情演化：满意度三信号（上座/战绩/事件）、品牌解约广播、连胜/低迷热度、冠军加成、
新内置冠名事件种子与 requires_naming 过滤、LLM 品牌扩展字段与套餐变体、结算挂载与重算跳过。"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.brand_service import (  # noqa: E402
    build_packages,
    variant_packages,
)
from astrbot_plugin_whleague_revenue_system.services.llm_writer import _clamp_brand  # noqa: E402

FIELDS = {
    "利物浦": (40000, 8000),
    "巴塞罗那": (30000, 3000),
    "纽卡斯尔联": (25000, 2000),
    "勒沃库森": (20000, 1000),
}


def _naming_fee(cap: float, fans: float, heat: float) -> float:
    return round((0.5 + 0.3 * cap / 10000 + 0.12 * fans / 10000) * heat, 3)


async def _seed_fields(env):
    for team, (cap, fans) in FIELDS.items():
        await env.stadium_service.import_attributes(team, influence=100, capacity=cap, tier=4)
        await env.dao.update_fans(team, fans)


async def _brand(env, name: str):
    return next(b for b in await env.dao.list_brands() if b["brand"] == name)


async def _add_custom_brand(env, name: str, heat: float = 1.0, tier: str = "",
                            packages: list | None = None) -> dict:
    await env.dao.add_brand(
        name, heat, source="custom", status="adopted", industry="科技", tier=tier,
        packages_json=(json.dumps({"variants": packages}, ensure_ascii=False)
                       if packages else "{}"),
    )
    return await _brand(env, name)


async def _match(env, season: int, window: int, round_no: int,
                 home: str, away: str) -> dict:
    """add_match 返回插入行数而非 id，须按轮次+主客队定位取回真实行。"""
    await env.dao.add_match(season, window, round_no, home, away)
    rows = [m for m in await env.dao.get_round_matches(season, window, round_no)
            if m["home_team"] == home and m["away_team"] == away]
    assert len(rows) == 1, (season, window, round_no, home, away, rows)
    return rows[0]


async def test_satisfaction_signals_math():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        await _add_custom_brand(env, "风行体育")
        await env.dao.add_naming("利物浦", "风行体育", 1.0, 2.0, 6, 1, 1)
        # 无场次无事件：上座中性 1.0 ≥ 口碑 good 0.85 → +1 档 → +0.025
        lines, details = await env.brand_service.evolve_sentiment(1, 1, {})
        d = details["利物浦"]
        assert d["delta"] == pytest.approx(0.025, abs=1e-9)
        assert d["sat"] == pytest.approx(1.025, abs=1e-9) and not d["terminated"]
        row = await env.dao.get_active_naming("利物浦")
        assert float(row["satisfaction"]) == pytest.approx(1.025, abs=1e-9)
        # 上座 0.4 ≤ bad 0.5 → −1 档；窗口内 1 胜 0 负 胜率 1.0 ≥ 0.55 → +1 档；
        # 合计 −0.01 < 0 → ×口碑 penalty_mult 0.75 = −0.0075
        m1 = await _match(env, 1, 2, 1, "利物浦", "巴塞罗那")
        await env.dao.set_match_result(int(m1["id"]), "W", 20000, 1.0, 0.5, 0.3, "2-1")
        _, details = await env.brand_service.evolve_sentiment(1, 2, {"利物浦": 0.4})
        assert details["利物浦"]["delta"] == pytest.approx(-0.0075, abs=1e-9)
        # 客场视角换算：利物浦客胜也应计胜（同窗第二次演化，信号不变）
        m2 = await _match(env, 1, 2, 2, "纽卡斯尔联", "利物浦")
        await env.dao.set_match_result(int(m2["id"]), "L", 15000, 0.8, 0.4, 0.2, "0-2")
        _, details = await env.brand_service.evolve_sentiment(1, 2, {"利物浦": 0.4})
        assert details["利物浦"]["delta"] == pytest.approx(-0.0075, abs=1e-9)
    finally:
        await env.teardown()


async def test_auto_terminate_broadcast():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        await _add_custom_brand(env, "大牌集团", tier="头部")  # 头部 floor 0.7、penalty 1.5
        await env.dao.add_naming("利物浦", "大牌集团", 1.0, 2.0, 6, 1, 1)
        await env.dao.update_naming_satisfaction("利物浦", 0.68)
        # 中性上座 +1 档：0.68+0.025=0.705 ≥ 0.7 不解约
        lines, details = await env.brand_service.evolve_sentiment(1, 1, {})
        assert not details["利物浦"]["terminated"]
        assert float((await env.dao.get_active_naming("利物浦"))["satisfaction"]) == pytest.approx(0.705, abs=1e-9)
        # 差上座 −1 档：delta −0.025×1.5=−0.0375 → 0.6675 < 0.7 → 解约（无赔偿）
        lines, details = await env.brand_service.evolve_sentiment(1, 2, {"利物浦": 0.4})
        assert details["利物浦"]["terminated"]
        assert any("大牌集团" in l and "解除冠名" in l and "无赔偿" in l for l in lines)
        assert await env.dao.get_active_naming("利物浦") is None
    finally:
        await env.teardown()


async def test_event_satisfaction_and_brand_heat():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        await _add_custom_brand(env, "风行体育")  # heat 1.0
        await env.dao.add_naming("利物浦", "风行体育", 1.0, 2.0, 6, 1, 1)
        notes = await env.event_engine._apply_effects(
            "利物浦", 1, 1, {"satisfaction": 0.5, "brand_heat": 0.05}, "联合营销")
        assert any("品牌满意度 +0.50" in n for n in notes)
        assert any("品牌热度 +0.05" in n for n in notes)
        assert float((await _brand(env, "风行体育"))["heat"]) == pytest.approx(1.05, abs=1e-9)
        # satisfaction 不在此落库，由演化步经权重并入
        assert float((await env.dao.get_active_naming("利物浦"))["satisfaction"]) == 1.0
        # 无冠名队：两键都忽略
        notes2 = await env.event_engine._apply_effects(
            "巴塞罗那", 1, 1, {"satisfaction": 0.5, "brand_heat": 0.05}, "联合营销")
        assert not any("品牌" in n for n in notes2)
        assert float((await _brand(env, "风行体育"))["heat"]) == pytest.approx(1.05, abs=1e-9)
        # 重算：热度类跳过（不重复改），备注标注；满意度信号备注保留
        notes3 = await env.event_engine._apply_effects(
            "利物浦", 1, 1, {"satisfaction": 0.5, "brand_heat": 0.05}, "联合营销",
            apply_state=False)
        assert any("品牌热度 +0.05（重算跳过）" in n for n in notes3)
        assert float((await _brand(env, "风行体育"))["heat"]) == pytest.approx(1.05, abs=1e-9)
        # 演化步消费事件信号：event_raw=0.5 → 0.2×0.5=+0.1；中性上座 +0.025 → 共 +0.125
        await env.dao.add_event_log("利物浦", 1, 1, "brand_cocreate",
                                    json.dumps({"satisfaction": 0.5}), "联合营销")
        _, details = await env.brand_service.evolve_sentiment(1, 1, {})
        assert details["利物浦"]["delta"] == pytest.approx(0.125, abs=1e-9)
    finally:
        await env.teardown()


async def test_choice_outcome_signal():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        await _add_custom_brand(env, "风行体育")
        await env.dao.add_naming("利物浦", "风行体育", 1.0, 2.0, 6, 1, 1)
        await env.dao.add_event_choice("利物浦", 1, 1, "brand_poach")
        ch = next(c for c in await env.dao.list_event_choices(1, 1)
                  if c["team_name"] == "利物浦")
        await env.dao.mark_choice_resolved(
            int(ch["id"]),
            json.dumps({"option": 2, "option_name": "接触竞价的新品牌", "auto": False,
                        "effects": {"satisfaction": -0.2}}, ensure_ascii=False))
        # 即发 + 选择双来源：raw = 0.5 − 0.2 = 0.3 → 0.2×0.3=+0.06；上座中性 +0.025 → 0.085
        await env.dao.add_event_log("利物浦", 1, 1, "brand_cocreate",
                                    json.dumps({"satisfaction": 0.5}), "联合营销")
        _, details = await env.brand_service.evolve_sentiment(1, 1, {})
        assert details["利物浦"]["delta"] == pytest.approx(0.085, abs=1e-9)
    finally:
        await env.teardown()


async def test_streak_slump_heat():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        await _add_custom_brand(env, "风行体育")  # heat 1.0
        await env.dao.add_naming("利物浦", "风行体育", 1.0, 2.0, 6, 1, 1)
        for i, opp in enumerate(["巴塞罗那", "纽卡斯尔联", "勒沃库森"], start=1):
            m = await _match(env, 1, 1, i, "利物浦", opp)
            await env.dao.set_match_result(int(m["id"]), "W", 30000, 1.0, 0.5, 0.3, "2-0")
        _, _ = await env.brand_service.evolve_sentiment(1, 2, {"利物浦": 0.4})
        assert float((await _brand(env, "风行体育"))["heat"]) == pytest.approx(1.03, abs=1e-9)
        # 近 3 场全败 → 低迷 −0.02（静默）
        for i, opp in enumerate(["巴塞罗那", "纽卡斯尔联", "勒沃库森"], start=4):
            m = await _match(env, 1, 2, i, "利物浦", opp)
            await env.dao.set_match_result(int(m["id"]), "L", 12000, 0.5, 0.2, 0.1, "0-3")
        _, _ = await env.brand_service.evolve_sentiment(1, 3, {"利物浦": 0.4})
        assert float((await _brand(env, "风行体育"))["heat"]) == pytest.approx(1.01, abs=1e-9)
    finally:
        await env.teardown()


async def test_champion_bonus():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        await _add_custom_brand(env, "风行体育")  # heat 1.0
        await env.dao.add_naming("利物浦", "风行体育", 1.0, 2.0, 6, 1, 1)
        m1 = await _match(env, 1, 1, 1, "利物浦", "巴塞罗那")
        await env.dao.set_match_result(int(m1["id"]), "W", 30000, 1.0, 0.5, 0.3, "2-1")
        m2 = await _match(env, 1, 1, 2, "利物浦", "纽卡斯尔联")
        await env.dao.set_match_result(int(m2["id"]), "W", 30000, 1.0, 0.5, 0.3, "3-0")
        m3 = await _match(env, 1, 1, 3, "巴塞罗那", "勒沃库森")
        await env.dao.set_match_result(int(m3["id"]), "D", 25000, 1.0, 0.5, 0.3, "1-1")
        # 缺比分场次不计积分（get_season_result_matches 只取有比分场次）
        m4 = await _match(env, 1, 1, 4, "利物浦", "勒沃库森")
        await env.dao.set_match_result(int(m4["id"]), "W", 30000, 1.0, 0.5, 0.3, None)
        info = await env.brand_service.champion_bonuses(1)
        assert info["team"] == "利物浦" and info["pts"] == 6
        assert info["brand"] == "风行体育"
        assert float((await _brand(env, "风行体育"))["heat"]) == pytest.approx(1.10, abs=1e-9)
        assert float((await env.dao.get_active_naming("利物浦"))["satisfaction"]) == pytest.approx(1.10, abs=1e-9)
        # 无比分 → None；冠军无冠名 → brand None 不加成
        assert await env.brand_service.champion_bonuses(2) is None
        await env.dao.terminate_naming("利物浦")
        info2 = await env.brand_service.champion_bonuses(1)
        assert info2["team"] == "利物浦" and info2["brand"] is None
        assert float((await _brand(env, "风行体育"))["heat"]) == pytest.approx(1.10, abs=1e-9)
    finally:
        await env.teardown()


async def test_new_builtin_events_seeded():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        pool = {e["event_id"]: e for e in await env.dao.list_events()}
        for eid in ("brand_cocreate", "brand_poach"):
            ev = pool[eid]
            assert ev["status"] == "adopted" and ev["category"] == "冠名联动"
            cond = json.loads(ev["conditions_json"])
            assert cond.get("requires_naming") is True
            assert len(json.loads(ev["options_json"])) == 2
        # 条件过滤：无冠名不符合，有冠名符合
        ev = pool["brand_cocreate"]
        stadium = {"tier": 4, "capacity": 50000, "fans_diehards": 0.0}
        assert not await env.event_engine._condition_ok(ev, stadium, False, [])
        assert await env.event_engine._condition_ok(ev, stadium, True, [])
        # 有冠名可触发为选择型事件
        await _seed_fields(env)
        await _add_custom_brand(env, "风行体育")
        await env.dao.add_naming("利物浦", "风行体育", 1.0, 2.0, 6, 1, 1)
        env.provider.set_response("品牌的活动方案摆上了桌。")
        result = await env.event_engine.trigger_team("利物浦", 1, 1, event_id="brand_cocreate")
        assert result["hits"][0]["type"] == "choice"
        choices = [c for c in await env.dao.list_event_choices(1, 1)
                   if c["team_name"] == "利物浦"]
        assert choices and choices[0]["event_id"] == "brand_cocreate"
    finally:
        await env.teardown()


async def test_llm_brand_extras_and_variants():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        env.provider.set_response(json.dumps([
            {"brand": "风行体育", "heat": 1.2, "tier": "头部", "industry": "科技",
             "pitch": "快如闪电的连接", "packages": [
                 {"pkg_name": "旗舰短约", "windows": 2, "factor": 2.0},
                 {"pkg_name": "长约", "windows": 10, "factor": 0.9},
                 {"pkg_name": "超限", "windows": 99, "factor": 0.1}]},
            {"brand": "", "heat": 5},  # 空名 → _clamp_brand 拒收
        ], ensure_ascii=False))
        added = await env.brand_service.generate_drafts(2)
        assert len(added) == 1
        assert added[0]["tier"] == "头部" and added[0]["industry"] == "科技"
        assert added[0]["pitch"] == "快如闪电的连接"
        pending = await env.dao.list_brands("pending")
        row = pending[0]
        variants = json.loads(row["packages_json"])["variants"]
        assert [(v["pkg_name"], v["windows"], v["factor"]) for v in variants] == [
            ("旗舰短约", 2, 2.0), ("长约", 10, 0.9), ("超限", 12, 0.5)]
        await env.brand_service.adopt(int(row["id"]))
        adopted = await _brand(env, "风行体育")
        base = round(_naming_fee(40000, 8000, 1.2), 3)  # 行业系数默认 1.0
        # 直签用自定义变体：套餐 1 = 旗舰短约
        signed = await env.brand_service.sign("利物浦", "风行体育", 1, 1, 1)
        assert signed["package"] == "旗舰短约" and signed["windows"] == 2
        assert signed["fee_per_window"] == pytest.approx(round(base * 2.0, 3), abs=1e-9)
        # 市场套餐：头部取第 2 档变体，口碑取第 1 档
        pkg_top = env.market_service._brand_package(adopted, "头部", base)
        assert pkg_top["pkg_name"] == "长约"
        pkg_low = env.market_service._brand_package(adopted, "口碑", base)
        assert pkg_low["pkg_name"] == "旗舰短约"
        # 变体无效回退默认三套餐；_clamp_brand 兜底
        assert variant_packages(dict(env.cfg), 5.0, "not json") is None
        assert variant_packages(dict(env.cfg), 5.0, "{}") is None
        assert build_packages(dict(env.cfg), 5.0)[0]["pkg_name"] == "稳健"
        clamped = _clamp_brand({"brand": "测试", "heat": 9, "tier": "王者",
                                "industry": "超长行业名称会被截断", "packages": "bad"})
        assert clamped["tier"] == "口碑" and clamped["heat"] == 1.5
        assert clamped["industry"] and len(clamped["industry"]) <= 8
        assert "packages" not in clamped
    finally:
        await env.teardown()


async def test_settle_mounts_evolution_and_redo_skips():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        await _add_custom_brand(env, "风行体育")
        await env.dao.add_naming("利物浦", "风行体育", 1.0, 2.0, 6, 1, 1)
        result = await env.window_service.settle()
        row = await env.dao.get_active_naming("利物浦")
        # 中性上座 1.0 ≥ 0.85 → +0.025；无场次无事件 → 0
        assert float(row["satisfaction"]) == pytest.approx(1.025, abs=1e-9)
        assert not any("解除冠名" in l for l in result["lines"])
        # 强制重算：行情演化是状态类，整步跳过，不重复步进
        await env.window_service.settle(force=True)
        row = await env.dao.get_active_naming("利物浦")
        assert float(row["satisfaction"]) == pytest.approx(1.025, abs=1e-9)
    finally:
        await env.teardown()


async def test_signal_bus_consumers():
    """v2.11.3 信号总线消费：upkeep 乘维护费、fee_mod 乘冠名费、fan_mood 并入死忠演化；
    重算同源重应用（乘数型）、演化仍跳过（步进型）。"""
    from astrbot_plugin_whleague_revenue_system.services import formula as F

    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        await _add_custom_brand(env, "风行体育")
        await env.dao.add_naming("利物浦", "风行体育", 1.0, 2.0, 6, 1, 1)
        await env.event_engine.add_custom(
            "球迷庆典", "氛围", 10,
            '{"signals": {"upkeep": 0.8, "fee_mod": 1.2, "fan_mood": 1.5}}')
        rows = {r["name"]: r for r in await env.dao.list_events("adopted")}
        await env.event_engine.trigger_team(
            "利物浦", 1, 1, event_id=rows["球迷庆典"]["event_id"])

        result = await env.window_service.settle()
        joined = "\n".join(result["lines"])
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        by_kind = {t["kind"]: t for t in txs}
        # tier4 基础维护 14.0（0 场）× 0.8 = 11.2；冠名费 2.0 × 1.2 = 2.4
        assert by_kind["maintenance"]["amount"] == pytest.approx(-11.2, abs=1e-6), by_kind
        assert by_kind["naming"]["amount"] == pytest.approx(2.4, abs=1e-6), by_kind
        # 死忠演化并入 mood：公式结果 ×(1+1.5/100)
        s1 = await env.dao.get_stadium("利物浦")
        target = F.diehard_target(env.cfg, float(s1["influence"]))
        base = F.evolve_fans(env.cfg, fans=8000.0, target=target,
                             attend_rate=1.0, youth_level=0, form_pts_value=4)
        # 冠名档位性格 fans_buff 先乘增长额（口碑 +0.5%），mood 再乘整体
        buff = float((await env.window_service._tier_profile("利物浦"))
                     .get("fans_buff", 0.0) or 0.0)
        buffed = 8000.0 + (base - 8000.0) * (1.0 + buff)
        expected = min(max(buffed * 1.015, 0.0), float(env.cfg.get("fans_cap", 10000)))
        log = await env.dao.get_window_fans_log(1, 1, "利物浦")
        assert log is not None, joined
        assert float(log["fans_after"]) == pytest.approx(expected, abs=1e-6), (log, expected)

        # 重算：乘数型同源重应用（金额复现），演化仍跳过（快照不被覆盖）
        await env.window_service.settle(force=True)
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        by_kind = {t["kind"]: t for t in txs}
        assert by_kind["maintenance"]["amount"] == pytest.approx(-11.2, abs=1e-6), by_kind
        assert by_kind["naming"]["amount"] == pytest.approx(2.4, abs=1e-6), by_kind
        log2 = await env.dao.get_window_fans_log(1, 1, "利物浦")
        assert float(log2["fans_after"]) == pytest.approx(expected, abs=1e-6)
    finally:
        await env.teardown()

