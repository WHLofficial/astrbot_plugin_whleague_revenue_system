"""天气推送测试：签名金标准、推送三态、只推已有天气、预览只读、payload 结构。

v3：sync_weather 只推已有天气到 club，不做补抽订正、不碰任何经营/财务字段；
赛季号即插件自身赛季号（club 当前赛季驱动，旧配置换算已下线）。
"""

import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services import club_sync  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.club_sync import ClubSyncClient  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.weather_service import WeatherError  # noqa: E402

GOLDEN_SECRET = "whl-golden-secret"
GOLDEN_TS = "1700000000"
GOLDEN_PATH = "/api/internal/weather-sync"
GOLDEN_BODY = (
    '{"season":9,"competition":"顶级联赛","roundNo":1,"items":['
    '{"home":"利物浦","away":"曼城(CPU)","weather":"雨","coef":0.8342}]}'
)
GOLDEN_SIGN = "8ecf0abca9d0244d2e8e9a3902a25dcd8fbb2aea34b4655ea152c81d1469e633"


class _FakeResponse:
    def __init__(self, status: int, text: str):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, script, calls, timeout=None):
        self._script = script
        self._calls = calls
        self._timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, data=None, headers=None):
        self._calls.append({"url": url, "data": data, "headers": headers})
        status, text = self._script[min(len(self._calls) - 1, len(self._script) - 1)]
        return _FakeResponse(status, text)


def install_fake_aiohttp(script):
    """把假 aiohttp 注入 sys.modules，返回 (calls, restore)。script=[(status, text), ...]。"""
    calls: list = []
    module = types.ModuleType("aiohttp")
    module.ClientTimeout = lambda **kw: kw
    module.ClientSession = lambda **kw: _FakeSession(script, calls, **kw)
    saved = sys.modules.get("aiohttp")
    sys.modules["aiohttp"] = module

    def restore():
        if saved is None:
            sys.modules.pop("aiohttp", None)
        else:
            sys.modules["aiohttp"] = saved

    return calls, restore


def remove_aiohttp():
    """强制 push_round 走缺依赖分支（sys.modules 置 None ⇒ import 抛 ImportError）。返回 restore 回调。"""
    saved = sys.modules.get("aiohttp")
    sys.modules["aiohttp"] = None

    def restore():
        if saved is None:
            sys.modules.pop("aiohttp", None)
        else:
            sys.modules["aiohttp"] = saved

    return restore


async def _sync_env(cfg_extra: dict | None = None):
    """v15 库 + 两行已预报天气（R12 顶级联赛），league_state=S9。"""
    cfg = {"club_sync_url": "https://club.example.com", "club_sync_secret": "s3cret"}
    cfg.update(cfg_extra or {})
    env = await TestEnv(cfg_override=cfg).setup()
    await env.dao.set_league_state(9, "S9")
    for tmid, home, away, weather, coef in (
        (101, "利物浦", "巴塞罗那", "雨", 0.8342),
        (102, "纽卡斯尔联", "勒沃库森", "晴", 1.02),
    ):
        await env.dao.upsert_match(9, 11, "顶级联赛", 12, home, away, tour_match_id=tmid)
        row = await env.db.fetchone("SELECT id FROM matches WHERE tour_match_id=?", (tmid,))
        await env.dao.set_match_weather_by_id(row["id"], weather, coef)
    return env


# ─── 签名 ─────────────────────────────────────────────────

async def test_sign_golden_vector():
    """金标准：与平台 src/lib/hmac.ts 同串同密钥必须得到同一签名（跨端互通锚）。"""
    assert club_sync.canonical_string("post", GOLDEN_PATH, GOLDEN_TS, GOLDEN_BODY) == (
        f"POST|{GOLDEN_PATH}|{GOLDEN_TS}|{GOLDEN_BODY}"
    )
    assert club_sync.sign(GOLDEN_SECRET, "POST", GOLDEN_PATH, GOLDEN_TS, GOLDEN_BODY) == GOLDEN_SIGN
    payload = {
        "season": 9, "competition": "顶级联赛", "roundNo": 1,
        "items": [{"home": "利物浦", "away": "曼城(CPU)", "weather": "雨", "coef": 0.8342}],
    }
    body = club_sync.encode_body(payload)
    assert body == GOLDEN_BODY, body
    assert len(body.encode("utf-8")) == 135
    assert club_sync.sign(GOLDEN_SECRET, "POST", GOLDEN_PATH, GOLDEN_TS, body) == GOLDEN_SIGN


