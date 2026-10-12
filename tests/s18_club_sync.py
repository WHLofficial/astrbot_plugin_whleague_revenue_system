"""天气同步测试：签名金标准、推送三态、补抽幂等、旧账订正守恒幂等、预览只读。"""

import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services import club_sync, formula  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.club_sync import ClubSyncClient  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.fixture_service import FixtureError  # noqa: E402

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
    cfg = {"club_sync_url": "https://club.example.com", "club_sync_secret": "s3cret"}
    cfg.update(cfg_extra or {})
    env = await TestEnv(cfg_override=cfg).setup()
    await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那\n1 纽卡斯尔联 勒沃库森\n")
    await env.fixture_service.forecast_round(1)
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


# ─── 补抽与订正 ───────────────────────────────────────────

async def test_fill_wx_coef_idempotent():
    env = await _sync_env()
    try:
        matches = await env.dao.get_round_matches(1, 1, 1)
        m = matches[0]
        await env.db.execute("UPDATE matches SET wx_coef=NULL WHERE id=?", (m["id"],))
        assert await env.dao.fill_wx_coef(m["id"], 0.9123) == 1
        assert await env.dao.fill_wx_coef(m["id"], 0.5555) == 0
        row = await env.dao.get_match(m["id"])
        assert row["wx_coef"] == 0.9123
    finally:
        await env.teardown()


async def test_weather_correction_atomic_and_idempotent():
    env = await _sync_env()
    try:
        await env.fixture_service.record_results(1, "利物浦 胜 2-1")
        matches = await env.dao.get_round_matches(1, 1, 1)
        m = [x for x in matches if x["home_team"] == "利物浦"][0]
        await env.db.execute(
            "UPDATE matches SET wx_coef=NULL, attendance=1000, ticket_revenue=1.0, "
            "commercial=0.0, broadcast=0.0 WHERE id=?", (m["id"],),
        )
        balance_before = (await env.dao.get_balance("利物浦"))["balance"]
        entries = [("ticket", 2.5, f"天气订正|match:{m['id']}", 1)]
        ok = await env.dao.apply_weather_correction(
            m["id"], 0.8, 21000, 3.5, 1.0, 0.0, "利物浦", 1, 1, entries, 1000,
        )
        assert ok is True
        row = await env.dao.get_match(m["id"])
        assert row["wx_coef"] == 0.8 and row["attendance"] == 21000
        assert row["ticket_revenue"] == 3.5 and row["commercial"] == 1.0
        assert (await env.dao.get_balance("利物浦"))["balance"] == balance_before + 2.5
        assert await env.dao.has_txn_note(f"天气订正|match:{m['id']}")

        # 二次调用：守卫 wx_coef IS NULL 失败 ⇒ False，且不重复记账
        again = await env.dao.apply_weather_correction(
            m["id"], 0.9, 9999, 9.9, 9.9, 9.9, "利物浦", 1, 1,
            [("ticket", 9.9, f"天气订正|match:{m['id']}", 1)], 21000,
        )
        assert again is False
        row2 = await env.dao.get_match(m["id"])
        assert row2["attendance"] == 21000 and row2["ticket_revenue"] == 3.5
        assert (await env.dao.get_balance("利物浦"))["balance"] == balance_before + 2.5
    finally:
        await env.teardown()


async def test_sync_weather_unconfigured_raises():
    env = await _sync_env({"club_sync_url": "", "club_sync_secret": ""})
    try:
        try:
            await env.fixture_service.sync_weather()
        except FixtureError as e:
            assert "club_sync_url" in str(e), e
        else:
            raise AssertionError("未配置时应当报错")
    finally:
        await env.teardown()


