"""主场统一补差测试（v2.8.0）：旧库模拟 → 预览方向/落库账务/死忠重定基/幂等。

战绩成分：scan 无法凭库内数值区分「旧规则录入」与「新规则录入」
（n∈{1,2} 的早期轮次两代系数本就不同）——因此全量回归重点在方向与
账务正确性；伪阳性的排除依赖部署时序（部署后立即预览/确认），
由 handler 提示文案保证。收入跟随实际入库上座（容量钳死时零增量）。

死忠成分：不做 0.95^k 近似回补，改为按饱和阶梯目标逐队一次性重定基
（round(diehard_target(cfg, influence))，与现值差 ≥1 才入清单）。
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services import formula  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.backfill_service import (  # noqa: E402
    LEGACY_MARKER_KEY,
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
    """镜像 services/backfill_service._scale 的算术（含按现容量钳制）：
    上座先按系数比缩放并钳容量，收入再跟随实际入库上座。"""
    cap = (await env.dao.get_stadium(home))["capacity"]
    ratio = formula.form_coef(env.cfg, NEUTRAL) / formula.form_coef(env.cfg, pts_old)
    att_new = min(int(round(corr["att"] * ratio)), cap)
    rev_ratio = att_new / corr["att"] if corr["att"] > 0 else 0.0
    # 与 services/backfill_service._scale 同构：先舍入总额，再由总额差取差额
    t_new = round(corr["t"] * rev_ratio, 4)
    c_new = round(corr["c"] * rev_ratio, 4)
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
    余额重算差额 = 各场票房/商业补差和；死忠按阶梯目标一次性重定基
   （已对齐的队伍不动）；完成后幂等拒绝二次执行。

    窗口语义：第 1 轮在窗口 1 结算（A 仅 1 场负），第 2~4 轮在推进后的
    窗口 2 录入/伪造。"""
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

        # 死忠重定基：建场死忠 = 默认影响力 90 的阶梯目标，天然对齐不入清单；
        # 人为偏离的两队（A/B）应出现在预览里，重定基方向为回落到目标
        fans_before_map = {s["team_name"]: s["fans_diehards"]
                           for s in await env.dao.list_stadiums()}
        infl_a = (await env.dao.get_stadium("利物浦"))["influence"]
        target = int(round(formula.diehard_target(env.cfg, infl_a)))
        await env.dao.update_fans("利物浦", 4000.0)
        await env.dao.update_fans("巴塞罗那", 5000.0)

        scan = await svc.scan()
        assert scan["done"] is False and scan["season"] == 1
        assert scan["form_done"] is False
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
        # 死忠重定基清单：仅人为偏离的 A/B
        fans_by_team = {f["team"]: f for f in scan["fans"]}
        assert set(fans_by_team) == {"利物浦", "巴塞罗那"}, scan["fans"]
        # 死忠清单三个数值字段必须为 int（formatter 用 :+d 格式码，float 会崩）
        for f in fans_by_team.values():
            assert all(isinstance(f[k], int) for k in ("before", "after", "delta")), f
        assert fans_by_team["利物浦"]["before"] == 4000
        assert fans_by_team["利物浦"]["after"] == target
        assert fans_by_team["巴塞罗那"]["after"] == target

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

        # 死忠重定基：偏离队改为阶梯目标，其余队伍原值不动
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
        assert scan2["marker"]["fans_teams"] == 2, scan2["marker"]
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
        assert scan["fans"] == [], scan["fans"]  # 新库建场即在阶梯目标上

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


async def test_backfill_legacy_marker_skips_form():
    """兼容：旧版战绩补差标记存在时，战绩成分整体跳过（比赛行/余额不动），
    死忠重定基照常执行；标记记录 form_skipped。"""
    env = await TestEnv().setup()
    try:
        svc = BackfillService(env.db, env.dao, env.cfg)
        imp = await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n1 巴塞罗那 纽卡斯尔联\n")
        assert imp["imported"] == 2, imp
        rec = await env.fixture_service.record_results(1, "利物浦 负\n")
        assert rec["count"] == 1
        await env.fixture_service.advance_window("tester")
        await env.fixture_service.import_fixtures("2 利物浦 巴塞罗那\n")
        # n=1 且旧分≠4：若无旧标记，scan 必然命中该场
        corr = await _corrupt_legacy(env, await _row(env, 2, "利物浦"), 6000, "L")
        await env.dao.set_config(LEGACY_MARKER_KEY, json.dumps({"executed_season": 1}))
        await env.dao.update_fans("利物浦", 4000.0)

        scan = await svc.scan()
        assert scan["form_done"] is True
        assert scan["affected"] == [], scan["affected"]
        assert [f["team"] for f in scan["fans"]] == ["利物浦"], scan["fans"]

        await env.dao.recompute_balance("利物浦")
        balance_before = (await env.dao.get_balance("利物浦"))["balance"]

        applied = await svc.apply()
        assert applied["applied"] is True

        m = await _row(env, 2, "利物浦")
        assert m["attendance"] == corr["att"], m["attendance"]  # 比赛行未动
        assert m["result"] == "L"
        balance_now = (await env.dao.get_balance("利物浦"))["balance"]
        assert abs(balance_now - balance_before) < 1e-6, balance_now
        target = int(round(formula.diehard_target(env.cfg, 90.0)))
        fans = int((await env.dao.get_stadium("利物浦"))["fans_diehards"])
        assert fans == target, fans  # 重定基照常

        marker = json.loads(await env.dao.get_config(MARKER_KEY))
        assert marker["matches"] == 0 and marker["form_skipped"] is True, marker
        try:
            await svc.apply()
            raise AssertionError("重复执行应被拒绝")
        except BackfillError:
            pass
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
