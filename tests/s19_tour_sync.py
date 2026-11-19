"""tour 全量同步引擎测试（v3）：stage offset、leg、点球/弃权/轮空/TBD 映射、
tour_match_id 优先/归一兜底匹配、改判覆盖、dao 事务写批。

纯函数与 dao 层直接测；端到端（club+tour stub 服务）由任务 4 的 stub 驱动脚本覆盖。
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests.common  # noqa: E402,F401  (install_stubs 副作用)

from astrbot_plugin_whleague_revenue_system.db.connection import DatabaseManager  # noqa: E402
from astrbot_plugin_whleague_revenue_system.db.dao import StadiumDAO  # noqa: E402
from astrbot_plugin_whleague_revenue_system.db.schema import init_schema  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.club_sync import parse_current_season  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.tour_sync import (  # noqa: E402
    TourSyncService,
    effective_round,
    final_result,
    load_round_map,
    local_rows_for_tournament,
    map_tournament_rows,
    normalize_team,
    plan_sync,
    stage_offsets,
)

STAGES_SECOND = [
    {"id": 201, "kind": "round_robin", "sortOrder": 1, "config": {}},
    {"id": 202, "kind": "round_robin", "sortOrder": 2, "config": {"source": 201}},
    {"id": 203, "kind": "elim", "sortOrder": 3, "config": {}},
]
STAGES_CUP = [
    {"id": 301, "kind": "round_robin", "sortOrder": 1, "config": {}},
    {"id": 302, "kind": "elim", "sortOrder": 2, "config": {}},
]


def _m(mid, sid, rnd, home, away, status="pending", leg=None, sh=None, sa=None,
       ph=None, pa=None, note=None, wo=None, wk=None, dy=None, tm=None,
       hp=None, ap=None, stage_name=""):
    return {
        "id": mid, "stageId": sid, "round": rnd, "slot": 1, "leg": leg,
        "homeTeamName": home, "awayTeamName": away,
        "scoreHome": sh, "scoreAway": sa, "penHome": ph, "penAway": pa,
        "status": status, "winnerEntryId": None, "note": note,
        "walkoverSide": wo, "kickoffWeek": wk, "kickoffDay": dy,
        "kickoffTime": tm, "events": [],
        "stageKind": "elim" if sid in (203, 302) else "round_robin",
        "stageName": stage_name,
        "homePlaceholder": hp, "awayPlaceholder": ap,
    }


async def test_normalize_and_stage_offsets():
    assert normalize_team("利物浦 (CPU)") == "利物浦"
    assert normalize_team("拜仁（CPU）") == "拜仁"
    assert normalize_team(" 利物浦  (cpu) ") == "利物浦"

    matches_second = [
        _m(1, 201, 14, "A", "B"), _m(2, 202, 6, "A", "B"),
        _m(3, 203, 2, "A", "B", leg=2),
    ]
    off = stage_offsets(STAGES_SECOND, matches_second)
    assert off == {201: 0, 202: 14, 203: 20}, off
    off_cup = stage_offsets(STAGES_CUP, [_m(1, 301, 6, "A", "B"), _m(2, 302, 1, "A", "B")])
    assert off_cup == {301: 0, 302: 6}, off_cup
    off_top = stage_offsets(
        [{"id": 9, "kind": "round_robin", "sortOrder": 1}], [_m(1, 9, 22, "A", "B")])
    assert off_top == {9: 0}, off_top
    # 淘汰赛不再返回 None（v2 的 elim=None 会整段丢弃淘汰赛）
    assert off[203] == 20


async def test_map_rows_leg_walkover_tbd_bye():
    mapped = map_tournament_rows(22, "次级联赛", STAGES_SECOND, [
        _m(9101, 201, 14, "勒沃库森", "多特蒙德", "finished", sh=2, sa=0, stage_name="预赛"),
        _m(9102, 202, 1, "勒沃库森", "纽卡斯尔联", "finished", sh=1, sa=0, stage_name="决赛"),
        # final 阶段实际 6 轮（S9=14 轮预赛 ⇒ 15–20），给全轮次保证 offset 累计正确
        _m(9110, 202, 6, "多特蒙德", "阿贾克斯", stage_name="决赛"),
        _m(9103, 203, 1, "勒沃库森", "纽卡斯尔联", "finished", leg=1, sh=2, sa=2, ph=5, pa=4, stage_name="附加赛"),
        _m(9104, 203, 1, "纽卡斯尔联", "勒沃库森", "finished", leg=2, sh=1, sa=2, stage_name="附加赛"),
        _m(9105, 203, 2, "勒沃库森", "阿贾克斯", leg=1, hp="胜者 R1", ap="胜者 R2"),
        _m(9106, 203, 3, "勒沃库森", None),
        _m(9107, 203, 4, None, None, note="轮空"),
    ])
    assert mapped["skipped"] == {"轮空": 1, "对手未定": 1, "淘汰赛未定": 1}, mapped["skipped"]
    rows = {r["tour_match_id"]: r for r in mapped["rows"]}
    assert set(rows) == {9101, 9102, 9110, 9103, 9104}
    # stage offset：base R14 → 14；final R1 → 15、final R6 → 20（次级 15–20）；elim R1 → 21（21–22）
    assert rows[9101]["round_no"] == 14
    assert rows[9102]["round_no"] == 15
    assert rows[9110]["round_no"] == 20
    assert rows[9103]["round_no"] == 21 and rows[9103]["leg"] == 1
    assert rows[9104]["leg"] == 2 and rows[9104]["round_no"] == 21
    # 展示保留 tour 原名；tournament_id/competition 随赛事传入
    assert rows[9101]["home_team"] == "勒沃库森" and rows[9101]["tour_match_id"] == 9101
    assert rows[9101]["tournament_id"] == 22 and rows[9101]["competition"] == "次级联赛"
    # 点球：elim 点球定胜负
    assert rows[9103]["result"] == "W" and rows[9103]["score"] == "2:2（点球 5:4）"
    assert rows[9104]["result"] == "L"
    # 轮次换算表：本地 round_no → tour stage 内轮次（club weather-sync/fixtures 定位键）
    assert mapped["round_map"] == {14: 14, 15: 1, 20: 6, 21: 1}, mapped["round_map"]


async def test_final_result_rules():
    # 小组/联赛点球仍按平局（pen 只展示）
    r = final_result(_m(1, 101, 1, "A", "B", "finished", sh=1, sa=1, ph=4, pa=2), is_elim=False)
    assert (r["result"], r["score"], r["pen_home"], r["pen_away"]) == ("D", "1:1（点球 4:2）", 4, 2)
    # 淘汰赛点球定胜负
    r = final_result(_m(1, 203, 1, "A", "B", "finished", sh=1, sa=1, ph=2, pa=4), is_elim=True)
    assert r["result"] == "L" and r["score"] == "1:1（点球 2:4）"
    # 普通胜平负 + 展示串
    assert final_result(_m(1, 101, 1, "A", "B", "finished", sh=2, sa=1), False)["score"] == "2:1"
    assert final_result(_m(1, 101, 1, "A", "B", "finished", sh=0, sa=3), False)["result"] == "L"
    # 弃权：result=C、score=弃权、比分不落
    r = final_result(_m(1, 101, 1, "A", "B", "finished", sh=3, sa=0, wo="home"), is_elim=False)
    assert (r["result"], r["score"], r["home_score"], r["walkover_side"]) == ("C", "弃权", None, "home")
    # live：只更新状态
    r = final_result(_m(1, 101, 1, "A", "B", "live"), is_elim=False)
    assert r["tour_status"] == "live" and r["result"] is None and r["score"] is None
    # pending
    assert final_result(_m(1, 101, 1, "A", "B"), is_elim=False)["tour_status"] == "pending"


def _local_row(mid, tid, rnd, leg, home, away, tmid=None, score=None,
               result=None, tstatus="pending", week=None, day=None, mtime=None):
    return {
        "id": mid, "season_number": 9, "tournament_id": tid, "competition": "顶级联赛",
        "round_no": rnd, "leg": leg, "stage_name": "", "home_team": home,
        "away_team": away, "home_score": None, "away_score": None, "pen_home": None,
        "pen_away": None, "result": result, "score": score, "tour_status": tstatus,
        "walkover_side": None, "weather": None, "wx_coef": None, "week_no": week,
        "day_no": day, "match_time": mtime, "tour_match_id": tmid, "synced_at": None,
    }


def _tour_row(mid, tid, rnd, home, away, leg=0, scheduled=True, score=None, tstatus="pending"):
    return {
        "tour_match_id": mid, "tournament_id": tid, "competition": "顶级联赛",
        "round_no": rnd, "leg": leg, "stage_name": "", "home_team": home,
        "away_team": away, "home_score": None, "away_score": None, "pen_home": None,
        "pen_away": None, "result": None, "score": score, "tour_status": tstatus,
        "walkover_side": None, "week_no": 1 if scheduled else None,
        "day_no": 2 if scheduled else None, "match_time": "19:00" if scheduled else None,
        "scheduled": scheduled,
    }


async def test_local_rows_for_tournament_filters_cross_competition():
    """同步报告按赛事比对：不得把其他赛事的本地行算成本赛事 missing_tour。"""
    local = [
        _local_row(1, 11, 12, 0, "利物浦", "巴塞罗那", tmid=9001),
        _local_row(2, 22, 15, 0, "冲刺队", "挑战队", tmid=9002),
    ]
    rows11 = local_rows_for_tournament(local, 11)
    rows22 = local_rows_for_tournament(local, 22)
    assert [r["id"] for r in rows11] == [1]
    assert [r["id"] for r in rows22] == [2]
    assert local_rows_for_tournament(local, 33) == []

    tour11 = [_tour_row(9001, 11, 12, "利物浦", "巴塞罗那")]
    tour22 = [_tour_row(9002, 22, 15, "冲刺队", "挑战队")]
    assert plan_sync(tour11, rows11)["missing_tour"] == []
    assert plan_sync(tour22, rows22)["missing_tour"] == []


async def test_plan_sync_tour_match_id_priority_and_normalization():
    # tour_match_id 优先：本地行队名写法旧/轮次错，仍按绑定命中
    local = [_local_row(1, 11, 30, 0, "利物浦(旧名)", "巴塞罗那", tmid=9001)]
    tour = [_tour_row(9001, 11, 12, "利物浦", "巴塞罗那", score="2:1", tstatus="finished")]
    plan = plan_sync(tour, local)
    assert len(plan["ops"]) == 1 and plan["ops"][0]["kind"] == "update"
    assert plan["ops"][0]["id"] == 1 and plan["ops"][0]["row"]["round_no"] == 12
    assert plan["missing_tour"] == []

    # 无 id：归一唯一键兜底（CPU 后缀归一）
    local = [_local_row(2, 11, 5, 0, "拜仁 (CPU)", "尤文图斯", tmid=None)]
    tour = [_tour_row(9002, 11, 5, "拜仁 (CPU)", "尤文图斯")]
    plan = plan_sync(tour, local)
    assert len(plan["ops"]) == 1 and plan["ops"][0]["kind"] == "update" and plan["ops"][0]["id"] == 2

    # 字段一致 → unchanged；本地未被匹配 → missing_tour
    local = [
        _local_row(3, 11, 6, 0, "利物浦", "巴塞罗那", tmid=9003, score="2:1",
                   result="W", tstatus="finished", week=1, day=2, mtime="19:00"),
        _local_row(4, 11, 7, 0, "切尔西", "阿森纳"),
    ]
    tour_row = _tour_row(9003, 11, 6, "利物浦", "巴塞罗那", score="2:1", tstatus="finished")
    tour_row["result"] = "W"  # 引擎落库行 result 由 final_result 计算后随行携带
    tour = [tour_row]
    plan = plan_sync(tour, local)
    assert plan["ops"] == [] and plan["unchanged"] == 1
    assert len(plan["missing_tour"]) == 1 and plan["missing_tour"][0]["home_team"] == "切尔西"
    # 未排期行计数
    plan2 = plan_sync([_tour_row(9003, 11, 6, "利物浦", "巴塞罗那", scheduled=False)], local)
    assert plan2["pending"] == 1


async def test_plan_sync_conflicts_reversed_home_away():
    """M4 回归：主客与本地相反记 conflicts（v2.13 行为，v3 重写时丢失）。

    不覆盖、不补建（无 insert/update op），本地行算已匹配（不进 missing_tour）。
    """
    local = [_local_row(1, 11, 12, 0, "巴塞罗那", "利物浦", tmid=None)]
    tour = [_tour_row(9001, 11, 12, "利物浦", "巴塞罗那")]
    plan = plan_sync(tour, local)
    assert plan["ops"] == [], plan["ops"]
    assert plan["conflicts"] == [{
        "competition": "顶级联赛", "round_no": 12, "leg": 0,
        "home_team": "利物浦", "away_team": "巴塞罗那",
        "reason": "主客与本地相反，未覆盖",
    }], plan["conflicts"]
    assert plan["missing_tour"] == [] and plan["unchanged"] == 0


async def test_sync_equal_tour_unscheduled_tolerates_local_time():
    """M5 回归（比对侧）：tour 未排期不把本地时刻算差异，否则每次同步虚报 update。"""
    local = [
        _local_row(1, 11, 12, 0, "利物浦", "巴塞罗那", tmid=9001,
                   week=7, day=6, mtime="19:30"),
    ]
    tour = [_tour_row(9001, 11, 12, "利物浦", "巴塞罗那", scheduled=False)]
    plan = plan_sync(tour, local)
    assert plan["ops"] == [] and plan["unchanged"] == 1 and plan["pending"] == 1, plan


async def test_round_map_load_and_effective_round():
    """M6 回归：换算表 kv 读写与 effective_round 回落。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        db = DatabaseManager(str(Path(tmp.name) / "rm.db"))
        dao = StadiumDAO(db)
        await db.init()
        try:
            await init_schema(db)
            assert await load_round_map(dao) == {}, "无 kv 时应为空表"
            await dao.set_config("tour_round_map", '{"22": {"15": 1, "20": 6}, "bad": "x"}')
            m = await load_round_map(dao)
            assert m == {22: {15: 1, 20: 6}}, m
            assert effective_round(m, 22, 15) == 1
            assert effective_round(m, 22, 20) == 6
            # 不在表内/无该赛事/空表：原值回落（顶级 1–22、冠军杯小组 1–6 本就对齐）
            assert effective_round(m, 22, 3) == 3
            assert effective_round(m, 11, 12) == 12
            assert effective_round({}, 22, 15) == 15
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_apply_ops_update_keeps_local_time_when_tour_unscheduled():
    """M5 回归（写库侧）：update op 的 NULL 时刻不得清空本地 CSV 时刻。

    生产库约 82 场 tour 未排期但本地有时刻的行，第一次 /主场赛程同步
    曾被直接 SET 成 NULL。
    """
    tmp = tempfile.TemporaryDirectory()
    try:
        db = DatabaseManager(str(Path(tmp.name) / "kt.db"))
        dao = StadiumDAO(db)
        await db.init()
        try:
            await init_schema(db)
            mid = await dao.upsert_match(9, 11, "顶级联赛", 12, "利物浦", "巴塞罗那",
                                         week_no=7, day_no=6, match_time="19:30",
                                         tour_match_id=9001)
            row = _tour_row(9001, 11, 12, "利物浦", "巴塞罗那", scheduled=False)
            row["synced_at"] = "2026-11-15 10:00:00"
            counts = await dao.apply_match_sync_ops(
                [{"kind": "update", "id": mid, "row": row}], 9
            )
            assert counts == {"created": 0, "updated": 1}, counts
            got = await db.fetchone("SELECT * FROM matches WHERE id=?", (mid,))
            assert (got["week_no"], got["day_no"], got["match_time"]) == (7, 6, "19:30"), \
                "tour 未排期不得清空本地时刻"
            # tour 排期后非空值照常覆盖
            row2 = _tour_row(9001, 11, 12, "利物浦", "巴塞罗那", scheduled=True)
            row2["synced_at"] = "2026-11-15 11:00:00"
            await dao.apply_match_sync_ops([{"kind": "update", "id": mid, "row": row2}], 9)
            got = await db.fetchone("SELECT * FROM matches WHERE id=?", (mid,))
            assert (got["week_no"], got["day_no"], got["match_time"]) == (1, 2, "19:00")
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_dao_upsert_and_apply_ops():
    tmp = tempfile.TemporaryDirectory()
    try:
        db = DatabaseManager(str(Path(tmp.name) / "t.db"))
        dao = StadiumDAO(db)
        await db.init()
        try:
            await init_schema(db)
            # upsert_match match_id 显式传参：跳过自匹配直接按 id 更新
            mid = await dao.upsert_match(9, 11, "顶级联赛", 30, "旧名", "巴塞罗那",
                                         tour_match_id=9001)
            got = await dao.upsert_match(
                9, 11, "顶级联赛", 12, "利物浦", "巴塞罗那", tour_match_id=9001,
                home_score=2, away_score=1, result="W", score="2:1",
                tour_status="finished", week_no=7, day_no=6, match_time="19:30",
                synced_at="2026-11-15 10:00:00", match_id=mid,
            )
            assert got == mid
            row = await db.fetchone("SELECT * FROM matches WHERE id=?", (mid,))
            # upsert_match 不覆盖队名（队名收敛由引擎的 apply_match_sync_ops 负责）
            assert row["round_no"] == 12 and row["home_team"] == "旧名"
            assert row["score"] == "2:1" and row["tour_status"] == "finished"
            # apply_match_sync_ops：insert + update 混合批，事务提交
            ins_row = _tour_row(9002, 11, 2, "利物浦", "勒沃库森")
            ins_row["synced_at"] = "2026-11-15 10:00:00"
            upd_row = _tour_row(9001, 11, 12, "利物浦", "巴塞罗那", score="3:0", tstatus="finished")
            upd_row["home_score"] = 3
            upd_row["synced_at"] = "2026-11-15 10:00:01"
            counts = await dao.apply_match_sync_ops([
                {"kind": "insert", "row": ins_row},
                {"kind": "update", "id": mid, "row": upd_row},
            ], 9)
            assert counts == {"created": 1, "updated": 1}, counts
            row = await db.fetchone("SELECT * FROM matches WHERE tour_match_id=9001")
            assert row["home_score"] == 3 and row["score"] == "3:0"
            assert row["home_team"] == "利物浦", "事务批写覆盖队名（tour 原名收敛）"
            # 单赛事事务回滚：批内 tour_match_id 唯一冲突 → 全批回滚
            bad_ins = _tour_row(9001, 11, 13, "X", "Y")  # tour_match_id 撞 partial 唯一索引
            bad_ins["synced_at"] = "2026-11-15 10:00:02"
            try:
                rollback_row = _tour_row(9500, 11, 20, "M", "N")
                rollback_row["synced_at"] = "2026-11-15 10:00:02"
                await dao.apply_match_sync_ops([
                    {"kind": "insert", "row": rollback_row},
                    {"kind": "insert", "row": bad_ins},
                ], 9)
                assert False, "唯一键冲突应使事务失败"
            except Exception as e:
                assert "UNIQUE" in str(e), str(e)
            n = await db.fetchone("SELECT COUNT(*) AS n FROM matches WHERE tour_match_id=9500")
            assert n["n"] == 0, "同批插入应随事务回滚"
            # list_matches_by_season 返回 v15 列集（无 kickoff_source）
            cols = await dao.list_matches_by_season(9)
            assert cols and "kickoff_source" not in cols[0].keys()
            assert "tournament_id" in cols[0].keys() and "tour_status" in cols[0].keys()
        finally:
            await db.close()
    finally:
        tmp.cleanup()


