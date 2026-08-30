"""冠名权市场引擎：四模式报价、档位校准与锁档、签约原子性与热度反馈、TTL、名额、冷落降热。"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from tests.common import TestEnv, TEAMS  # noqa: E402
from astrbot_plugin_whleague_revenue_system.config.defaults import DEFAULT_CONFIG  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.brand_service import build_packages  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.market_service import (  # noqa: E402
    MarketError,
)

# 四队属性（容量/死忠），估值断言用
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


async def test_recalibrate_tiers():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)  # 4 队 → top_k = ceil(4/8) = 1
        changed = await env.market_service.recalibrate_tiers()
        assert changed == 5  # 亚马逊→头部，麒麟/阿迪/可口/海底→新兴，星海/CVS 保持口碑
        tiers = {b["brand"]: b["tier"] for b in await env.dao.list_brands()}
        assert tiers["亚马逊"] == "头部"
        assert tiers["麒麟生物"] == "新兴" and tiers["海底捞"] == "新兴"
        assert tiers["星海通讯"] == "口碑" and tiers["CVS Health"] == "口碑"
        # 锁档：星海锁头部并升热度，重跑不被排名拉走
        star = await _brand(env, "星海通讯")
        await env.market_service.set_tier(int(star["id"]), "头部")
        await env.dao.update_brand_heat(int(star["id"]), 1.4)
        assert await env.market_service.recalibrate_tiers() == 0
        tiers = {b["brand"]: b["tier"] for b in await env.dao.list_brands()}
        assert tiers["星海通讯"] == "头部"  # 锁档不随热度流动
        assert tiers["亚马逊"] == "头部"  # 未锁档中热度最高者仍是头部
    finally:
        await env.teardown()


async def test_targeted_offers_mingbiao():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        lines = await env.market_service.run_window(1, 1)
        assert any("招商期开启" in l for l in lines)
        assert any("→ 利物浦" in l for l in lines)  # 明标播报明细行
        assert any("/主场招商" in l for l in lines)
        rnd = await env.dao.get_market_round(1, 1)
        assert rnd and rnd["status"] == "open" and rnd["bid_mode"] == "明标"
        offers = await env.dao.list_round_offers(int(rnd["id"]))
        assert offers and all(o["status"] == "pending" and o["team_name"] for o in offers)
        # 头部盯估值最高 1-2 队，进取套餐（2 窗）；金额 = 套餐价（fee×1.25）
        amz = [o for o in offers if o["brand"] == "亚马逊"]
        assert 1 <= len(amz) <= 2
        assert all(o["windows"] == 2 for o in amz)
        fee_top = _naming_fee(40000, 8000, 1.3)
        assert any(o["amount"] == build_packages(dict(DEFAULT_CONFIG), fee_top)[1]["amount"]
                   for o in amz)
        # 口碑品牌广撒，稳健套餐（6 窗）
        star = [o for o in offers if o["brand"] == "星海通讯"]
        assert star and all(o["windows"] == 6 for o in star)
        # 已有生效冠名的队不进估值池
        await env.dao.add_naming("利物浦", "海底捞", 0.9, 1.0, 4, 1, 1)
        lines2 = await env.market_service.run_window(1, 2)
        rnd2 = await env.dao.get_market_round(1, 2)
        offers2 = await env.dao.list_round_offers(int(rnd2["id"]))
        assert all(o["team_name"] != "利物浦" for o in offers2)
        assert any("招商期开启" in l for l in lines2)
    finally:
        await env.teardown()


async def test_changtai_skip():
    env = await TestEnv(cfg_override={"market_bid_mode": "常态", "max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        assert await env.market_service.run_window(1, 1) == []
        assert await env.dao.get_market_round(1, 1) is None
        lines = await env.market_service.board("利物浦", 1, 1)
        assert "常态模式" in lines[0] and "/主场冠名" in lines[0]
        with pytest.raises(MarketError, match="常态"):
            await env.market_service.accept("利物浦", 1, 1, 1)
    finally:
        await env.teardown()


async def test_board_visibility():
    env = await TestEnv(cfg_override={"market_bid_mode": "暗标", "max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        lines = await env.market_service.run_window(1, 1)
        assert any("明细仅收价队可见" in l for l in lines)
        assert not any("→ " in l for l in lines)  # 暗标不播报明细
        board = await env.market_service.board("利物浦", 1, 1)
        assert any("利物浦 收到的报价" in l for l in board)
        assert any("/主场签约" in l for l in board)
        assert not any("全联赛报价板" in l for l in board)  # 暗标无公开板
    finally:
        await env.teardown()
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()  # 明标（默认）
    try:
        await _seed_fields(env)
        await env.market_service.run_window(1, 1)
        board = await env.market_service.board("利物浦", 1, 1)
        assert any("全联赛报价板" in l for l in board)
        joined = "\n".join(board)
        assert "→ 纽卡斯尔联" in joined or "→ 巴塞罗那" in joined  # 他队报价可见
    finally:
        await env.teardown()


async def test_accept_flow_and_heat():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        await env.market_service.run_window(1, 1)
        rnd0 = await env.dao.get_market_round(1, 1)
        mine = await env.dao.list_pending_offers_for_team("利物浦")
        offer = next(o for o in mine if o["brand"] == "亚马逊")
        result = await env.market_service.accept("利物浦", int(offer["id"]), 1, 1)
        assert result["team"] == "利物浦" and result["brand"] == "亚马逊"
        assert result["fee_per_window"] == offer["amount"]
        assert result["windows"] == offer["windows"]
        naming = await env.dao.get_active_naming("利物浦")
        assert naming["fee_per_window"] == offer["amount"]
        assert naming["windows_remaining"] == offer["windows"]
        assert json.loads(naming["package_json"])["pkg_no"] == 2  # 头部进取套餐
        row = await env.dao.get_market_offer(int(offer["id"]))
        assert row["status"] == "accepted"
        # 成交热度 +0.02
        amz = await _brand(env, "亚马逊")
        assert amz["heat"] == pytest.approx(1.32)
        # 重复签约 / 定向他人
        with pytest.raises(MarketError, match="已被接受"):
            await env.market_service.accept("利物浦", int(offer["id"]), 1, 1)
        other = next(o for o in await env.dao.list_round_offers(int(rnd0["id"]))
                     if o["team_name"] == "纽卡斯尔联")
        with pytest.raises(MarketError, match="定向"):
            await env.market_service.accept("利物浦", int(other["id"]), 1, 1)
        # 推进收口：有成交的品牌不吃冷落降热
        await env.market_service.run_window(1, 2)
        rnd1 = await env.dao.get_market_round(1, 1)
        assert rnd1["status"] == "settled"
        amz = await _brand(env, "亚马逊")
        assert amz["heat"] == pytest.approx(1.32)
    finally:
        await env.teardown()


async def test_fcfs_offers_and_taken():
    env = await TestEnv(cfg_override={"market_bid_mode": "先到先得", "max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        lines = await env.market_service.run_window(1, 1)
        assert any("公开挂价" in l for l in lines)
        assert any("先签先得" in l for l in lines)
        fcfs = await env.dao.list_fcfs_offers()
        assert len(fcfs) == 7  # 全部 adopted 品牌各挂一口价
        assert all(o["team_name"] == "" and o["windows"] == 6 for o in fcfs)  # 稳健套餐
        # 一口价按平均队况（avg_cap=28750, avg_fans=3500），金额为稳健套餐价
        amz = next(o for o in fcfs if o["brand"] == "亚马逊")
        fee_avg = _naming_fee(28750, 3500, 1.3)
        assert amz["amount"] == build_packages(dict(DEFAULT_CONFIG), fee_avg)[0]["amount"]
        # 第一队签约成功
        result = await env.market_service.accept("利物浦", int(amz["id"]), 1, 1)
        assert result["brand"] == "亚马逊"
        # 第二队抢同一挂价 → 已被接受
        with pytest.raises(MarketError, match="已被接受"):
            await env.market_service.accept("巴塞罗那", int(amz["id"]), 1, 1)
        # 已有生效冠名的队签约 → 显式拒绝
        other = next(o for o in fcfs if o["brand"] == "海底捞")
        await env.dao.add_naming("纽卡斯尔联", "可口可乐", 1.0, 1.0, 4, 1, 1)
        with pytest.raises(MarketError, match="已有生效冠名"):
            await env.market_service.accept("纽卡斯尔联", int(other["id"]), 1, 1)
    finally:
        await env.teardown()


async def test_ttl_expiry():
    env = await TestEnv(cfg_override={"market_offer_ttl_hours": 1, "max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        await env.market_service.run_window(1, 1)
        mine = await env.dao.list_pending_offers_for_team("利物浦")
        assert mine and mine[0]["expire_at"] is not None  # TTL 生效
        # 时间流逝：expire_at 置为过去 → 懒清理置 expired → 签约被拒
        await env.db.execute("UPDATE market_offers SET expire_at='2000-01-01 00:00:00'")
        assert await env.dao.expire_stale_offers() >= len(mine)
        board = await env.market_service.board("利物浦", 1, 1)
        assert any("已过期" in l for l in board)
        with pytest.raises(MarketError, match="失效"):
            await env.market_service.accept("利物浦", int(mine[0]["id"]), 1, 1)
        row = await env.dao.get_market_offer(int(mine[0]["id"]))
        assert row["status"] == "expired"
    finally:
        await env.teardown()
    env = await TestEnv(cfg_override={"market_offer_ttl_hours": 0, "max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        await env.market_service.run_window(1, 1)
        mine = await env.dao.list_pending_offers_for_team("利物浦")
        assert all(o["expire_at"] is None for o in mine)  # 0=不限时
        result = await env.market_service.accept("利物浦", int(mine[0]["id"]), 1, 1)
        assert result["team"] == "利物浦"
    finally:
        await env.teardown()


async def test_ignored_heat_drop():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        await env.market_service.run_window(1, 1)
        star = await _brand(env, "星海通讯")
        before = float(star["heat"])
        await env.market_service.run_window(1, 2)  # 上轮全未成交 → 冷落降热
        star = await _brand(env, "星海通讯")
        assert float(star["heat"]) == pytest.approx(before - 0.03)
        rnd1 = await env.dao.get_market_round(1, 1)
        assert rnd1["status"] == "settled"
        old_offers = await env.dao.list_round_offers(int(rnd1["id"]))
        assert all(o["status"] == "expired" for o in old_offers)  # 未签约报价作废
    finally:
        await env.teardown()


async def test_quota_full():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        await _seed_fields(env)
        # 亚马逊（头部）名额 1 队：已有生效合同 → 不再发报价
        await env.dao.add_naming("利物浦", "亚马逊", 1.3, 2.0, 2, 1, 1)
        await env.market_service.run_window(1, 1)
        rnd = await env.dao.get_market_round(1, 1)
        offers = await env.dao.list_round_offers(int(rnd["id"]))
        assert all(o["brand"] != "亚马逊" for o in offers)
        # 签约路径的名额校验：手工挂一份亚马逊报价，签时名额已满
        amz = await _brand(env, "亚马逊")
        offer_id = await env.dao.add_market_offer(
            int(rnd["id"]), int(amz["id"]), "纽卡斯尔联", 1, 1.0, 6, "{}", None
        )
        with pytest.raises(MarketError, match="名额已满"):
            await env.market_service.accept("纽卡斯尔联", int(offer_id), 1, 1)
    finally:
        await env.teardown()


async def test_set_tier_lock():
    env = await TestEnv().setup()
    try:
        cvs = await _brand(env, "CVS Health")
        with pytest.raises(MarketError, match="档位"):
            await env.market_service.set_tier(int(cvs["id"]), "王者")
        with pytest.raises(MarketError, match="不存在"):
            await env.market_service.set_tier(9999, "头部")
        await env.market_service.set_tier(int(cvs["id"]), "头部", "医疗")
        row = await _brand(env, "CVS Health")
        assert row["tier"] == "头部" and int(row["tier_locked"]) == 1
        assert row["industry"] == "医疗"
        await env.market_service.recalibrate_tiers()
        row = await _brand(env, "CVS Health")
        assert row["tier"] == "头部"  # 热度 0.7 也不被排名拉走
    finally:
        await env.teardown()
