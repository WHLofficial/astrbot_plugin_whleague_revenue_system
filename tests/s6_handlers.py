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

    # 检查注册的命令名（v2.2：40 个，管理 29 + 玩家 11）
    src = inspect.getsource(plugin_main)
    expected_commands = [
        "主场赛程导入", "主场天气", "主场天气覆盖", "主场赛果录入", "主场推进", "主场赛季命名",
        "主场属性导入", "主场设施", "主场改名", "主场发放",
        "主场事件", "主场事件选择", "主场事件选择列表", "主场事件结算", "主场事件取消",
        "主场事件生成", "主场事件列表", "主场事件采纳", "主场事件丢弃", "主场事件写",
        "主场品牌生成", "主场品牌列表", "主场品牌采纳", "主场品牌丢弃", "主场结算",
        "主场设置", "主场查看配置", "主场添加管理", "主场删除管理",
        "主场帮助", "主场", "主场轮次统计", "主场赛季统计",
        "主场档期", "主场冠名", "主场退冠名", "主场财务",
        "主场轮次统计图", "主场赛季走势图", "主场轮次预告图",
    ]
    for cmd in expected_commands:
        assert f'@filter.command("{cmd}"' in src, f"缺少命令注册: {cmd}"

    # 旧名一律以 alias 过渡，不再单独注册
    for alias_line in (
        '@filter.command("主场赛果录入", alias={"主场赛果"})',
        '@filter.command("主场推进", alias={"主场推进窗口", "主场推进赛季"})',
        '@filter.command("主场事件选择", alias={"主场事件选择导入"})',
        '@filter.command("主场", alias={"主场信息"})',
        '@filter.command("主场档期", alias={"球场活动"})',
        '@filter.command("主场冠名", alias={"冠名"})',
        '@filter.command("主场退冠名", alias={"退冠名"})',
    ):
        assert alias_line in src, f"缺少别名注册: {alias_line}"
    for gone in ("主场属性", "主场事件选择导入", "主场推进窗口", "主场推进赛季",
                 "主场信息", "球场活动", "冠名", "退冠名", "主场赛果", "球场命名"):
        assert f'@filter.command("{gone}")' not in src, f"旧命令应移除/改为别名: {gone}"


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

        # 添加管理：非法 QQ 应返回提示而非抛异常
        event4 = _FakeEvent("/主场添加管理 不是数字", sender="admin", is_admin=True)
        results4 = [r async for r in handler.add_admin(event4)]
        assert results4 and "QQ" in results4[0], results4
    finally:
        await env.teardown()


async def test_admin_rename_handler():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=100.0)
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

        # 改名成功（新名可含空格，取队名后的整段）
        ev = _FakeEvent("/主场改名 利物浦 新 安菲尔德", sender="admin", is_admin=True)
        out = [r async for r in handler.rename_stadium(ev)]
        assert out and "新 安菲尔德" in out[0] and "利物浦主场" in out[0], out

        # 缺参 → 用法提示
        ev2 = _FakeEvent("/主场改名", sender="admin", is_admin=True)
        out2 = [r async for r in handler.rename_stadium(ev2)]
        assert out2 and "用法" in out2[0], out2

        # 非管理员拒绝
        ev3 = _FakeEvent("/主场改名 利物浦 X球场", sender="10001")
        out3 = [r async for r in handler.rename_stadium(ev3)]
        assert out3 and "没有权限" in out3[0], out3
    finally:
        await env.teardown()


