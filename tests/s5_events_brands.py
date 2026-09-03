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
        # 内置 24 条：6 即发 + 18 选择（v2.10.0 增品牌联合营销/竞品挖角 2 条冠名联动）
        builtin = [r for r in rows if r["source"] == "builtin"]
        assert len(builtin) == 24, len(builtin)
        instant = [r for r in builtin if r["event_type"] == "instant"]
        choice = [r for r in builtin if r["event_type"] == "choice"]
        assert len(instant) == 6, len(instant)
        assert len(choice) == 18, len(choice)
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
        assert len([r for r in rows2 if r["source"] == "builtin"]) == 24
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
        # 事件条件（冠名联动）不满足时不会被随机抽中——直接指定仍可强制触发
        forced = await env.event_engine.trigger_team("利物浦", 1, 1, event_id="brand_crisis")
        assert forced["hits"][0]["event"] == "冠名品牌危机", forced["hits"]
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
        # 广播 = LLM 叙述 + 选项列表（含概率与效果金额）
        text = hit["broadcast"]
        assert "周边爆款" in text and "利物浦" in text
        assert "围巾印到一半" in text  # LLM 叙述被采用
        assert "①" in text and "②" in text
        assert "60%" in text and "40%" in text and "50%" in text  # 概率表恢复显示
        assert "资金+5.0M" in text and "维护−3.0M" in text  # 效果金额照实列出
        assert "死忠+2.0%" in text and "死忠-2.0%" in text  # 负号走 ASCII（:+.1f 格式化）
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


async def test_trigger_all_event_occurrence_cap():
    """v2.4：一次全员分配中同一事件最多被抽中 event_max_occurrences（默认 2）次。"""
    from collections import Counter

    from astrbot_plugin_whleague_revenue_system.config.defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["event_max_occurrences"] == 2
    env = await TestEnv(cfg_override={"event_hit_probability": 1.0}).setup()
    try:
        # 事件池收敛为 3 个无条件自定义事件，保证上限必然生效
        await env.db.execute(
            "UPDATE event_pool SET status='discarded' WHERE source='builtin'")
        for i in range(3):
            await env.dao.upsert_event(
                f"cap_ev{i}", f"上限测试{i}", "测试", 1, "{}", "{}", "t",
                source="custom", status="adopted",
            )
        for team in ("利物浦", "巴塞罗那", "纽卡斯尔联", "勒沃库森", "曼城", "AC米兰"):
            await env.stadium_service.import_attributes(team, influence=120.0)

        result = await env.event_engine.trigger_all(1, 1)
        assert result["triggered"] == 6, result
        assert result["capped"] == 0, result
        counts = Counter(h["event_id"] for h in result["hits"])
        assert len(counts) == 3 and max(counts.values()) == 2, counts

        # 上限=1：每事件至多一次，其余 3 队因上限未中彩
        env.cfg["event_max_occurrences"] = 1
        result2 = await env.event_engine.trigger_all(1, 1)
        assert result2["triggered"] == 3, result2
        assert result2["capped"] == 3, result2
        counts2 = Counter(h["event_id"] for h in result2["hits"])
        assert len(counts2) == 3 and max(counts2.values()) == 1, counts2
    finally:
        await env.teardown()


async def test_cancel_team_events_latest_batch_cleanup():
    """v2.4：all 取消=最近一次分配（按 created_at 取最新一批），更早批次保留。"""
    from astrbot_plugin_whleague_revenue_system.services.event_engine import EventError

    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        bal0 = (await env.dao.get_balance("利物浦"))["balance"]
        # 批1（同一秒 10:00:00）：subsidy(即发 +4.0M) + 周边爆款(选择·待定)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="subsidy")
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        await env.db.execute(
            "UPDATE events_log SET created_at='2026-08-24 10:00:00' WHERE team_name='利物浦'")
        await env.db.execute(
            "UPDATE event_choices SET created_at='2026-08-24 10:00:00' WHERE team_name='利物浦'")
        # 批2（间隔 >60 秒，超出批次窗口）：暴雨滂沱(即发·上座×0.85)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="storm_buzz")
        await env.db.execute(
            "UPDATE events_log SET created_at='2026-08-24 10:02:00' "
            "WHERE team_name='利物浦' AND event_id='storm_buzz'")

        # 第一次 all：只撤批2 的暴雨滂沱；批1 完整保留
        res = await env.event_engine.cancel_team_events("利物浦", 1, 1)
        assert res["cancelled"] == 1 and res["instant"] == 1 and res["choice"] == 0, res
        assert res["skipped"] == [], res
        assert (await env.dao.get_stadium("利物浦"))["next_attendance_mod"] == 1.0
        logs = {lg["event_id"] for lg in await env.dao.get_window_events("利物浦", 1, 1)}
        assert logs == {"subsidy", "merch_hit"}, logs
        assert (await env.dao.get_event_choice("利物浦", 1, 1, "merch_hit")) is not None

        # 第二次 all：批1 全部清空（即发回退数值 + 选择待定移除）
        res2 = await env.event_engine.cancel_team_events("利物浦", 1, 1)
        assert res2["cancelled"] == 2, res2
        assert res2["instant"] == 1 and res2["choice"] == 1 and res2["skipped"] == [], res2
        assert await env.dao.get_window_events("利物浦", 1, 1) == []
        assert (await env.dao.get_event_choice("利物浦", 1, 1, "merch_hit")) is None
        txs = [t for t in await env.dao.list_transactions("利物浦", season=1, window_seq=1)
               if t["kind"] == "event"]
        assert not [t for t in txs if t["note"] == "政府补贴"], txs  # subsidy 流水已撤
        assert abs((await env.dao.get_balance("利物浦"))["balance"] - bal0) < 1e-6

        # 第三次 all：无可取消 → 报错
        try:
            await env.event_engine.cancel_team_events("利物浦", 1, 1)
            assert False, "应已无任何分配"
        except EventError as e:
            assert "没有分配任何事件" in str(e)
    finally:
        await env.teardown()


