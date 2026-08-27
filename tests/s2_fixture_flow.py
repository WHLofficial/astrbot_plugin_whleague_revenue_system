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
        # 固定利物浦天气为晴：断言「坐满 12000」不受随机天气影响（随机流会被其他测试位移）
        await env.fixture_service.set_weather(1, "利物浦", "晴")

        # 初始化属性（影响力不同，便于验证对手拉力；死忠演化在结算时进行）
        await env.stadium_service.import_attributes("利物浦", influence=180.0)
        await env.stadium_service.import_attributes("巴塞罗那", influence=90.0)
        await env.stadium_service.import_attributes("纽卡斯尔联", influence=120.0)
        await env.stadium_service.import_attributes("勒沃库森", influence=120.0)
        # 死忠手动补齐 3600（与影响力脱钩），验证上座计算而非演化
        await env.dao.update_fans("利物浦", 3600.0)

        result = await env.fixture_service.record_results(
            1, "利物浦 胜\n纽卡斯尔联 平\n"
        )
        assert result["count"] == 2, result
        r = result["results"][0]
        assert r["home"] == "利物浦"
        assert r["result"] == "W"
        # 顶级队（死忠 3600）1.2 万座：应该坐满
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


async def test_season_naming():
    """赛季命名：校验/改名/未变化；推进带名（非法名不推进）。"""
    from astrbot_plugin_whleague_revenue_system.services.fixture_service import FixtureError

    env = await TestEnv().setup()
    try:
        r1 = await env.fixture_service.name_season("  S8 黄金联赛  ", "tester")
        assert r1 == {"season": 1, "old": None, "new": "S8 黄金联赛"}  # 去空白
        assert await env.dao.get_season_name(1) == "S8 黄金联赛"
        r2 = await env.fixture_service.name_season("S8 修订版", "tester")
        assert r2["old"] == "S8 黄金联赛" and r2["new"] == "S8 修订版"
        # 校验：空 / 换行 / 超长 / 未变化
        for bad in ("   ", "S8\n黄金", "超" * 31, "S8 修订版"):
            try:
                await env.fixture_service.name_season(bad, "tester")
                assert False, f"应拒绝: {bad!r}"
            except FixtureError:
                pass
        # 推进带名：命名新赛季且窗口归位
        st = await env.fixture_service.advance_season("tester", "S9 新赛季")
        assert st["season_number"] == 2 and st["name"] == "S9 新赛季"
        assert await env.dao.season_label(2) == "S9 新赛季"
        assert await env.dao.season_label(1) == "S8 修订版"  # 旧赛季名保留
        # 推进带非法名（含纯空白名）：整体不推进（窗口/轮次状态不动）
        state_before = await env.dao.get_league_state()
        for bad in ("非\n法", "   "):
            try:
                await env.fixture_service.advance_season("tester", bad)
                assert False, f"非法名应整体失败: {bad!r}"
            except FixtureError:
                pass
        state_after = await env.dao.get_league_state()
        assert state_before["season_number"] == state_after["season_number"] == 2
        assert state_after["window_seq"] == 1
        # 推进不带名：回退 第 N 赛季
        st3 = await env.fixture_service.advance_season("tester")
        assert st3["name"] is None and st3["season_number"] == 3
        assert await env.dao.season_label(3) == "第 3 赛季"
    finally:
        await env.teardown()


async def test_parse_result_aliases():
    from astrbot_plugin_whleague_revenue_system.services.fixture_service import (
        parse_result_lines,
    )

    rows = parse_result_lines("利物浦 W\n巴塞罗那 平\n纽卡 负")
    assert rows == [("利物浦", "W", None), ("巴塞罗那", "D", None), ("纽卡", "L", None)]
    rows2 = parse_result_lines("利物浦 胜 2-1\n纽卡 负 0-0PK2-4")
    assert rows2 == [("利物浦", "W", "2-1"), ("纽卡", "L", "0-0PK2-4")]


