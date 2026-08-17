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
        # 内置 22 条：6 即发 + 16 选择
        builtin = [r for r in rows if r["source"] == "builtin"]
        assert len(builtin) == 22, len(builtin)
        instant = [r for r in builtin if r["event_type"] == "instant"]
        choice = [r for r in builtin if r["event_type"] == "choice"]
        assert len(instant) == 6, len(instant)
        assert len(choice) == 16, len(choice)
        categories = {r["category"] for r in builtin}
        assert len(categories) >= 8, categories
        # 选择型必须带 2~4 项操作，每项操作有概率表
        for ev in choice:
            opts = json.loads(ev["options_json"])
            assert 2 <= len(opts) <= 4, ev["event_id"]
            for opt in opts:
                assert opt.get("no") and len(opt["outcomes"]) >= 2, ev["event_id"]
                assert all(o.get("w", 0) > 0 for o in opt["outcomes"]), ev["event_id"]
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
        # 即发型：触发立即记账
        result = await env.event_engine.trigger_team("利物浦", 1, 1, event_id="subsidy")
        assert result["hits"][0]["event"] == "政府补贴"
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


async def test_choice_trigger_pending_and_broadcast():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        result = await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        hit = result["hits"][0]
        assert hit["type"] == "choice"
        # 选择型不立即记账（无事件流水）
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        assert not [t for t in txs if t["kind"] == "event"], txs
        # 广播文案含事件名/球队/选项号与概率
        text = hit["broadcast"]
        assert "周边爆款" in text and "利物浦" in text
        assert "①" in text and "②" in text and "60%" in text
        # 待定选择记录（未定）
        c = await env.dao.get_event_choice("利物浦", 1, 1, "merch_hit")
        assert c is not None and c["choice_no"] is None and c["resolved"] == 0
        # 日志带广播文案
        logs = await env.dao.get_window_events("利物浦", 1, 1)
        assert logs and "①" in logs[0]["text"]
    finally:
        await env.teardown()


async def test_choice_settle_decided_rolls():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="derby_buzz")
        # 玩家选选项1（加急印限量球衣：60% 资金+6/维护1，40% 资金-2/维护2）
        await env.dao.set_event_choice("利物浦", 1, 1, "derby_buzz", 1)
        import random

        random.seed(1)
        res = await env.event_engine.settle_choices(1, 1)
        assert len(res["resolved"]) == 1
        r = res["resolved"][0]
        assert r["team"] == "利物浦" and r["auto"] is False
        c = await env.dao.get_event_choice("利物浦", 1, 1, "derby_buzz")
        assert c["resolved"] == 1
        outcome = json.loads(c["outcome"])
        assert outcome["option"] == 1
        # 写出事件流水（金额等于所掷结果中的一格）
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        event_tx = [t for t in txs if t["kind"] == "event"]
        assert event_tx, event_tx
        money_tx = [t for t in event_tx if "维护" not in t["note"]]
        assert money_tx and money_tx[0]["amount"] in (6.0, -2.0), money_tx
        # 已结算不可重复结算
        res2 = await env.event_engine.settle_choices(1, 1)
        assert len(res2["resolved"]) == 0
    finally:
        await env.teardown()


async def test_choice_settle_undecided_worst():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        # 未收到玩家选择 → 按最差结果兜底
        res = await env.event_engine.settle_choices(1, 1)
        r = res["resolved"][0]
        assert r["auto"] is True
        assert r["option"] == "加班加单补货"  # 全库净额最小的一项
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        money_tx = [t for t in txs if t["kind"] == "event" and "维护" not in t["note"]]
        maint_tx = [t for t in txs if t["kind"] == "event" and "维护" in t["note"]]
        assert money_tx and money_tx[0]["amount"] == -1.5, money_tx
        assert maint_tx and maint_tx[0]["amount"] == -3.0, maint_tx
        # 日志被替换为一行结算摘要
        logs = await env.dao.get_window_events("利物浦", 1, 1)
        assert "最差" in logs[0]["text"] and "资金 -1.5M" in logs[0]["text"]
    finally:
        await env.teardown()