async def test_cancel_team_events_skips_resolved_choice():
    """v2.4：最近一次分配含已结算选择 → 跳过并提示，账目/行保留，更早批次不受波及。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        # 批1（10:00:00）：subsidy(即发 +4.0M)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="subsidy")
        await env.db.execute(
            "UPDATE events_log SET created_at='2026-08-24 10:00:00' WHERE team_name='利物浦'")
        # 批2（间隔 >60 秒，超出批次窗口）：德比热度(选择·已结算，账已动)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="derby_buzz")
        await env.dao.set_event_choice("利物浦", 1, 1, "derby_buzz", 1)
        await env.event_engine.settle_now(1, 1)
        await env.db.execute(
            "UPDATE events_log SET created_at='2026-08-24 10:02:00' "
            "WHERE team_name='利物浦' AND event_id='derby_buzz'")
        await env.db.execute(
            "UPDATE event_choices SET created_at='2026-08-24 10:02:00' "
            "WHERE team_name='利物浦' AND event_id='derby_buzz'")

        # all：最近一批只有已结算事件 → 0 取消、跳过名单含德比热度
        res = await env.event_engine.cancel_team_events("利物浦", 1, 1)
        assert res["cancelled"] == 0 and res["instant"] == 0 and res["choice"] == 0, res
        assert res["skipped"] == ["德比热度"], res
        # 批1 与已结算批次均原样保留（日志/选择行/流水）
        logs = {lg["event_id"] for lg in await env.dao.get_window_events("利物浦", 1, 1)}
        assert logs == {"subsidy", "derby_buzz"}, logs
        assert (await env.dao.get_event_choice("利物浦", 1, 1, "derby_buzz"))["resolved"] == 1
        txs = [t for t in await env.dao.list_transactions("利物浦", season=1, window_seq=1)
               if t["kind"] == "event"]
        assert [t for t in txs if t["note"] == "政府补贴"], txs  # subsidy 流水未被波及
        assert [t for t in txs if t["note"].startswith("德比热度")], txs  # 已结算账目保留
    finally:
        await env.teardown()


async def test_cancel_latest_assignment_multi_team():
    """v2.5：裸 all（全局）——同一批跨两队的即发+选择一并回退，按队分列。"""
    from astrbot_plugin_whleague_revenue_system.services.event_engine import EventError

    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.stadium_service.import_attributes("巴塞罗那", influence=150.0)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="subsidy")
        await env.event_engine.trigger_team("巴塞罗那", 1, 1, event_id="merch_hit")
        await env.db.execute(
            "UPDATE events_log SET created_at='2026-08-24 11:00:00' "
            "WHERE team_name IN ('利物浦', '巴塞罗那')")
        await env.db.execute(
            "UPDATE event_choices SET created_at='2026-08-24 11:00:00'")

        res = await env.event_engine.cancel_latest_assignment(1, 1)
        assert res["cancelled"] == 2 and res["instant"] == 1 and res["choice"] == 1, res
        assert res["skipped"] == [], res
        assert any("利物浦：" in ln and "政府补贴" in ln for ln in res["lines"]), res
        assert any("巴塞罗那：" in ln and "周边爆款" in ln for ln in res["lines"]), res
        assert await env.event_engine.list_team_events("利物浦", 1, 1) == []
        assert await env.event_engine.list_team_events("巴塞罗那", 1, 1) == []
        try:
            await env.event_engine.cancel_latest_assignment(1, 1)
            assert False, "应已无任何分配"
        except EventError as e:
            assert "没有分配任何事件" in str(e)
    finally:
        await env.teardown()


async def test_cancel_latest_assignment_later_batches_and_skip():
    """v2.5：全局最新一批=最大时间戳——单队补触发/已结算批依次占位，更早批保留。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.stadium_service.import_attributes("巴塞罗那", influence=150.0)
        await env.stadium_service.import_attributes("曼城", influence=150.0)
        # 批1（11:00:00）：利物浦 政府补贴
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="subsidy")
        await env.db.execute(
            "UPDATE events_log SET created_at='2026-08-24 11:00:00' WHERE team_name='利物浦'")
        # 批2（11:02:00）：巴塞罗那 德比热度（选择·已结算）
        await env.event_engine.trigger_team("巴塞罗那", 1, 1, event_id="derby_buzz")
        await env.dao.set_event_choice("巴塞罗那", 1, 1, "derby_buzz", 1)
        await env.event_engine.settle_now(1, 1)
        await env.db.execute(
            "UPDATE events_log SET created_at='2026-08-24 11:02:00' "
            "WHERE team_name='巴塞罗那' AND event_id='derby_buzz'")
        await env.db.execute(
            "UPDATE event_choices SET created_at='2026-08-24 11:02:00' "
            "WHERE team_name='巴塞罗那' AND event_id='derby_buzz'")
        # 批3（11:04:00）：曼城 暴雨滂沱（单队补触发 → 成为全局最新一批）
        await env.event_engine.trigger_team("曼城", 1, 1, event_id="storm_buzz")
        await env.db.execute(
            "UPDATE events_log SET created_at='2026-08-24 11:04:00' WHERE team_name='曼城'")

        # 第一次：只撤批3（曼城）
        res = await env.event_engine.cancel_latest_assignment(1, 1)
        assert res["cancelled"] == 1 and res["instant"] == 1 and res["choice"] == 0, res
        assert res["skipped"] == [], res
        assert (await env.dao.get_stadium("曼城"))["next_attendance_mod"] == 1.0
        logs = {lg["event_id"] for lg in await env.dao.get_window_events("利物浦", 1, 1)}
        assert logs == {"subsidy"}, logs  # 批1 未受波及
        # 第二次：批2 全为已结算 → 0 取消 + 跳过（带队名前缀）
        res2 = await env.event_engine.cancel_latest_assignment(1, 1)
        assert res2["cancelled"] == 0, res2
        assert res2["skipped"] == ["巴塞罗那·德比热度"], res2
        assert (await env.dao.get_event_choice("巴塞罗那", 1, 1, "derby_buzz"))["resolved"] == 1
        assert {lg["event_id"] for lg in await env.dao.get_window_events("利物浦", 1, 1)} \
            == {"subsidy"}
    finally:
        await env.teardown()


async def test_cancel_team_events_window_merge():
    """v2.5+：批次窗口 60 秒——间隔不足 60 秒的两次触发视为同一批，一次撤净。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="subsidy")
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        await env.db.execute(
            "UPDATE events_log SET created_at='2026-08-24 10:00:00' "
            "WHERE team_name='利物浦' AND event_id='subsidy'")
        await env.db.execute(
            "UPDATE events_log SET created_at='2026-08-24 10:00:40' "
            "WHERE team_name='利物浦' AND event_id='merch_hit'")
        await env.db.execute(
            "UPDATE event_choices SET created_at='2026-08-24 10:00:40' "
            "WHERE team_name='利物浦'")

        res = await env.event_engine.cancel_team_events("利物浦", 1, 1)
        assert res["cancelled"] == 2 and res["instant"] == 1 and res["choice"] == 1, res
        assert res["skipped"] == [], res
        assert await env.dao.get_window_events("利物浦", 1, 1) == []
        assert (await env.dao.get_event_choice("利物浦", 1, 1, "merch_hit")) is None
    finally:
        await env.teardown()


async def test_cancel_latest_assignment_window_merge():
    """v2.5+：窗口内跨队合并（40 秒间隔视为一批），窗口外批次（90 秒间隔）先撤。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.stadium_service.import_attributes("巴塞罗那", influence=150.0)
        await env.stadium_service.import_attributes("曼城", influence=150.0)
        # 利物浦/巴塞罗那 间隔 40 秒 → 同一批；曼城 90 秒后 → 下一批
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="subsidy")
        await env.event_engine.trigger_team("巴塞罗那", 1, 1, event_id="merch_hit")
        await env.event_engine.trigger_team("曼城", 1, 1, event_id="storm_buzz")
        await env.db.execute(
            "UPDATE events_log SET created_at='2026-08-24 11:00:10' "
            "WHERE team_name='利物浦' AND event_id='subsidy'")
        await env.db.execute(
            "UPDATE events_log SET created_at='2026-08-24 11:00:50' "
            "WHERE team_name='巴塞罗那' AND event_id='merch_hit'")
        await env.db.execute(
            "UPDATE event_choices SET created_at='2026-08-24 11:00:50' "
            "WHERE team_name='巴塞罗那'")
        await env.db.execute(
            "UPDATE events_log SET created_at='2026-08-24 11:02:00' "
            "WHERE team_name='曼城' AND event_id='storm_buzz'")

        # 第一次：最新一批=窗口外的曼城
        res = await env.event_engine.cancel_latest_assignment(1, 1)
        assert res["cancelled"] == 1 and res["instant"] == 1, res
        assert (await env.dao.get_stadium("曼城"))["next_attendance_mod"] == 1.0
        # 第二次：利物浦+巴塞罗那（40 秒间隔）合并为一批撤净
        res2 = await env.event_engine.cancel_latest_assignment(1, 1)
        assert res2["cancelled"] == 2 and res2["instant"] == 1 and res2["choice"] == 1, res2
        assert res2["skipped"] == [], res2
        assert any("利物浦：" in ln for ln in res2["lines"]), res2
        assert any("巴塞罗那：" in ln for ln in res2["lines"]), res2
        assert await env.event_engine.list_team_events("利物浦", 1, 1) == []
        assert await env.event_engine.list_team_events("巴塞罗那", 1, 1) == []
        assert await env.event_engine.list_team_events("曼城", 1, 1) == []
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


