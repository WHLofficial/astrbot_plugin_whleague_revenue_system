"""属性导入：越界校验、自动记账、建设券、设施、改名、发放。"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.stadium_service import StadiumError  # noqa: E402


async def test_import_attributes_and_costs():
    env = await TestEnv(cfg_override={"max_open_tier": 4}).setup()
    try:
        # 首次导入自动建场（默认 1.2 万座 0 级、默认影响力 90 → 阶梯死忠 2340）
        result = await env.stadium_service.import_attributes("利物浦", influence=150.0)
        assert result["stadium"]["capacity"] == 12000
        assert result["stadium"]["tier"] == 0
        # 死忠不随影响力即时跳变（演化在窗口结算进行），初始为默认影响力目标
        assert abs(result["stadium"]["fans_diehards"] - 2340.0) < 1e-6
        assert abs(result["stadium"]["influence"] - 150.0) < 1e-6

        # 扩容 12000 → 20000：差量 8000 座 × 0.1/100 = 8M
        result = await env.stadium_service.import_attributes("利物浦", capacity=20000)
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        expand = [t for t in txs if t["kind"] == "expand"]
        assert expand and abs(expand[0]["amount"] + 8.0) < 1e-6, expand
        # 建设券返还 25% = 2M
        credits = [t for t in txs if t["kind"] == "credit"]
        assert credits and abs(credits[0]["amount"] - 2.0) < 1e-6, credits
        balance = await env.dao.get_balance("利物浦")
        assert abs(balance["build_credit"] - 2.0) < 1e-6

        # 升级 0→1：4.5M
        result = await env.stadium_service.import_attributes("利物浦", tier=1)
        assert result["stadium"]["tier"] == 1
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        upgrades = [t for t in txs if t["kind"] == "upgrade"]
        assert upgrades and abs(upgrades[0]["amount"] + 4.5) < 1e-6, upgrades

        # 再升 1→2：6M（累计）
        await env.stadium_service.import_attributes("利物浦", tier=2)
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        upgrades = [t for t in txs if t["kind"] == "upgrade"]
        assert abs(upgrades[0]["amount"] + 6.0) < 1e-6

        # 建设券抵扣：升级到 3 级费用 10M，支出备注应体现建设券抵扣
        await env.stadium_service.import_attributes("利物浦", tier=3)
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        upgrades = [t for t in txs if t["kind"] == "upgrade"]
        assert "建设券抵扣" in upgrades[0]["note"], upgrades[0]["note"]
        # 抵扣用掉的券由本次支出的 25% 返还补上（2.5M）
        balance = await env.dao.get_balance("利物浦")
        assert abs(balance["build_credit"] - 2.5) < 1e-6, balance["build_credit"]
    finally:
        await env.teardown()


async def test_validation_errors():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=100.0)
        # 容量越界（0 级上限 2.5 万）
        try:
            await env.stadium_service.import_attributes("利物浦", capacity=30000)
            raise AssertionError("0 级容量 3 万应被拒绝")
        except StadiumError:
            pass
        # 等级越界（max_open_tier=1）
        try:
            await env.stadium_service.import_attributes("利物浦", tier=2)
            raise AssertionError("tier 2 未开放应被拒绝")
        except StadiumError:
            pass
        # 等级内容量下限（1 级下限 2 万）
        await env.stadium_service.import_attributes("利物浦", tier=1)
        try:
            await env.stadium_service.import_attributes("利物浦", capacity=15000)
            raise AssertionError("1 级容量 1.5 万应被拒绝")
        except StadiumError:
            pass
    finally:
        await env.teardown()


async def test_facility_import():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=100.0)
        result = await env.stadium_service.import_facility("利物浦", "commercial", 3)
        assert result["level"] == 3
        # 3 级累计 3+5+8 = 16M
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        upgrades = [t for t in txs if t["kind"] == "upgrade"]
        assert abs(upgrades[-1]["amount"] + 16.0) < 1e-6, upgrades
        # 降级不收费
        await env.stadium_service.import_facility("利物浦", "commercial", 1)
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        assert len([t for t in txs if t["kind"] == "upgrade"]) == 1
        # 未知设施
        try:
            await env.stadium_service.import_facility("利物浦", "unknown", 1)
            raise AssertionError("未知设施应被拒绝")
        except StadiumError:
            pass
    finally:
        await env.teardown()


async def test_admin_rename():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=100.0)
        r = await env.stadium_service.admin_rename("利物浦", "安菲尔德")
        assert r["old"] == "利物浦主场" and r["new"] == "安菲尔德"
        stadium = await env.dao.get_stadium("利物浦")
        assert stadium["name"] == "安菲尔德"
        # 管理员操作免费：余额不变、不产生任何流水
        balance = await env.dao.get_balance("利物浦")
        assert abs(balance["balance"] - 50.0) < 1e-6
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        assert not [t for t in txs if t["kind"] == "rename"]
        # 无球场的队伍报错（不自动建场）
        try:
            await env.stadium_service.admin_rename("不存在队", "X 球场")
            raise AssertionError("应抛 StadiumError")
        except StadiumError:
            pass
        assert await env.dao.get_stadium("不存在队") is None
        # 名称校验：空白 / 含换行 / 超 30 字 / 与现名相同
        for bad in ("  ", "超" * 31, "安菲\n尔德"):
            try:
                await env.stadium_service.admin_rename("利物浦", bad)
                raise AssertionError("应抛 StadiumError")
            except StadiumError:
                pass
        try:
            await env.stadium_service.admin_rename("利物浦", "安菲尔德")
            raise AssertionError("同名改名应抛 StadiumError")
        except StadiumError as e:
            assert "未变化" in str(e)
    finally:
        await env.teardown()


async def test_grant_all():
    env = await TestEnv().setup()
    try:
        result = await env.stadium_service.grant_all()
        assert result["created"] == 4
        stadiums = await env.dao.list_stadiums()
        assert len(stadiums) == 4
        # 幂等
        result2 = await env.stadium_service.grant_all()
        assert result2["created"] == 4
        assert len(await env.dao.list_stadiums()) == 4
    finally:
        await env.teardown()