async def test_endpoint_join():
    client = ClubSyncClient({"club_sync_url": "https://club.example.com/", "club_sync_secret": "x"})
    assert client.endpoint() == "https://club.example.com" + GOLDEN_PATH
    client2 = ClubSyncClient({"club_sync_url": "https://club.example.com" + GOLDEN_PATH, "club_sync_secret": "x"})
    assert client2.endpoint() == "https://club.example.com" + GOLDEN_PATH
    assert ClubSyncClient({}).enabled is False


# ─── 推送三态 ─────────────────────────────────────────────

async def test_push_bypass_when_unconfigured():
    client = ClubSyncClient({"club_sync_url": "", "club_sync_secret": ""})
    assert await client.push_round(9, "顶级联赛", 1, []) == {"status": "bypass"}


async def test_push_missing_dependency_graceful():
    restore = remove_aiohttp()
    try:
        client = ClubSyncClient({"club_sync_url": "https://x.test", "club_sync_secret": "s"})
        result = await client.push_round(9, "顶级联赛", 1, [])
        assert result["status"] == "error", result
        assert "aiohttp" in result["error"]
    finally:
        restore()


async def test_push_success_body_and_headers():
    calls, restore = install_fake_aiohttp([(200, '{"ok":true,"verdicts":[{"home":"利物浦","status":"ok"}]}')])
    try:
        client = ClubSyncClient({"club_sync_url": "https://club.example.com", "club_sync_secret": "s3cret"})
        items = [{"home": "利物浦", "away": "曼城(CPU)", "weather": "雨", "coef": 0.8342}]
        result = await client.push_round(9, "顶级联赛", 1, items)
        assert result["status"] == "ok", result
        assert result["response"]["verdicts"][0]["home"] == "利物浦"
        assert len(calls) == 1
        call = calls[0]
        assert call["url"] == "https://club.example.com" + GOLDEN_PATH
        assert call["data"].decode("utf-8") == club_sync.encode_body(
            {"season": 9, "competition": "顶级联赛", "roundNo": 1, "items": items}
        )
        # 签名吃的是发送的同一字节串
        ts = call["headers"]["x-timestamp"]
        assert call["headers"]["x-sign"] == club_sync.sign(
            "s3cret", "POST", GOLDEN_PATH, ts, call["data"].decode("utf-8")
        )
        assert ts.isdigit()
    finally:
        restore()


async def test_push_retries_5xx_then_reports_error():
    calls, restore = install_fake_aiohttp([(500, "boom")])
    try:
        client = ClubSyncClient({"club_sync_url": "https://club.example.com", "club_sync_secret": "s"})
        result = await client.push_round(9, "顶级联赛", 1, [])
        assert result["status"] == "error", result
        assert "HTTP 500" in result["error"]
        assert len(calls) == 3, calls  # 5xx 重试 3 次
    finally:
        restore()


async def test_push_4xx_no_retry():
    calls, restore = install_fake_aiohttp([(403, '{"error":"bad_signature"}')])
    try:
        client = ClubSyncClient({"club_sync_url": "https://club.example.com", "club_sync_secret": "s"})
        result = await client.push_round(9, "顶级联赛", 1, [])
        assert result["status"] == "error", result
        assert "bad_signature" in result["error"]
        assert len(calls) == 1, calls  # 4xx 确定性拒绝不重试
    finally:
        restore()


# ─── sync_weather（只推已有天气） ─────────────────────────

async def test_sync_weather_unconfigured_returns_bypass():
    env = await _sync_env({"club_sync_url": "", "club_sync_secret": ""})
    try:
        result = await env.weather_service.sync_weather()
        assert result["status"] == "bypass" and "club_sync_url" in result["reason"], result
    finally:
        await env.teardown()


