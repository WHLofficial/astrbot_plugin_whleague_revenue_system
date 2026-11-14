"""赛程时刻同步测试（P3-C）：队名归一、赛事识别、轮次偏移、映射、同步计划、写库。"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services import tour_sync  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.tour_sync import (  # noqa: E402
    TourSyncService,
    competition_of,
    map_tour_matches,
    normalize_team,
    plan_kickoff_sync,
    stage_offsets,
)

STAGES_SUB = [
    {"id": 1, "kind": "round_robin", "config": {"loops": 2}},
    {"id": 2, "kind": "round_robin", "config": {"loops": 2, "source": {"from": 1, "to": 4}}},
    {"id": 3, "kind": "elim", "config": {"legs": 2}},
]


def _tour_match(mid, stage_id, round_no, home, away, week=5, day=6, time="17:30",
                status="pending"):
    return {"id": mid, "stageId": stage_id, "round": round_no, "homeTeamName": home,
            "awayTeamName": away, "kickoffWeek": week, "kickoffDay": day,
            "kickoffTime": time, "status": status}


def _matches_for_offsets():
    # 基础阶段跑到 14 轮，决赛阶段从 1 开始
    return [_tour_match(i, 1, i, "A", "B") for i in range(1, 15)] + \
           [_tour_match(100, 2, 1, "C", "D")] + [_tour_match(200, 3, 1, "E", "F")]


async def test_normalize_and_competition():
    assert normalize_team("曼城(CPU)") == "曼城"
    assert normalize_team("AC米兰（CPU）") == "AC米兰"
    assert normalize_team("  利物浦 ") == "利物浦"
    assert normalize_team(None) == ""
    assert competition_of("S9 | 顶级联赛") == "顶级联赛"
    assert competition_of("S9 | 次级联赛") == "次级联赛"
    assert competition_of("S9 | 冠军杯") == "冠军杯"
    assert competition_of("S9 | 超级杯") is None


async def test_stage_offsets():
    offsets = stage_offsets(STAGES_SUB, _matches_for_offsets())
    assert offsets == {1: 0, 2: 14, 3: None}, offsets
    # 只有基础阶段（顶级联赛）
    top = [{"id": 1, "kind": "round_robin", "config": {"loops": 2}}]
    assert stage_offsets(top, [_tour_match(1, 1, 22, "A", "B")]) == {1: 0}


async def test_map_tour_matches():
    out = map_tour_matches("S9 | 次级联赛", STAGES_SUB, _matches_for_offsets()
                           + [_tour_match(300, 1, 3, "利物浦(CPU)", "纽卡")])
    assert out["competition"] == "次级联赛"
    rows = {r["tour_match_id"]: r for r in out["rows"]}
    assert rows[300]["round_no"] == 3 and rows[300]["home"] == "利物浦"
    assert rows[100]["round_no"] == 15, rows[100]          # 决赛阶段叠加 14 轮
    assert 200 not in rows                                  # 淘汰赛不同步
    assert out["skipped"]["淘汰赛"] == 1, out["skipped"]


async def test_map_keeps_unscheduled_rows():
    matches = [
        _tour_match(1, 1, 1, "利物浦", "纽卡", week=None, time=None),
        _tour_match(2, 1, 2, None, "纽卡"),
        _tour_match(3, 1, 3, "利物浦", "纽卡"),
    ]
    out = map_tour_matches("S9 | 顶级联赛", [STAGES_SUB[0]], matches)
    assert out["skipped"]["未排时刻"] == 1
    assert out["skipped"]["对手未定"] == 1
    rows = {r["tour_match_id"]: r for r in out["rows"]}
    assert set(rows) == {1, 3}                     # 未排时刻也保留，供补建行
    assert rows[1]["scheduled"] is False and rows[1]["week"] is None
    assert rows[3]["scheduled"] is True and rows[3]["round_no"] == 3


async def test_map_unknown_competition():
    try:
        map_tour_matches("S9 | 超级杯", [], [])
        raise AssertionError("应拒绝认不出的赛事")
    except tour_sync.TourSyncError:
        pass


async def test_plan_sync_updates_and_conflicts():
    tour_rows = map_tour_matches("S9 | 顶级联赛", [STAGES_SUB[0]], [
        _tour_match(1, 1, 1, "利物浦", "纽卡"),                    # 更新
        _tour_match(2, 1, 2, "曼城(CPU)", "切尔西"),               # 不变
        _tour_match(3, 1, 3, "阿森纳", "热刺", time="20:00"),      # 已完赛，时刻不同也不覆盖
        _tour_match(4, 1, 4, "曼联", "里昂"),                      # 主客相反
        _tour_match(5, 1, 5, "巴萨", "拜仁"),                      # 本地没有
        _tour_match(6, 1, 7, "皇马", "尤文", week=None, time=None),  # tour 未排期
    ])["rows"]
    local = [
        {"id": 11, "competition": "顶级联赛", "round_no": 1, "home_team": "利物浦",
         "away_team": "纽卡", "week_no": 9, "day_no": 7, "match_time": "18:00",
         "tour_match_id": None, "result": None},
        {"id": 12, "competition": "顶级联赛", "round_no": 2, "home_team": "曼城",
         "away_team": "切尔西", "week_no": 5, "day_no": 6, "match_time": "17:30",
         "tour_match_id": 2, "result": None},
        {"id": 13, "competition": "顶级联赛", "round_no": 3, "home_team": "阿森纳",
         "away_team": "热刺", "week_no": 5, "day_no": 6, "match_time": "17:30",
         "tour_match_id": 3, "result": "W"},
        {"id": 14, "competition": "顶级联赛", "round_no": 4, "home_team": "里昂",
         "away_team": "曼联", "week_no": 5, "day_no": 6, "match_time": "17:30",
         "tour_match_id": None, "result": None},
        {"id": 15, "competition": "顶级联赛", "round_no": 6, "home_team": "多特",
         "away_team": "巴黎", "week_no": 5, "day_no": 6, "match_time": "17:30",
         "tour_match_id": None, "result": None},
    ]
    plan = plan_kickoff_sync(tour_rows, local)
    assert [u["match_id"] for u in plan["updates"]] == [11], plan["updates"]
    assert [u["tour_match_id"] for u in plan["unchanged"]] == [2]
    assert {c["tour_match_id"] for c in plan["conflicts"]} == {3, 4}, plan["conflicts"]
    assert [m["tour_match_id"] for m in plan["missing_local"]] == [5, 6]
    assert plan["pending"] == []                    # 本地无行先补建，补建后才谈 pending
    assert [m["home_team"] for m in plan["missing_tour"]] == ["多特"]


class _FakeResponse:
    def __init__(self, status, text):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, routes, calls, timeout=None):
        self._routes = routes
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url):
        self._calls.append(url)
        for suffix, payload in self._routes.items():
            if url.endswith(suffix):
                return _FakeResponse(200, payload if isinstance(payload, str) else
                                     __import__("json").dumps(payload))
        return _FakeResponse(404, '{"error":"not found"}')


def install_fake_aiohttp(routes):
    import json
    calls = []
    mod = types.ModuleType("aiohttp")
    mod.ClientTimeout = lambda **kw: None
    mod.ClientSession = lambda timeout=None: _FakeSession(routes, calls)
    old = sys.modules.get("aiohttp")
    sys.modules["aiohttp"] = mod
    return calls, json, old


def restore_aiohttp(old):
    if old is None:
        sys.modules.pop("aiohttp", None)
    else:
        sys.modules["aiohttp"] = old


async def test_service_sync_writes_snapshot():
    env = await TestEnv().setup()
    try:
        await env.dao.add_match(1, 1, 1, "利物浦", "纽卡", "顶级联赛")
        await env.dao.add_match(1, 1, 15, "阿森纳", "热刺", "次级联赛")
        routes = {
            "/api/public/tournaments": {"tournaments": [
                {"id": 7, "name": "S9 | 顶级联赛"}, {"id": 8, "name": "S8 | 顶级联赛"},
            ]},
            "/api/public/tournaments/7": {"stages": STAGES_SUB[:1]},
            "/api/public/tournaments/7/matches": {"matches": [
                _tour_match(101, 1, 1, "利物浦(CPU)", "纽卡"),
                _tour_match(102, 1, 2, "曼城", "切尔西"),
                _tour_match(103, 1, 3, "阿森纳", "热刺", week=None, day=None, time=None),
            ]},
        }
        calls, json, old = install_fake_aiohttp(routes)
        try:
            svc = TourSyncService(env.dao, {"tour_api_base": "https://tour.test",
                                            "tour_sync_season": "S9"})
            r = await svc.sync(1)
        finally:
            restore_aiohttp(old)
        assert r["status"] == "ok", r
        assert r["created"] == 2 and r["updated"] == 2 and r["fetched"] == 3, r
        assert len(r["pending"]) == 1 and r["pending"][0]["tour_match_id"] == 103, r
        assert any(u.endswith("/api/public/tournaments/7/matches") for u in calls), calls
        assert not any("S8" in u for u in calls)
        m = (await env.dao.get_round_matches(1, 1, 1))[0]
        assert m["week_no"] == 5 and m["day_no"] == 6 and m["match_time"] == "17:30", m
        assert m["tour_match_id"] == 101 and m["kickoff_source"] == "tour", m
        # 补建行也写入了时刻；未排期行建了空时刻行
        m2 = (await env.dao.get_round_matches(1, 1, 2))[0]
        assert m2["match_time"] == "17:30" and m2["tour_match_id"] == 102, m2
        m3 = (await env.dao.get_round_matches(1, 1, 3))[0]
        assert m3["match_time"] is None and m3["tour_match_id"] is None, m3

        # 再跑一次：全部未变，pending 不计更新
        calls, json, old = install_fake_aiohttp(routes)
        try:
            r2 = await svc.sync(1)
        finally:
            restore_aiohttp(old)
        assert r2["created"] == 0 and r2["updated"] == 0 and r2["unchanged"] == 2, r2
        assert len(r2["pending"]) == 1, r2
        # 本地多出来的次级场次计入 tour 暂无此场
        assert r2["missing_tour"] and r2["missing_tour"][0]["round_no"] == 15, r2
    finally:
        await env.teardown()


async def test_service_errors_are_returned():
    env = await TestEnv().setup()
    try:
        svc = TourSyncService(env.dao, {"tour_api_base": "", "tour_sync_season": "S9"})
        assert (await svc.sync(1))["status"] == "bypass"

        routes = {"/api/public/tournaments": {"tournaments": []}}
        calls, json, old = install_fake_aiohttp(routes)
        try:
            svc2 = TourSyncService(env.dao, {"tour_api_base": "https://tour.test",
                                             "tour_sync_season": "S9"})
            r = await svc2.sync(1)
        finally:
            restore_aiohttp(old)
        assert r["status"] == "error" and "S9" in r["error"], r
    finally:
        await env.teardown()
