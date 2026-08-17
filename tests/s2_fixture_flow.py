"""全链路测试：赛程导入 → 天气预报 → 赛果录入 → 上座/票房 → 统计 → 推进赛季窗口。"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.fixture_service import FixtureError  # noqa: E402


async def test_fixture_import_and_weather():
    env = await TestEnv().setup()
    try:
        result = await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n1 纽卡斯尔联 勒沃库森\n"
        )
        assert result["imported"] == 2, result
        assert result["skipped"] == 0

        # 未知球队：跳过并报告错误
        result2 = await env.fixture_service.import_fixtures("2 利物浦 不存在的队")
        assert result2["imported"] == 0
        assert result2["skipped"] == 1
        assert result2["errors"], result2["errors"]

        # 天气：晴/多云/雨/雪 之一
        fc = await env.fixture_service.forecast_round(1)
        assert len(fc["matches"]) == 2
        assert all(m["weather"] in ("晴", "多云", "雨", "雪") for m in fc["matches"])

        # 覆盖天气
        await env.fixture_service.set_weather(1, "利物浦", "雪")
        matches = await env.dao.get_round_matches(1, 1, 1)
        liverpool = [m for m in matches if m["home_team"] == "利物浦"][0]
        assert liverpool["weather"] == "雪"
    finally:
        await env.teardown()


async def test_record_results_full():
    env = await TestEnv().setup()
    try:
        await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n1 纽卡斯尔联 勒沃库森\n"
        )
        await env.fixture_service.forecast_round(1)

        # 初始化属性（影响力不同，便于验证对手拉力；死忠演化在结算时进行）
        await env.stadium_service.import_attributes("利物浦", influence=180.0)
        await env.stadium_service.import_attributes("巴塞罗那", influence=90.0)
        await env.stadium_service.import_attributes("纽卡斯尔联", influence=120.0)
        await env.stadium_service.import_attributes("勒沃库森", influence=120.0)
        # 死忠按新影响力补齐（3600），验证上座计算而非演化
        await env.dao.update_fans("利物浦", 3600.0)

        result = await env.fixture_service.record_results(
            1, "利物浦 胜\n纽卡斯尔联 平\n"
        )
        assert result["count"] == 2, result
        r = result["results"][0]
        assert r["home"] == "利物浦"
        assert r["result"] == "W"
        # 顶级队（180 影响力 → 死忠 3600）1.2 万座：应该坐满
        assert r["attendance"] == 12000, r
        assert r["ticket"] > 1.0

        # 重复录入应报错
        try:
            await env.fixture_service.record_results(1, "利物浦 负\n")
            raise AssertionError("重复录入应报错")
        except FixtureError:
            pass

        # 余额已入账
        balance = await env.dao.get_balance("利物浦")
        assert balance["balance"] > 40.0, balance["balance"]  # 50 起步 + 收入

        # 流水存在（ticket 按窗口查；init 初始资金记在 0/0 窗口）
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        kinds = {t["kind"] for t in txs}
        assert "ticket" in kinds, kinds
        all_txs = await env.dao.list_transactions("利物浦", limit=200)
        assert any(t["kind"] == "init" for t in all_txs)
    finally:
        await env.teardown()


async def test_round_and_season_stats():
    env = await TestEnv().setup()
    try:
        await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n1 纽卡斯尔联 勒沃库森\n"
        )
        await env.fixture_service.forecast_round(1)
        await env.fixture_service.record_results(1, "利物浦 胜\n纽卡斯尔联 胜\n")

        rs = await env.fixture_service.round_stats(1)
        assert rs["totals"]["attendance"] > 0

        ss = await env.season_stats() if hasattr(env, "season_stats") else await env.fixture_service.season_stats()
        assert len(ss["rows"]) == 2
        for row in ss["rows"]:
            assert row["total"] > 0
            assert row["max"] >= row["min"]
            assert row["avg"] >= row["min"]
    finally:
        await env.teardown()


async def test_advance_window_season():
    env = await TestEnv().setup()
    try:
        st = await env.fixture_service.advance_window("tester")
        assert st["window_seq"] == 2
        st2 = await env.fixture_service.advance_season("tester")
        assert st2["season_number"] == 2
        assert st2["window_seq"] == 1
    finally:
        await env.teardown()


async def test_parse_result_aliases():
    from astrbot_plugin_whleague_revenue_system.services.fixture_service import (
        parse_result_lines,
    )

    rows = parse_result_lines("利物浦 W\n巴塞罗那 平\n纽卡 负")
    assert rows == [("利物浦", "W"), ("巴塞罗那", "D"), ("纽卡", "L")]