async def test_choice_handlers_flow():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
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

        # 触发单队选择型事件（强制指定事件）→ 广播含选项
        ev = _FakeEvent("/主场事件 利物浦 merch_hit", sender="admin", is_admin=True)
        out = [r async for r in handler.trigger_event(ev)]
        assert out and "①" in out[0] and "周边爆款" in out[0], out

        # 单条录入选择（支持 ①②③④ 符号；单行走批量解析，一条即一条）
        ev2 = _FakeEvent("/主场事件选择 利物浦 周边爆款 ①", sender="admin", is_admin=True)
        out2 = [r async for r in handler.set_choice(ev2)]
        assert out2 and "已录入 1 条" in out2[0] and "周边爆款" in out2[0], out2

        # 列表展示已选状态
        ev3 = _FakeEvent("/主场事件选择列表", sender="admin", is_admin=True)
        out3 = [r async for r in handler.list_choices(ev3)]
        assert out3 and "已选" in out3[0] and "周边爆款" in out3[0], out3

        # 多行批量（重置为未定再录②）——同一命令吸收旧「选择导入」
        await env.dao.set_event_choice("利物浦", 1, 1, "merch_hit", None)
        ev4 = _FakeEvent("/主场事件选择\n利物浦 周边爆款 ②", sender="admin", is_admin=True)
        out4 = [r async for r in handler.set_choice(ev4)]
        assert out4 and "已录入 1 条" in out4[0] and "选2" in out4[0], out4

        # 无参数提示用法
        ev5 = _FakeEvent("/主场事件选择列表 0", sender="admin", is_admin=True)
        out5 = [r async for r in handler.list_choices(ev5)]
        assert out5 and "正整数" in out5[0], out5

        # 事件列表：即发显示效果数值、选择显示选项数
        ev6 = _FakeEvent("/主场事件列表", sender="admin", is_admin=True)
        out6 = [r async for r in handler.list_events(ev6)]
        joined = "\n".join(out6)
        assert "[即发]" in joined and "[选择]" in joined, joined
        assert "个选项" in joined, joined
        assert "政府补贴" in joined and 'money' in joined, joined

        # 独立事件结算：LLM 结果短文案应出现在回复中（而非只给数据变化行）
        env.provider.set_response("结算播报：周边爆款意外大卖，全场爆满。")
        ev8 = _FakeEvent("/主场事件结算", sender="admin", is_admin=True)
        out8 = [r async for r in handler.settle_events_now(ev8)]
        assert out8 and "事件结算" in out8[0], out8
        assert "结算播报：周边爆款意外大卖" in out8[0], out8
        ev9 = _FakeEvent("/主场事件结算 全部", sender="admin", is_admin=True)
        out9 = [r async for r in handler.settle_events_now(ev9)]
        assert out9 and ("事件结算" in out9[0] or "没有待结算" in out9[0]), out9
    finally:
        await env.teardown()