class _DaoStub:
    """最小 dao 桩：league_state 无行（双无场景）。"""

    async def get_league_state(self):
        return None

    async def set_league_state(self, season, label):
        return None


async def test_club_current_season_resolution():
    """club 当前赛季解析：running 优先、10 分钟缓存第二次不发请求、失败回落 league_state。"""
    from astrbot_plugin_whleague_revenue_system.services.club_sync import ClubSyncClient

    calls = {"n": 0}
    box = {"payload": None}

    async def _fake_fetch(self):
        calls["n"] += 1
        return parse_current_season(box["payload"]) if box["payload"] is not None else None

    original = ClubSyncClient._fetch_season_remote
    ClubSyncClient._fetch_season_remote = _fake_fetch
    tmp = tempfile.TemporaryDirectory()
    db = DatabaseManager(str(Path(tmp.name) / "t.db"))
    dao = StadiumDAO(db)
    await db.init()
    try:
        await init_schema(db)
        ClubSyncClient.reset_season_cache()
        # running 优先：候选数组含 preparing/running 时取 running
        box["payload"] = {
            "season": [{"season": 8, "status": "preparing"},
                       {"season": 9, "status": "running"}],
            "tournaments": [],
        }
        client = ClubSyncClient({"club_sync_url": "http://club.test"})
        cur = await client.fetch_current_season(dao)
        assert cur["season"] == 9 and cur["status"] == "running" and cur["source"] == "club", cur
        assert cur["season_label"] == "S9"
        # 成功后写 league_state
        ls = await dao.get_league_state()
        assert ls["season_number"] == 9 and ls["season_label"] == "S9"
        # 10 分钟缓存：第二次调用不发请求（跨实例共享）
        before = calls["n"]
        cur2 = await ClubSyncClient({"club_sync_url": "http://club.test"}).fetch_current_season(dao)
        assert calls["n"] == before, "缓存命中不应发请求"
        assert cur2["season"] == 9
        # 失败回落 league_state：远端无 preparing/running 赛季 → 回落本地（不写缓存）
        ClubSyncClient.reset_season_cache()
        box["payload"] = {"season": {"season": 5, "status": "finished"}, "tournaments": []}
        cur3 = await client.fetch_current_season(dao)
        assert cur3["source"] == "league_state" and cur3["season"] == 9, cur3
        assert cur3["tournaments"] == [] and cur3["status"] is None
    finally:
        ClubSyncClient._fetch_season_remote = original
        ClubSyncClient.reset_season_cache()
        await db.close()
        tmp.cleanup()


