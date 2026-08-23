"""事件与品牌：默认库、触发命中/条件、LLM 设计草稿（桩）、品牌签约/解约。"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402


class _RetryProvider:
    """首次返回空（模拟空内容），第二次起返回预设 JSON。"""

    def __init__(self, response: str):
        self._response = response
        self.calls = 0
        self.prompts = []

    async def text_chat(self, prompt, session_id=""):
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            return type("R", (), {"result_str": ""})()
        return type("R", (), {"result_str": self._response})()


class _EmptyProvider:
    def __init__(self):
        self.calls = 0

    async def text_chat(self, prompt, session_id=""):
        self.calls += 1
        return type("R", (), {"result_str": ""})()


class _CaptureProvider:
    """记录每次 prompt，便于断言 prompt 内容。"""

    def __init__(self, response: str):
        self._response = response
        self.prompts = []
        self.calls = 0

    async def text_chat(self, prompt, session_id=""):
        self.calls += 1
        self.prompts.append(prompt)
        return type("R", (), {"result_str": self._response})()


class _CompletionTextProvider:
    """模拟新版 AstrBot LLMResponse：只带 completion_text、无 result_str。"""

    def __init__(self, response: str):
        self._response = response
        self.calls = 0

    async def text_chat(self, prompt, session_id=""):
        self.calls += 1
        return type("LLMResponseLike", (), {"completion_text": self._response,
                                            "result_chain": None})()


class _PlainStrProvider:
    """个别 provider 直接返回 str。"""

    def __init__(self, response: str):
        self._response = response
        self.calls = 0

    async def text_chat(self, prompt, session_id=""):
        self.calls += 1
        return self._response


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
        env.provider.set_response("围巾印到一半，机器突然停了，加单还是不加单？")
        result = await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        hit = result["hits"][0]
        assert hit["type"] == "choice"
        # 选择型不立即记账（无事件流水）
        txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
        assert not [t for t in txs if t["kind"] == "event"], txs
        # 广播 = LLM 叙述 + 选项列表（不含概率）
        text = hit["broadcast"]
        assert "周边爆款" in text and "利物浦" in text
        assert "围巾印到一半" in text  # LLM 叙述被采用
        assert "①" in text and "②" in text
        assert "60%" not in text and "资金" not in text  # 概率/金额不出现在广播
        # 待定选择记录（未定）
        c = await env.dao.get_event_choice("利物浦", 1, 1, "merch_hit")
        assert c is not None and c["choice_no"] is None and c["resolved"] == 0
        # 日志带广播文案
        logs = await env.dao.get_window_events("利物浦", 1, 1)
        assert logs and "①" in logs[0]["text"]
    finally:
        await env.teardown()


async def test_cancel_instant_event_reverses_effects():
    """取消即发事件：删流水重算余额、死忠/上座修正按原比例回退、日志删除。"""
    from astrbot_plugin_whleague_revenue_system.services.event_engine import EventError

    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        bal_before = (await env.dao.get_balance("利物浦"))["balance"]
        fans_before = (await env.dao.get_stadium("利物浦"))["fans_diehards"]
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="tifo_viral")  # +1.0M 死忠+3%
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="storm_buzz")  # 上座×0.85

        # 按【名】取消 TIFO出圈：资金流水删除+余额复原+死忠回退，不影响另一事件
        res = await env.event_engine.cancel_team_event("利物浦", "TIFO出圈", 1, 1)
        assert res["kind"] == "instant" and res["event"] == "TIFO出圈"
        txs = [t for t in await env.dao.list_transactions("利物浦", season=1, window_seq=1)
               if t["kind"] == "event"]
        assert not [t for t in txs if t["note"] == "TIFO出圈"], txs
        assert abs((await env.dao.get_balance("利物浦"))["balance"] - bal_before) < 1e-6
        st = await env.dao.get_stadium("利物浦")
        assert abs(st["fans_diehards"] - fans_before) < 1e-6  # ×1.03 后回退复原
        assert abs(st["next_attendance_mod"] - 0.85) < 1e-9  # storm_buzz 的修正未动

        # 按【id】取消 暴雨滂沱：上座修正回退归 1，日志全部清空
        res2 = await env.event_engine.cancel_team_event("利物浦", "storm_buzz", 1, 1)
        assert res2["kind"] == "instant"
        st2 = await env.dao.get_stadium("利物浦")
        assert abs(st2["next_attendance_mod"] - 1.0) < 1e-9
        assert await env.dao.get_window_events("利物浦", 1, 1) == []

        # 再取消 → 报错（无任何分配）
        try:
            await env.event_engine.cancel_team_event("利物浦", "TIFO出圈", 1, 1)
            assert False, "应无可取消事件"
        except EventError as e:
            assert "没有分配任何事件" in str(e)
    finally:
        await env.teardown()


async def test_cancel_instant_latest_occurrence_only():
    """同名即发事件多次触发：每次取消最新一条，流水逐条回退。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        bal0 = (await env.dao.get_balance("利物浦"))["balance"]
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="subsidy")  # +4.0
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="subsidy")  # +4.0

        res = await env.event_engine.cancel_team_event("利物浦", "subsidy", 1, 1)
        assert res["occurrences"] == 1
        assert abs((await env.dao.get_balance("利物浦"))["balance"] - (bal0 + 4.0)) < 1e-6

        res2 = await env.event_engine.cancel_team_event("利物浦", "政府补贴", 1, 1)  # 按中文名
        assert res2["occurrences"] == 0
        assert abs((await env.dao.get_balance("利物浦"))["balance"] - bal0) < 1e-6
    finally:
        await env.teardown()


