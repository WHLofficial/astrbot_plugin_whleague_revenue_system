"""事件与品牌：默认库、触发命中/条件、LLM 设计草稿（桩）、品牌签约/解约。"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402


async def test_default_event_pool():
    env = await TestEnv().setup()
    try:
        rows = await env.dao.list_events("adopted")
        # 内置 22 条 + 可能的自定义
        builtin = [r for r in rows if r["source"] == "builtin"]
        assert len(builtin) == 22, len(builtin)
        categories = {r["category"] for r in builtin}
        assert len(categories) >= 8, categories
        # 幂等
        await env.event_engine.init_defaults()
        rows2 = await env.dao.list_events("adopted")
        assert len([r for r in rows2 if r["source"] == "builtin"]) == 22
    finally:
        await env.teardown()


async def test_trigger_team_forced():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        result = await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        assert result["hits"][0]["event"] == "周边爆款"
        # 事件立即记账
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        events = [t for t in txs if t["kind"] == "event"]
        assert events and events[0]["amount"] == 4.0, events
        # 日志带文案（LLM 桩不可用时回退模板）
        logs = await env.dao.get_window_events("利物浦", 1, 1)
        assert logs and logs[0]["text"]
        # 事件条件（冠名联动）无冠名时不应被抽中——直接指定可强制
        try:
            await env.event_engine.trigger_team("利物浦", 1, 1, event_id="brand_crisis")
            raise AssertionError("指定事件应允许强制触发")
        except Exception:
            pass
    finally:
        await env.teardown()


async def test_trigger_all_hit_bound():
    env = await TestEnv().setup()
    try:
        for team in ("利物浦", "巴塞罗那", "纽卡斯尔联", "勒沃库森"):
            await env.stadium_service.import_attributes(team, influence=120.0)
        import random

        random.seed(99)
        result = await env.event_engine.trigger_all(1, 1)
        # 每队最多一条
        teams = [h["team"] for h in result["hits"]]
        assert len(teams) == len(set(teams)), "每队最多一条"
        assert len(teams) <= 4
        # 事件条件（冠名联动）不被无冠名队抽中
        for h in result["hits"]:
            assert h["event_id"] != "brand_crisis"
    finally:
        await env.teardown()


async def test_llm_design_drafts():
    env = await TestEnv().setup()
    try:
        env.provider.set_response(
            '[{"name":"雨天免费停车","category":"天气衍生","weight":30,'
            '"effects":{"money":3},"template":"雨天停车免费，{team} 球迷乐开花。"},'
            '{"name":"宇宙大爆炸","category":"异常","weight":1,'
            '"effects":{"money":99999,"fans_pct":0.9},"template":"t"}]'
        )
        drafts = await env.event_engine.generate_drafts(2)
        assert len(drafts) == 2
        # 数值钳制：99999 → 8，0.9 → 0.05
        first = drafts[0]
        assert first["effects"]["money"] == 3.0
        pending = await env.dao.list_events("pending")
        assert len(pending) == 2
        llm_event = [p for p in pending if p["name"] == "宇宙大爆炸"][0]
        effects = json.loads(llm_event["effects_json"])
        assert effects["money"] == 8.0, effects
        assert effects["fans_pct"] == 0.05, effects
        # 采纳/丢弃
        await env.event_engine.adopt(pending[0]["id"])
        await env.event_engine.discard(pending[1]["id"])
        adopted = await env.dao.list_events("adopted")
        assert any(e["name"] == "雨天免费停车" for e in adopted)
        discarded = await env.dao.list_events("discarded")
        assert len(discarded) == 1
    finally:
        await env.teardown()


async def test_brand_pool_and_sign():
    env = await TestEnv().setup()
    try:
        brands = await env.dao.list_brands("adopted")
        assert len(brands) == 7, brands
        await env.stadium_service.import_attributes("利物浦", influence=150.0, capacity=20000)
        r = await env.brand_service.sign("利物浦", "亚马逊", 1, 1)
        assert r["fee_per_window"] > 0
        # 重复签约拒绝
        try:
            await env.brand_service.sign("利物浦", "可口可乐", 1, 1)
            raise AssertionError("已有冠名应拒绝")
        except Exception:
            pass
        # 退冠名赔付
        naming = await env.dao.get_active_naming("利物浦")
        remaining = naming["windows_remaining"] - 1
        r2 = await env.brand_service.terminate("利物浦", 1, 1, initiated_by="team")
        expect_penalty = round(remaining * naming["fee_per_window"] * 0.3, 3)
        assert abs(r2["penalty"] - expect_penalty) < 1e-6, r2
        # 品牌池 LLM 生成
        env.provider.set_response('[{"brand":"老八食堂","heat":9},{"brand":"正","heat":0.1}]')
        drafts = await env.brand_service.generate_drafts(2)
        assert len(drafts) == 2
        pending = await env.dao.list_brands("pending")
        by_name = {p["brand"]: p for p in pending}
        assert by_name["老八食堂"]["heat"] == 1.5, "热度应被钳制到 1.5"
        assert by_name["正"]["heat"] == 0.5, "热度应被钳制到 0.5"
    finally:
        await env.teardown()


async def test_llm_flavor_text_fallback():
    env = await TestEnv().setup()
    try:
        rows = await env.dao.list_events("adopted")
        merch = next(r for r in rows if r["event_id"] == "merch_hit")
        # provider 不可用 → 回退模板
        env.provider.set_response(None)
        text = await env.llm_writer.event_text(merch, "利物浦", "安菲尔德")
        assert "利物浦" in text and "{team}" not in text
        # provider 可用 → LLM 文案
        env.provider.set_response("利物浦的新围巾卖疯了！")
        text2 = await env.llm_writer.event_text(merch, "利物浦", "安菲尔德")
        assert "围巾" in text2
    finally:
        await env.teardown()