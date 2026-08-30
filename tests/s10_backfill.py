"""主场统一补差测试（v2.8.0）：旧库模拟 → 预览方向/落库账务/死忠重定基/幂等。

战绩成分：scan 无法凭库内数值区分「旧规则录入」与「新规则录入」
（n∈{1,2} 的早期轮次两代系数本就不同）——因此全量回归重点在方向与
账务正确性；伪阳性的排除依赖部署时序（部署后立即预览/确认），
由 handler 提示文案保证。收入跟随实际入库上座（容量钳死时零增量）。

死忠成分：不做 0.95^k 近似回补，改为按饱和阶梯目标逐队一次性重定基
（round(diehard_target(cfg, influence))，与现值差 ≥1 才入清单）。

满座微降成分（v2.9.2）：恰好=现容量的已录非取消主场（任意赛季）回修为
容量×random(0.985, 0.999)；战绩修正后恰好钳到容量的场次一并微降，
修正后低于容量的前满座视为不再满座、不再处理。

成分级幂等（v2.9.3）：战绩成分任一标记存在即跳过（强制也不放行）；
已执行过但仍有待处理项（新满座/死忠偏离）时 apply 放行只做待处理项；
强制模式重抽所有处于满座区间的场次（含已微降过的）。
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

        # 幂等：标记落库后（无待处理项）拒绝重复执行，scan 报告 done
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
        assert scan2["marker"]["forced"] is False, scan2["marker"]
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
        assert scan["sellouts"] == [], scan["sellouts"]
        assert scan["fans"] == [], scan["fans"]  # 新库建场即在阶梯目标上

        applied = await svc.apply()
        assert applied["applied"] is True
        assert applied["affected"] == [] and applied["fans"] == []
        assert applied["sellouts"] == [], applied["sellouts"]
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
        assert scan["sellouts"] == [], scan["sellouts"]  # 满座微降不受旧标记影响，照常扫描
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
        assert marker["matches"] == 0 and marker["sellouts"] == 0 \
            and marker["form_skipped"] is True, marker
        try:
            await svc.apply()
            raise AssertionError("重复执行应被拒绝")
        except BackfillError:
            pass
    finally:
        await env.teardown()


async def test_backfill_sellout_recap():
    """满座微降（v2.9.2）：非战绩影响的历史满座（任意赛季=现容量）与战绩
    修正后恰好钳到容量的场次都回修为容量×random(0.985, 0.999)；战绩修正后
    低于容量的前满座视为不再满座、不再微降。票/商按最终上座等比缩放、
    转播不动；余额 = 落库前 + 战绩差额 + 满座差额。"""
    env = await TestEnv().setup()
    try:
        svc = BackfillService(env.db, env.dao, env.cfg)

        # ── 窗口 1：真录 利物浦 R1（负，上座远低于容量）──
        imp1 = await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n1 巴塞罗那 纽卡斯尔联\n")
        assert imp1["imported"] == 2, imp1
        rec1 = await env.fixture_service.record_results(1, "利物浦 负\n")
        assert rec1["count"] == 1
        cap = (await env.dao.get_stadium("利物浦"))["capacity"]
        await env.dao.upsert_facility("巴塞罗那", "commercial", 2)
        anchor_l1 = dict(await _row(env, 1, "利物浦"))
        await env.fixture_service.advance_window("tester")

        # ── 窗口 2：导入 R2/R3，按旧规则伪造 ──
        imp2 = await env.fixture_service.import_fixtures(
            "2 利物浦 巴塞罗那\n2 巴塞罗那 纽卡斯尔联\n3 巴塞罗那 勒沃库森\n")
        assert imp2["imported"] == 3, imp2
        # 巴萨 R1：n=0 旧分中性（不受战绩影响），但上座=现容量 → 满座微降清单
        sellout_plain = await _corrupt_legacy(env, await _row(env, 1, "巴塞罗那"), cap, "W")
        # 利物浦 R2：前置 [L] → 旧分 0（0.70 上调）→ 缩放后钳到容量 → apply 时一并微降
        corr_clamp = await _corrupt_legacy(env, await _row(env, 2, "利物浦"), cap - 1000, "L")
        # 巴萨 R2：前置 [W] → 旧分 3（0.94 上调），普通战绩补差
        corr_b2 = await _corrupt_legacy(env, await _row(env, 2, "巴塞罗那"), 4000, "W")
        # 巴萨 R3：前置 [W,W] → 旧分 6（1.10 下调），录入为满座 → 修正后低于容量，不再微降
        corr_b3 = await _corrupt_legacy(env, await _row(env, 3, "巴塞罗那"), cap, "W")

        legacy_pts = {("利物浦", 2): 0, ("巴塞罗那", 2): 3, ("巴塞罗那", 3): 6}
        corr_map = {("利物浦", 2): corr_clamp, ("巴塞罗那", 2): corr_b2,
                    ("巴塞罗那", 3): corr_b3}
        expect = {k: await _scale_expected(env, k[0], v, corr_map[k])
                  for k, v in legacy_pts.items()}
        assert expect[("利物浦", 2)]["att_new"] == cap, expect[("利物浦", 2)]
        assert expect[("巴塞罗那", 3)]["att_new"] < cap, expect[("巴塞罗那", 3)]

        for team in ("利物浦", "巴塞罗那"):
            await env.dao.recompute_balance(team)
        balance_before = {t: (await env.dao.get_balance(t))["balance"]
                          for t in ("利物浦", "巴塞罗那")}

        scan = await svc.scan()
        assert scan["form_done"] is False
        by_key = {(a["home"], a["round_no"]): a for a in scan["affected"]}
        assert set(by_key) == set(legacy_pts), sorted(by_key)
        sells = {(s["home"], s["round_no"]) for s in scan["sellouts"]}
        assert sells == {("巴塞罗那", 1), ("巴塞罗那", 3)}, scan["sellouts"]

        applied = await svc.apply()
        assert applied["applied"] is True

        # 满座微降后上座区间（12000 座 → 11820~11988），票/商按最终上座精确回推
        lo = int(cap * formula.SELL_OUT_FILL[0])
        hi = int(cap * formula.SELL_OUT_FILL[1])
        m_plain = await _row(env, 1, "巴塞罗那")
        assert lo <= m_plain["attendance"] < cap, m_plain["attendance"]
        ratio = m_plain["attendance"] / cap
        d_plain = (round(sellout_plain["t"] * ratio, 4) - sellout_plain["t"],
                   round(sellout_plain["c"] * ratio, 4) - sellout_plain["c"])
        m_clamp = await _row(env, 2, "利物浦")
        assert lo <= m_clamp["attendance"] < cap, m_clamp["attendance"]
        ratio = m_clamp["attendance"] / cap
        exp_clamp = expect[("利物浦", 2)]
        d_clamp = (round(exp_clamp["t_new"] * ratio, 4) - exp_clamp["t_new"],
                   round(exp_clamp["c_new"] * ratio, 4) - exp_clamp["c_new"])
        # 巴萨 R3：修正后低于容量，不再微降（精确等于战绩镜像值）
        m_b3 = await _row(env, 3, "巴塞罗那")
        exp_b3 = expect[("巴塞罗那", 3)]
        assert m_b3["attendance"] == exp_b3["att_new"], m_b3["attendance"]
        assert abs(m_b3["ticket_revenue"] - exp_b3["t_new"]) < 1e-9, m_b3
        # 转播收入全部不动
        for m in (m_plain, m_clamp, m_b3):
            assert m["broadcast"] == 0.9, m["broadcast"]
        # 非满座锚点 利物浦 R1 完全不动
        now_l1 = await _row(env, 1, "利物浦")
        for col in _RESULT_COLS:
            assert now_l1[col] == anchor_l1[col], col

        # 余额 = 落库前 + 战绩差额 + 满座差额
        for team, dsell in (("利物浦", d_clamp), ("巴塞罗那", d_plain)):
            d_form = sum(expect[k]["d_ticket"] + expect[k]["d_commercial"]
                         for k in expect if k[0] == team)
            now = (await env.dao.get_balance(team))["balance"]
            assert abs(now - (balance_before[team] + d_form + dsell[0] + dsell[1])) < 1e-6, team

        # 满座微降流水：巴萨 R1 票+商（商等级 2）、利物浦 R2 仅票（商等级 0）
        txs_a = await env.dao.list_transactions("巴塞罗那", season=1, limit=200)
        fixes_a = [t for t in txs_a if "满座微降" in t["note"]]
        assert len(fixes_a) == 2, [(t["kind"], t["amount"]) for t in fixes_a]
        assert {t["kind"] for t in fixes_a} == {"ticket", "commercial"}
        assert all(t["window_seq"] == 1 and t["round_no"] == 1 for t in fixes_a)
        txs_l = await env.dao.list_transactions("利物浦", season=1, limit=200)
        fixes_l = [t for t in txs_l if "满座微降" in t["note"]]
        assert len(fixes_l) == 1 and fixes_l[0]["kind"] == "ticket", fixes_l
        assert fixes_l[0]["window_seq"] == 2 and fixes_l[0]["round_no"] == 2
        # 战绩系数补差流水不受满座成分影响
        assert len([t for t in txs_a if "战绩系数补差" in t["note"]]) == 4, txs_a
        assert len([t for t in txs_l if "战绩系数补差" in t["note"]]) == 1, txs_l

        # 幂等：标记记录满座微降场数（巴萨 R3 跳过、利物浦 R2 补入 → 2 场）
        marker = json.loads(await env.dao.get_config(MARKER_KEY))
        assert marker["sellouts"] == 2 and marker["forced"] is False, marker
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


async def test_backfill_unified_marker_pending_sellout():
    """v2.9.1→v2.9.2 迁移：统一标记已存在（payload 无满座成分），普通模式
    不得整体拦截——战绩成分跳过、满座成分照常执行；做完后无待处理项才拒绝。"""
    env = await TestEnv().setup()
    try:
        svc = BackfillService(env.db, env.dao, env.cfg)
        imp = await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n1 巴塞罗那 纽卡斯尔联\n")
        assert imp["imported"] == 2, imp
        rec = await env.fixture_service.record_results(1, "利物浦 胜\n")
        assert rec["count"] == 1
        cap = (await env.dao.get_stadium("利物浦"))["capacity"]
        # 利物浦 R1 上座=现容量（旧时期满座入账）
        corr = await _corrupt_legacy(env, await _row(env, 1, "利物浦"), cap, "W")
        # 手动埋 v2.9.1 式统一标记（payload 无 sellouts 键）
        await env.dao.set_config(MARKER_KEY, json.dumps({
            "executed_season": 1, "matches": 3, "fans_teams": 2,
            "form_skipped": False}))

        scan = await svc.scan()
        assert scan["done"] is True and scan["form_done"] is True
        assert scan["affected"] == [], scan["affected"]
        assert [(s["home"], s["round_no"]) for s in scan["sellouts"]] == \
            [("利物浦", 1)], scan["sellouts"]
        assert scan["fans"] == [], scan["fans"]  # 默认影响力建场即在阶梯目标

        await env.dao.recompute_balance("利物浦")
        balance_before = (await env.dao.get_balance("利物浦"))["balance"]

        applied = await svc.apply()
        assert applied["applied"] is True

        m = await _row(env, 1, "利物浦")
        lo, hi = formula.SELL_OUT_FILL
        assert int(cap * lo) <= m["attendance"] < cap, m["attendance"]
        ratio = m["attendance"] / cap
        assert abs(m["ticket_revenue"] - round(corr["t"] * ratio, 4)) < 1e-9, m
        assert m["broadcast"] == 0.9, m["broadcast"]  # 转播不动
        d = ((round(corr["t"] * ratio, 4) - corr["t"])
             + (round(corr["c"] * ratio, 4) - corr["c"]))
        now = (await env.dao.get_balance("利物浦"))["balance"]
        assert abs(now - (balance_before + d)) < 1e-6, now
        marker = json.loads(await env.dao.get_config(MARKER_KEY))
        assert marker["sellouts"] == 1 and marker["form_skipped"] is True, marker

        # 做完后无待处理项：普通 apply 拒绝
        try:
            await svc.apply()
            raise AssertionError("无待处理项的重复执行应被拒绝")
        except BackfillError:
            pass
    finally:
        await env.teardown()


async def test_backfill_force_rerun():
    """强制重算（v2.9.3）：首次补差后普通模式无待处理项被拒；强制模式重抽
    所有处于满座区间的场次（含已微降过的），票/商按当前值等比缩放；战绩
    成分永不参与——新规则 n∈{1,2} 的伪阳性场次不被触碰；标记记录 forced。"""
    env = await TestEnv().setup()
    try:
        svc = BackfillService(env.db, env.dao, env.cfg)
        imp = await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n1 巴塞罗那 纽卡斯尔联\n")
        assert imp["imported"] == 2, imp
        rec = await env.fixture_service.record_results(1, "利物浦 胜\n")
        assert rec["count"] == 1
        cap = (await env.dao.get_stadium("利物浦"))["capacity"]
        await _corrupt_legacy(env, await _row(env, 1, "利物浦"), cap, "W")

        applied = await svc.apply()
        assert applied["applied"] is True
        m1 = await _row(env, 1, "利物浦")
        lo, hi = formula.SELL_OUT_FILL
        assert int(cap * lo) <= m1["attendance"] < cap, m1["attendance"]

        # 伪阳性护栏：首次补差后按新规则录入一场 n∈{1,2} 场次（低上座）
        await env.fixture_service.advance_window("tester")
        await env.fixture_service.import_fixtures("2 利物浦 巴塞罗那\n")
        rec2 = await env.fixture_service.record_results(2, "利物浦 负 3000\n")
        assert rec2["count"] == 1
        m2_before = dict(await _row(env, 2, "利物浦"))

        # 普通模式：满座已处理（落区间）、战绩被标记跳过 → 无待处理项，拒绝
        scan = await svc.scan()
        assert scan["done"] is True and scan["affected"] == []
        assert scan["sellouts"] == [], scan["sellouts"]
        try:
            await svc.apply()
            raise AssertionError("无待处理项应被拒绝")
        except BackfillError:
            pass

        # 强制模式：命中已微降场次，战绩清单仍为空
        fscan = await svc.scan(force=True)
        assert fscan["force"] is True
        assert fscan["affected"] == [], fscan["affected"]
        assert [(s["home"], s["round_no"]) for s in fscan["sellouts"]] == \
            [("利物浦", 1)], fscan["sellouts"]

        await env.dao.recompute_balance("利物浦")
        balance_before = (await env.dao.get_balance("利物浦"))["balance"]

        import random
        random.seed(20260829)
        applied2 = await svc.apply(force=True)
        assert applied2["applied"] is True
        # 同种子复算期望值：钉住满座重抽确实发生（防 ratio==1.0 的退化通过）
        random.seed(20260829)
        expected_att = int(cap * random.uniform(*formula.SELL_OUT_FILL))

        m1b = await _row(env, 1, "利物浦")
        assert m1b["attendance"] == expected_att, (m1b["attendance"], expected_att)
        assert int(cap * lo) <= m1b["attendance"] < cap, m1b["attendance"]
        ratio = m1b["attendance"] / m1["attendance"]
        t1, c1 = float(m1["ticket_revenue"]), float(m1["commercial"])
        assert abs(m1b["ticket_revenue"] - round(t1 * ratio, 4)) < 1e-9, m1b
        assert abs(m1b["commercial"] - round(c1 * ratio, 4)) < 1e-9, m1b
        assert m1b["broadcast"] == 0.9, m1b["broadcast"]
        # 伪阳性场次完全不动
        m2_after = await _row(env, 2, "利物浦")
        for col in _RESULT_COLS:
            assert m2_after[col] == m2_before[col], col
        # 余额自洽：重算后 = 落库前 + 本次满座差额
        d = (round(t1 * ratio, 4) - t1) + (round(c1 * ratio, 4) - c1)
        now = (await env.dao.get_balance("利物浦"))["balance"]
        assert abs(now - (balance_before + d)) < 1e-6, now
        marker = json.loads(await env.dao.get_config(MARKER_KEY))
        assert marker["forced"] is True and marker["sellouts"] == 1, marker
    finally:
        await env.teardown()