async def test_llm_design_skips_nondict_items():
    """LLM 返回数组中的非 dict 项逐条跳过，不再让整个批次失败（事件+品牌）。"""
    env = await TestEnv().setup()
    try:
        env.provider.set_response(
            '["垃圾字符串", 42, {"name":"正常事件","category":"天气衍生","weight":5,'
            '"effects":{"money":1},"template":"t"}]'
        )
        drafts = await env.event_engine.generate_drafts(3)
        assert len(drafts) == 1, drafts
        assert drafts[0]["name"] == "正常事件"
        env.provider.set_response('["x", {"brand":"测试品牌","heat":5}]')
        bd = await env.brand_service.generate_drafts(2)
        assert len(bd) == 1 and bd[0]["brand"] == "测试品牌", bd
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
        from astrbot_plugin_whleague_revenue_system.services.brand_service import BrandError
        try:
            await env.brand_service.sign("利物浦", "可口可乐", 1, 1)
            raise AssertionError("已有冠名应拒绝")
        except BrandError:
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
        from astrbot_plugin_whleague_revenue_system.services.event_engine import EventError
        try:
            await env.event_engine.record_choice("利物浦", 1, 1, "周边爆款", 2)
            raise AssertionError("已结算选择应拒绝修改")
        except EventError:
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


async def test_extract_json_tolerates_surrounding_prose():
    """兜底解析：LLM 在 JSON 前后附带说明文字时仍能取出数组。"""
    from astrbot_plugin_whleague_revenue_system.services.llm_writer import _extract_json

    raw = _extract_json('好的，以下是本次设计：[{"name":"a","weight":5}] 请查收。')
    assert isinstance(raw, list) and raw[0]["name"] == "a"
    # 纯净输入不受影响，仍走严格解析
    assert _extract_json('[{"name":"b"}]') == [{"name": "b"}]
    assert _extract_json("完全没有结构化内容") is None


async def test_extract_json_tolerates_trailing_comma():
    """兜底解析：围栏内数组带尾逗号时仍能解析。"""
    from astrbot_plugin_whleague_revenue_system.services.llm_writer import _extract_json

    raw = _extract_json('```json\n[{"name":"a","effects":{"money":1,},},]\n```')
    assert isinstance(raw, list) and raw[0]["effects"]["money"] == 1

async def test_add_custom_instant_effects_clamped():
    """自定义即发事件效果入库前钳制：越界取边界、非数值拒绝、触发按钳后值入账。"""
    from astrbot_plugin_whleague_revenue_system.services.event_engine import EventError

    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        # 正向越界金额钳到 +8.0（event_money_clamp 默认）
        await env.event_engine.add_custom("天价赞助", "商业", 5, '{"money": 999999}')
        rows = {r["name"]: r for r in await env.dao.list_events("adopted")}
        effects = json.loads(rows["天价赞助"]["effects_json"])
        assert effects["money"] == 8.0, effects
        # 负向越界与 attendance_mod 越界同理
        await env.event_engine.add_custom("巨额罚款", "事故", 5, '{"money": -1e9, "attendance_mod": 99}')
        rows = {r["name"]: r for r in await env.dao.list_events("adopted")}
        clamped = json.loads(rows["巨额罚款"]["effects_json"])
        assert clamped["money"] == -8.0 and clamped["attendance_mod"] == 2.0, clamped
        # 非数值直接拒绝且不入库
        try:
            await env.event_engine.add_custom("坏效果", "事故", 5, '{"money": "abc"}')
            assert False, "非数值 effects 应被拒绝"
        except EventError as e:
            assert "非法数值" in str(e)
        assert not any(r["name"] == "坏效果" for r in await env.dao.list_events("adopted"))
        # 触发按钳后值入账
        bal0 = (await env.dao.get_balance("利物浦"))["balance"]
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id=rows["天价赞助"]["event_id"])
        txs = [t for t in await env.dao.list_transactions("利物浦", season=1, window_seq=1)
               if t["kind"] == "event"]
        assert any(abs(t["amount"] - 8.0) < 1e-9 for t in txs), txs
        assert abs((await env.dao.get_balance("利物浦"))["balance"] - (bal0 + 8.0)) < 1e-6
    finally:
        await env.teardown()


async def test_clamp_passes_satisfaction_brand_heat():
    """v2.11.1 放行：钳制层不再丢弃 satisfaction/brand_heat，越界取边界、0 剔除。"""
    from astrbot_plugin_whleague_revenue_system.services.llm_writer import _clamp_effects

    clamps = {"money": 8.0, "fans": 0.05, "maintenance": 5.0,
              "satisfaction": 0.5, "brand_heat": 0.3}
    out = _clamp_effects({"money": 1.0, "satisfaction": 0.9, "brand_heat": -0.5}, clamps)
    assert out["satisfaction"] == 0.5 and out["brand_heat"] == -0.3, out
    # 0 视为无效果不落键（同 attendance_mod=1.0 的条件保留口径）
    out2 = _clamp_effects({"satisfaction": 0.0, "brand_heat": 0.0}, clamps)
    assert "satisfaction" not in out2 and "brand_heat" not in out2, out2
    # config 未配置钳幅时按注册表默认放行
    out3 = _clamp_effects({"satisfaction": 0.4},
                          {"money": 8.0, "fans": 0.05, "maintenance": 5.0})
    assert out3["satisfaction"] == 0.4, out3


async def test_add_custom_brand_keys_survive():
    """手写事件 satisfaction/brand_heat 入库（v2.11.1 前被钳制层静默丢弃）。"""
    env = await TestEnv().setup()
    try:
        await env.event_engine.add_custom(
            "赞助商答谢夜", "商业", 10,
            '{"money": 2.0, "satisfaction": 0.9, "brand_heat": -0.8}')
        rows = {r["name"]: r for r in await env.dao.list_events("adopted")}
        effects = json.loads(rows["赞助商答谢夜"]["effects_json"])
        assert effects["satisfaction"] == 0.5 and effects["brand_heat"] == -0.3, effects
    finally:
        await env.teardown()