async def test_club_current_season_fallback_uses_cached_tournaments():
    """M7 回归：club 成功时赛事映射落 kv；回落 league_state 时读回。

    旧实现回落时 tournaments 恒为空列表，/主场赛程同步 直接报
    「无法确定当前赛季的赛事列表」——设计 §3.1「失败时用缓存」在主同步命令上走不通。
    """
    from astrbot_plugin_whleague_revenue_system.services.club_sync import ClubSyncClient

    box = {"payload": None}

    async def _fake_fetch(self):
        return parse_current_season(box["payload"]) if box["payload"] is not None else None

    original = ClubSyncClient._fetch_season_remote
    ClubSyncClient._fetch_season_remote = _fake_fetch
    tmp = tempfile.TemporaryDirectory()
    db = DatabaseManager(str(Path(tmp.name) / "kv.db"))
    dao = StadiumDAO(db)
    await db.init()
    try:
        await init_schema(db)
        ClubSyncClient.reset_season_cache()
        box["payload"] = {
            "season": {"season": 9, "status": "running"},
            "tournaments": [
                {"tournamentId": 11, "competitionType": "league_premier"},
                {"tournamentId": 22, "competitionType": "league_second"},
                {"tournamentId": 33, "competitionType": "champions_cup"},
            ],
        }
        client = ClubSyncClient({"club_sync_url": "http://club.test"})
        cur = await client.fetch_current_season(dao)
        assert cur["source"] == "club" and len(cur["tournaments"]) == 3, cur
        # 远端失败回落 league_state：tournaments 从 kv 读回，主同步命令可用
        ClubSyncClient.reset_season_cache()
        box["payload"] = None
        cur2 = await client.fetch_current_season(dao)
        assert cur2["source"] == "league_state" and cur2["season"] == 9, cur2
        assert [t["tournament_id"] for t in cur2["tournaments"]] == [11, 22, 33], cur2
        assert {t["competition"] for t in cur2["tournaments"]} == {"顶级联赛", "次级联赛", "冠军杯"}
        # 无 kv 的全新库回落仍是空列表（无缓存可用，如实报错由上层负责）
        await dao.set_config("club_tournaments", "not-json")
        cur3 = await client.fetch_current_season(dao)
        assert cur3["tournaments"] == [], cur3
    finally:
        ClubSyncClient._fetch_season_remote = original
        ClubSyncClient.reset_season_cache()
        await db.close()
        tmp.cleanup()


async def test_service_bypass_and_season_errors():
    svc = TourSyncService(None, {"tour_api_base": "", "club_sync_url": ""})
    r = await svc.sync()
    assert r["status"] == "bypass" and "tour_api_base" in r["reason"]
    # club 不可达 + league_state 空 → 无法确定当前赛季（保留端口必连失败）
    svc2 = TourSyncService(_DaoStub(), {
        "tour_api_base": "https://tour.test",
        "club_sync_url": "http://127.0.0.1:1",
    })
    r2 = await svc2.sync()
    assert r2["status"] == "error" and "无法确定当前赛季" in r2["error"], r2


if __name__ == "__main__":
    from tests.common import run_module
    sys.exit(run_module("tests.s19_tour_sync"))