async def test_cancel_choice_pending_and_refused_when_resolved():
    """选择型取消：待定/已选未结可删行重触发；已结算拒绝并引导强制重算。"""
    from astrbot_plugin_whleague_revenue_system.services.event_engine import EventError

    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        # 待定取消：删选择行+广播日志，无流水
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        res = await env.event_engine.cancel_team_event("利物浦", "周边爆款", 1, 1)
        assert res["kind"] == "choice" and res["removed_logs"] >= 1
        assert await env.dao.get_event_choice("利物浦", 1, 1, "merch_hit") is None
        assert await env.dao.get_window_events("利物浦", 1, 1) == []
        assert not [t for t in await env.dao.list_transactions("利物浦", season=1, window_seq=1)
                    if t["kind"] == "event"]
        # 重新触发 → 全新待定；已选未结也可取消（按 id 引用）
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        await env.dao.set_event_choice("利物浦", 1, 1, "merch_hit", 1)
        res2 = await env.event_engine.cancel_team_event("利物浦", "merch_hit", 1, 1)
        assert res2["kind"] == "choice"
        assert await env.dao.get_event_choice("利物浦", 1, 1, "merch_hit") is None
        # 已结算 → 拒绝
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        await env.dao.set_event_choice("利物浦", 1, 1, "merch_hit", 1)
        await env.event_engine.settle_now(1, 1)
        try:
            await env.event_engine.cancel_team_event("利物浦", "周边爆款", 1, 1)
            assert False, "已结算应拒绝取消"
        except EventError as e:
            assert "强制" in str(e)
    finally:
        await env.teardown()