async def test_build_credit_influence_apply_cancel():
    """v2.11.2 数值键：建设券/影响力钳制放行、触发生效、重算跳过、取消反演。"""
    from astrbot_plugin_whleague_revenue_system.services.llm_writer import _clamp_effects

    clamps = {"money": 8.0, "fans": 0.05, "maintenance": 5.0,
              "satisfaction": 0.5, "brand_heat": 0.3,
              "build_credit": 5.0, "influence": 10.0}
    out = _clamp_effects({"build_credit": 99.0, "influence": -99.0}, clamps)
    assert out["build_credit"] == 5.0 and out["influence"] == -10.0, out
    assert "build_credit" not in _clamp_effects({"build_credit": 0.0}, clamps)

    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.event_engine.add_custom(
            "市政拨款", "基建", 10, '{"build_credit": 3.0, "influence": 5.0}')
        rows = {r["name"]: r for r in await env.dao.list_events("adopted")}
        effects = json.loads(rows["市政拨款"]["effects_json"])
        assert effects["build_credit"] == 3.0 and effects["influence"] == 5.0, effects
        # 触发：建设券列直改、影响力基准直改
        r = await env.event_engine.trigger_team(
            "利物浦", 1, 1, event_id=rows["市政拨款"]["event_id"])
        notes = r["hits"][0]["notes"]
        assert any("建设券 +3.0M" in n for n in notes), notes
        assert any("影响力 +5.0" in n for n in notes), notes
        assert (await env.dao.get_balance("利物浦"))["build_credit"] == 3.0
        assert (await env.dao.get_stadium("利物浦"))["influence"] == 155.0
        # 重算跳过：状态不动，备注标注
        notes = await env.event_engine._apply_effects(
            "利物浦", 1, 1, {"build_credit": 1.0, "influence": 2.0}, "复核",
            apply_state=False)
        assert any("建设券 +1.0M（重算跳过）" in n for n in notes), notes
        assert any("影响力 +2.0（重算跳过）" in n for n in notes), notes
        assert (await env.dao.get_balance("利物浦"))["build_credit"] == 3.0
        # 取消反演：反向加减
        c = await env.event_engine.cancel_team_event("利物浦", "市政拨款", 1, 1)
        assert any("建设券已反向调整" in n for n in c["notes"]), c["notes"]
        assert (await env.dao.get_balance("利物浦"))["build_credit"] == 0.0
        assert (await env.dao.get_stadium("利物浦"))["influence"] == 150.0
    finally:
        await env.teardown()


async def test_signals_clamp_apply_collect():
    """v2.11.3 信号总线：钳制（未知名剔除/步进对称/乘数区间/0 剔除）、
    备注不落状态（无重算跳过标记，同 satisfaction 先例）、采集聚合双来源。"""
    from astrbot_plugin_whleague_revenue_system.services.llm_writer import _clamp_effects
    from astrbot_plugin_whleague_revenue_system.services.event_effects import (
        collect_window_signals, mult_factor, step_value)

    sig_clamps = {"fan_mood_clamp": 2.0, "upkeep_low": 0.5, "upkeep_high": 2.0,
                  "fee_mod_low": 0.5, "fee_mod_high": 2.0}
    clamps = {"money": 8.0, "fans": 0.05, "maintenance": 5.0,
              "satisfaction": 0.5, "brand_heat": 0.3,
              "build_credit": 5.0, "influence": 10.0, "signals": sig_clamps}
    out = _clamp_effects(
        {"signals": {"fan_mood": 3.0, "upkeep": 0.4, "fee_mod": 0.8, "unknown": 5.0}},
        clamps)
    assert out["signals"] == {"fan_mood": 2.0, "upkeep": 0.5, "fee_mod": 0.8}, out
    assert "signals" not in _clamp_effects({"signals": {"fan_mood": 0.0}}, clamps)
    assert "signals" not in _clamp_effects({"signals": "垃圾"}, clamps)

    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.event_engine.add_custom(
            "更衣室风波", "氛围", 10,
            '{"signals": {"fan_mood": 0.3, "fee_mod": 0.8, "unknown": 9.9}}')
        rows = {r["name"]: r for r in await env.dao.list_events("adopted")}
        effects = json.loads(rows["更衣室风波"]["effects_json"])
        assert effects["signals"] == {"fan_mood": 0.3, "fee_mod": 0.8}, effects
        r = await env.event_engine.trigger_team(
            "利物浦", 1, 1, event_id=rows["更衣室风波"]["event_id"])
        notes = r["hits"][0]["notes"]
        assert any("粉丝情绪 +0.3" in n for n in notes), notes
        assert any("冠名费 ×0.8" in n for n in notes), notes
        # 只出备注不落状态：apply_state=False 备注相同（无「重算跳过」标记）
        notes2 = await env.event_engine._apply_effects(
            "利物浦", 1, 1, {"signals": {"fan_mood": -0.5}}, "复核", apply_state=False)
        assert any("粉丝情绪 -0.5" in n for n in notes2), notes2
        assert not any("重算跳过" in n for n in notes2), notes2
        # 采集：events_log（触发已写）+ 选择结算 outcome 双来源；乘数 Π、步进 Σ
        choice = await env.dao.get_event_choice("利物浦", 1, 1, "evt_x")
        if choice is None:
            await env.dao.add_event_choice("利物浦", 1, 1, "evt_x")
            await env.dao.set_event_choice("利物浦", 1, 1, "evt_x", 1)
            choice = await env.dao.get_event_choice("利物浦", 1, 1, "evt_x")
        await env.dao.mark_choice_resolved(
            choice["id"],
            json.dumps({"option": "办", "effects": {"signals": {"fee_mod": 0.9}}}))
        sig = await collect_window_signals(env.dao, 1, 1)
        assert sig["fan_mood"]["利物浦"] == [0.3], sig
        assert sorted(sig["fee_mod"]["利物浦"]) == [0.8, 0.9], sig
        assert step_value(sig, "fan_mood", "利物浦") == 0.3
        assert abs(mult_factor(sig, "fee_mod", "利物浦", 0.5, 2.0) - 0.72) < 1e-9
        assert mult_factor(sig, "upkeep", "利物浦", 0.5, 2.0) == 1.0
        assert mult_factor(sig, "fee_mod", "巴塞罗那", 0.5, 2.0) == 1.0
    finally:
        await env.teardown()


