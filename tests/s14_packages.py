"""冠名合同套餐层：三套餐结构、签约快照、续约、对赌奖金、档位性格 buff、旧掷骰解约移除。"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.config.defaults import DEFAULT_CONFIG  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services import formula  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.brand_service import (  # noqa: E402
    BrandError,
    BrandService,
    build_packages,
)
from astrbot_plugin_whleague_revenue_system.services.formula import naming_fee  # noqa: E402


async def test_build_packages_structure():
    cfg = dict(DEFAULT_CONFIG)
    pkgs = build_packages(cfg, 10.0)
    assert [p["pkg_no"] for p in pkgs] == [1, 2, 3]
    stable, short, bet = pkgs
    assert stable["pkg_name"] == "稳健" and stable["windows"] == 6 and stable["amount"] == 8.5
    assert short["pkg_name"] == "进取" and short["windows"] == 2 and short["amount"] == 12.5
    assert bet["pkg_name"] == "对赌" and bet["windows"] == 4 and bet["amount"] == 7.0
    assert bet["bonus"] == 4.9 and bet["bet_attend"] == 0.8 and bet["bet_fans"] == 0.03
    assert stable["bonus"] == 0.0 and short["bonus"] == 0.0
    # 自定义系数
    cfg2 = dict(DEFAULT_CONFIG, naming_pkg_stable_windows=3, naming_pkg_stable_factor=0.9,
                naming_pkg_bet_bonus_rate=1.0)
    p2 = build_packages(cfg2, 10.0)
    assert p2[0]["windows"] == 3 and p2[0]["amount"] == 9.0
    assert p2[2]["bonus"] == 7.0
    # desc 为展示字段
    assert all(p["desc"] for p in pkgs)


async def test_bet_bonus_due_lines():
    pkg = {"pkg_no": 3, "bonus": 4.9, "bet_attend": 0.8, "bet_fans": 0.03}
    assert formula.bet_bonus_due(pkg, 0.8, 0.0) == 4.9, "上座达线即发"
    assert formula.bet_bonus_due(pkg, 0.5, 0.03) == 4.9, "死忠达线即发"
    assert formula.bet_bonus_due(pkg, 0.79, 0.029) == 0.0, "双线未达不发"
    assert formula.bet_bonus_due({"pkg_no": 1, "bonus": 4.9}, 1.0, 1.0) == 0.0, "非对赌套餐不发"
    assert formula.bet_bonus_due({"pkg_no": 3, "bonus": 0.0}, 1.0, 1.0) == 0.0, "零奖金不发"
    assert formula.bet_bonus_due({"pkg_no": 3}, 1.0, 1.0) == 0.0, "缺奖金字段不发"


async def test_sign_package_snapshot():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0, capacity=20000)
        await env.dao.update_fans("利物浦", 3000)
        # builtin 行业映射
        brands = {b["brand"]: b for b in await env.dao.list_brands()}
        assert brands["亚马逊"]["industry"] == "科技"
        assert brands["麒麟生物"]["industry"] == "医疗"
        # 套餐号非法
        for bad in (0, 4, "x"):
            try:
                await env.brand_service.sign("利物浦", "亚马逊", 1, 1, package_no=bad)
                raise AssertionError(f"套餐号 {bad} 应拒绝")
            except BrandError:
                pass
        # 对赌套餐签约：快照入合同
        r = await env.brand_service.sign("利物浦", "亚马逊", 1, 1, package_no=3)
        assert r["package"] == "对赌" and r["package_no"] == 3 and r["windows"] == 4
        naming = await env.dao.get_active_naming("利物浦")
        snap = json.loads(naming["package_json"])
        expect_base = round(naming_fee(env.cfg, 20000, 3000, 1.3) * 1.0, 3)
        assert snap == {"pkg_no": 3, "pkg_name": "对赌", "base_fee": expect_base,
                        "amount": round(expect_base * 0.7, 3), "windows": 4,
                        "bonus": round(round(expect_base * 0.7, 3) * 0.7, 3),
                        "bet_attend": 0.8, "bet_fans": 0.03}, snap
        assert naming["fee_per_window"] == snap["amount"]
        # 行业系数：科技 1.5 后底价放大
        await env.dao.terminate_naming("利物浦")
        env.cfg["market_industry_templates"] = json.dumps({"科技": 1.5}, ensure_ascii=False)
        await env.brand_service.sign("利物浦", "亚马逊", 1, 1, package_no=1)
        snap2 = json.loads((await env.dao.get_active_naming("利物浦"))["package_json"])
        assert snap2["pkg_no"] == 1 and snap2["bonus"] == 0.0
        assert snap2["base_fee"] == round(expect_base * 1.5, 3), snap2
        assert snap2["windows"] == 6, "套餐 1=稳健 6 窗"
    finally:
        await env.teardown()


async def test_renew_rules():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0, capacity=20000)
        # 无合同拒绝
        try:
            await env.brand_service.renew("利物浦", 1, 1)
            raise AssertionError("无生效冠名应拒绝续约")
        except BrandError:
            pass
        await env.brand_service.sign("利物浦", "亚马逊", 1, 1, package_no=1)
        # 剩多窗拒绝
        try:
            await env.brand_service.renew("利物浦", 2, 1)
            raise AssertionError("剩多窗应拒绝续约")
        except BrandError as e:
            assert "最后 1 窗" in str(e)
        # 推到剩 1 窗，满意度不足拒绝
        for _ in range(5):
            await env.dao.tick_naming("利物浦")
        await env.db.execute("UPDATE naming_rights SET satisfaction=0.8 WHERE team_name='利物浦'")
        try:
            await env.brand_service.renew("利物浦", 2, 2)
            raise AssertionError("满意度不足应拒绝续约")
        except BrandError as e:
            assert "满意度" in str(e)
        # 满意 → 按进取套餐无缝换约
        await env.db.execute("UPDATE naming_rights SET satisfaction=1.2 WHERE team_name='利物浦'")
        r = await env.brand_service.renew("利物浦", 2, 2, package_no=2)
        assert r["package_no"] == 2 and r["windows"] == 2 and r["package"] == "进取"
        row = await env.dao.get_active_naming("利物浦")
        assert row["windows_remaining"] == 2 and row["windows_total"] == 2
        assert row["signed_season"] == 2 and row["signed_window"] == 2
        assert row["status"] == "active", "原地换约保持 active"
        snap = json.loads(row["package_json"])
        assert snap["pkg_no"] == 2 and snap["amount"] == row["fee_per_window"]
    finally:
        await env.teardown()


async def test_legacy_contract_settle_unchanged():
    """legacy 合同（package_json='{}'）按老公式费率照收，无奖金。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0, capacity=20000)
        await env.dao.add_naming("利物浦", "亚马逊", 1.3, 2.5, 4, 1, 1)
        await env.dao.update_fans("利物浦", 100000)  # 高死忠增长也不触发（无套餐）
        result = await env.window_service.settle()
        joined = "\n".join(result["lines"])
        assert "对赌奖金" not in joined
        naming_txs = [t for t in await env.dao.list_transactions("利物浦", season=1, window_seq=1)
                      if t["kind"] == "naming"]
        assert [(t["amount"], t["note"]) for t in naming_txs] == \
            [(2.5, "亚马逊 冠名费")], naming_txs
    finally:
        await env.teardown()


