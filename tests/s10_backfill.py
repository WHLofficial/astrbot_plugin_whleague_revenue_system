"""战绩系数补差测试：旧库模拟 → 预览方向/落库账务/幂等，以及 n=0 数据零受影响护栏。

scan 无法凭库内数值区分「旧规则录入」与「新规则录入」（n∈{1,2} 的早期轮次
两代系数本就不同）——因此全量回归重点在方向与账务正确性；伪阳性的排除
依赖部署时序（部署后立即预览/确认），由 handler 提示文案保证。
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services import formula  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.backfill_service import (  # noqa: E402
    MARKER_KEY,
    BackfillError,
    BackfillService,
)

NEUTRAL = formula.NEUTRAL_FORM_PTS


async def _row(env, round_no: int, home: str):
    """按赛季+轮次+主队定位比赛（跨窗口查找：推进后导入的轮次落在窗口 2）。"""
    found = []
    for window in (1, 2):
        found += [x for x in await env.dao.get_round_matches(1, window, round_no)
                  if x["home_team"] == home]
    assert len(found) == 1, (round_no, home, found)
    return found[0]


async def _corrupt_legacy(env, m, att_old: int, letter: str) -> dict:
    """按旧规则语义覆盖某场为已录结果（n∈{1,2} 时旧代码按实际积分查表，
    数值已揉进上座）。广播收入用 0.9 占位，验证补差不动转播；
    票/商流水照旧规则全额入账，模拟升级前的既有账目。"""
    fac = await env.dao.get_facilities(m["home_team"])
    t_old, c_old, _ = formula.match_revenues(
        env.cfg, att_old, fac.get("commercial", 0), fac.get("broadcast", 0))
    await env.dao.set_match_result(m["id"], letter, att_old, t_old, c_old, 0.9)
    note = f"第{m['round_no']}轮 主{m['home_team']} vs 客{m['away_team']}"
    for kind, amount in (("ticket", t_old), ("commercial", c_old)):
        await env.dao.add_transaction(
            m["home_team"], m["season_number"], m["window_seq"], kind, amount,
            note=note, round_no=m["round_no"])
    return {"id": m["id"], "att": att_old, "t": t_old, "c": c_old,
            "letter": letter}


async def _scale_expected(env, home: str, pts_old: int, corr: dict) -> dict:
    """镜像 services/backfill_service._scale 的算术（含按现容量钳制）。"""
    cap = (await env.dao.get_stadium(home))["capacity"]
    ratio = formula.form_coef(env.cfg, NEUTRAL) / formula.form_coef(env.cfg, pts_old)
    att_new = min(int(round(corr["att"] * ratio)), cap)
    # 与 services/backfill_service._scale 同构：先舍入总额，再由总额差取差额
    t_new = round(corr["t"] * ratio, 4)
    c_new = round(corr["c"] * ratio, 4)
    return {
        "att_new": att_new,
        "d_att": att_new - corr["att"],
        "t_new": t_new,
        "c_new": c_new,
        "d_ticket": round(t_new - corr["t"], 4),
        "d_commercial": round(c_new - corr["c"], 4),
    }


_RESULT_COLS = ("result", "score", "attendance", "ticket_revenue",
                "commercial", "broadcast")


async def test_backfill_scan_and_apply():
    """旧库模拟：系数偏低的场次上调、两连胜 1.10 的场次下调；
    余额重算差额 = 各场票房/商业补差和；死忠只回补结算时点凑不满
    3 场且积分≤1 的队伍；完成后幂等拒绝二次执行。

    窗口语义：第 1 轮在窗口 1 结算（A 仅 1 场负 → 演化曾多扣 −5%），
    第 2~4 轮在推进后的窗口 2 录入/伪造。"""
    env = await TestEnv().setup()
    try:
        svc = BackfillService(env.db, env.dao, env.cfg)

        # ── 窗口 1：只导并真录三个首轮主场 ──
        imp1 = await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n1 巴塞罗那 纽卡斯尔联\n1 勒沃库森 纽卡斯尔联\n")
        assert imp1["imported"] == 3, imp1
        rec1 = await env.fixture_service.record_results(
            1, "利物浦 负\n巴塞罗那 胜\n勒沃库森 胜\n")
        assert rec1["count"] == 3
        await env.fixture_service.advance_window("tester")

        # ── 窗口 2：导入其余轮次 ──
        imp2 = await env.fixture_service.import_fixtures(
            "2 利物浦 巴塞罗那\n2 巴塞罗那 纽卡斯尔联\n2 勒沃库森 纽卡斯尔联\n"
            "3 利物浦 巴塞罗那\n3 勒沃库森 纽卡斯尔联\n"
            "4 利物浦 巴塞罗那\n")
        assert imp2["imported"] == 6, imp2

        # 按旧规则伪造早期轮次：
        #   A 第2/3轮（前置 [L]/[L,L] → 旧分 0 → coef 0.70 上调）
        #   B 第2轮（前置 [W] → 旧分 3 → 0.94 上调）
        #   C 第2轮（前置 [W] → 0.94 上调）；C 第3轮（前置 [W,W] → 6 分
        #     → 1.10 下调，验证连胜偏高方向的回缩）
        corr = {
            ("利物浦", 2): await _corrupt_legacy(env, await _row(env, 2, "利物浦"), 6000, "L"),
            ("利物浦", 3): await _corrupt_legacy(env, await _row(env, 3, "利物浦"), 6500, "L"),
            ("巴塞罗那", 2): await _corrupt_legacy(env, await _row(env, 2, "巴塞罗那"), 7000, "W"),
            ("勒沃库森", 2): await _corrupt_legacy(env, await _row(env, 2, "勒沃库森"), 7200, "W"),
            ("勒沃库森", 3): await _corrupt_legacy(env, await _row(env, 3, "勒沃库森"), 9500, "W"),
        }
        # 不应被触碰的锚点：窗口 1 的三场真录 + A 第4轮（n≥3 真录）
        anchors = {t: dict(await _row(env, 1, t))
                   for t in ("利物浦", "巴塞罗那", "勒沃库森")}
        rec4 = await env.fixture_service.record_results(4, "利物浦 胜 2-1")
        assert rec4["count"] == 1
        anchor_a4 = dict(await _row(env, 4, "利物浦"))

        legacy_pts = {
            ("利物浦", 2): 0, ("利物浦", 3): 0,
            ("巴塞罗那", 2): 3,
            ("勒沃库森", 2): 3, ("勒沃库森", 3): 6,
        }
        expect = {k: await _scale_expected(env, k[0], v, corr[k])
                  for k, v in legacy_pts.items()}

        # 死忠 k（忠实复现旧规则伤害）：窗口 1 结算时，A 仅 1 场负 →
        # 旧代码近 3 场 pts=0 ≤1 → ×0.95；纽卡斯尔联零主场 → 旧代码
        # pts 同为 0 → 也被扣；B/C 有胜绩不受影响
        fans_before_map = {s["team_name"]: s["fans_diehards"]
                           for s in await env.dao.list_stadiums()}
        await env.dao.update_fans("利物浦", 4000.0)
        await env.dao.add_window_summary(1, 1)

        scan = await svc.scan()
        assert scan["done"] is False and scan["season"] == 1
        by_key = {(a["home"], a["round_no"]): a for a in scan["affected"]}
        assert set(by_key) == set(corr), sorted(by_key)
        for key, exp in expect.items():
            a = by_key[key]
            assert a["att_new"] == exp["att_new"], (key, a["att_new"])
            assert a["d_ticket"] == exp["d_ticket"], (key, a["d_ticket"])
            assert a["d_commercial"] == exp["d_commercial"], (key, a["d_commercial"])
        upscale_keys = [k for k in corr if k != ("勒沃库森", 3)]
        assert all(by_key[k]["d_att"] > 0 for k in upscale_keys), \
            "系数偏低（不足三分查表）的场次应上调"
        assert by_key[("勒沃库森", 3)]["d_att"] < 0, \
            "两连胜 1.10 场次应下调"
        # 死忠 k：A（1 场负）与纽卡斯尔联（零历史）在窗口 1 结算时均被旧规则多扣 −5%
        fans_by_team = {f["team"]: f for f in scan["fans"]}
        assert {t: f["k"] for t, f in fans_by_team.items()} \
            == {"利物浦": 1, "纽卡斯尔联": 1}
        f_a = fans_by_team["利物浦"]
        assert f_a["before"] == 4000 and f_a["after"] == int(round(4000 / 0.95))
        nkc_before = fans_before_map["纽卡斯尔联"]
        assert fans_by_team["纽卡斯尔联"]["after"] == int(round(nkc_before / 0.95))

        touched = ["利物浦", "巴塞罗那", "勒沃库森"]
        # 与部署时点等价的均衡态：先按既有流水归一（生产中该口径由
        # 窗口结算维护），补差只应带来各场 d_ticket/d_commercial 的增量
        balance_before = {}
        for team in touched:
            await env.dao.recompute_balance(team)
            balance_before[team] = (await env.dao.get_balance(team))["balance"]

        applied = await svc.apply()
        assert applied["applied"] is True

        for key, exp in expect.items():
            home, rnd = key
            m = await _row(env, rnd, home)
            assert m["attendance"] == exp["att_new"], (key, m["attendance"])
            assert abs(m["ticket_revenue"] - exp["t_new"]) < 1e-9, \
                (key, m["ticket_revenue"], exp["t_new"])
            assert abs(m["commercial"] - exp["c_new"]) < 1e-9, \
                (key, m["commercial"])
            assert m["broadcast"] == 0.9, (key, m["broadcast"])  # 转播收入不动
            assert m["result"] == corr[key]["letter"], (key, m["result"])
        for team, snap in anchors.items():
            now = await _row(env, snap["round_no"], team)
            for col in _RESULT_COLS:
                assert now[col] == snap[col], (team, col)
        now_a4 = await _row(env, anchor_a4["round_no"], "利物浦")
        for col in _RESULT_COLS:
            assert now_a4[col] == anchor_a4[col], col

        # 余额重算后差额 = 该队各场票房/商业补差之和
        for team in touched:
            d_sum = sum(expect[k]["d_ticket"] + expect[k]["d_commercial"]
                        for k in expect if k[0] == team)
            now = (await env.dao.get_balance(team))["balance"]
            assert abs(now - (balance_before[team] + d_sum)) < 1e-6, team
        # 补差流水带标记注释；条数 = 各队非零差额数（零额不落流水）
        expected_fixes: dict[str, int] = {}
        for (home, _), exp in expect.items():
            n = int(exp["d_ticket"] != 0) + int(exp["d_commercial"] != 0)
            expected_fixes[home] = expected_fixes.get(home, 0) + n
        for team, count in expected_fixes.items():
            txs = await env.dao.list_transactions(team, season=1, limit=200)
            fixes = [t for t in txs if "战绩系数补差" in t["note"]]
            assert len(fixes) == count, [(t["kind"], t["amount"]) for t in fixes]

        # 死忠回补：受影响两队按 0.95^k 回补，其余队伍原值不动
        for s in await env.dao.list_stadiums():
            t = s["team_name"]
            if t in fans_by_team:
                assert int(s["fans_diehards"]) == fans_by_team[t]["after"], t
            else:
                assert s["fans_diehards"] == fans_before_map[t], t

        # 幂等：标记落库后拒绝重复执行，scan 报告 done
        assert await env.dao.get_config(MARKER_KEY)
        try:
            await svc.apply()
            raise AssertionError("重复执行应被拒绝")
        except BackfillError:
            pass
        scan2 = await svc.scan()
        assert scan2["done"] is True
        assert scan2["marker"]["matches"] == len(corr), scan2["marker"]
    finally:
        await env.teardown()


async def test_backfill_zero_when_all_rows_have_no_history():
    """护栏：所有已录场次的历史都为 0 时（首批赛果，新旧规则同为中性 4 分）
    scan 应为空；仍可写入完成标记并拒绝二次执行。"""
    env = await TestEnv().setup()
    try:
        svc = BackfillService(env.db, env.dao, env.cfg)
        imp = await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n1 巴塞罗那 纽卡斯尔联\n"
        )
        assert imp["imported"] == 2, imp
        rec = await env.fixture_service.record_results(
            1, "利物浦 负\n巴塞罗那 胜\n")
        assert rec["count"] == 2

        scan = await svc.scan()
        assert scan["done"] is False
        assert scan["affected"] == [], scan["affected"]
        assert scan["fans"] == [], scan["fans"]

        applied = await svc.apply()
        assert applied["applied"] is True
        assert applied["affected"] == [] and applied["fans"] == []
        try:
            await svc.apply()
            raise AssertionError("重复执行应被拒绝")
        except BackfillError:
            pass
        assert (await svc.scan())["done"] is True
    finally:
        await env.teardown()


async def test_backfill_requires_league_state():
    env = await TestEnv().setup()
    try:
        svc = BackfillService(env.db, env.dao, env.cfg)
        await env.db.execute("DELETE FROM league_state")
        try:
            await svc.scan()
            raise AssertionError("未初始化联赛应报错")
        except BackfillError as e:
            assert "初始化" in str(e)
    finally:
        await env.teardown()