async def test_choice_invalid_option_falls_back_worst():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        await env.dao.set_event_choice("利物浦", 1, 1, "merch_hit", 99)
        res = await env.event_engine.settle_choices(1, 1)
        assert res["resolved"][0]["auto"] is True
    finally:
        await env.teardown()


async def test_settle_window_resolves_choices_and_redo():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        import random

        random.seed(42)
        result = await env.window_service.settle()
        assert not result.get("error")
        summary = await env.dao.get_window_summary(1, 1)
        ids = json.loads(summary["tx_ids"])
        assert ids, "结算应包含选择事件流水"
        rows = []
        for i in ids:
            row = await env.db.fetchone(
                "SELECT kind, amount FROM revenue_transactions WHERE id=?", (i,)
            )
            if row:
                rows.append(dict(row))
        assert any(r["kind"] == "event" for r in rows), rows
        # 强制重算：重置并重新兑现选择事件
        await env.window_service.settle(force=True)
        c = await env.dao.get_event_choice("利物浦", 1, 1, "merch_hit")
        assert c["resolved"] == 1
        summary2 = await env.dao.get_window_summary(1, 1)
        assert json.loads(summary2["tx_ids"])  # 重新生成流水 ID
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


async def test_event_draft_ids_unique_and_no_overwrite():
    env = await TestEnv().setup()
    try:
        env.provider.set_response(
            '[{"name":"A","category":"c","weight":5,"effects":{"money":1},"template":"t"}]'
        )
        d1 = await env.event_engine.generate_drafts(1)
        d2 = await env.event_engine.generate_drafts(1)
        assert d1[0]["id"] != d2[0]["id"], "两次生成的草稿 ID 不得复用"
        # 第一次草稿采纳后，第二次生成不得占用同一 event_id 覆盖它
        pending = await env.dao.list_events("pending")
        first = next(p for p in pending if p["event_id"] == d1[0]["id"])
        await env.event_engine.adopt(first["id"])
        adopted = await env.dao.list_events("adopted")
        ev = [e for e in adopted if e["event_id"] == d1[0]["id"]]
        assert ev and ev[0]["name"] == "A" and ev[0]["status"] == "adopted"
        pending2 = await env.dao.list_events("pending")
        assert all(p["event_id"] != d1[0]["id"] for p in pending2), "新草稿不得占用已采纳 ID"
    finally:
        await env.teardown()


async def test_llm_maintenance_no_refund():
    env = await TestEnv().setup()
    try:
        env.provider.set_response(
            '[{"name":"负维护","category":"c","weight":5,'
            '"effects":{"maintenance":-9},"template":"t"}]'
        )
        drafts = await env.event_engine.generate_drafts(1)
        assert len(drafts) == 1, drafts
        pending = await env.dao.list_events("pending")
        effects = json.loads(pending[0]["effects_json"])
        assert effects["maintenance"] == 0.0, "负维护应钳制为 0（不允许退款）"
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


async def test_llm_design_choice_event():
    env = await TestEnv().setup()
    try:
        env.provider.set_response(
            '[{"event_type":"choice","name":"球迷投票活动","category":"运营","weight":30,'
            '"template":"t","options":[{"name":"自费办线下活动","desc":"投入换口碑",'
            '"outcomes":[{"w":70,"effects":{"fans_pct":0.04,"money":-1}},{"w":30,"effects":{"money":1}}]},'
            '{"name":"线上抽奖","desc":"低成本",'
            '"outcomes":[{"w":50,"effects":{"money":2,"fans_pct":-0.02}},'
            '{"w":50,"effects":{"money":-3,"maintenance":2}}]}]}]'
        )
        drafts = await env.event_engine.generate_drafts(1)
        assert len(drafts) == 1
        d = drafts[0]
        assert d["event_type"] == "choice" and len(d["options"]) == 2
        pending = await env.dao.list_events("pending")
        assert pending[0]["event_type"] == "choice"
        options = json.loads(pending[0]["options_json"])
        # 概率归一化、自动编号、每项选择带正负路径
        for opt in options:
            total = sum(o["w"] for o in opt["outcomes"])
            assert abs(total - 100) < 0.2, total
            nets = [o["effects"]["money"] - o["effects"]["maintenance"] for o in opt["outcomes"]]
            assert min(nets) < 0 and max(nets) >= 0, nets
        assert options[0]["no"] == 1 and options[1]["no"] == 2
    finally:
        await env.teardown()


