"""插件装配冒烟：main.py 可导入、命令已注册、handler 可调用。"""

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

    def get_sender_name(self):
        return "tester"

    def get_group_id(self):
        return "99999"

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        self.results.append(text)
        return text


async def test_plugin_import_and_commands():
    from astrbot_plugin_whleague_revenue_system import main as plugin_main

    # 检查注册的命令名
    src = inspect.getsource(plugin_main)
    expected_commands = [
        "主场赛程导入", "主场天气", "主场赛果", "主场轮次统计",
        "主场推进窗口", "主场推进赛季", "主场属性", "主场属性导入",
        "主场设施", "主场发放", "主场事件", "主场事件生成", "主场事件列表",
        "主场事件采纳", "主场事件丢弃", "主场事件写", "主场品牌生成",
        "主场品牌列表", "主场品牌采纳", "主场品牌丢弃", "主场结算",
        "主场设置", "主场查看配置", "主场添加管理", "主场删除管理",
        "主场", "主场信息", "主场赛季统计", "球场命名", "球场活动",
        "冠名", "退冠名", "主场财务",
    ]
    for cmd in expected_commands:
        assert f'@filter.command("{cmd}")' in src, f"缺少命令注册: {cmd}"


async def test_handler_smoke():
    env = await TestEnv().setup()
    try:
        # 赛程导入 handler（管理员）
        event = _FakeEvent("/主场赛程导入\n1 利物浦 巴塞罗那", sender="admin", is_admin=True)
        from astrbot_plugin_whleague_revenue_system.handlers.admin import AdminHandler

        handler = AdminHandler(env.__class__ and type("P", (), {
            "dao": env.dao,
            "config_cache": env.cfg,
            "fixture_service": env.fixture_service,
            "stadium_service": env.stadium_service,
            "event_engine": env.event_engine,
            "brand_service": env.brand_service,
            "window_service": env.window_service,
            "bridge": env.bridge,
            "_persist_config": lambda k, v: None,
        })())
        results = [r async for r in handler.import_fixtures(event)]
        assert results and "已导入" in results[0], results

        # 非管理员拒绝
        event2 = _FakeEvent("/主场赛程导入 1 利物浦 巴塞罗那", sender="10001")
        results2 = [r async for r in handler.import_fixtures(event2)]
        assert "没有权限" in results2[0], results2

        # 玩家查看（绑定 10001 → 利物浦）
        from astrbot_plugin_whleague_revenue_system.handlers.player import PlayerHandler

        ph = PlayerHandler(type("P", (), {
            "dao": env.dao,
            "config_cache": env.cfg,
            "fixture_service": env.fixture_service,
            "stadium_service": env.stadium_service,
            "brand_service": env.brand_service,
            "bridge": env.bridge,
        })())
        event3 = _FakeEvent("/主场", sender="10001")
        results3 = [r async for r in ph.my_stadium(event3)]
        assert results3 and "利物浦" in results3[0], results3
    finally:
        await env.teardown()