async def test_settle_bet_bonus_fans_line():
    """对赌套餐：死忠增长率达线当窗发奖金（流水可重算）。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=500.0, capacity=20000)
        await env.dao.update_fans("利物浦", 3000)
        await env.brand_service.sign("利物浦", "亚马逊", 1, 1, package_no=3)
        # 前置：影响力 500 → 目标 8200，一轮演化增长远超 3% 线
        assert formula.diehard_target(env.cfg, 500.0) > 8200.0 - 1e-6
        result = await env.window_service.settle()
        joined = "\n".join(result["lines"])
        assert "对赌奖金" in joined, joined
        naming_txs = [t for t in await env.dao.list_transactions("利物浦", season=1, window_seq=1)
                      if t["kind"] == "naming"]
        amounts = sorted(t["amount"] for t in naming_txs)
        assert len(amounts) == 2, naming_txs
        snap = json.loads((await env.dao.get_active_naming("利物浦"))["package_json"])
        assert amounts[0] == snap["bonus"] and amounts[1] == snap["amount"], amounts
        # 重算：奖金随流水重记（条件可复算），账目一致
        await env.window_service.settle(force=True)
        naming_txs2 = [t for t in await env.dao.list_transactions("利物浦", season=1, window_seq=1)
                       if t["kind"] == "naming"]
        assert sorted(t["amount"] for t in naming_txs2) == amounts, naming_txs2
    finally:
        await env.teardown()


async def test_settle_attend_buff_activity_income():
    """头部档 attend_buff=0.02 乘活动收入（性格按实时档位）。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0, capacity=20000)
        await env.brand_service.sign("利物浦", "亚马逊", 1, 1, package_no=1)
        await env.dao.add_booking("利物浦", 1, 1, 1, "esports", "")
        # 口碑档（默认）无加成；活动收入含随机项，seed 固定保证重算可复现
        import random
        random.seed(42)
        await env.window_service.settle()
        txs = [t for t in await env.dao.list_transactions("利物浦", season=1, window_seq=1)
               if t["kind"] == "activity"]
        assert len(txs) == 1, txs
        base_settled = txs[0]["amount"]
        random.seed(42)
        await env.window_service.settle(force=True)
        txs_r = [t for t in await env.dao.list_transactions("利物浦", season=1, window_seq=1)
                 if t["kind"] == "activity"]
        assert abs(txs_r[0]["amount"] - base_settled) < 1e-6, "口碑档重算应一致"
        # 提到头部档 → 重算收入 ×1.02
        await env.db.execute("UPDATE brand_pool SET tier='头部' WHERE brand='亚马逊'")
        random.seed(42)
        await env.window_service.settle(force=True)
        txs2 = [t for t in await env.dao.list_transactions("利物浦", season=1, window_seq=1)
                if t["kind"] == "activity"]
        expect = round(base_settled * 1.02, 3)
        assert abs(txs2[0]["amount"] - expect) < 1e-6, \
            (txs2[0]["amount"], expect)
    finally:
        await env.teardown()


