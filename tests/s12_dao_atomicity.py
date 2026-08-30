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


async def test_record_entries_atomic_pair():
    """record_entries：余额增量与流水同事务落盘，并发配对不产生账实分离。"""
    env = await TestEnv().setup()
    try:
        await env.dao.ensure_balance("利物浦", 50.0)
        ids = await env.dao.record_entries("利物浦", 1, 1, [
            ("ticket", 30.0, "票", 3),
            ("commercial", -5.5, "商", None),
        ], build_credit_amount=2.0)
        assert len(ids) == 2 and all(ids), ids
        row = await env.dao.get_balance("利物浦")
        assert abs(row["balance"] - 74.5) < 1e-6, row
        assert abs(row["build_credit"] - 2.0) < 1e-6, row
        txs = await env.dao.list_transactions("利物浦")
        assert {t["kind"] for t in txs} >= {"init", "ticket", "commercial"}
        # 未重算时账实即一致（配对原子性的核心断言）
        total = sum(t["amount"] for t in txs if t["kind"] != "credit")
        assert abs(row["balance"] - total) < 1e-6, (row["balance"], total)
        # 空 entries 是无操作
        assert await env.dao.record_entries("利物浦", 1, 1, []) == []

        async def pair(i):
            await env.dao.record_entries("利物浦", 1, 1, [("event", 1.0, f"e{i}", None)])

        await asyncio.gather(*(pair(i) for i in range(20)))
        row = await env.dao.get_balance("利物浦")
        txs = await env.dao.list_transactions("利物浦")
        total = sum(t["amount"] for t in txs if t["kind"] != "credit")
        assert abs(row["balance"] - total) < 1e-6, (row["balance"], total)
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


async def test_execute_transaction_commit_and_rollback():
    """事务回调成功提交、异常整体回滚；回调内调 dao 触发死锁守卫。"""
    env = await TestEnv().setup()
    try:
        db = env.dao._db

        async def ok(conn):
            await conn.execute(
                "INSERT INTO club_balance (team_name, balance) VALUES ('甲', 100.0)"
            )
            return "done"

        assert await db.execute_transaction(ok) == "done"
        row = await env.dao._db.fetchone(
            "SELECT balance FROM club_balance WHERE team_name='甲'"
        )
        assert row and abs(row["balance"] - 100.0) < 1e-6, row

        async def boom(conn):
            await conn.execute(
                "INSERT INTO club_balance (team_name, balance) VALUES ('乙', 5.0)"
            )
            raise ValueError("boom")

        try:
            await db.execute_transaction(boom)
        except ValueError:
            pass
        else:
            raise AssertionError("事务回调异常应向上抛出")
        assert await env.dao._db.fetchone(
            "SELECT * FROM club_balance WHERE team_name='乙'"
        ) is None

        async def reenter(conn):
            await env.dao.get_balance("甲")

        try:
            await db.execute_transaction(reenter)
        except RuntimeError as e:
            assert "禁止在事务回调" in str(e), e
        else:
            raise AssertionError("回调内调 dao 应触发死锁守卫")
    finally:
        await env.teardown()


async def test_execute_locked_retry_and_other_errors():
    """database is locked 重试后成功；其他 OperationalError 立即上抛。"""
    import aiosqlite

    env = await TestEnv().setup()
    try:
        db = env.dao._db

        class _Flaky:
            def __init__(self, inner):
                self._inner = inner
                self.calls = 0

            async def execute(self, sql, params=()):
                self.calls += 1
                if self.calls == 1:
                    raise aiosqlite.OperationalError("database is locked")
                return await self._inner.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        proxy = _Flaky(db._conn)
        db._conn = proxy
        try:
            await db.execute(
                "UPDATE club_balance SET balance=1.0 WHERE team_name='不存在'"
            )
            assert proxy.calls == 2, proxy.calls
        finally:
            db._conn = proxy._inner

        class _Boom:
            def __init__(self, inner):
                self._inner = inner

            async def execute(self, sql, params=()):
                raise aiosqlite.OperationalError("no such table: xx")

            def __getattr__(self, name):
                return getattr(self._inner, name)

        boom = _Boom(db._conn)
        db._conn = boom
        try:
            try:
                await db.execute("SELECT 1")
            except aiosqlite.OperationalError as e:
                assert "no such table" in str(e), e
            else:
                raise AssertionError("非 locked OperationalError 应立即上抛")
        finally:
            db._conn = boom._inner
    finally:
        await env.teardown()
