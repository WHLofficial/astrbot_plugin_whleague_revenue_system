"""联赛状态监听器：推进窗口/赛季后广播事件、异常隔离、幂等注册。"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402


async def test_window_advance_fires_listener():
    env = await TestEnv().setup()
    try:
        events = []
        env.fixture_service.state_listeners.append(events.append)
        result = await env.fixture_service.advance_window("tester")
        assert result == {"season_number": 1, "window_seq": 2}, result
        assert events == [{"event": "window_advanced", "season_number": 1, "window_seq": 2}], events
    finally:
        await env.teardown()


async def test_season_advance_fires_listener_with_name():
    env = await TestEnv().setup()
    try:
        events = []
        env.fixture_service.state_listeners.append(events.append)
        result = await env.fixture_service.advance_season("tester", "钢铁纪元")
        assert result["season_number"] == 2 and result["name"] == "钢铁纪元", result
        assert len(events) == 1, events
        ev = events[0]
        assert ev["event"] == "season_advanced"
        assert ev["season_number"] == 2 and ev["window_seq"] == 1 and ev["name"] == "钢铁纪元"

        # 未命名推进：name 为 None 也照常广播
        await env.fixture_service.advance_season("tester")
        assert len(events) == 2 and events[1]["name"] is None, events
    finally:
        await env.teardown()


async def test_listener_exception_isolated():
    env = await TestEnv().setup()
    try:
        calls = []

        def broken(_ev):
            raise RuntimeError("listener boom")

        async def ok(ev):
            calls.append(("async", ev["event"]))

        def ok_sync(ev):
            calls.append(("sync", ev["event"]))

        env.fixture_service.state_listeners.extend([broken, ok, ok_sync])
        # 坏监听器不阻断推进，也不影响后续监听器（同步/异步均可）
        result = await env.fixture_service.advance_window("tester")
        assert result["window_seq"] == 2, result
        assert ("async", "window_advanced") in calls and ("sync", "window_advanced") in calls

        await env.fixture_service.advance_season("tester")
        assert [kind for kind, _ in calls] == ["async", "sync", "async", "sync"], calls
    finally:
        await env.teardown()


async def test_plugin_register_idempotent():
    from astrbot_plugin_whleague_revenue_system.main import StadiumPlugin

    plugin = StadiumPlugin.__new__(StadiumPlugin)  # 只测注册表逻辑，无需完整初始化
    plugin._state_listeners = []

    def fn(_ev):
        pass

    assert plugin.register_state_listener(fn) is True
    assert plugin.register_state_listener(fn) is False  # 幂等
    another = lambda _ev: None  # noqa: E731
    assert plugin.register_state_listener(another) is True
    assert plugin._state_listeners == [fn, another]