async def test_sync_weather_full_flow_payload_and_idempotent():
    calls, restore = install_fake_aiohttp([(200, '{"ok":true,"verdicts":[{"home":"利物浦","status":"ok"}]}')])
    env = await _sync_env()
    try:
        result = await env.weather_service.sync_weather()
        assert result["status"] == "ok" and result["preview"] is False, result
        totals = result["totals"]
        assert totals["rounds"] == 1 and totals["matches"] == 2, totals
        assert totals["push_ok"] == 1 and totals["push_error"] == 0, totals

        # payload：season/competition/roundNo/items 恰四键；items 恰 home/away/weather/coef
        payload = json.loads(calls[0]["data"].decode("utf-8"))
        assert payload["season"] == 9 and payload["competition"] == "顶级联赛"
        assert payload["roundNo"] == 12, payload
        assert set(payload) == {"season", "competition", "roundNo", "items"}, payload
        by_home = {i["home"]: i for i in payload["items"]}
        assert set(by_home) == {"利物浦", "纽卡斯尔联"}
        assert set(by_home["利物浦"]) == {"home", "away", "weather", "coef"}
        assert by_home["利物浦"]["weather"] == "雨" and by_home["利物浦"]["coef"] == 0.8342
        assert by_home["纽卡斯尔联"]["weather"] == "晴" and by_home["纽卡斯尔联"]["coef"] == 1.02

        # 推送载荷与落库固化系数一致
        row = await env.dao.get_round_matches(9, 12, "顶级联赛")
        coefs = {m["home_team"]: m["wx_coef"] for m in row}
        assert by_home["利物浦"]["coef"] == coefs["利物浦"]
        assert by_home["纽卡斯尔联"]["coef"] == coefs["纽卡斯尔联"]

        # 只推不订正：推送全程不触发任何财务写（财务表不存在、经营列无值变化）
        fin_tables = [
            r["name"] for r in await env.db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('revenue_transactions', 'club_balance')"
            )
        ]
        assert fin_tables == [], fin_tables
        for m in row:
            for col in ("attendance", "ticket_revenue", "commercial", "broadcast"):
                assert col not in m.keys(), (m["home_team"], col)

        # 幂等：再跑一次只推不改（payload 相同、库不变）
        before = [dict(m) for m in await env.dao.list_weathered_matches()]
        again = await env.weather_service.sync_weather()
        assert again["totals"]["push_ok"] == 1
        assert json.loads(calls[1]["data"].decode("utf-8")) == payload
        assert [dict(m) for m in await env.dao.list_weathered_matches()] == before
    finally:
        restore()
        await env.teardown()


async def test_sync_weather_preview_no_request_no_write():
    calls, restore = install_fake_aiohttp([(200, '{"ok":true}')])
    env = await _sync_env()
    try:
        result = await env.weather_service.sync_weather(preview=True)
        assert result["status"] == "preview" and result["preview"] is True, result
        assert calls == [], "预览不得发请求"
        assert result["totals"]["push_ok"] == 0 and result["totals"]["push_error"] == 0
        for r in result["rounds"]:
            assert r["push"]["status"] == "preview"
        # 库不变
        rows = [dict(m) for m in await env.dao.list_weathered_matches()]
        assert {m["home_team"]: (m["weather"], m["wx_coef"]) for m in rows} == {
            "利物浦": ("雨", 0.8342), "纽卡斯尔联": ("晴", 1.02),
        }
    finally:
        restore()
        await env.teardown()