async def test_cancel_attendance_mod_consumed():
    """上座修正已随赛果消耗：取消时跳过回退并提示，日志照删。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="storm_buzz")  # 上座×0.85
        await env.dao.reset_attendance_mod("利物浦")  # 模拟赛果录入后修正被消耗归 1
        res = await env.event_engine.cancel_team_event("利物浦", "暴雨滂沱", 1, 1)
        assert any("消耗" in w for w in res["warnings"]), res
        assert (await env.dao.get_stadium("利物浦"))["next_attendance_mod"] == 1.0
        assert await env.dao.get_window_events("利物浦", 1, 1) == []
    finally:
        await env.teardown()


async def test_list_team_events_and_unknown_ref():
    """列出该队本窗口事件（类型/状态）；未知引用的错误信息列出可取消项。"""
    from astrbot_plugin_whleague_revenue_system.services.event_engine import EventError

    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        assert await env.event_engine.list_team_events("利物浦", 1, 1) == []
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="subsidy")
        rows = {r["event_id"]: r for r in
                await env.event_engine.list_team_events("利物浦", 1, 1)}
        assert rows["merch_hit"]["kind"] == "选择" and rows["merch_hit"]["status"] == "待定"
        assert rows["subsidy"]["kind"] == "即发" and rows["subsidy"]["status"] == "已生效"
        try:
            await env.event_engine.cancel_team_event("利物浦", "不存在的事件", 1, 1)
            assert False, "未知引用应报错"
        except EventError as e:
            assert "周边爆款" in str(e) and "政府补贴" in str(e)  # 错误信息列出可取消项
    finally:
        await env.teardown()


async def test_settle_now_decided_and_all():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.stadium_service.import_attributes("巴塞罗那", influence=150.0)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        await env.event_engine.trigger_team("巴塞罗那", 1, 1, event_id="new_wave")
        await env.dao.set_event_choice("利物浦", 1, 1, "merch_hit", 1)
        import random

        random.seed(3)
        # 只结已导入选择
        res = await env.event_engine.settle_now(1, 1)
        assert len(res["resolved"]) == 1 and res["resolved"][0]["team"] == "利物浦"
        assert not res["resolved"][0]["auto"]
        remain = await env.dao.get_unresolved_choices(1, 1)
        assert [c for c in remain if c["team_name"] == "巴塞罗那"], "未定应保留"
        # 全部结算（连同未定按最差兜底）
        res2 = await env.event_engine.settle_now(1, 1, include_undecided=True)
        assert [r for r in res2["resolved"] if r["team"] == "巴塞罗那"]
        assert await env.dao.get_unresolved_choices(1, 1) == []
    finally:
        await env.teardown()


async def test_settle_result_text_replaces_broadcast():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        await env.dao.set_event_choice("利物浦", 1, 1, "merch_hit", 1)
        await env.event_engine.settle_now(1, 1)
        logs = await env.dao.get_window_events("利物浦", 1, 1)
        text = logs[0]["text"]
        # 结算后日志为结果短文案（回退摘要），不再是广播（不含「请在窗口结算前」）
        assert "请在窗口结算前" not in text
        assert ("周边爆款" in text) and ("选1" in text or "自动最差" in text)
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


async def test_event_name_uniqueness_guard():
    """重名防护：手写与采纳均拒绝与池内已有事件同名（防流水 note 撞名/按名匹配歧义）。"""
    from astrbot_plugin_whleague_revenue_system.services.event_engine import EventError

    env = await TestEnv().setup()
    try:
        # 手写重名（内置已有「政府补贴」）→ 拒绝；换名成功
        try:
            await env.event_engine.add_custom("政府补贴", "分类", 5, '{"money": 3}')
            assert False, "重名手写应拒绝"
        except EventError as e:
            assert "已存在" in str(e)
        await env.event_engine.add_custom("政府补贴·二期", "分类", 5, '{"money": 3}')
        # 采纳路径：手工造一条与池内同名的 pending 草稿 → 拒绝
        await env.dao.upsert_event(
            "dup_draft", "政府补贴", "草稿类", 5, "{}", "{}", "",
            source="llm", status="pending",
        )
        pending = [r for r in await env.dao.list_events("pending")
                   if r["event_id"] == "dup_draft"]
        assert pending, "草稿应已写入"
        try:
            await env.event_engine.adopt(pending[0]["id"])
            assert False, "重名草稿采纳应拒绝"
        except EventError as e:
            assert "重名" in str(e)
        assert not [r for r in await env.dao.list_events("adopted")
                    if r["event_id"] == "dup_draft"], "拒绝后不应进入采纳池"
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


async def test_llm_design_retry_on_empty_once():
    env = await TestEnv().setup()
    try:
        prov = _RetryProvider('[{"name":"雨天免费","category":"天气","weight":5,'
                              '"event_type":"instant","effects":{"money":1},"template":"t"}]')
        env.provider = prov
        drafts = await env.event_engine.generate_drafts(1)
        # 首次空内容自动重试一次后成功
        assert len(drafts) == 1 and prov.calls == 2
        # 持续为空 → 明确报「未返回内容」
        empty = _EmptyProvider()
        env.provider = empty
        try:
            await env.event_engine.generate_drafts(1)
            raise AssertionError("持续空内容应报错")
        except Exception as e:
            assert "未返回内容" in str(e)
        assert empty.calls == 2
    finally:
        await env.teardown()


async def test_llm_design_prompt_uses_single_brace():
    env = await TestEnv().setup()
    try:
        prov = _CaptureProvider('[{"name":"占位事件","category":"c","weight":5,'
                                '"event_type":"choice","template":"","options":['
                                '{"name":"甲","desc":"","outcomes":[{"w":60,"effects":{"money":1}},'
                                '{"w":40,"effects":{"money":-1}}]},'
                                '{"name":"乙","desc":"","outcomes":[{"w":60,"effects":{"money":1}},'
                                '{"w":40,"effects":{"money":-1}}]}]}]')
        env.provider = prov
        drafts = await env.event_engine.generate_drafts(1)
        assert len(drafts) == 1 and drafts[0]["event_type"] == "choice"
        prompt = prov.prompts[0]
        # 设计 prompt 中的占位符必须是单花括号（普通字符串非 f-string），不能出现双花括号
        assert "{{team}}" not in prompt and "{{stadium}}" not in prompt
        assert "{team}" in prompt and "{stadium}" in prompt
        # 草稿持久化后 template 同样为单花括号（_fill_template 才能替换）
        pending = await env.dao.list_events("pending")
        assert "{team}" in pending[0]["template"] and "{{team}}" not in pending[0]["template"]
    finally:
        await env.teardown()


async def test_ask_reads_llmresponse_completion_text():
    """新版 AstrBot：text_chat 返回 LLMResponse（只有 completion_text，无 result_str）。"""
    env = await TestEnv().setup()
    try:
        env.provider = _CompletionTextProvider(
            '[{"name":"新版返回事件","category":"c","weight":5,'
            '"event_type":"instant","effects":{"money":1},"template":"t"}]'
        )
        drafts = await env.event_engine.generate_drafts(1)
        assert len(drafts) == 1 and drafts[0]["name"] == "新版返回事件"
        assert env.provider.calls == 1
    finally:
        await env.teardown()


async def test_ask_plain_str_result():
    """个别 provider 直接返回 str 文本。"""
    env = await TestEnv().setup()
    try:
        env.provider = _PlainStrProvider(
            '[{"name":"纯字符串事件","category":"c","weight":5,'
            '"event_type":"instant","effects":{"money":1},"template":"t"}]'
        )
        drafts = await env.event_engine.generate_drafts(1)
        assert len(drafts) == 1 and drafts[0]["name"] == "纯字符串事件"
    finally:
        await env.teardown()