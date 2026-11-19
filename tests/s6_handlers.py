"""handler 测试：8 条保留命令的门禁、参数校验与回显路径。

v3：旧经营/财务/事件/冠名/市场/档期 handler 已随命令下线删除，
仅保留 4 条管理命令（同步/天气/覆盖/推送）+ 3 图 + 帮助的门禁与用法测试。
"""

import asyncio
import inspect
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402


class _FakeEvent:
    def __init__(self, message: str, sender: str = "10001", is_admin: bool = False):
        self.message = message
        self.sender = sender
        self.admin = is_admin
        self.results = []

    def get_message_str(self):
        return self.message

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return "99999"

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        self.results.append(text)
        return text

    def image_result(self, result):
        self.results.append(result)
        return result


async def test_plugin_import_and_commands():
    from astrbot_plugin_whleague_revenue_system import main as plugin_main

    # 检查注册的命令名（v3：恰 8 条，管理 4 + 玩家 4）
    src = inspect.getsource(plugin_main)
    expected_commands = [
        "主场赛程同步", "主场天气", "主场天气覆盖", "主场天气推送",
        "主场轮次预告图", "主场轮次统计图", "主场赛季走势图", "主场帮助",
    ]
    for cmd in expected_commands:
        assert f'@filter.command("{cmd}"' in src, f"缺少命令注册: {cmd}"
    # v3 起旧经营/财务/事件/冠名/市场/档期命令全部下线
    for gone in ("主场赛程导入", "主场赛果录入", "主场推进", "主场赛季命名", "主场属性导入",
                 "主场设施", "主场改名", "主场发放", "主场事件", "主场品牌",
                 "主场结算", "主场设置", "主场查看配置", "主场添加管理", "主场删除管理",
                 "主场补差", "主场档期", "主场冠名", "主场财务", "主场轮次统计", "主场赛季统计"):
        assert f'@filter.command("{gone}")' not in src, f"旧命令应已下线: {gone}"


class _Plugin:
    """最小插件桩：dao + weather/tour service + 门禁开关。"""

    def __init__(self, env, deny=False, is_admin=False):
        self.dao = dao_proxy = type("DaoProxy", (), {})()

        async def _is_admin(qq):
            return bool(is_admin)

        dao_proxy.is_admin = _is_admin
        self.weather_service = env.weather_service
        self.tour_sync_service = env.tour_sync_service
        self.config_cache = dict(env.cfg)
        self._deny = deny

        handler = self

        async def _guard(event):
            return "你没有权限执行此操作" if handler._deny else None

        # AdminHandler._guard 是实例方法：给实例换绑
        self._guard_override = _guard


async def test_admin_weather_handlers_flow():
    env = await TestEnv().setup()
    try:
        from astrbot_plugin_whleague_revenue_system.handlers.admin import AdminHandler

        await env.dao.set_league_state(9, "S9")
        handler = AdminHandler(_Plugin(env, is_admin=True))

        # 用法/缺参
        out = [r async for r in handler.forecast_weather(_FakeEvent("/主场天气"))]
        assert out and "用法" in out[0], out
        out = [r async for r in handler.weather_override(_FakeEvent("/主场天气覆盖"))]
        assert out and "用法" in out[0], out
        # 轮次解析错误
        out = [r async for r in handler.forecast_weather(_FakeEvent("/主场天气 顶级"))]
        assert out and "轮次需包含数字" in out[0], out
    finally:
        await env.teardown()


async def test_admin_commands_denied_for_non_admin():
    """4 条管理命令对非管理员一律拒绝（权限门禁）。"""
    env = await TestEnv().setup()
    try:
        from astrbot_plugin_whleague_revenue_system.handlers.admin import AdminHandler

        handler = AdminHandler(_Plugin(env, deny=True))
        for cmd_text, method in (
            ("/主场赛程同步", handler.sync_fixture_times),
            ("/主场天气 顶级12", handler.forecast_weather),
            ("/主场天气覆盖 顶级12 利物浦 晴", handler.weather_override),
            ("/主场天气推送", handler.weather_push),
        ):
            out = [r async for r in method(_FakeEvent(cmd_text))]
            assert out == ["你没有权限执行此操作"], (cmd_text, out)
    finally:
        await env.teardown()


async def test_admin_sync_bypass_and_error_paths():
    env = await TestEnv().setup()
    try:
        from astrbot_plugin_whleague_revenue_system.handlers.admin import AdminHandler

        handler = AdminHandler(_Plugin(env, is_admin=True))
        # tour 未配置 → bypass
        env.tour_sync_service_sync_result = {"status": "bypass", "reason": "未配置 tour_api_base"}

        async def _fake_sync():
            return {"status": "bypass", "reason": "未配置 tour_api_base"}

        handler._plugin.tour_sync_service.sync = _fake_sync
        out = [r async for r in handler.sync_fixture_times(_FakeEvent("/主场赛程同步"))]
        assert out and "未配置 tour_api_base" in out[0], out

        # club/season 双无 → error 文案透传
        async def _fake_sync_err():
            return {"status": "error", "error": "无法确定当前赛季"}

        handler._plugin.tour_sync_service.sync = _fake_sync_err
        out = [r async for r in handler.sync_fixture_times(_FakeEvent("/主场赛程同步"))]
        assert out == ["⚠️ 无法确定当前赛季"], out
    finally:
        await env.teardown()


async def test_player_chart_handlers_parse_errors():
    env = await TestEnv().setup()
    try:
        from astrbot_plugin_whleague_revenue_system.handlers.player import PlayerHandler

        handler = PlayerHandler(_Plugin(env))
        out = [r async for r in handler.round_chart(_FakeEvent("/主场轮次统计图"))]
        assert out and "用法" in out[0], out
        out = [r async for r in handler.preview_chart(_FakeEvent("/主场轮次预告图"))]
        assert out and "用法" in out[0], out
        out = [r async for r in handler.season_chart(_FakeEvent("/主场赛季走势图 冠军杯"))]
        assert out and "顶级|次级" in out[0], out
        out = [r async for r in handler.round_chart(_FakeEvent("/主场轮次统计图 顶级"))]
        assert out and "轮次需包含数字" in out[0], out
    finally:
        await env.teardown()


async def test_player_help_admin_vs_player_sections():
    env = await TestEnv().setup()
    try:
        from astrbot_plugin_whleague_revenue_system.handlers.player import PlayerHandler

        handler = PlayerHandler(_Plugin(env))
        out = [r async for r in handler.help(_FakeEvent("/主场帮助", is_admin=True))]
        assert out and "管理命令" in out[0] and "赛程同步" in out[0], out
        out2 = [r async for r in handler.help(_FakeEvent("/主场帮助", is_admin=False))]
        assert out2 and "管理命令" not in out2[0] and "玩家命令" in out2[0], out2
    finally:
        await env.teardown()