async def test_hit_prob_config_defensive():
    """hit_prob 防御：NaN 不再等价于全员命中；非法/负值回退默认；>1 钳为必中。"""
    from unittest.mock import patch

    env = await TestEnv().setup()
    try:
        for team in ("利物浦", "巴塞罗那", "纽卡斯尔联", "勒沃库森"):
            await env.stadium_service.import_attributes(team, influence=120.0)
        await env.event_engine.add_custom("必中测试", "通用", 100, '{"money": 1}')
        # random() 恒 0.99：默认 0.4 与回退 0.4 均不命中，钳到 1.0 则必命中
        with patch("random.random", return_value=0.99):
            for bad in (float("nan"), -0.5, "abc"):
                env.cfg["event_hit_probability"] = bad
                r = await env.event_engine.trigger_all(1, 1)
                assert r["hits"] == [], (bad, r["hits"])
            env.cfg["event_hit_probability"] = 5.0
            r = await env.event_engine.trigger_all(1, 1)
            assert len(r["hits"]) == 4, r["hits"]
    finally:
        await env.teardown()


async def test_choice_empty_outcomes_settle_safe():
    """遗留空 outcomes 数据不崩结算：已定按无效果结算、未定整条跳过，均不中断整批。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.stadium_service.import_attributes("巴塞罗那", influence=150.0)
        # 直接落库模拟旧版/手改遗留数据（写路径 _clamp_choice_event 已拒绝空 outcomes）
        await env.dao.upsert_event(
            "legacy_bad", "烂尾工程", "事故", 10,
            "{}", "{}", "烂尾工程：{team} 出了状况。",
            source="custom", status="adopted", event_type="choice",
            options_json=json.dumps([
                {"no": 1, "name": "赶工", "outcomes": []},
                {"no": 2, "name": "停工", "outcomes": []},
            ], ensure_ascii=False),
        )
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="legacy_bad")
        await env.event_engine.trigger_team("巴塞罗那", 1, 1, event_id="legacy_bad")
        await env.event_engine.record_choice("利物浦", 1, 1, "烂尾工程", 1)
        r = await env.event_engine.settle_choices(1, 1)
        by_team = {x["team"]: x for x in r["resolved"]}
        # 已定：选项存在但无结果 → 无效果结算 + 说明备注，不中断
        assert by_team["利物浦"].get("skipped") is not True, by_team["利物浦"]
        assert "选项无结果配置，按无效果结算" in by_team["利物浦"]["notes"], by_team["利物浦"]
        # 未定：全部选项无结果 → 整条跳过
        assert by_team["巴塞罗那"].get("skipped") is True, by_team["巴塞罗那"]
        # 均不产生资金流水
        for team in ("利物浦", "巴塞罗那"):
            txs = [t for t in await env.dao.list_transactions(team, season=1, window_seq=1)
                   if t["kind"] == "event"]
            assert txs == [], (team, txs)
    finally:
        await env.teardown()


async def test_import_choices_spaced_event_name():
    """批量导入：事件名可含空格——末位 token 为选项号，中间归事件名。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.event_engine.add_custom(
            "赞助商 考察", "运营", 5, "{}", event_type="choice",
            options_text=json.dumps([
                {"name": "高调接待", "outcomes": [
                    {"w": 60, "effects": {"money": 3}},
                    {"w": 40, "effects": {"money": -2}}]},
                {"name": "低调安排", "outcomes": [
                    {"w": 70, "effects": {"fans_pct": 0.02}},
                    {"w": 30, "effects": {"maintenance": 1}}]},
            ]),
        )
        ev = next(r for r in await env.dao.list_events("adopted")
                  if r["name"] == "赞助商 考察")
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id=ev["event_id"])
        r = await env.event_engine.import_choices("利物浦 赞助商 考察 ①")
        assert r[0]["ok"] is True and r[0]["event"] == "赞助商 考察", r
        assert r[0]["choice_no"] == 1, r
        # 旧式无空格名不受影响，越界仍逐行报错
        r2 = await env.event_engine.import_choices("利物浦 赞助商 考察 999")
        assert r2[0]["ok"] is False and "选项号需在" in r2[0]["error"], r2
    finally:
        await env.teardown()


async def test_llm_design_sanitizes_text_fields():
    """LLM 文本字段经 sanitize_text 清洗：控制字符剥离、超长截断（事件+品牌）。"""
    env = await TestEnv().setup()
    try:
        env.provider.set_response(
            '[{"name":"爆\\n炸事件","category":"天\\u0000气衍生","weight":30,'
            '"effects":{"money":1},"template":"' + "长" * 250 + '"}]'
        )
        drafts = await env.event_engine.generate_drafts(1)
        assert len(drafts) == 1, drafts
        assert drafts[0]["name"] == "爆炸事件", drafts[0]["name"]
        # category/template 不在 generate_drafts 返回值里，查 pending 行
        row = (await env.dao.list_events("pending"))[0]
        assert "\n" not in row["name"] and "\x00" not in row["category"], row
        assert len(row["template"]) <= 200, len(row["template"])
        env.provider.set_response('[{"brand":"冠\\n名品牌","heat":5}]')
        bd = await env.brand_service.generate_drafts(1)
        assert len(bd) == 1 and bd[0]["brand"] == "冠名品牌", bd
    finally:
        await env.teardown()


async def test_structural_keys_clamped():
    """v2.11.4 结构键钳制：取值域外剔除、delta 收敛 ±1、计数钳上限、标量归一。"""
    from astrbot_plugin_whleague_revenue_system.services.llm_writer import _clamp_effects

    clamps = {"money": 8.0, "fans": 0.05, "maintenance": 5.0,
              "satisfaction": 0.5, "brand_heat": 0.3, "booking_cancel": 2.0,
              "facility_keys": {"pitch", "broadcast"},
              "activity_keys": {"concert", "open_day"},
              "weather_keys": {"雨", "雪"}}
    out = _clamp_effects(
        {"facility": {"key": "pitch", "delta": 9}, "booking_cancel": {"count": 99},
         "booking_gift": "open_day", "weather_set": "雨", "offer_spawn": 1}, clamps)
    assert out["facility"] == {"key": "pitch", "delta": 1}, out
    assert out["booking_cancel"] == {"count": 2}, out
    # 标量形态按 ±1 收敛（LLM 无量纲意图），dict 才能表达具体条数
    assert _clamp_effects({"booking_cancel": 99}, clamps)["booking_cancel"] == {
        "count": 1}
    assert out["booking_gift"] == {"type": "open_day"}, out
    assert out["weather_set"] == {"weather": "雨"}, out
    assert out["offer_spawn"] == {"count": 1}, out
    # 域外/零值/负 delta 落空剔除
    out2 = _clamp_effects(
        {"facility": {"key": "nope", "delta": 1}, "booking_cancel": 0,
         "booking_gift": "karaoke", "weather_set": "台风", "offer_spawn": 0}, clamps)
    assert not ({"facility", "booking_cancel", "booking_gift", "weather_set",
                 "offer_spawn"} & set(out2)), out2
    # 键域缺省（None）时不校验取值（手写通道直用场景）
    out3 = _clamp_effects({"facility": {"key": "youth", "delta": -4},
                           "weather_set": "晴"}, {"money": 8.0, "fans": 0.05,
                                                  "maintenance": 5.0})
    assert out3["facility"] == {"key": "youth", "delta": -1}, out3
    assert out3["weather_set"] == {"weather": "晴"}, out3