async def test_fans_buff_evolve():
    """口碑档 fans_buff=0.005 乘死忠增长额（状态类随重算跳过）。"""
    base_env = await TestEnv().setup()
    buff_env = await TestEnv().setup()
    try:
        for env in (base_env, buff_env):
            await env.stadium_service.import_attributes("利物浦", influence=500.0, capacity=20000)
            await env.dao.update_fans("利物浦", 3000)
        await buff_env.brand_service.sign("利物浦", "可口可乐", 1, 1, package_no=1)
        await base_env.window_service.settle()
        await buff_env.window_service.settle()
        # 死忠演化不产生流水，直接读库对比
        after_base = (await base_env.dao.get_stadium("利物浦"))["fans_diehards"]
        after_buff = (await buff_env.dao.get_stadium("利物浦"))["fans_diehards"]
        expect = 3000.0 + (after_base - 3000.0) * (1.0 + 0.005)
        assert abs(after_buff - expect) < 0.5, (after_buff, expect, after_base)
        assert after_buff > after_base, "口碑 buff 应放大增长"
        # 重算不重复演化：buff 环境强制重算后死忠不变
        await buff_env.window_service.settle(force=True)
        after_redo = (await buff_env.dao.get_stadium("利物浦"))["fans_diehards"]
        assert abs(after_redo - after_buff) < 1e-6
    finally:
        await base_env.teardown()
        await buff_env.teardown()


async def test_dice_terminate_removed():
    """旧「死忠暴跌掷骰解约」已由确定性规则（满意度下限，批次④）取代。"""
    assert not hasattr(BrandService, "maybe_brand_terminate")


