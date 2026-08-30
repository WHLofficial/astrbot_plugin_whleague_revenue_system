"""DAO 资金/取号原子性：recompute 单语句、建设券不扣负、轮次号并发唯一（审查 🟡 A 项回归）。"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv, install_stubs  # noqa: E402,F401  (导入即装桩)


async def test_recompute_balance_single_statement():
    """余额重算：SUM 与写回同语句，重算后余额恒等于含 init 在内的非 credit 流水合计。"""
    env = await TestEnv().setup()
    try:
        await env.dao.ensure_balance("利物浦", 100.0)  # 实插 → 补记 init 流水 +100
        await env.dao.add_transaction("利物浦", 1, 1, kind="ticket", amount=50.0)
        await env.dao.add_transaction("利物浦", 1, 1, kind="credit", amount=7.0)
        await env.dao.add_transaction("利物浦", 1, 2, kind="commercial", amount=-12.5)
        # 模拟外部污染后重算自愈
        await env.dao._db.execute("UPDATE club_balance SET balance=999.0 WHERE team_name='利物浦'")
        await env.dao.recompute_balance("利物浦")
        bal = (await env.dao.get_balance("利物浦"))["balance"]
        assert abs(bal - 137.5) < 1e-6, bal

        # 并发「流水+入账+重算」交错后归一，不变量成立：balance == SUM(非 credit)
        async def bump():
            for _ in range(20):
                await env.dao.add_transaction("利物浦", 1, 1, kind="commercial", amount=1.0)
                await env.dao.apply_balance("利物浦", 1.0)
                await env.dao.recompute_balance("利物浦")

        await asyncio.gather(bump(), bump())
        await env.dao.recompute_balance("利物浦")
        bal = (await env.dao.get_balance("利物浦"))["balance"]
        assert abs(bal - 177.5) < 1e-6, bal
    finally:
        await env.teardown()


async def test_ensure_balance_init_tx_once():
    """ensure_balance 实插时同事务补记 init 流水；重复调用不重复记账，recompute 不丢启动资金。"""
    env = await TestEnv().setup()
    try:
        await env.dao.ensure_balance("利物浦", 50.0)
        await env.dao.ensure_balance("利物浦", 50.0)  # 已存在 → 不再补记
        txs = [t for t in await env.dao.list_transactions("利物浦") if t["kind"] == "init"]
        assert len(txs) == 1, txs
        assert abs(txs[0]["amount"] - 50.0) < 1e-6 and txs[0]["season_number"] == 0, txs[0]
        await env.dao.recompute_balance("利物浦")
        bal = (await env.dao.get_balance("利物浦"))["balance"]
        assert abs(bal - 50.0) < 1e-6, bal
        # 建场路径走 ensure_balance，同样只产生一条 init（不双记）
        await env.stadium_service.ensure_stadium("巴塞罗那")
        await env.stadium_service.ensure_stadium("巴塞罗那")
        txs = [t for t in await env.dao.list_transactions("巴塞罗那") if t["kind"] == "init"]
        assert len(txs) == 1, txs
    finally:
        await env.teardown()


async def test_deduct_build_credit_never_negative():
    """建设券扣减事务化：券不足只扣余额内部分，并发扣减合计不超过持有量。"""
    env = await TestEnv().setup()
    try:
        await env.dao.ensure_balance("利物浦", 0.0)
        await env.dao.apply_balance("利物浦", 0.0, build_credit_amount=10.0)
        used1 = await env.dao.deduct_build_credit("利物浦", 8.0)
        assert abs(used1 - 8.0) < 1e-6, used1
        used2 = await env.dao.deduct_build_credit("利物浦", 8.0)
        assert abs(used2 - 2.0) < 1e-6, used2  # 只扣剩余 2，绝不透支
        row = await env.dao.get_balance("利物浦")
        assert abs(row["build_credit"]) < 1e-6, row

        # 并发两次各扣 8：合计实际抵扣必须等于持有量 10
        await env.dao.apply_balance("利物浦", 0.0, build_credit_amount=10.0)
        u1, u2 = await asyncio.gather(
            env.dao.deduct_build_credit("利物浦", 8.0),
            env.dao.deduct_build_credit("利物浦", 8.0),
        )
        assert abs(u1 + u2 - 10.0) < 1e-6, (u1, u2)
        row = await env.dao.get_balance("利物浦")
        assert row["build_credit"] >= -1e-6, row
    finally:
        await env.teardown()


async def test_add_named_round_concurrent_unique_numbers():
    """命名轮次登记事务化：并发登记不同轮次不撞号，已登记返回原号。"""
    env = await TestEnv().setup()
    try:
        n1, n2 = await asyncio.gather(
            env.dao.add_named_round(1, "联赛", "第一轮"),
            env.dao.add_named_round(1, "联赛", "第二轮"),
        )
        assert {n1, n2} == {1, 2}, (n1, n2)
        # 重复登记同一轮次文本返回原号
        assert await env.dao.add_named_round(1, "联赛", "第一轮") == min(n1, n2)
        assert await env.dao.add_named_round(1, "联赛", "第三轮") == 3
        # 不同赛事独立取号
        assert await env.dao.add_named_round(1, "杯赛", "第一轮") == 1
    finally:
        await env.teardown()