async def test_facility_apply_cancel():
    """v2.11.4 facility：升降级沿建造通道、边界落空回写实际 delta、取消对称反演。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.event_engine.add_custom("球场翻新", "基建", 10,
                                          '{"facility": {"key": "pitch", "delta": 1}}')
        row = next(r for r in await env.dao.list_events("adopted")
                   if r["name"] == "球场翻新")
        r = await env.event_engine.trigger_team("利物浦", 1, 1, event_id=row["event_id"])
        assert any("设施 草皮 0→1 级" in n for n in r["hits"][0]["notes"]), r["hits"]
        assert await env.dao.get_facility_level("利物浦", "pitch") == 1
        log = (await env.dao.get_window_events("利物浦", 1, 1))[0]
        assert json.loads(log["effect_json"])["facility"] == {"key": "pitch", "delta": 1}
        c = await env.event_engine.cancel_team_event("利物浦", "球场翻新", 1, 1)
        assert any("设施 草皮 已回退" in n for n in c["notes"]), c["notes"]
        assert await env.dao.get_facility_level("利物浦", "pitch") == 0
        # 下界落空：0 级再降 → 备注落空、实际 delta=0 回写，取消不动等级
        await env.event_engine.add_custom("草皮降级", "事故", 10,
                                          '{"facility": {"key": "pitch", "delta": -1}}')
        row2 = next(r for r in await env.dao.list_events("adopted")
                    if r["name"] == "草皮降级")
        r2 = await env.event_engine.trigger_team("利物浦", 1, 1, event_id=row2["event_id"])
        assert any("最低档" in n for n in r2["hits"][0]["notes"]), r2["hits"]
        assert await env.dao.get_facility_level("利物浦", "pitch") == 0
        log2 = [lg for lg in await env.dao.get_window_events("利物浦", 1, 1)
                if lg["event_id"] == row2["event_id"]][0]
        assert json.loads(log2["effect_json"])["facility"] == {"key": "pitch", "delta": 0}
        c2 = await env.event_engine.cancel_team_event("利物浦", "草皮降级", 1, 1)
        assert not any("已回退" in n for n in c2["notes"]), c2["notes"]
        assert await env.dao.get_facility_level("利物浦", "pitch") == 0
        # 重算跳过：状态不动
        notes = await env.event_engine._apply_effects(
            "利物浦", 1, 1, {"facility": {"key": "pitch", "delta": -1}}, "复核",
            apply_state=False)
        assert any("重算跳过" in n for n in notes), notes
        assert await env.dao.get_facility_level("利物浦", "pitch") == 0
    finally:
        await env.teardown()


async def test_booking_cancel_apply_restore():
    """v2.11.4 booking_cancel：取消本窗最高档位、enrich 明细落盘、取消按明细恢复。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.dao.add_booking("利物浦", 1, 1, 1, "concert", "张三")
        await env.dao.add_booking("利物浦", 1, 1, 2, "esports", "李四")
        await env.event_engine.add_custom("档期冲突", "事故", 10, '{"booking_cancel": 1}')
        row = next(r for r in await env.dao.list_events("adopted")
                   if r["name"] == "档期冲突")
        r = await env.event_engine.trigger_team("利物浦", 1, 1, event_id=row["event_id"])
        assert any("档期取消×1（档2 电竞赛事）" in n for n in r["hits"][0]["notes"]), r["hits"]
        left = await env.dao.get_bookings("利物浦", 1, 1)
        assert [int(b["slot_no"]) for b in left] == [1], left
        log = (await env.dao.get_window_events("利物浦", 1, 1))[0]
        enriched = json.loads(log["effect_json"])["booking_cancel"]
        assert enriched == {"count": 1, "cancelled": [
            {"slot_no": 2, "activity_type": "esports", "booked_by": "李四"}]}, enriched
        # 取消事件：按明细恢复原档位/活动/预订人
        c = await env.event_engine.cancel_team_event("利物浦", "档期冲突", 1, 1)
        assert any("已恢复被取消的档期×1" in n for n in c["notes"]), c["notes"]
        back = {int(b["slot_no"]): b for b in await env.dao.get_bookings("利物浦", 1, 1)}
        assert back[2]["activity_type"] == "esports" and back[2]["booked_by"] == "李四", back
        # 恢复后再取消：档2 先被取消、又被他人重占 → warning、档1 不受影响
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id=row["event_id"])
        await env.dao.add_booking("利物浦", 1, 1, 2, "concert", "王五")
        c2 = await env.event_engine.cancel_team_event("利物浦", "档期冲突", 1, 1)
        assert any("已被重新占用" in w for w in c2["warnings"]), c2["warnings"]
        slots = sorted(int(b["slot_no"]) for b in await env.dao.get_bookings("利物浦", 1, 1))
        assert slots == [1, 2], slots
    finally:
        await env.teardown()


async def test_booking_gift_and_cancel():
    """v2.11.4 booking_gift：赠最低空档位（booked_by=事件）、满则落空、撤销仅删事件赠送。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.event_engine.add_custom("粉丝福利", "运营", 10, '{"booking_gift": "open_day"}')
        row = next(r for r in await env.dao.list_events("adopted")
                   if r["name"] == "粉丝福利")
        r = await env.event_engine.trigger_team("利物浦", 1, 1, event_id=row["event_id"])
        assert any("档期赠送：球迷开放日（档1）" in n for n in r["hits"][0]["notes"]), r["hits"]
        b = (await env.dao.get_bookings("利物浦", 1, 1))[0]
        assert int(b["slot_no"]) == 1 and b["activity_type"] == "open_day" \
            and b["booked_by"] == "事件", b
        log = (await env.dao.get_window_events("利物浦", 1, 1))[0]
        assert json.loads(log["effect_json"])["booking_gift"] == {
            "type": "open_day", "slot_no": 1}
        # 撤销：仅该档为事件赠送才删
        c = await env.event_engine.cancel_team_event("利物浦", "粉丝福利", 1, 1)
        assert any("赠送档期已撤销" in n for n in c["notes"]), c["notes"]
        assert await env.dao.get_bookings("利物浦", 1, 1) == []
        # 满则落空（两档都被占用）：无 slot_no enrich，取消静默不崩
        await env.dao.add_booking("利物浦", 1, 1, 1, "concert", "运营组")
        await env.dao.add_booking("利物浦", 1, 1, 2, "concert", "运营组")
        r2 = await env.event_engine.trigger_team("利物浦", 1, 1, event_id=row["event_id"])
        assert any("已满" in n for n in r2["hits"][0]["notes"]), r2["hits"]
        c2 = await env.event_engine.cancel_team_event("利物浦", "粉丝福利", 1, 1)
        assert not any("赠送档期已撤销" in n for n in c2["notes"]), c2["notes"]
        assert len(await env.dao.get_bookings("利物浦", 1, 1)) == 2
    finally:
        await env.teardown()


async def test_weather_set_apply_cancel():
    """v2.11.4 weather_set：改下一场未赛主场天气、取消恢复原值、已录赛果不回退。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.dao.add_match(1, 1, 1, "利物浦", "曼联")
        await env.event_engine.add_custom("人工降雨", "天气", 10, '{"weather_set": "雨"}')
        row = next(r for r in await env.dao.list_events("adopted")
                   if r["name"] == "人工降雨")
        r = await env.event_engine.trigger_team("利物浦", 1, 1, event_id=row["event_id"])
        assert any("主场天气改为「雨」" in n for n in r["hits"][0]["notes"]), r["hits"]
        m = await env.dao.get_match(1)
        assert m["weather"] == "雨", dict(m)
        log = (await env.dao.get_window_events("利物浦", 1, 1))[0]
        assert json.loads(log["effect_json"])["weather_set"] == {
            "weather": "雨", "match_id": 1, "old": None}
        c = await env.event_engine.cancel_team_event("利物浦", "人工降雨", 1, 1)
        assert any("已回退" in n for n in c["notes"]), c["notes"]
        m = await env.dao.get_match(1)
        assert m["weather"] is None, dict(m)
        # 已录赛果：可触发但不回退（warning）
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id=row["event_id"])
        await env.dao.set_match_result(1, "已赛", 30000, 1.0, 0.5, 0.3)
        c2 = await env.event_engine.cancel_team_event("利物浦", "人工降雨", 1, 1)
        assert any("已录赛果" in w for w in c2["warnings"]), c2["warnings"]
        m = await env.dao.get_match(1)
        assert m["weather"] == "雨", dict(m)
    finally:
        await env.teardown()


