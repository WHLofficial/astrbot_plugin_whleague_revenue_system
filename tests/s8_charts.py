"""三图测试（v3）：轮次预告图（比分列）、轮次统计图（club 上座映射、缺失 —）、
赛季走势图（次级从 0 起积分、冠军杯报错）、轮次查询无 window 过滤、文件名无 window 段。"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests.common  # noqa: E402,F401  (install_stubs 副作用)

from astrbot_plugin_whleague_revenue_system.db.connection import DatabaseManager  # noqa: E402
from astrbot_plugin_whleague_revenue_system.db.dao import StadiumDAO  # noqa: E402
from astrbot_plugin_whleague_revenue_system.db.schema import init_schema  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services import chart_service as cs  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.chart_service import (  # noqa: E402
    ChartError,
    ChartService,
    _round_rows,
    _season_points,
)
from astrbot_plugin_whleague_revenue_system.services.club_sync import ClubSyncClient  # noqa: E402


def _row(mid, tid, comp, rnd, home, away, tstatus="pending", leg=0, score=None,
         result=None, week=None, day=None, mtime=None, weather=None):
    return {
        "id": mid, "season_number": 9, "tournament_id": tid, "competition": comp,
        "round_no": rnd, "leg": leg, "stage_name": "", "home_team": home,
        "away_team": away, "home_score": None, "away_score": None, "pen_home": None,
        "pen_away": None, "result": result, "score": score,
        "tour_status": tstatus, "walkover_side": None, "weather": weather,
        "wx_coef": None, "week_no": week, "day_no": day, "match_time": mtime,
        "tour_match_id": mid, "synced_at": None,
    }


FX = {
    101: {"matchId": 101, "attendance": 20000, "stadium_name": "安菲尔德", "capacity": 54000},
    # 102 缺失 → 「—」
    103: {"matchId": 103, "attendance": 8000, "stadium_name": "圣詹姆斯公园", "capacity": 52000},
}


async def _env():
    tmp = tempfile.TemporaryDirectory()
    db = DatabaseManager(os.path.join(tmp.name, "t.db"))
    dao = StadiumDAO(db)
    await db.init()
    try:
        await init_schema(db)
        await dao.set_league_state(9, "S9")
        return tmp, db, dao
    except BaseException:
        await db.close()
        tmp.cleanup()
        raise


async def test_round_rows_missing_attendance_keeps_rows():
    matches = [
        _row(101, 11, "顶级联赛", 12, "利物浦", "巴塞罗那", "finished", score="2:1",
             result="W", week=7, day=6, mtime="19:30"),
        _row(102, 11, "顶级联赛", 12, "切尔西", "阿森纳", "finished", score="1:1",
             result="D", week=7, day=6, mtime="21:00"),
        _row(103, 11, "顶级联赛", 12, "纽卡斯尔联", "勒沃库森", "finished", score="弃权",
             result="C", week=7, day=7, mtime="22:00"),
        _row(104, 11, "顶级联赛", 12, "皇马", "拜仁", "pending", week=8, day=1, mtime="18:00"),
    ]
    rows, total, counted, played_count = _round_rows(matches, FX, {})
    # 不丢行：总行数 == 已赛行数（3），未赛行不画
    assert played_count == 3 and len(rows) == 3, (played_count, rows)
    by_home = {r[5]: r for r in rows}
    # 上座缺失行显示 —
    chelsea = by_home["切尔西"]
    assert chelsea[9] == "—" and chelsea[10] == "—", chelsea
    # 映射命中行显示数值 + 上座率
    liver = by_home["利物浦"]
    assert liver[9] == "20,000" and liver[10] == "37.0%" and liver[8] == "安菲尔德", liver
    assert liver[6] == "2:1"
    # 弃权行显示「弃权」且不计入合计
    assert by_home["纽卡斯尔联"][6] == "弃权" and by_home["纽卡斯尔联"][9] == "-"
    assert total == 20000 and counted == 1, (total, counted)


async def test_season_points_starts_from_zero():
    rows = [
        _row(1, 11, "次级联赛", 1, "勒沃库森", "多特蒙德", "finished", score="2:1", result="W"),
        _row(2, 11, "次级联赛", 2, "多特蒙德", "勒沃库森", "finished", score="1:1", result="D"),
        _row(3, 11, "次级联赛", 3, "勒沃库森", "阿贾克斯", "finished", score="弃权", result="C"),
        _row(4, 11, "次级联赛", 3, "多特蒙德", "纽卡斯尔联", "finished", score="2:0", result="W"),
    ]
    series, max_round = _season_points(rows)
    assert max_round == 3
    # 首轮积分即首轮战果（无带入分，从 0 起累计）
    assert series["勒沃库森"][0] == (1, 3)
    assert series["多特蒙德"][0] == (1, 0)
    # R2 平局各 +1；R3 勒沃库森弃权不计、多特蒙德胜 +3
    assert series["勒沃库森"][1] == (2, 4) and series["勒沃库森"][2] == (3, 4)
    assert series["多特蒙德"][2] == (3, 4)


async def _render_env(monkey_fixtures=True):
    tmp, db, dao = await _env()
    svc = ChartService(db, dao, {"club_sync_url": "http://club.test", "club_sync_secret": "s"})
    if monkey_fixtures:

        async def _stub_fetch(self, tid, rnd):
            return _FX_BY_ROUND.get(rnd, [])

        original = ClubSyncClient.fetch_fixtures
        ClubSyncClient.fetch_fixtures = _stub_fetch
        return tmp, db, dao, svc, original
    return tmp, db, dao, svc, None


_FX_BY_ROUND = {
    12: [FX[101], FX[103]],
    1: [dict(FX[101], matchId=201, attendance=15000),
        dict(FX[101], matchId=202, attendance=18000)],
    2: [dict(FX[101], matchId=203, attendance=16000)],
    3: [],
}


async def _seed(dao):
    await dao.upsert_match(9, 11, "顶级联赛", 12, "利物浦", "巴塞罗那", tour_match_id=101,
                           week_no=7, day_no=6, match_time="19:30")
    await dao.upsert_match(9, 11, "顶级联赛", 12, "切尔西", "阿森纳", tour_match_id=102,
                           week_no=7, day_no=6, match_time="21:00")
    await dao.upsert_match(9, 11, "顶级联赛", 12, "纽卡斯尔联", "勒沃库森", tour_match_id=103,
                           week_no=7, day_no=7, match_time="22:00")
    await dao.upsert_match(9, 11, "顶级联赛", 13, "利物浦", "尤文图斯", tour_match_id=104)
    for mid, score, result in ((101, "2:1", "W"), (102, "1:1", "D"), (103, "弃权", "C")):
        await db_set_result(dao, mid, score, result)


async def db_set_result(dao, tmid, score, result):
    row = await dao._db.fetchone("SELECT id FROM matches WHERE tour_match_id=?", (tmid,))
    await dao._db.execute(
        "UPDATE matches SET score=?, result=?, tour_status='finished' WHERE id=?",
        (score, result, row["id"]),
    )


async def test_render_round_chart_attendance_missing_and_png():
    tmp, db, dao, svc, original = await _render_env()
    try:
        await _seed(dao)
        out = await svc.render_round_chart(12, "顶级联赛")
        p = Path(out)
        assert p.exists() and p.stat().st_size > 0
        assert p.name == "round_s9_顶级联赛_r12.png", p.name
        # 全部未赛 → 还没有赛果
        try:
            await svc.render_round_chart(13, "顶级联赛")
            raise AssertionError("全 pending 轮应报「还没有赛果」")
        except ChartError as e:
            assert "还没有赛果" in str(e), str(e)
    finally:
        ClubSyncClient.fetch_fixtures = original
        await db.close()
        tmp.cleanup()


async def test_render_round_preview_chart_png_and_score_column():
    tmp, db, dao, svc, original = await _render_env()
    try:
        await _seed(dao)
        out = await svc.render_round_preview_chart(12, "顶级联赛")
        p = Path(out)
        assert p.exists() and p.stat().st_size > 0
        assert p.name == "preview_s9_顶级联赛_r12.png", p.name
        # 预告图对应 R12 本地三行（已赛行比分列非空、弃权显示「弃权」）
        sched = await db.fetchall("SELECT tour_match_id, score FROM matches WHERE round_no=12 ORDER BY tour_match_id")
        assert [r["score"] for r in sched] == ["2:1", "1:1", "弃权"]
    finally:
        ClubSyncClient.fetch_fixtures = original
        await db.close()
        tmp.cleanup()


async def test_render_season_chart_second_div_and_champions_cup():
    tmp, db, dao, svc, original = await _render_env()
    try:
        for rnd, home, away, score, result in (
            (1, "勒沃库森", "多特蒙德", "2:1", "W"),
            (2, "多特蒙德", "勒沃库森", "1:1", "D"),
        ):
            await dao.upsert_match(9, 22, "次级联赛", rnd, home, away,
                                   tour_match_id=200 + rnd)
            row = await db.fetchone("SELECT id FROM matches WHERE tour_match_id=?", (200 + rnd,))
            await db.execute(
                "UPDATE matches SET score=?, result=?, tour_status='finished' WHERE id=?",
                (score, result, row["id"]),
            )
        out = await svc.render_season_chart("次级联赛")
        p = Path(out)
        assert p.exists() and p.stat().st_size > 0
        assert p.name == "season_s9_次级联赛.png", p.name
        try:
            await svc.render_season_chart("冠军杯")
            raise AssertionError("冠军杯应报错")
        except ChartError as e:
            assert "顶级/次级" in str(e), str(e)
    finally:
        ClubSyncClient.fetch_fixtures = original
        await db.close()
        tmp.cleanup()


async def test_fetch_fixtures_cache_second_call_no_request():
    calls = {"n": 0}

    async def _counting(self, tid, rnd):
        calls["n"] += 1
        return [{"matchId": 1, "attendance": 100, "stadium_name": "S", "capacity": 1000}]

    original = ClubSyncClient._fetch_fixtures_remote
    ClubSyncClient._fetch_fixtures_remote = _counting
    ClubSyncClient.reset_fixtures_cache()
    try:
        client = ClubSyncClient({"club_sync_url": "http://club.test", "club_sync_secret": "s"})
        a = await client.fetch_fixtures(11, 12)
        b = await client.fetch_fixtures(11, 12)
        assert a == b and calls["n"] == 1, calls
        c = await client.fetch_fixtures(11, 13)
        assert calls["n"] == 2 and c[0]["attendance"] == 100
    finally:
        ClubSyncClient._fetch_fixtures_remote = original
        ClubSyncClient.reset_fixtures_cache()


async def test_parse_fixtures_payload_club_shape():
    """H1 回归：club /api/fixtures 实际形状——顶层 matches、stadium 为对象。

    与 club 仓 src/worker/routes/fixtures.ts:28-42 + weather-ops.ts buildRoundView
    同构：{tournamentId, round, matches:[{matchId, homeClub, awayTeamName,
    stageName, finished, stadium:{name,capacity,tier,venueName}|null, weather,
    attendance}]}。旧解析只认 fixtures/items 键与扁平 stadiumName/capacity，
    对真实响应永远返回 []（上座 100% 缺失）。
    """
    from astrbot_plugin_whleague_revenue_system.services.club_sync import (
        parse_fixtures_payload,
    )

    payload = {
        "tournamentId": 11, "round": 1,
        "matches": [
            {
                "matchId": 101, "tournamentId": 11, "round": 1,
                "homeClub": {"id": 5, "name": "利物浦"},
                "awayTeamName": "巴塞罗那", "stageName": "常规赛", "finished": True,
                "stadium": {"name": "安菲尔德", "capacity": 54000, "tier": 1,
                            "venueName": "星光冠名的安菲尔德"},
                "weather": "晴", "attendance": 20000,
            },
            {
                "matchId": 102, "tournamentId": 11, "round": 1,
                "homeClub": {"id": 6, "name": "切尔西"},
                "awayTeamName": "阿森纳", "stageName": "常规赛", "finished": True,
                "stadium": {"name": "斯坦福桥", "capacity": 40000, "tier": 1,
                            "venueName": ""},
                "weather": None, "attendance": None,
            },
            # 无球场行的场次不会出现在 /api/fixtures（club 侧已过滤），
            # 但解析层对 stadium=None 也不得整批失败
            {"matchId": 103, "homeClub": {"id": 7, "name": "X"},
             "awayTeamName": "Y", "stageName": "", "finished": False,
             "stadium": None, "weather": None, "attendance": None},
        ],
    }
    out = parse_fixtures_payload(payload)
    assert [f["matchId"] for f in out] == [101, 102, 103]
    assert out[0]["attendance"] == 20000 and out[0]["capacity"] == 54000
    assert out[0]["stadium_name"] == "星光冠名的安菲尔德", "展示名优先 venueName（生效冠名）"
    assert out[1]["stadium_name"] == "斯坦福桥", "无冠名回落球场名"
    assert out[1]["attendance"] is None and out[1]["capacity"] == 40000
    assert out[2]["stadium_name"] is None and out[2]["capacity"] is None
    # 旧形状（顶层 fixtures/items + 扁平字段）兼容；非法形状返回 []
    old = parse_fixtures_payload(
        {"fixtures": [{"matchId": "9", "attendance": 1, "stadiumName": "S", "capacity": 10}]}
    )
    assert old == [{"matchId": 9, "attendance": 1, "stadium_name": "S", "capacity": 10}]
    assert parse_fixtures_payload({"nope": 1}) == []
    assert parse_fixtures_payload("bad") == []
    assert parse_fixtures_payload([{"nomatchid": 1}]) == []


async def test_round_query_has_no_window_filter():
    tmp, db, dao = await _env()
    try:
        # 同轮次跨「窗口」概念不存在：v15 直查赛季+轮次+赛事
        await dao.upsert_match(9, 11, "顶级联赛", 12, "A", "B", tour_match_id=301)
        await dao.upsert_match(9, 22, "次级联赛", 12, "C", "D", tour_match_id=302)
        rows = await dao.get_round_matches(9, 12)
        assert len(rows) == 2, rows
        rows = await dao.get_round_matches(9, 12, "顶级联赛")
        assert len(rows) == 1 and rows[0]["tournament_id"] == 11
    finally:
        await db.close()
        tmp.cleanup()


if __name__ == "__main__":
    from tests.common import run_module
    sys.exit(run_module("tests.s8_charts"))