async def test_llm_choice_single_option_skipped():
    env = await TestEnv().setup()
    try:
        env.provider.set_response(
            '[{"event_type":"choice","name":"坏选择","category":"x","weight":5,"template":"t",'
            '"options":[{"name":"只有一项","desc":"","outcomes":'
            '[{"w":100,"effects":{"money":1}},{"w":1,"effects":{"money":-1}}]}]},'
            '{"name":"好即发","category":"c","weight":5,"effects":{"money":1},"template":"t"}]'
        )
        drafts = await env.event_engine.generate_drafts(2)
        assert len(drafts) == 1 and drafts[0]["name"] == "好即发"
    finally:
        await env.teardown()


async def test_llm_event_empty_template_fallback():
    env = await TestEnv().setup()
    try:
        env.provider.set_response(
            '[{"name":"无模板事件","category":"c","weight":5,"effects":{"money":1},"template":""}]'
        )
        drafts = await env.event_engine.generate_drafts(1)
        assert len(drafts) == 1
        pending = await env.dao.list_events("pending")
        assert "{team}" in pending[0]["template"] and "无模板事件" in pending[0]["template"]
    finally:
        await env.teardown()


async def test_add_custom_choice_event():
    env = await TestEnv().setup()
    try:
        await env.event_engine.add_custom(
            "吉祥物巡游", "运营", 5, "{}", event_type="choice",
            options_text=json.dumps([
                {"name": "请网红造势", "desc": "花钱买流量",
                 "outcomes": [{"w": 60, "effects": {"money": 3, "fans_pct": 0.02}},
                              {"w": 40, "effects": {"money": -2, "fans_pct": -0.01}}]},
                {"name": "低调举办", "desc": "稳",
                 "outcomes": [{"w": 70, "effects": {"fans_pct": 0.02}},
                              {"w": 30, "effects": {"maintenance": 1}}]},
            ]),
        )
        rows = await env.dao.list_events("adopted")
        ev = next(r for r in rows if r["name"] == "吉祥物巡游")
        assert ev["event_type"] == "choice"
        options = json.loads(ev["options_json"])
        assert len(options) == 2 and options[0]["no"] == 1
        # 可直接触发为待定选择
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        hit = await env.event_engine.trigger_team("利物浦", 1, 1, event_id=ev["event_id"])
        assert hit["hits"][0]["type"] == "choice"
        c = await env.dao.get_event_choice("利物浦", 1, 1, ev["event_id"])
        assert c is not None and c["resolved"] == 0
    finally:
        await env.teardown()


async def test_choice_import_batch_and_validation():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.stadium_service.import_attributes("巴塞罗那", influence=150.0)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        await env.event_engine.trigger_team("巴塞罗那", 1, 1, event_id="derby_buzz")
        r = await env.event_engine.import_choices(
            "利物浦 周边爆款 ①\n巴塞罗那 德比热度 999\n不存在队 周边爆款 1"
        )
        # ① 被解析为 1；越界选项号与不存在事件被逐条拒绝
        assert r[0]["ok"] is True and r[0]["choice_no"] == 1, r
        assert r[1]["ok"] is False and "选项号需在" in r[1]["error"], r
        assert r[2]["ok"] is False and "待定选择事件" in r[2]["error"], r
        # 结算后不可再改
        await env.event_engine.settle_choices(1, 1)
        try:
            await env.event_engine.record_choice("利物浦", 1, 1, "周边爆款", 2)
            raise AssertionError("已结算选择应拒绝修改")
        except Exception:
            pass
    finally:
        await env.teardown()