async def test_schedule_fields_and_score():
    env = await TestEnv().setup()
    try:
        r = await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那 W12 D6 15:00\n2 利物浦 巴塞罗那 15:00\n3 利物浦 巴塞罗那\n"
        )
        assert r["imported"] == 3, r
        m1 = (await env.dao.get_round_matches(1, 1, 1))[0]
        assert m1["week_no"] == 12 and m1["day_no"] == 6 and m1["match_time"] == "15:00"
        m2 = (await env.dao.get_round_matches(1, 1, 2))[0]
        assert m2["week_no"] is None and m2["match_time"] == "15:00"
        m3 = (await env.dao.get_round_matches(1, 1, 3))[0]
        assert m3["week_no"] is None and m3["day_no"] is None and m3["match_time"] is None

        # 非法天/时间拒绝
        for bad in ("4 利物浦 巴塞罗那 D9 15:00", "5 利物浦 巴塞罗那 25:00"):
            try:
                await env.fixture_service.import_fixtures(bad)
                raise AssertionError(f"应拒绝: {bad}")
            except FixtureError:
                pass

        # 赛果带比分
        await env.fixture_service.set_weather(1, "利物浦", "晴")
        rec = await env.fixture_service.record_results(1, "利物浦 胜 2-1")
        assert rec["results"][0]["score"] == "2-1"
        m1b = (await env.dao.get_round_matches(1, 1, 1))[0]
        assert m1b["score"] == "2-1"

        # 星期单字
        from astrbot_plugin_whleague_revenue_system.services import formula

        assert formula.weekday_name(1) == "一"
        assert formula.weekday_name(6) == "六"
        assert formula.weekday_name(7) == "日"
        assert formula.weekday_name(0) == "" and formula.weekday_name(8) == ""
    finally:
        await env.teardown()


async def test_competitions_share_round_numbers():
    env = await TestEnv().setup()
    try:
        # 顶级9/次级9/冠军3 都是各自赛事的首个轮次文本 → 各赛事内均第 1 轮，同窗口共存互不干扰
        r = await env.fixture_service.import_fixtures(
            "顶级9 利物浦 巴塞罗那\n次级9 纽卡斯尔联 勒沃库森\n冠军3 利物浦 勒沃库森\n"
        )
        assert r["imported"] == 3, r
        comp_top, rn_top = await env.fixture_service.resolve_round_arg("顶级9")
        comp_sub, rn_sub = await env.fixture_service.resolve_round_arg("次级9")
        comp_cup, rn_cup = await env.fixture_service.resolve_round_arg("冠军3")
        assert (comp_top, rn_top) == ("顶级联赛", 1)
        assert (comp_sub, rn_sub) == ("次级联赛", 1)
        assert (comp_cup, rn_cup) == ("冠军杯", 1)
        top = await env.dao.get_round_matches(1, 1, rn_top, comp_top)
        sub = await env.dao.get_round_matches(1, 1, rn_sub, comp_sub)
        assert len(top) == 1 and top[0]["competition"] == "顶级联赛"
        assert len(sub) == 1 and sub[0]["competition"] == "次级联赛"
        # 不带赛事过滤：同一轮次号下含三个赛事的比赛
        assert len(await env.dao.get_round_matches(1, 1, 1)) == 3
        # 同名主队不同赛事可共存（利物浦在顶级 和 冠军杯 均主场）
        assert len(await env.dao.get_round_matches(1, 1, rn_cup, comp_cup)) == 1

        # 预报/天气/赛果/统计按赛事隔离
        fc_top = await env.fixture_service.forecast_round(rn_top, comp_top)
        assert len(fc_top["matches"]) == 1 and fc_top["matches"][0]["home"] == "利物浦"
        fc_sub = await env.fixture_service.forecast_round(rn_sub, comp_sub)
        assert len(fc_sub["matches"]) == 1
        await env.fixture_service.set_weather(rn_top, "利物浦", "晴", comp_top)
        top_m = await env.dao.get_round_matches(1, 1, rn_top, comp_top)
        assert top_m[0]["weather"] == "晴"
        # 次级联赛的天气未被污染
        sub_m = await env.dao.get_round_matches(1, 1, rn_sub, comp_sub)
        assert sub_m[0]["weather"] in ("晴", "多云", "雨", "雪")

        rec = await env.fixture_service.record_results(rn_top, "利物浦 胜\n", comp_top)
        assert rec["count"] == 1
        rs = await env.fixture_service.round_stats(rn_top, comp_top)
        assert rs["totals"]["attendance"] > 0
        assert rs["totals"]["ticket"] > 0
        # 次级 尚无赛果
        rs_sub = await env.fixture_service.round_stats(rn_sub, comp_sub)
        assert rs_sub["totals"]["attendance"] == 0
    finally:
        await env.teardown()