async def test_sync_weather_full_flow_and_idempotent():
    calls, restore = install_fake_aiohttp([(200, '{"ok":true,"verdicts":[{"home":"利物浦","status":"ok"}]}')])
    env = await _sync_env()
    try:
        await env.fixture_service.record_results(1, "利物浦 胜 2-1")
        matches = await env.dao.get_round_matches(1, 1, 1)
        settled = [x for x in matches if x["home_team"] == "利物浦"][0]
        pending = [x for x in matches if x["home_team"] == "纽卡斯尔联"][0]
        # 模拟 v13 前旧账：系数未落库；已结算场次另塞入错误收入值
        await env.db.execute("UPDATE matches SET wx_coef=NULL WHERE id=?", (pending["id"],))
        await env.db.execute(
            "UPDATE matches SET wx_coef=NULL, attendance=1000, ticket_revenue=1.0, "
            "commercial=0.0, broadcast=0.0 WHERE id=?", (settled["id"],),
        )
        balance_before = (await env.dao.get_balance("利物浦"))["balance"]

        result = await env.fixture_service.sync_weather()
        assert result["preview"] is False
        totals = result["totals"]
        assert totals["rounds"] == 1 and totals["matches"] == 2, totals
        assert totals["fixed"] == 2 and totals["corrected"] == 1, totals
        assert totals["push_ok"] == 1 and totals["push_error"] == 0, totals

        row = await env.dao.get_match(settled["id"])
        assert row["wx_coef"] is not None
        assert row["attendance"] != 1000 and row["ticket_revenue"] != 1.0
        pending_row = await env.dao.get_match(pending["id"])
        assert pending_row["wx_coef"] is not None and pending_row["result"] is None

        # 账本：订正流水与余额增量一致
        txn = await env.dao.list_transactions("利物浦", limit=50)
        fixes = [t for t in txn if t["note"] == f"天气订正|match:{settled['id']}"]
        assert fixes, txn
        delta = round(sum(t["amount"] for t in fixes), 4)
        assert delta == result["totals"]["delta"], (delta, result["totals"]["delta"])
        assert (await env.dao.get_balance("利物浦"))["balance"] == round(balance_before + delta, 4)

        # 推送载荷与落库系数一致
        payload = json.loads(calls[0]["data"].decode("utf-8"))
        assert payload["roundNo"] == 1, payload
        assert payload["competition"] == result["rounds"][0]["competition"], payload
        assert payload["season"] == 1, payload
        by_home = {i["home"]: i for i in payload["items"]}
        assert by_home["利物浦"]["coef"] == row["wx_coef"]
        assert by_home["纽卡斯尔联"]["coef"] == pending_row["wx_coef"]

        # 再跑一次：不再补抽、不再订正、余额不变（幂等）
        txn_count = len(txn)
        again = await env.fixture_service.sync_weather()
        assert again["totals"]["fixed"] == 0 and again["totals"]["corrected"] == 0, again["totals"]
        assert (await env.dao.get_balance("利物浦"))["balance"] == round(balance_before + delta, 4)
        assert len(await env.dao.list_transactions("利物浦", limit=50)) == txn_count
    finally:
        restore()
        await env.teardown()


async def test_sync_weather_cancelled_skips_correction():
    calls, restore = install_fake_aiohttp([(200, '{"ok":true}')])
    env = await _sync_env()
    try:
        await env.fixture_service.record_results(1, "利物浦 取消")
        matches = await env.dao.get_round_matches(1, 1, 1)
        m = [x for x in matches if x["home_team"] == "利物浦"][0]
        assert m["result"] == "C"
        await env.db.execute("UPDATE matches SET wx_coef=NULL WHERE id=?", (m["id"],))
        txn_before = len(await env.dao.list_transactions("利物浦", limit=50))
        result = await env.fixture_service.sync_weather()
        assert result["totals"]["fixed"] == 1 and result["totals"]["corrected"] == 0, result["totals"]
        assert (await env.dao.get_match(m["id"]))["wx_coef"] is not None
        assert len(await env.dao.list_transactions("利物浦", limit=50)) == txn_before
    finally:
        restore()
        await env.teardown()


async def test_sync_weather_preview_readonly():
    calls, restore = install_fake_aiohttp([(200, '{"ok":true}')])
    env = await _sync_env()
    try:
        await env.fixture_service.record_results(1, "利物浦 胜 2-1")
        matches = await env.dao.get_round_matches(1, 1, 1)
        settled = [x for x in matches if x["home_team"] == "利物浦"][0]
        pending = [x for x in matches if x["home_team"] == "纽卡斯尔联"][0]
        await env.db.execute(
            "UPDATE matches SET wx_coef=NULL, attendance=1000, ticket_revenue=1.0, "
            "commercial=0.0, broadcast=0.0 WHERE id=?", (settled["id"],),
        )
        await env.db.execute("UPDATE matches SET wx_coef=NULL WHERE id=?", (pending["id"],))
        balance_before = (await env.dao.get_balance("利物浦"))["balance"]

        result = await env.fixture_service.sync_weather(preview=True)
        assert result["preview"] is True
        assert result["totals"]["corrected"] == 1 and result["totals"]["push_ok"] == 0
        assert calls == []  # 预览不推送
        assert (await env.dao.get_match(settled["id"]))["wx_coef"] is None
        assert (await env.dao.get_match(pending["id"]))["wx_coef"] is None
        assert (await env.dao.get_balance("利物浦"))["balance"] == balance_before
        assert not await env.dao.has_txn_note(f"天气订正|match:{settled['id']}")
        # 预览给出预估差异（旧值 1000 座 vs 重算值）
        entry = result["rounds"][0]["corrected"][0]
        assert entry["attendance"][0] == 1000 and entry["attendance"][1] > 0
    finally:
        restore()
        await env.teardown()