async def test_condition_keys_extended():
    """v2.11.5 条件键直测：fans/balance/facility_min/weather_is/last_result。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        dao, engine = env.dao, env.event_engine
        st = await dao.get_stadium("利物浦")

        async def ok(cond):
            return await engine._condition_ok(
                {"conditions_json": json.dumps(cond, ensure_ascii=False)}, st, False, [])

        # fans_diehards 基线（import 后），先读实际值再设界
        fans = float(st["fans_diehards"] or 0.0)
        assert await ok({"min_fans": fans, "max_fans": fans})
        assert not await ok({"min_fans": fans + 1})
        assert not await ok({"max_fans": fans - 1})
        # 资金：读实际余额定界，再看加钱后判定翻转
        bal0 = float((await dao.get_balance("利物浦"))["balance"])
        assert await ok({"min_balance": bal0, "max_balance": bal0})
        assert not await ok({"min_balance": bal0 + 1})
        await dao.apply_balance("利物浦", 5.0)
        assert await ok({"min_balance": bal0 + 5})
        assert not await ok({"max_balance": bal0 + 4})
        # 设施：pitch 升到 2 级，min 2 满足 / min 3 不满足；等级值非法忽略、合法未知键判 False
        await dao.upsert_facility("利物浦", "pitch", 2)
        assert await ok({"facility_min": {"pitch": 2, "youth": "abc"}})
        assert await ok({"facility_min": {"pitch": 2}})
        assert not await ok({"facility_min": {"pitch": 3}})
        assert not await ok({"facility_min": {"youth": 1}})
        # 天气：无未赛主场不满足 → 加场次后相等满足
        assert not await ok({"weather_is": "雨"})
        await dao.add_match(1, 1, 1, "利物浦", "曼联")
        assert not await ok({"weather_is": "雨"})
        await dao.set_match_weather(1, "雨")
        assert await ok({"weather_is": "雨"})
        # last_result：无完赛主场不满足 → 录 2:1 胜后 win 满足 / loss 不满足
        assert not await ok({"last_result": "win"})
        await dao.set_match_result(1, "已赛", 30000, 1.0, 0.5, 0.3, score="2:1")
        assert await ok({"last_result": "win"})
        assert await ok({"last_result": "W"})  # 误写大写由归一映射兜底
        assert not await ok({"last_result": "loss"})
    finally:
        await env.teardown()


async def test_soft_condition_dynamic_weight():
    """v2.11.5 软条件：不满足按 factor 衰减仍可抽中，硬条件事件被剔除。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        # A：容量 999999 永不满足，soft=1，weight 40 → 衰减 10；B：无条件 weight 10
        await env.dao.upsert_event("softA", "软A", "测试", 40,
                                   '{"min_capacity": 999999}', "{}", "t{team}{stadium}",
                                   "builtin", "adopted", soft_conditions=1)
        await env.dao.upsert_event("hardB", "硬B", "测试", 10, "{}", "{}",
                                   "t{team}{stadium}", "builtin", "adopted")
        hits = {"softA": 0, "hardB": 0}
        for _ in range(300):
            r = await env.event_engine.trigger_team("利物浦", 1, 1)
            name = r["hits"][0]["event"]
            hits["软A" if name == "软A" else name] = hits.get(name, 0) + 1
        assert hits["软A"] > 0, hits      # 软条件仍可中
        assert hits["硬B"] > 0, hits      # 硬条件正常中
        # 软衰减后 40×0.25=10 vs 10 → 软A约半数
        ratio = hits["软A"] / (hits["软A"] + hits["硬B"])
        assert 0.3 < ratio < 0.7, hits
        # 指定触发不走条件判定：容量条件在也强制中
        row = next(r for r in await env.dao.list_events("adopted") if r["name"] == "软A")
        r = await env.event_engine.trigger_team("利物浦", 1, 1, event_id=row["event_id"])
        assert r["hits"][0]["event"] == "软A"
    finally:
        await env.teardown()


async def test_clamp_conditions_whitelist():
    """v2.11.5 LLM 条件白名单：数值取整、枚举域外剔除、结构键逐项校验。"""
    from astrbot_plugin_whleague_revenue_system.services.llm_writer import _clamp_conditions  # noqa: E402
    clamps = {
        "activity_keys": {"concert", "open_day"},
        "weather_keys": {"晴", "雨"},
        "facility_keys": {"pitch", "youth"},
        "facility_max_level": 5,
    }
    out = _clamp_conditions({
        "min_tier": 2.7, "max_tier": -1, "min_capacity": "12000", "min_fans": 8000,
        "min_balance": 3, "max_balance": "x",
        "requires_naming": "true", "requires_activity": "esports",
        "facility_min": {"pitch": 2, "youth": 9, "pool": 1, "bad": 0},
        "weather_is": "冰雹", "last_result": "WIN",
        "hacker_key": 1,
    }, clamps)
    assert out == {
        "min_tier": 2, "min_capacity": 12000, "min_fans": 8000,
        "min_balance": 3,
        "requires_naming": True,
        "facility_min": {"pitch": 2},
        "last_result": "win",
    }, out
    # 域内枚举放行；无域（缺省 clamps）不校验
    out2 = _clamp_conditions({"requires_activity": "concert", "weather_is": "雨",
                              "facility_min": {"youth": 3}}, clamps)
    assert out2 == {"requires_activity": "concert", "weather_is": "雨",
                    "facility_min": {"youth": 3}}, out2
    out3 = _clamp_conditions({"requires_activity": "什么都行", "weather_is": "x"}, {})
    assert out3 == {"requires_activity": "什么都行", "weather_is": "x"}, out3