async def test_sync_weather_only_current_season_and_round_conversion():
    """M3/M6 回归：只推当前赛季；roundNo 经换算表转为 tour stage 内轮次。

    旧实现推全部赛季的已预报场次（覆盖 club 旧季记录/全部 unmatched），且直接推
    带 stage offset 的本地 round_no（club 按 tour 的 m.round 定位，次级 15+/冠军杯
    7+ 必 unmatched）。
    """
    calls, restore = install_fake_aiohttp([(200, '{"ok":true,"verdicts":[]}')])
    env = await _sync_env()
    try:
        # 旧赛季 S8 已预报行：不推，只计 skipped_other_season
        await env.dao.upsert_match(8, 11, "顶级联赛", 5, "旧季主队", "旧季客队",
                                   tour_match_id=55)
        old_row = await env.db.fetchone("SELECT id FROM matches WHERE tour_match_id=55")
        await env.dao.set_match_weather_by_id(old_row["id"], "雨", 0.85)
        # 换算表：tid=11 的本地第 12 轮 = tour stage 内第 3 轮（offset 场景）
        await env.dao.set_config("tour_round_map", '{"11": {"12": 3}}')
        # 迁移行文本「联赛」推送时归一为「顶级联赛」（club 只认三类标准名，原样推必 400）
        await env.db.execute(
            "UPDATE matches SET competition='联赛' WHERE tour_match_id=102"
        )
        result = await env.weather_service.sync_weather()
        assert result["status"] == "ok", result
        assert result["totals"]["skipped_other_season"] == 1, result["totals"]
        assert result["totals"]["rounds"] == 1 and result["totals"]["matches"] == 2
        assert len(calls) == 1, "旧赛季场次不得产生推送"
        payload = json.loads(calls[0]["data"].decode("utf-8"))
        assert payload["season"] == 9 and payload["roundNo"] == 3, payload
        assert {i["home"] for i in payload["items"]} == {"利物浦", "纽卡斯尔联"}
        # 回显点名跳过数
        from astrbot_plugin_whleague_revenue_system.handlers.admin import AdminHandler

        assert "跳过旧赛季已预报场次：1" in AdminHandler._format_sync(result)
    finally:
        restore()
        await env.teardown()


async def test_sync_weather_no_weather_raises():
    env = await TestEnv(cfg_override={
        "club_sync_url": "https://club.example.com", "club_sync_secret": "s",
    }).setup()
    try:
        try:
            await env.weather_service.sync_weather()
        except WeatherError as e:
            assert "没有已预报天气" in str(e), e
        else:
            raise AssertionError("无天气场次时应当报错")
    finally:
        await env.teardown()


async def test_handler_usage_text():
    from astrbot_plugin_whleague_revenue_system.handlers.admin import AdminHandler
    from tests.s6_handlers import _FakeEvent

    class _Plugin:
        def __init__(self):
            self.dao = None

    handler = AdminHandler(_Plugin())

    async def _allow(event):
        return None

    handler._guard = _allow  # 只测参数校验分支

    async def collect(event):
        return [r async for r in handler.weather_push(event)]

    out = await collect(_FakeEvent("主场天气推送 乱七八糟"))
    assert out and "用法" in out[0], out


async def test_format_sync_surfaces_platform_verdicts():
    """平台逐场 verdict 必须回显给人看：落库数 + 预览/旁路/失败三态。"""
    from astrbot_plugin_whleague_revenue_system.handlers.admin import AdminHandler

    def _result(verdicts, summary, *, status="ok", preview=False):
        return {
            "preview": preview,
            "rounds": [
                {
                    "season": 9,
                    "competition": "顶级联赛",
                    "round_no": 3,
                    "matches": len(verdicts),
                    "push": (
                        {"status": status}
                        if status != "ok"
                        else {"status": "ok", "response": {"ok": True, "verdicts": verdicts, "summary": summary}}
                    ),
                }
            ],
            "totals": {"rounds": 1, "matches": len(verdicts),
                       "push_ok": 1 if status == "ok" else 0, "push_error": 0 if status == "ok" else 1},
        }

    ok_text = AdminHandler._format_sync(_result(
        [{"home": "利物浦", "away": "曼城", "status": "created", "matchId": 11},
         {"home": "利物浦", "away": "曼联", "status": "unchanged", "matchId": 12}],
        {"pushed": 2},
    ))
    assert "推送 ok（平台落库 2/2 场）" in ok_text, ok_text

    assert "旁路" in AdminHandler._format_sync(_result([], {}, status="bypass"))
    preview_text = AdminHandler._format_sync(
        _result([{"home": "利物浦", "away": "曼城", "status": "created", "matchId": 11}], {"pushed": 1},
                status="preview", preview=True)
    )
    assert "预览" in preview_text and "只算不发" in preview_text, preview_text