async def test_named_round_same_name_same_round():
    """纯文字轮次（无数字）：同名即为同一轮，跨导入/天气/赛果/统计恒同号。"""
    env = await TestEnv().setup()
    try:
        # 两行同名「顶级」→ 同一轮、同一赛事
        r = await env.fixture_service.import_fixtures(
            "顶级 利物浦 巴塞罗那\n顶级 纽卡斯尔联 勒沃库森\n"
        )
        assert r["imported"] == 2, r
        matches = await env.dao.get_window_matches(1, 1)
        assert {m["round_no"] for m in matches} == {1}
        assert {m["competition"] for m in matches} == {"顶级联赛"}

        # 天气/赛果/统计命令用同一文字轮次 → 解析到同一号
        comp, round_no = await env.fixture_service.resolve_round_arg("顶级")
        assert (comp, round_no) == ("顶级联赛", 1)
        await env.fixture_service.set_weather(round_no, "利物浦", "晴", comp)
        await env.fixture_service.record_results(round_no, "利物浦 胜", comp)
        rs = await env.fixture_service.round_stats(round_no, comp)
        assert rs["totals"]["attendance"] > 0

        # 不同名（次级）→ 各赛事从 1 起（先导入完成登记；命令侧只读）
        r_sub = await env.fixture_service.import_fixtures("次级 巴塞罗那 利物浦")
        assert r_sub["imported"] == 1, r_sub
        comp2, no2 = await env.fixture_service.resolve_round_arg("次级")
        assert (comp2, no2) == ("次级联赛", 1), (comp2, no2)

        # 未登记的命名轮次（命令侧只读）应报错而非自动建号
        try:
            await env.fixture_service.resolve_round_arg("冠军")
            assert False, "未登记的命名轮次应报错"
        except ValueError as e:
            assert "尚未导入" in str(e)

        # 与显式数字文本混排：「顶级9」是独立轮次文本，按（赛事,首现序）编号（不再按数字=9）
        r2 = await env.fixture_service.import_fixtures("顶级9 利物浦 巴塞罗那\n")
        assert r2["imported"] == 1, r2
        comp9, no9 = await env.fixture_service.resolve_round_arg("顶级9")
        assert (comp9, no9) == ("顶级联赛", 2), (comp9, no9)
        assert await env.dao.get_named_round(1, "顶级联赛", "顶级") == 1
        assert await env.dao.get_named_round(1, "顶级联赛", "顶级9") == 2
    finally:
        await env.teardown()


async def test_result_derived_from_score_text():
    """文本赛果支持直接写比分：由比分自动判主队胜平负并保留比分原文。"""
    env = await TestEnv().setup()
    try:
        await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n1 纽卡斯尔联 勒沃库森\n1 巴塞罗那 利物浦\n"
        )
        r = await env.fixture_service.record_results(
            1, "利物浦 2-1\n纽卡斯尔联 0-0PK2-4\n巴塞罗那 1-1\n"
        )
        assert r["count"] == 3, r
        by = {m["home_team"]: m for m in await env.dao.get_round_matches(1, 1, 1)}
        assert by["利物浦"]["result"] == "W" and by["利物浦"]["score"] == "2-1"
        assert by["纽卡斯尔联"]["result"] == "L" and by["纽卡斯尔联"]["score"] == "0-0PK2-4"
        assert by["巴塞罗那"]["result"] == "D" and by["巴塞罗那"]["score"] == "1-1"
        # 显式胜平负仍可用
        await env.fixture_service.import_fixtures("2 利物浦 巴塞罗那")
        r2 = await env.fixture_service.record_results(2, "利物浦 胜 3-0")
        assert r2["count"] == 1
        m = (await env.dao.get_round_matches(1, 1, 2))[0]
        assert m["result"] == "W" and m["score"] == "3-0"
    finally:
        await env.teardown()