async def test_cancel_event_handler_flow():
    """v2.2：/主场事件取消——用法/只带队名列出/取消即发回退/取消选择/未知引用。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        from astrbot_plugin_whleague_revenue_system.handlers.admin import AdminHandler

        handler = AdminHandler(type("P", (), {
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

        # 无参用法
        out = [r async for r in handler.cancel_event(
            _FakeEvent("/主场事件取消", sender="admin", is_admin=True))]
        assert out and "用法" in out[0], out

        # 只带队名：该队本窗口事件（此时为空）
        out2 = [r async for r in handler.cancel_event(
            _FakeEvent("/主场事件取消 利物浦", sender="admin", is_admin=True))]
        assert out2 and "没有分配任何事件" in out2[0], out2

        # 触发即发 + 选择各一 → 列表显示类型/状态与事件 id
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="subsidy")
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="merch_hit")
        out3 = [r async for r in handler.cancel_event(
            _FakeEvent("/主场事件取消 利物浦", sender="admin", is_admin=True))]
        assert out3 and "即发·已生效" in out3[0] and "选择·待定" in out3[0], out3
        assert "subsidy" in out3[0] and "merch_hit" in out3[0], out3

        # 取消即发：成功文案 + 回退明细
        out4 = [r async for r in handler.cancel_event(
            _FakeEvent("/主场事件取消 利物浦 政府补贴", sender="admin", is_admin=True))]
        assert out4 and "已取消" in out4[0] and "资金流水已撤销" in out4[0], out4

        # 取消选择：待定移除、可重新触发
        out5 = [r async for r in handler.cancel_event(
            _FakeEvent("/主场事件取消 利物浦 周边爆款", sender="admin", is_admin=True))]
        assert out5 and "选择事件" in out5[0] and "可重新触发" in out5[0], out5

        # 未知引用（此前事件均已取消）→ 错误信息可读
        out6 = [r async for r in handler.cancel_event(
            _FakeEvent("/主场事件取消 利物浦 不存在的事件", sender="admin", is_admin=True))]
        assert out6 and "没有分配任何事件" in out6[0], out6
    finally:
        await env.teardown()


async def test_cancel_all_handler_flow():
    """v2.4：all 参数——只撤最近一次分配（按生成时间），大小写不敏感，无记录提示。"""
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        from astrbot_plugin_whleague_revenue_system.handlers.admin import AdminHandler

        handler = AdminHandler(type("P", (), {
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

        # 用法含 all
        out = [r async for r in handler.cancel_event(
            _FakeEvent("/主场事件取消", sender="admin", is_admin=True))]
        assert out and "[事件名|事件id|all]" in out[0], out

        # 批1 政府补贴 → 隔 1.1 秒 → 批2 暴雨滂沱（保证秒级时间戳不同）
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="subsidy")
        import asyncio

        await asyncio.sleep(1.1)
        await env.event_engine.trigger_team("利物浦", 1, 1, event_id="storm_buzz")

        # all：只撤批2，回复含明细；批1 保留
        out2 = [r async for r in handler.cancel_event(
            _FakeEvent("/主场事件取消 利物浦 all", sender="admin", is_admin=True))]
        assert out2 and "最近一次分配" in out2[0] and "共 1 个" in out2[0], out2
        assert "暴雨滂沱" in out2[0] and "上座修正已回退" in out2[0], out2
        rows = await env.event_engine.list_team_events("利物浦", 1, 1)
        assert [r for r in rows if r["event_id"] == "subsidy"], rows
        assert not [r for r in rows if r["event_id"] == "storm_buzz"], rows

        # 再 all（大写）撤批1
        out3 = [r async for r in handler.cancel_event(
            _FakeEvent("/主场事件取消 利物浦 ALL", sender="admin", is_admin=True))]
        assert out3 and "共 1 个" in out3[0] and "政府补贴" in out3[0], out3
        assert await env.event_engine.list_team_events("利物浦", 1, 1) == []

        # 无记录 → 提示
        out4 = [r async for r in handler.cancel_event(
            _FakeEvent("/主场事件取消 利物浦 all", sender="admin", is_admin=True))]
        assert out4 and "没有分配任何事件" in out4[0], out4
    finally:
        await env.teardown()


async def test_named_round_command_resolution():
    """天气/赛果命令输入纯文字轮次：解析到与导入登记一致的轮次号。"""
    env = await TestEnv().setup()
    try:
        await env.fixture_service.import_fixtures("顶级 利物浦 巴塞罗那")
        from astrbot_plugin_whleague_revenue_system.handlers.admin import AdminHandler

        handler = AdminHandler(type("P", (), {
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
        # /主场天气覆盖 顶级 利物浦 晴 → 解析到登记轮次 1(顶级联赛)
        ev = _FakeEvent("/主场天气覆盖 顶级 利物浦 晴", sender="admin", is_admin=True)
        out = [r async for r in handler.weather_override(ev)]
        assert out and "第 1 轮(顶级联赛)" in out[0] and "晴" in out[0], out
        # /主场赛果录入 顶级 … → 解析到登记轮次 1(顶级联赛)
        ev2 = _FakeEvent("/主场赛果录入 顶级\n利物浦 胜", sender="admin", is_admin=True)
        out2 = [r async for r in handler.record_results(ev2)]
        assert out2 and "第 1 轮(顶级联赛)" in out2[0], out2
    finally:
        await env.teardown()


async def test_v2_consolidation_admin():
    """v2.0 重组：推进枚举合并、天气读写拆分、属性导入吸收单队。"""
    env = await TestEnv().setup()
    try:
        await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那")
        await env.stadium_service.import_attributes("利物浦", influence=100.0)
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

        # 天气拆分：查询只带轮次；4 参引导覆盖命令；覆盖命令独立写入
        out = [r async for r in handler.forecast_weather(_FakeEvent("/主场天气 1", sender="admin", is_admin=True))]
        assert out and "天气预报" in out[0], out
        out = [r async for r in handler.forecast_weather(_FakeEvent("/主场天气 1 利物浦 晴", sender="admin", is_admin=True))]
        assert out and "主场天气覆盖" in out[0], out
        out = [r async for r in handler.weather_override(_FakeEvent("/主场天气覆盖 1 利物浦 雪", sender="admin", is_admin=True))]
        assert out and "天气改为 雪" in out[0], out

        # 属性导入：单行即单队更新（吸收旧 /主场属性）
        out = [r async for r in handler.import_attributes_batch(
            _FakeEvent("/主场属性导入 利物浦 120 - -", sender="admin", is_admin=True))]
        assert out and "✅ 利物浦" in out[0], out
        st = await env.dao.get_stadium("利物浦")
        assert abs(st["influence"] - 120.0) < 1e-6

        # 主场推进：枚举参数 / 非法值 / 缺参（放最后——推进会变更赛季状态）
        out = [r async for r in handler.advance(_FakeEvent("/主场推进 赛季", sender="admin", is_admin=True))]
        assert out and "已进入 第 2 赛季" in out[0], out
        out = [r async for r in handler.advance(_FakeEvent("/主场推进 明天", sender="admin", is_admin=True))]
        assert out and "参数需为" in out[0], out
        out = [r async for r in handler.advance(_FakeEvent("/主场推进", sender="admin", is_admin=True))]
        assert out and "用法" in out[0], out

        # 主场赛季命名：缺参用法 / 首次命名 / 改名 / 未变化 / 超长（当前=第 2 赛季）
        out = [r async for r in handler.name_season(_FakeEvent("/主场赛季命名", sender="admin", is_admin=True))]
        assert out and "用法" in out[0], out
        out = [r async for r in handler.name_season(
            _FakeEvent("/主场赛季命名 S8 黄金联赛", sender="admin", is_admin=True))]
        assert out and "已命名为「S8 黄金联赛」" in out[0], out
        out = [r async for r in handler.name_season(
            _FakeEvent("/主场赛季命名 S8 修订版", sender="admin", is_admin=True))]
        assert out and "由「S8 黄金联赛」改名为「S8 修订版」" in out[0], out
        out = [r async for r in handler.name_season(
            _FakeEvent("/主场赛季命名 S8 修订版", sender="admin", is_admin=True))]
        assert out and "未变化" in out[0], out
        out = [r async for r in handler.name_season(
            _FakeEvent(f"/主场赛季命名 {'超' * 31}", sender="admin", is_admin=True))]
        assert out and "名称最长" in out[0], out
        # 推进时直接命名新赛季：成功文案与落库
        out = [r async for r in handler.advance(
            _FakeEvent("/主场推进 赛季 S9 新赛季", sender="admin", is_admin=True))]
        assert out and "已进入 S9 新赛季 窗口 1" in out[0], out
        assert await env.dao.get_season_name(3) == "S9 新赛季"
    finally:
        await env.teardown()


async def test_v2_player_home_arg_and_help():
    """v2.0：/主场 带参查任意队；/主场帮助 分级；轮次统计玩家可用。"""
    env = await TestEnv().setup()
    try:
        await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那")
        await env.stadium_service.import_attributes("利物浦", influence=100.0)
        await env.fixture_service.record_results(1, "利物浦 胜", "联赛")
        from astrbot_plugin_whleague_revenue_system.handlers.player import PlayerHandler

        ph = PlayerHandler(type("P", (), {
            "dao": env.dao,
            "config_cache": env.cfg,
            "fixture_service": env.fixture_service,
            "stadium_service": env.stadium_service,
            "brand_service": env.brand_service,
            "bridge": env.bridge,
        })())

        # /主场 <队名> 直接查任意队（无需绑定）；卡片与轮次统计头展示赛季名
        await env.dao.set_season_name(1, "测试赛季", "tester")
        out = [r async for r in ph.my_stadium(_FakeEvent("/主场 利物浦", sender="99999"))]
        assert out and "利物浦" in out[0] and "测试赛季" in out[0], out

        # /主场帮助：玩家只见玩家段，管理员追加管理段
        out = [r async for r in ph.help(_FakeEvent("/主场帮助", sender="10001"))]
        assert out and "玩家命令" in out[0] and "管理命令" not in out[0], out
        out = [r async for r in ph.help(_FakeEvent("/主场帮助", sender="admin", is_admin=True))]
        assert out and "管理命令" in out[0], out

        # 轮次统计（玩家 handler，无管理门禁），标题含赛季名
        out = [r async for r in ph.round_stats(_FakeEvent("/主场轮次统计 1", sender="10001"))]
        assert out and "统计" in out[0] and "测试赛季" in out[0], out
    finally:
        await env.teardown()


async def test_none_returning_handlers_no_crash():
    """服务成功返回 None（采纳/丢弃/自定义）的 handler 成功路径不得崩溃。"""
    env = await TestEnv().setup()
    try:
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

        # 1) 自定义事件：event_engine.add_custom 返回 None → 成功路径
        ev = _FakeEvent('/主场事件写 测试事件 分类 5 {"money":1}', sender="admin", is_admin=True)
        out = [r async for r in handler.add_custom_event(ev)]
        assert out and "已加入事件池" in out[0], out

        # 2) 事件丢弃成功路径（生产 pending 后丢弃）
        env.provider.set_response(
            '[{"name":"待扔事件","category":"c","weight":5,'
            '"event_type":"instant","effects":{"money":1},"template":"t"}]'
        )
        await env.event_engine.generate_drafts(1)
        pending = await env.dao.list_events("pending")
        assert pending and pending[0]["name"] == "待扔事件"
        ev2 = _FakeEvent(f"/主场事件丢弃 {pending[0]['id']}", sender="admin", is_admin=True)
        out2 = [r async for r in handler.discard_event(ev2)]
        assert out2 and "已丢弃" in out2[0], out2
        # 丢弃后再丢同一条应给出提示而非崩溃
        ev2b = _FakeEvent(f"/主场事件丢弃 {pending[0]['id']}", sender="admin", is_admin=True)
        out2b = [r async for r in handler.discard_event(ev2b)]
        assert out2b and ("不存在" in out2b[0] or "已丢弃" in out2b[0]), out2b

        # 3) 品牌丢弃成功路径
        env.provider.set_response('[{"brand":"老八食堂","heat":1.2}]')
        await env.brand_service.generate_drafts(1)
        pend_b = await env.dao.list_brands("pending")
        assert pend_b and pend_b[0]["brand"] == "老八食堂"
        ev3 = _FakeEvent(f"/主场品牌丢弃 {pend_b[0]['id']}", sender="admin", is_admin=True)
        out3 = [r async for r in handler.discard_brand(ev3)]
        assert out3 and "已丢弃" in out3[0], out3
    finally:
        await env.teardown()