async def test_llm_drafts_persist_conditions():
    """v2.11.5 generate_drafts 持久化 conditions/soft_conditions 两类事件。"""
    env = await TestEnv().setup()
    try:
        env.provider.set_response(
            '[{"name":"暴雨保卫战","category":"天气衍生","weight":30,'
            '"conditions":{"weather_is":"雨","min_tier":2},"soft_conditions":true,'
            '"effects":{"money":1},"template":"暴雨来袭，{team}严阵以待。"},'
            '{"name":"连败反弹","category":"士气","weight":20,'
            '"conditions":{"last_result":"loss","facility_min":{"youth":2}},'
            '"effects":{"fans_pct":0.01},"template":"{team}连败后众志成城。"}]'
        )
        drafts = await env.event_engine.generate_drafts(2)
        assert len(drafts) == 2
        assert drafts[0]["conditions"] == {"weather_is": "雨", "min_tier": 2}
        assert drafts[0]["soft_conditions"] == 1
        pending = await env.dao.list_events("pending")
        by_name = {p["name"]: p for p in pending}
        assert json.loads(by_name["暴雨保卫战"]["conditions_json"]) == {
            "weather_is": "雨", "min_tier": 2}
        assert by_name["暴雨保卫战"]["soft_conditions"] == 1
        assert json.loads(by_name["连败反弹"]["conditions_json"]) == {
            "last_result": "loss", "facility_min": {"youth": 2}}
        assert by_name["连败反弹"]["soft_conditions"] == 0
    finally:
        await env.teardown()


async def test_choice_event_conditions_persist():
    """v2.11.5 选择事件同样携带条件与软开关。"""
    env = await TestEnv().setup()
    try:
        env.provider.set_response(
            '[{"event_type":"choice","name":"球迷请愿","category":"球迷","weight":15,'
            '"conditions":{"min_fans":5000},"soft_conditions":1,'
            '"options":[{"name":"答应","desc":"答应请求","outcomes":['
            '{"w":3,"effects":{"money":-2,"fans_pct":0.02}},'
            '{"w":1,"effects":{"money":-1,"fans_pct":0.005}}]},'
            '{"name":"拒绝","desc":"一口回绝","outcomes":['
            '{"w":3,"effects":{"money":0,"fans_pct":-0.01}},'
            '{"w":1,"effects":{"money":1,"fans_pct":-0.03}}]}],'
            '"template":"{team}球迷在{stadium}外请愿。"}]'
        )
        drafts = await env.event_engine.generate_drafts(1)
        assert drafts[0]["event_type"] == "choice"
        assert drafts[0]["conditions"] == {"min_fans": 5000}
        assert drafts[0]["soft_conditions"] == 1
        pending = await env.dao.list_events("pending")
        assert json.loads(pending[0]["conditions_json"]) == {"min_fans": 5000}
        assert pending[0]["soft_conditions"] == 1
    finally:
        await env.teardown()


async def test_add_custom_conditions():
    """code review 修复：手写通道 conditions_text/soft_conditions 钳制并持久化。"""
    env = await TestEnv().setup()
    try:
        await env.event_engine.add_custom(
            "豪门前夜", "条件", 20, '{"money": 2}',
            conditions_text='{"min_tier": 2.7, "max_tier": -1, "hacker": 1,'
                            '"facility_min": {"pitch": 2, "youth": 9}}',
            soft_conditions=True)
        row = next(r for r in await env.dao.list_events("adopted")
                   if r["name"] == "豪门前夜")
        assert json.loads(row["conditions_json"]) == {
            "min_tier": 2, "facility_min": {"pitch": 2}}, row["conditions_json"]
        assert row["soft_conditions"] == 1
        # 选择事件同样携带
        opts = ('[{"name":"a","desc":"x","outcomes":[{"w":3,"effects":{"money":1}},'
                '{"w":1,"effects":{"money":-1}}]},{"name":"b","desc":"y","outcomes":'
                '[{"w":3,"effects":{"money":-1}},{"w":1,"effects":{"money":2}}]}]')
        await env.event_engine.add_custom(
            "条件选择", "条件", 10, "{}", event_type="choice", options_text=opts,
            conditions_text='{"weather_is": "雨"}')
        row2 = next(r for r in await env.dao.list_events("adopted")
                    if r["name"] == "条件选择")
        assert json.loads(row2["conditions_json"]) == {"weather_is": "雨"}
        assert row2["soft_conditions"] == 0
        # 非法 conditions 文本拒绝
        try:
            await env.event_engine.add_custom(
                "坏条件", "条件", 5, '{"money": 1}', conditions_text="not json")
            assert False, "应当拒绝"
        except Exception as e:
            assert "conditions 需为 JSON" in str(e), e
    finally:
        await env.teardown()
async def test_offer_spawn_apply_cancel():
    """v2.11.4 offer_spawn：未签约品牌按热度上门递价（稳健套餐）、取消撤销 pending 报价。"""
    from astrbot_plugin_whleague_revenue_system.services import formula

    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.dao.open_market_round(1, 1, "明标", "test")
        await env.db.execute("DELETE FROM brand_pool")
        await env.dao.add_brand("星际冷却液", 1.2, "llm", industry="科技")
        await env.event_engine.add_custom("品牌上门", "招商", 10, '{"offer_spawn": 1}')
        row = next(r for r in await env.dao.list_events("adopted")
                   if r["name"] == "品牌上门")
        r = await env.event_engine.trigger_team("利物浦", 1, 1, event_id=row["event_id"])
        notes = r["hits"][0]["notes"]
        assert any("上门递价" in n and "/主场签约 可签" in n for n in notes), notes
        offers = await env.dao.list_pending_offers_for_team("利物浦")
        assert len(offers) == 1, offers
        offer = offers[0]
        assert offer["status"] == "pending" and offer["windows"] >= 1, dict(offer)
        assert offer["amount"] > 0, dict(offer)
        # 报价金额 = naming_fee × industry_factor（稳健套餐口径）
        s = await env.dao.get_stadium("利物浦")
        brand = await env.dao.get_brand_by_name("星际冷却液")
        expect = round(formula.naming_fee(env.cfg, float(s["capacity"]),
                                          float(s["fans_diehards"]), float(brand["heat"]))
                       * formula.industry_factor(env.cfg, "科技"), 3)
        pkg = formula.parse_json_object(offer["package_json"])
        assert abs(pkg["base_fee"] - expect) < 1e-6, (pkg, expect)
        log = (await env.dao.get_window_events("利物浦", 1, 1))[0]
        assert json.loads(log["effect_json"])["offer_spawn"] == {
            "offer_id": int(offer["id"])}, log["effect_json"]
        # 取消事件 → pending 报价被删
        c = await env.event_engine.cancel_team_event("利物浦", "品牌上门", 1, 1)
        assert any("上门报价已撤销" in n for n in c["notes"]), c["notes"]
        assert await env.dao.list_pending_offers_for_team("利物浦") == []
        # 无开放轮次：落空备注、不生成报价
        await env.dao.settle_market_round(1)
        r2 = await env.event_engine.trigger_team("利物浦", 1, 1, event_id=row["event_id"])
        assert any("无开放招商轮次" in n for n in r2["hits"][0]["notes"]), r2["hits"]
        assert await env.dao.list_pending_offers_for_team("利物浦") == []
    finally:
        await env.teardown()
