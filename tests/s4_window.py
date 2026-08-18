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
        # 影响力 150 → 死忠 3000 满目标；改为 214 后死忠应追上
        await _seed_window(env, fans_override={"利物浦": 3000})
        await env.stadium_service.import_attributes("利物浦", influence=214.42)
        # 下一窗口再赛一场，让演化有数据
        await env.fixture_service.advance_window("tester")
        await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那\n")
        await env.fixture_service.forecast_round(1)
        await env.fixture_service.record_results(1, "利物浦 胜\n")
        await env.window_service.settle()
        stadium = await env.dao.get_stadium("利物浦")
        # 目标 4288，应朝上追（涨粉方向）
        assert stadium["fans_diehards"] > 3000, stadium["fans_diehards"]

        # 掉粉方向：影响力跌回 100（目标 2000）
        await env.stadium_service.import_attributes("利物浦", influence=100.0)
        await env.fixture_service.advance_window("tester")
        await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那\n")
        await env.fixture_service.forecast_round(1)
        await env.fixture_service.record_results(1, "利物浦 负\n")
        before = (await env.dao.get_stadium("利物浦"))["fans_diehards"]
        await env.window_service.settle()
        after = (await env.dao.get_stadium("利物浦"))["fans_diehards"]
        assert after < before, f"掉粉方向应下降: {before} -> {after}"
        # 死忠对齐目标后（影响力不变）不演化
        await env.dao.update_fans("利物浦", 2000.0)
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
        # 4 队均离目标、无已录赛果（上座率中性 1.0、战绩修正 ×0.95）
        for team, infl in (("利物浦", 150.0), ("巴塞罗那", 100.0),
                           ("纽卡斯尔联", 120.0), ("勒沃库森", 120.0)):
            await env.stadium_service.import_attributes(team, influence=infl)
        await env.window_service.settle()
        from astrbot_plugin_whleague_revenue_system.services import formula

        fans = (await env.dao.get_stadium("利物浦"))["fans_diehards"]
        # 单轮演化：1800 + 1200×0.5×(0.6+0.4×1.0) = 2400，再 ×0.95 = 2280；
        # 旧实现会按球队数重复演化（4 队 → 约 2668）
        assert abs(fans - 2280.0) < 0.01, f"应仅演化一轮: {fans}"
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


async def test_brand_terminate_on_fan_drop():
    env = await TestEnv().setup()
    try:
        await _seed_window(env, fans_override={"利物浦": 4000})
        await env.brand_service.sign("利物浦", "亚马逊", 1, 1)
        # 死忠暴跌 50%：跌到 2000（目标 3000 以下，掉粉方向）
        await env.dao.update_fans("利物浦", 4000)
        await env.stadium_service.import_attributes("利物浦", influence=100.0)  # 目标 2000
        # 强制让掉粉发生
        import random

        random.seed(1)
        # naming_terminate_probability 默认 0.3，这里手动触发验证
        await env.fixture_service.advance_window("tester")
        await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那\n")
        await env.fixture_service.forecast_round(1)
        await env.fixture_service.record_results(1, "利物浦 负\n")
        before_fans = (await env.dao.get_stadium("利物浦"))["fans_diehards"]
        after_fans = await env.fans_service.evolve(1, 2)
        fans_after = before_fans
        for fd in after_fans:
            if fd["team"] == "利物浦":
                fans_after = fd["after"]
        result = await env.brand_service.maybe_brand_terminate("利物浦", 1, 2, before_fans, fans_after)
        # 掉幅若 ≥30% 且概率命中则解约
        drop = (before_fans - fans_after) / before_fans if before_fans else 0
        if drop >= 0.3:
            assert result is not None, "触发跌幅阈值应有机会解约"
        naming = await env.dao.get_active_naming("利物浦")
        assert naming is None or naming["status"] == "active"
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