async def test_same_round_multiple_home_matches():
    """同轮同赛事同主队不同客队的多场照常导入（不再以主队判重）；完全相同行仍跳过。"""
    env = await TestEnv().setup()
    try:
        r = await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n1 利物浦 勒沃库森\n"
        )
        assert r["imported"] == 2, r
        matches = await env.dao.get_round_matches(1, 1, 1)
        assert len(matches) == 2
        aways = {m["away_team"] for m in matches}
        assert aways == {"巴塞罗那", "勒沃库森"}
        # 完全相同的行重复导入仍跳过并提示
        r2 = await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那")
        assert r2["imported"] == 0
        assert any("重复" in e for e in r2["errors"]), r2["errors"]
    finally:
        await env.teardown()


async def test_record_cancelled_match():
    """录入「主队 取消」：result=C、无观众/无收益、不记流水、不能重复录入。"""
    from astrbot_plugin_whleague_revenue_system.services.fixture_service import FixtureError

    env = await TestEnv().setup()
    try:
        await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n1 纽卡斯尔联 勒沃库森"
        )
        r = await env.fixture_service.record_results(1, "利物浦 取消\n纽卡斯尔联 胜")
        assert r["count"] == 2, r
        by = {m["home_team"]: m for m in await env.dao.get_round_matches(1, 1, 1)}
        m_c = by["利物浦"]
        assert m_c["result"] == "C" and m_c["attendance"] is None
        assert m_c["ticket_revenue"] == 0 and m_c["commercial"] == 0 and m_c["broadcast"] == 0
        m_w = by["纽卡斯尔联"]
        assert m_w["result"] == "W" and m_w["attendance"] is not None
        assert await env.dao.list_transactions("利物浦", 1, 1) == []
        # 取消场次也视为已录赛果，不能重复录入
        try:
            await env.fixture_service.record_results(1, "利物浦 取消")
            assert False, "取消场次应拒绝重复录入"
        except FixtureError as e:
            assert "已录入" in str(e)
    finally:
        await env.teardown()


async def test_first_two_rounds_neutral_form():
    """历史不足 3 场时录入一律按中性 4 分（v2.5.1 规则），第 4 场起恢复实际积分。"""
    env = await TestEnv().setup()
    try:
        await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n2 利物浦 巴塞罗那\n"
            "3 利物浦 巴塞罗那\n4 利物浦 巴塞罗那\n"
        )
        r1 = await env.fixture_service.record_results(1, "利物浦 胜")
        assert r1["results"][0]["form_pts"] == 4, r1
        r2 = await env.fixture_service.record_results(2, "利物浦 胜")
        assert r2["results"][0]["form_pts"] == 4, r2
        # 第 3 场录入时历史仍只有 2 场，实际积分已是 6 也强制中性（旧规则会返回 6）
        r3 = await env.fixture_service.record_results(3, "利物浦 胜")
        assert r3["results"][0]["form_pts"] == 4, r3
        # 第 4 场起近 3 场凑满，恢复真实积分（W,W,W → 9）
        r4 = await env.fixture_service.record_results(4, "利物浦 平")
        assert r4["results"][0]["form_pts"] == 9, r4
    finally:
        await env.teardown()


async def test_cancelled_excluded_from_form_points():
    """状态积分忽略取消场次：不占「近 3 场」窗口，往前补足 3 场已赛。"""
    env = await TestEnv().setup()
    try:
        await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n2 利物浦 巴塞罗那\n3 利物浦 巴塞罗那\n"
            "4 利物浦 巴塞罗那\n5 利物浦 巴塞罗那\n"
        )
        await env.fixture_service.record_results(1, "利物浦 胜")
        await env.fixture_service.record_results(2, "利物浦 胜")
        await env.fixture_service.record_results(3, "利物浦 取消")
        await env.fixture_service.record_results(4, "利物浦 胜")
        # 第 5 轮录入前：最近 3 场非取消 = r4/r2/r1 全胜 → Pts 9（补足 3 场）
        r5 = await env.fixture_service.record_results(5, "利物浦 平")
        assert r5["count"] == 1
        assert r5["results"][0]["form_pts"] == 9, r5
    finally:
        await env.teardown()