async def test_sign_package_two_explicit():
    """直签显式套餐号 2（进取）：常态模式可选套餐（缺省走稳健=套餐 1）。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0, capacity=20000)
        await env.dao.update_fans("利物浦", 3000)
        r = await env.brand_service.sign("利物浦", "亚马逊", 1, 1, package_no=2)
        assert r["package"] == "进取" and r["package_no"] == 2 and r["windows"] == 2
        naming = await env.dao.get_active_naming("利物浦")
        snap = json.loads(naming["package_json"])
        expect_base = round(naming_fee(env.cfg, 20000, 3000, 1.3) * 1.0, 3)
        assert snap["amount"] == round(expect_base * 1.25, 3)
        assert naming["fee_per_window"] == snap["amount"]
    finally:
        await env.teardown()


async def test_bet_bonus_fans_line_survives_redo():
    """对赌奖金 redo 口径（schema v11）：上座线关闭后奖金只来自死忠增长，
    强制重算按 window_fans_log 快照复算不丢。"""
    env = await TestEnv(cfg_override={"naming_pkg_bet_attend": 1.5}).setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=500.0, capacity=20000)
        await env.dao.update_fans("利物浦", 3000)
        await env.brand_service.sign("利物浦", "亚马逊", 1, 1, package_no=3)
        # 前置：影响力 500 → 目标 8200，一轮演化增长远超 3% 死忠线
        assert formula.diehard_target(env.cfg, 500.0) > 8200.0 - 1e-6
        result = await env.window_service.settle()
        joined = "\n".join(result["lines"])
        assert "对赌奖金" in joined, joined  # 上座 1.0 < 1.5 线不达，纯死忠线触发
        amounts = sorted(t["amount"] for t in
                         await env.dao.list_transactions("利物浦", season=1, window_seq=1)
                         if t["kind"] == "naming")
        assert len(amounts) == 2, amounts
        # redo：evolve 跳过（fd 为空），奖金按死忠快照重算不丢
        await env.window_service.settle(force=True)
        amounts2 = sorted(t["amount"] for t in
                          await env.dao.list_transactions("利物浦", season=1, window_seq=1)
                          if t["kind"] == "naming")
        assert amounts2 == amounts, amounts2
        log = await env.dao.get_window_fans_log(1, 1, "利物浦")
        assert log is not None and float(log["fans_before"]) == 3000.0
        # 冠名窗口数不因 redo 重复扣减
        naming = await env.dao.get_active_naming("利物浦")
        assert int(naming["windows_remaining"]) == int(naming["windows_total"]) - 1
    finally:
        await env.teardown()


async def test_bet_bonus_redo_reads_fans_log():
    """redo 的死忠增长口径以 window_fans_log 为唯一来源：快照达线即发。"""
    env = await TestEnv(cfg_override={"naming_pkg_bet_attend": 1.5}).setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=500.0, capacity=20000)
        await env.dao.update_fans("利物浦", 8000)
        await env.brand_service.sign("利物浦", "亚马逊", 1, 1, package_no=3)
        # 目标 8200：本轮演化增长 < 3%，首结不发奖金
        assert formula.diehard_target(env.cfg, 500.0) > 8200.0 - 1e-6
        await env.window_service.settle()
        amounts = sorted(t["amount"] for t in
                         await env.dao.list_transactions("利物浦", season=1, window_seq=1)
                         if t["kind"] == "naming")
        assert len(amounts) == 1, amounts  # 只有冠名费
        # 快照写入达线增长 → redo 按快照发放奖金（口径来源是快照本身）
        await env.dao.upsert_window_fans_log(1, 1, "利物浦", 3000.0, 8200.0)
        await env.window_service.settle(force=True)
        snap = json.loads((await env.dao.get_active_naming("利物浦"))["package_json"])
        amounts2 = [t["amount"] for t in
                    await env.dao.list_transactions("利物浦", season=1, window_seq=1)
                    if t["kind"] == "naming"]
        assert snap["bonus"] in amounts2, (snap, amounts2)
    finally:
        await env.teardown()
