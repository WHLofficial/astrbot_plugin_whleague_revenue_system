"""窗口结算：维护费、档期兑现、冠名费、死忠演化（非对称）、重复结算保护。"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.window_service import SettleError  # noqa: E402


async def _seed_window(env, fans_override: dict | None = None):
    """布置：2 队、1 轮、各 1 主场、录入赛果。"""
    await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那\n1 纽卡斯尔联 勒沃库森\n")
    await env.fixture_service.forecast_round(1)
    for team, infl in (("利物浦", 150.0), ("巴塞罗那", 100.0), ("纽卡斯尔联", 120.0), ("勒沃库森", 120.0)):
        await env.stadium_service.import_attributes(team, influence=infl)
    if fans_override:
        for team, fans in fans_override.items():
            await env.dao.update_fans(team, fans)
    await env.fixture_service.record_results(1, "利物浦 胜\n纽卡斯尔联 胜\n")


async def test_settle_maintenance_activity_naming():
    env = await TestEnv().setup()
    try:
        await _seed_window(env)
        # 档期 + 冠名
        await env.dao.add_booking("利物浦", 1, 1, 1, "esports", "")
        await env.brand_service.sign("利物浦", "亚马逊", 1, 1)

        result = await env.window_service.settle()
        assert result["season"] == 1 and result["window_seq"] == 1
        joined = "\n".join(result["lines"])

        # 维护费：0 级 1.2 万座 1 场 = 2 + 0.8×1.2×1 = 2.96M
        assert "维护" in joined
        # 活动收入
        assert "活动" in joined
        # 冠名费
        assert "冠名" in joined
        # 死忠演化（满目标无变化则无行）
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        kinds = {t["kind"] for t in txs}
        assert "maintenance" in kinds and "activity" in kinds and "naming" in kinds

        # 重复结算保护
        try:
            await env.window_service.settle()
            raise AssertionError("重复结算应报错")
        except SettleError:
            pass
        # 强制可重算
        await env.window_service.settle(force=True)
    finally:
        await env.teardown()


async def test_settle_fans_evolution_asymmetric():
    env = await TestEnv().setup()
    try:
        # 影响力 150 → 阶梯目标 3780；改为 214.42（目标 4773）后死忠应追上
        await _seed_window(env, fans_override={"利物浦": 3000})
        await env.stadium_service.import_attributes("利物浦", influence=214.42)
        # 下一窗口再赛一场，让演化有数据
        await env.fixture_service.advance_window("tester")
        await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那\n")
        await env.fixture_service.forecast_round(1)
        await env.fixture_service.record_results(1, "利物浦 胜\n")
        await env.window_service.settle()
        stadium = await env.dao.get_stadium("利物浦")
        # 目标 4773，应朝上追（涨粉方向）
        assert stadium["fans_diehards"] > 3000, stadium["fans_diehards"]

        # 掉粉方向：影响力跌回 100（阶梯目标 2600）
        await env.stadium_service.import_attributes("利物浦", influence=100.0)
        await env.fixture_service.advance_window("tester")
        await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那\n")
        await env.fixture_service.forecast_round(1)
        await env.fixture_service.record_results(1, "利物浦 负\n")
        before = (await env.dao.get_stadium("利物浦"))["fans_diehards"]
        await env.window_service.settle()
        after = (await env.dao.get_stadium("利物浦"))["fans_diehards"]
        assert after < before, f"掉粉方向应下降: {before} -> {after}"
        # 死忠对齐目标后（影响力不变）不演化：阶梯目标 2600
        await env.dao.update_fans("利物浦", 2600.0)
        await env.fixture_service.advance_window("tester")
        await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那\n")
        await env.fixture_service.forecast_round(1)
        await env.fixture_service.record_results(1, "利物浦 平\n")
        before = (await env.dao.get_stadium("利物浦"))["fans_diehards"]
        await env.window_service.settle()
        after = (await env.dao.get_stadium("利物浦"))["fans_diehards"]
        assert abs(after - before) < 1.0, f"满目标不应演化: {before} -> {after}"
    finally:
        await env.teardown()


async def test_evolve_once_per_settle():
    env = await TestEnv().setup()
    try:
        # 4 队均离目标、无已录赛果（上座率中性 1.0；v2.5.1 起不足 3 场按中性
        # 4 分，不再触发 ±5% 战绩修正）；单轮演化（v2.8.0 饱和阶梯）：
        # 建场死忠 2340（默认影响力 90 → 90×26），influence 150 → 目标 3780，
        # 2340 + (3780−2340)×0.5×(0.6+0.4×1.0) = 3060；
        # 旧实现会按球队数重复演化（多轮叠涨）
        for team, infl in (("利物浦", 150.0), ("巴塞罗那", 100.0),
                           ("纽卡斯尔联", 120.0), ("勒沃库森", 120.0)):
            await env.stadium_service.import_attributes(team, influence=infl)
        await env.window_service.settle()

        fans = (await env.dao.get_stadium("利物浦"))["fans_diehards"]
        assert abs(fans - 3060.0) < 0.01, f"应仅演化一轮: {fans}"
    finally:
        await env.teardown()


async def test_settle_force_idempotent():
    env = await TestEnv().setup()
    try:
        await _seed_window(env)
        await env.dao.add_booking("利物浦", 1, 1, 1, "esports", "")
        await env.brand_service.sign("利物浦", "亚马逊", 1, 1)
        import random

        random.seed(42)
        await env.window_service.settle()
        balance1 = (await env.dao.get_balance("利物浦"))["balance"]
        txs1 = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        fans1 = (await env.dao.get_stadium("利物浦"))["fans_diehards"]

        # 固定随机种子：档期收入等随机项两次结算掷出相同结果
        random.seed(42)
        await env.window_service.settle(force=True)
        balance2 = (await env.dao.get_balance("利物浦"))["balance"]
        txs2 = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        fans2 = (await env.dao.get_stadium("利物浦"))["fans_diehards"]
        assert abs(balance2 - balance1) < 1e-6, f"余额应一致: {balance1} vs {balance2}"
        assert len(txs2) == len(txs1), f"流水条数应一致: {len(txs1)} vs {len(txs2)}"
        assert sorted(t["kind"] for t in txs2) == sorted(t["kind"] for t in txs1)
        assert abs(fans2 - fans1) < 1e-6, "强制重算不应重复演化"
        # 冠名窗口数不应重复扣减（4 窗口 → 3）
        naming = await env.dao.get_active_naming("利物浦")
        assert naming["windows_remaining"] == 3, naming["windows_remaining"]
        # 赛果门票流水（非结算创建）不应被撤销
        assert any(t["kind"] == "ticket" for t in txs2), txs2
    finally:
        await env.teardown()


async def test_brand_terminate_drop_below_threshold():
    """死忠单窗跌幅未达阈值（默认 0.3）时，即便解约概率命中也不解约
    （命中分支见 test_brand_terminate_returns_tx_id）。"""
    from unittest.mock import patch
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=100.0)
        await env.brand_service.sign("利物浦", "亚马逊", 1, 1)
        with patch("random.random", return_value=0.0):
            result = await env.brand_service.maybe_brand_terminate("利物浦", 1, 2, 4000, 3900)
        assert result is None, result
        naming = await env.dao.get_active_naming("利物浦")
        assert naming is not None, "未达阈值不应解约"
    finally:
        await env.teardown()


async def test_redo_skips_choice_state_effects():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        import random

        random.seed(7)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="new_wave")
        # 选项1 办球迷开放日：死忠 +4%/资金-0.5（70%）或死忠 +1%/资金-1.5（30%）
        await env.dao.set_event_choice("利物浦", 1, 1, "new_wave", 1)
        await env.window_service.settle()
        fans_after_1 = (await env.dao.get_stadium("利物浦"))["fans_diehards"]
        # 强制重算：选择事件只重记账目、不再叠加死忠（避免复利）
        random.seed(8)
        await env.window_service.settle(force=True)
        fans_after_redo = (await env.dao.get_stadium("利物浦"))["fans_diehards"]
        assert fans_after_redo == fans_after_1, (fans_after_1, fans_after_redo)
        summary = await env.dao.get_window_summary(1, 1)
        assert json.loads(summary["tx_ids"]), "重算后仍应生成结算流水"
        # 结算回顾标注「重算跳过」
        logs = await env.dao.get_window_events("利物浦", 1, 1)
        assert any("重算跳过" in (l["text"] or "") for l in logs), [l["text"] for l in logs]
    finally:
        await env.teardown()

async def test_settle_marker_reconciles_created_txs():
    env = await TestEnv().setup()
    try:
        await _seed_window(env)
        await env.dao.add_booking("利物浦", 1, 1, 1, "esports", "")
        await env.brand_service.sign("利物浦", "亚马逊", 1, 1)
        await env.window_service.settle()

        marker = await env.dao.get_window_summary(1, 1)
        assert marker is not None
        marked = set(json.loads(marker["tx_ids"] or "[]"))
        assert marked, "标记应记录结算创建的流水 ID"
        revenue_kinds = {"ticket", "commercial", "broadcast"}
        for s in await env.dao.list_stadiums():
            txs = await env.dao.list_transactions(s["team_name"])
            for t in txs:
                if t["season_number"] == 0:
                    continue  # init 开场资金流水
                if t["kind"] in revenue_kinds:
                    assert t["id"] not in marked, t
                else:
                    assert t["id"] in marked, \
                        f"{s['team_name']} 流水 {t['kind']}#{t['id']} 未入标记"
    finally:
        await env.teardown()


async def test_settle_crash_then_force_rerun_no_double_charge():
    env = await TestEnv().setup()
    try:
        await _seed_window(env)
        await env.dao.add_booking("利物浦", 1, 1, 1, "esports", "")
        await env.brand_service.sign("利物浦", "亚马逊", 1, 1)

        real_add = env.dao.record_entries
        calls = {"n": 0}

        async def flaky_add(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("模拟结算中途崩溃")
            return await real_add(*args, **kwargs)

        env.dao.record_entries = flaky_add
        crashed = False
        try:
            await env.window_service.settle()
        except RuntimeError:
            crashed = True
        assert crashed, "应在第 3 笔流水处模拟崩溃"
        env.dao.record_entries = real_add

        marker = await env.dao.get_window_summary(1, 1)
        assert marker is not None, "崩溃后标记应已存在（结算权先认领）"
        assert json.loads(marker["tx_ids"] or "[]"), "崩溃前创建的流水应已增量落盘"

        # 强制重算：撤销已落盘流水后补齐，不得重复扣费
        await env.window_service.settle(force=True)
        for s in await env.dao.list_stadiums():
            team = s["team_name"]
            txs = await env.dao.list_transactions(team)
            total = sum(t["amount"] for t in txs if t["kind"] != "credit")
            bal = (await env.dao.get_balance(team))["balance"]
            assert abs(bal - total) < 1e-6, f"{team} 余额应等于流水和: {bal} vs {total}"
            for kind in ("maintenance", "activity", "naming"):
                n = sum(1 for t in txs
                        if t["kind"] == kind and t["season_number"] == 1
                        and t["window_seq"] == 1)
                assert n <= 1, f"{team} {kind} 流水重复 {n} 条"
        naming = await env.dao.get_active_naming("利物浦")
        assert naming["windows_remaining"] == 4, naming["windows_remaining"]
    finally:
        await env.teardown()


async def test_brand_terminate_returns_tx_id():
    from unittest.mock import patch
    env = await TestEnv().setup()
    try:
        await _seed_window(env, fans_override={"利物浦": 4000})
        await env.brand_service.sign("利物浦", "亚马逊", 1, 1)
        with patch("random.random", return_value=0.0):
            result = await env.brand_service.maybe_brand_terminate("利物浦", 1, 1, 4000.0, 2000.0)
        assert result is not None, "跌幅 50% 且概率命中应解约"
        assert result["tx_id"], result
        naming = await env.dao.get_active_naming("利物浦")
        assert naming is None, "解约后不应再有生效冠名"
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        match = [t for t in txs if t["id"] == result["tx_id"]]
        assert match and match[0]["kind"] == "naming" and match[0]["amount"] == 0.0, match
        assert "解约" in match[0]["note"], match[0]["note"]
    finally:
        await env.teardown()


async def test_window_attend_rate_zero_capacity_guard():
    """C🟡6：容量非正时窗口上座率按中性 1.0 返回，不除零。"""
    env = await TestEnv().setup()
    try:
        assert await env.fans_service.window_attend_rate("利物浦", 1, 1, 0) == 1.0
        assert await env.fans_service.window_attend_rate("利物浦", 1, 1, -100) == 1.0
        # 已有录入场次 + 容量 0 → 仍 1.0（修复前 ZeroDivisionError）
        await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那\n")
        await env.fixture_service.record_results(1, "利物浦 胜")
        assert await env.fans_service.window_attend_rate("利物浦", 1, 1, 0) == 1.0
    finally:
        await env.teardown()