async def test_sync_weather_no_weather_raises():
    env = await TestEnv(cfg_override={
        "club_sync_url": "https://club.example.com", "club_sync_secret": "s",
    }).setup()
    try:
        try:
            await env.fixture_service.sync_weather()
        except FixtureError as e:
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
    """平台逐场 verdict 必须回显给人看：created/replaced/unchanged 算落库，其余点名列原因。"""
    from astrbot_plugin_whleague_revenue_system.handlers.admin import AdminHandler

    def _result(verdicts, summary, *, status="ok", preview=False, corrected=()):
        return {
            "preview": preview,
            "rounds": [
                {
                    "season": 9,
                    "competition": "顶级联赛",
                    "round_no": 3,
                    "matches": len(verdicts),
                    "fixed": 1,
                    "corrected": list(corrected),
                    "skipped": [],
                    "push": (
                        {"status": status}
                        if status != "ok"
                        else {"status": "ok", "competition": "顶级联赛", "response": {"ok": True, "verdicts": verdicts, "summary": summary}}
                    ),
                }
            ],
            "totals": {"rounds": 1, "matches": len(verdicts), "fixed": 1, "corrected": len(corrected), "delta": 0.0,
                       "push_ok": 1 if status == "ok" else 0, "push_error": 0 if status == "ok" else 1},
        }

    mixed = AdminHandler._format_sync(
        _result(
            [
                {"home": "利物浦", "away": "曼城", "status": "created", "matchId": 11},
                {"home": "利物浦", "away": "曼联", "status": "unmatched", "detail": "该轮没有这对主客队"},
                {"home": "阿森纳", "away": "曼城", "status": "skipped_no_stadium", "detail": "主队没有球场行"},
            ],
            {"created": 1, "replaced": 0, "unchanged": 0, "unmatched": 1, "skipped_no_stadium": 1, "bad_weather": 0,
             "settled": 0, "pushed": 1},
        )
    )
    assert "平台落库 1/3 场" in mixed, mixed
    assert "created×" not in mixed, mixed  # created 是落库成功，不许被算成未落库
    assert "unmatched×1" in mixed and "skipped_no_stadium×1" in mixed, mixed
    assert "利物浦 vs 曼联：unmatched——该轮没有这对主客队" in mixed, mixed

    all_landed = AdminHandler._format_sync(
        _result(
            [{"home": "利物浦", "away": "曼城", "status": "created", "matchId": 11},
             {"home": "阿森纳", "away": "曼城", "status": "unchanged", "matchId": 12}],
            {"created": 1, "replaced": 0, "unchanged": 1, "unmatched": 0, "skipped_no_stadium": 0, "bad_weather": 0,
             "settled": 1, "pushed": 2},
        )
    )
    assert "平台落库 2/2 场" in all_landed, all_landed
    assert "1 场已结算（历史账待订正批收敛）" in all_landed, all_landed
    assert "未落库" not in all_landed, all_landed

    assert "旁路" in AdminHandler._format_sync(_result([], {}, status="bypass"))
    preview_text = AdminHandler._format_sync(
        _result([{"home": "利物浦", "away": "曼城", "status": "created", "matchId": 11}], {"pushed": 1}, status="preview", preview=True)
    )
    assert "预览" in preview_text and "只算不写" in preview_text, preview_text


async def test_formula_roll_weather_with_coef():
    from astrbot_plugin_whleague_revenue_system.config.defaults import DEFAULT_CONFIG

    cfg = dict(DEFAULT_CONFIG)
    weather, coef = formula.roll_weather_with_coef(cfg)
    assert weather in ("晴", "多云", "雨", "雪"), weather
    lo, hi = formula.weather_ranges(cfg)[weather]
    assert lo <= coef <= hi, (weather, coef, lo, hi)
    assert coef == round(coef, 4), coef
    # 带系数结算：同系数两次调用上座差异只来自扰动（±3% 内），无系数回落不报错
    args = dict(fans=20000.0, capacity=40000, tier=2, weather=weather,
                form_pts_value=5, home_influence=90.0, away_influence=88.0)
    a = formula.attendance(cfg, wx_coef=coef, **args)
    b = formula.attendance(cfg, wx_coef=coef, **args)
    assert abs(a - b) <= max(a, b) * 0.07, (a, b)
    assert formula.attendance(cfg, wx_coef=None, **args) >= 0
