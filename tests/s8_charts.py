"""统计图：轮次表格图 / 赛季走势图渲染与命令路径。"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.chart_service import (  # noqa: E402
    ChartError,
    ChartService,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_ok(path) -> bool:
    data = Path(path).read_bytes()
    return data[:8] == _PNG_MAGIC and len(data) > 4000


def _color_count(path, target, tol=14) -> int:
    from PIL import Image

    im = Image.open(path).convert("RGB")
    try:
        w, h = im.size
        px = im.load()
        return sum(1 for y in range(0, h, 3) for x in range(0, w, 3)
                   if abs(px[x, y][0] - target[0]) < tol
                   and abs(px[x, y][1] - target[1]) < tol
                   and abs(px[x, y][2] - target[2]) < tol)
    finally:
        im.close()


def _header_band_count(path, header_rgb, tol=10) -> int:
    """统计图中表头填充色出现的横向色带数（用于确认多区块都存在）。"""
    from PIL import Image

    im = Image.open(path).convert("RGB")
    try:
        w, h = im.size
        px = im.load()
        bands, in_band = 0, False
        for y in range(h):
            run = sum(
                1 for x in range(0, w, 4)
                if abs(px[x, y][0] - header_rgb[0]) < tol
                and abs(px[x, y][1] - header_rgb[1]) < tol
                and abs(px[x, y][2] - header_rgb[2]) < tol
            )
            if run > 30 and not in_band:
                bands += 1
                in_band = True
            elif run <= 30:
                in_band = False
        return bands
    finally:
        im.close()


PURPLE = (177, 156, 217)
ORANGE = (233, 113, 50)
GREEN = (92, 180, 142)


async def _seed_round(env, round_no: int = 1, with_two: bool = False):
    for team, inf in [("利物浦", 150.0), ("巴塞罗那", 120.0),
                      ("纽卡斯尔联", 130.0), ("勒沃库森", 110.0)]:
        await env.stadium_service.import_attributes(team, influence=inf)
    lines = f"{round_no} 利物浦 巴塞罗那"
    if with_two:
        lines += f"\n{round_no} 纽卡斯尔联 勒沃库森"
    await env.fixture_service.import_fixtures(lines)
    for home in (("利物浦", "纽卡斯尔联") if with_two else ("利物浦",)):
        await env.fixture_service.set_weather(round_no, home, "晴")
    results = "利物浦 胜" + ("\n纽卡斯尔联 平" if with_two else "")
    await env.fixture_service.record_results(round_no, results)


async def test_round_chart_renders():
    env = await TestEnv().setup()
    try:
        await _seed_round(env, with_two=True)
        svc = ChartService(env.db, env.dao, env.cfg)
        path = await svc.render_round_chart(1)
        assert _png_ok(path), path
        assert "round_s1" in path
        # 存档图表目录
        assert (Path(env.db.db_path).parent / "charts").is_dir()
    finally:
        await env.teardown()


async def test_season_chart_renders():
    env = await TestEnv().setup()
    try:
        await _seed_round(env, round_no=1, with_two=True)
        await env.fixture_service.advance_window("tester")
        await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那")
        await env.fixture_service.set_weather(1, "利物浦", "雨")
        await env.fixture_service.record_results("1", "利物浦 负")
        svc = ChartService(env.db, env.dao, env.cfg)
        path = await svc.render_season_chart()
        assert _png_ok(path), path
        assert "season_s1" in path
    finally:
        await env.teardown()


async def test_chart_no_data_errors():
    env = await TestEnv().setup()
    try:
        svc = ChartService(env.db, env.dao, env.cfg)
        try:
            await svc.render_round_chart(1)
            assert False, "无赛程应报错"
        except ChartError as e:
            assert "没有已录入赛果" in str(e)
        # 有赛程但未录赛果
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.stadium_service.import_attributes("巴塞罗那", influence=120.0)
        await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那")
        try:
            await svc.render_round_chart(1)
            assert False, "无赛果应报错"
        except ChartError as e:
            assert "没有已录入赛果" in str(e)
        try:
            await svc.render_season_chart()
            assert False, "赛季无数据应报错"
        except ChartError as e:
            assert "还没有已录赛果" in str(e)
    finally:
        await env.teardown()


async def test_round_chart_full_columns():
    env = await TestEnv().setup()
    try:
        await env.stadium_service.import_attributes("利物浦", influence=150.0)
        await env.stadium_service.import_attributes("巴塞罗那", influence=120.0)
        await env.fixture_service.import_fixtures("顶级9 利物浦 巴塞罗那 W12 D6 15:00")
        await env.fixture_service.set_weather(9, "利物浦", "晴", "顶级联赛")
        await env.fixture_service.record_results(9, "利物浦 胜 2-1", "顶级联赛")
        svc = ChartService(env.db, env.dao, env.cfg)
        path = await svc.render_round_chart(9, "顶级联赛")
        assert _png_ok(path), path
        from PIL import Image

        im = Image.open(path)
        try:
            w, h = im.size
        finally:
            im.close()
        assert w > 1400, f"11 列图表应更宽: {w}x{h}"
    finally:
        await env.teardown()


async def test_preview_chart_renders():
    env = await TestEnv().setup()
    try:
        for team, inf in [("利物浦", 150.0), ("巴塞罗那", 120.0),
                          ("纽卡斯尔联", 130.0), ("勒沃库森", 110.0)]:
            await env.stadium_service.import_attributes(team, influence=inf)
        await env.fixture_service.import_fixtures(
            "顶级9 利物浦 巴塞罗那 W12 D6 15:00\n顶级9 纽卡斯尔联 勒沃库森 W12 D6 20:00\n"
        )
        # 只预报第一场，第二场留「待预报」
        await env.fixture_service.set_weather(9, "利物浦", "晴", "顶级联赛")
        svc = ChartService(env.db, env.dao, env.cfg)
        path = await svc.render_round_preview_chart(9, "顶级联赛")
        assert _png_ok(path), path
        from PIL import Image

        im = Image.open(path)
        try:
            w, h = im.size
        finally:
            im.close()
        assert h > 400, f"两块堆叠应更高: {w}x{h}"
        # 命令路径
        from astrbot_plugin_whleague_revenue_system.handlers.player import PlayerHandler

        ph = PlayerHandler(type("P", (), {
            "dao": env.dao,
            "config_cache": env.cfg,
            "fixture_service": env.fixture_service,
            "stadium_service": env.stadium_service,
            "brand_service": env.brand_service,
            "bridge": env.bridge,
            "chart_service": svc,
        })())
        ev = _FakeEvent("/主场轮次预告图 顶级9")
        out = [r async for r in ph.preview_chart(ev)]
        assert out and isinstance(out[0], str) and _png_ok(out[0]), out
        ev2 = _FakeEvent("/主场轮次预告图")
        out2 = [r async for r in ph.preview_chart(ev2)]
        assert out2 and "用法" in out2[0], out2
    finally:
        await env.teardown()


async def test_preview_chart_no_fixtures():
    env = await TestEnv().setup()
    try:
        svc = ChartService(env.db, env.dao, env.cfg)
        try:
            await svc.render_round_preview_chart(1)
            assert False, "无赛程应报错"
        except ChartError as e:
            assert "还没有赛程" in str(e)
    finally:
        await env.teardown()


async def test_chart_competition_colors():
    env = await TestEnv().setup()
    try:
        for team, inf in [("利物浦", 150.0), ("巴塞罗那", 120.0),
                          ("纽卡斯尔联", 130.0), ("勒沃库森", 110.0)]:
            await env.stadium_service.import_attributes(team, influence=inf)
        r = await env.fixture_service.import_fixtures(
            "顶级9 利物浦 巴塞罗那\n次级9 纽卡斯尔联 勒沃库森\n冠军3 利物浦 勒沃库森\n"
        )
        assert r["imported"] == 3, r
        for rnd, comp, line in [
            (9, "顶级联赛", "利物浦 胜"),
            (9, "次级联赛", "纽卡斯尔联 平"),
            (3, "冠军杯", "利物浦 负"),
        ]:
            await env.fixture_service.set_weather(rnd, line.split()[0], "晴", comp)
            await env.fixture_service.record_results(rnd, line, comp)
        svc = ChartService(env.db, env.dao, env.cfg)
        p_top = await svc.render_round_chart(9, "顶级联赛")
        p_sub = await svc.render_round_chart(9, "次级联赛")
        p_cup = await svc.render_round_chart(3, "冠军杯")
        assert _color_count(p_top, ORANGE) > 200, "顶级联赛应为橙色表头"
        assert _color_count(p_sub, PURPLE) > 200, "次级联赛应为紫色表头"
        assert _color_count(p_cup, GREEN) > 200, "冠军杯应为绿色表头"
        assert _color_count(p_top, PURPLE) == 0, "顶级图不应出现紫色"
        assert _color_count(p_sub, ORANGE) == 0, "次级图不应出现橙色"
        # 默认联赛（无前缀）用绿色
        await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那")
        await env.fixture_service.record_results(1, "利物浦 胜")
        p_league = await svc.render_round_chart(1)
        assert _color_count(p_league, GREEN) > 200, "默认联赛应为绿色表头"
    finally:
        await env.teardown()


class _FakeEvent:
    def __init__(self, message: str, sender: str = "10001"):
        self.message = message
        self.sender = sender
        self.results = []

    def get_message_str(self):
        return self.message

    def get_sender_id(self):
        return self.sender

    def get_sender_name(self):
        return "tester"

    def get_group_id(self):
        return "99999"

    def get_messages(self):
        return []

    def is_admin(self):
        return False

    def plain_result(self, text):
        self.results.append(text)
        return text

    def image_result(self, path):
        self.results.append(path)
        return path


async def test_chart_commands():
    env = await TestEnv().setup()
    try:
        await _seed_round(env, with_two=True)
        from astrbot_plugin_whleague_revenue_system.handlers.player import PlayerHandler

        ph = PlayerHandler(type("P", (), {
            "dao": env.dao,
            "config_cache": env.cfg,
            "fixture_service": env.fixture_service,
            "stadium_service": env.stadium_service,
            "brand_service": env.brand_service,
            "bridge": env.bridge,
            "chart_service": ChartService(env.db, env.dao, env.cfg),
        })())
        ev = _FakeEvent("/主场轮次统计图 1")
        out = [r async for r in ph.round_chart(ev)]
        assert out and isinstance(out[0], str) and _png_ok(out[0]), out
        # 非管理员也可查（命令本身无权限校验）
        ev2 = _FakeEvent("/主场轮次统计图")
        out2 = [r async for r in ph.round_chart(ev2)]
        assert out2 and "用法" in out2[0], out2
        ev3 = _FakeEvent("/主场赛季走势图 3")
        out3 = [r async for r in ph.season_chart(ev3)]
        assert out3 and isinstance(out3[0], str) and _png_ok(out3[0]), out3
    finally:
        await env.teardown()


async def test_chart_bundled_font_and_override():
    """内置字体存在可加载；chart_font_path 覆盖优先于系统扫描。"""
    from astrbot_plugin_whleague_revenue_system.services.chart_service import (
        _font,
        _list_plugin_fonts,
        set_font_override,
    )

    bundled = _list_plugin_fonts()
    assert bundled, "插件应内置中文字体"
    assert os.path.exists(bundled[0])
    # 覆盖到内置字体：_font 不应抛 ChartError，且能拿到 Regular 字重
    set_font_override(bundled[0])
    try:
        f = _font(20)
        assert f is not None
    finally:
        set_font_override("")
    # 无效覆盖路径 → 回退系统字体（本机 msyh 在候选清单内），不抛错
    set_font_override(r"C:\Windows\Fonts\not_exist.ttf")
    try:
        _font(20, bold=False)
    finally:
        set_font_override("")


async def test_chart_bundled_font_weight_differentiates():
    """同一内置变量字体下，bold=True 必须真的切到粗体（避免全图等重 Regular）。

    CJK 字形各字重前进宽度一致（全宽方块），但笔画墨量随字重增加，故用渲染墨量判别。
    """
    from PIL import Image, ImageDraw

    from astrbot_plugin_whleague_revenue_system.services.chart_service import (
        _font,
        _list_plugin_fonts,
        set_font_override,
    )

    def _ink(font) -> int:
        img = Image.new("RGB", (260, 140), "white")
        dr = ImageDraw.Draw(img)
        dr.text((8, 8), "合计观众场次", font=font, fill="black")
        px = img.load()
        return sum(1 for y in range(img.size[1]) for x in range(img.size[0])
                   if px[x, y][0] < 128)

    bundled = _list_plugin_fonts()
    assert bundled, "插件应内置中文字体"
    set_font_override(bundled[0])
    try:
        ink_regular = _ink(_font(40, bold=False))
        ink_bold = _ink(_font(40, bold=True))
        assert ink_bold > ink_regular * 1.3, (
            f"内置字体下粗体墨量应明显更大: regular={ink_regular} bold={ink_bold}"
        )
    finally:
        set_font_override("")


async def test_chart_long_team_names_and_preview_height():
    """超长队名渲染不崩溃；预告图高度按实际绘制范围（无底部多余空白）。"""
    from PIL import Image

    env = await TestEnv().setup()
    try:
        long_a = "皇家马德里足球俱乐部阿斯图里亚斯竞技队"
        long_b = "巴塞罗那城际联合体育协会精英队"
        for team, inf in [(long_a, 150.0), (long_b, 130.0)]:
            await env.stadium_service.import_attributes(team, influence=inf)
        await env.fixture_service.import_fixtures(f"顶级9 {long_a} {long_b}\n")
        await env.fixture_service.set_weather(9, long_a, "晴", "顶级联赛")
        await env.fixture_service.record_results(9, f"{long_a} 胜 2-1", "顶级联赛")
        svc = ChartService(env.db, env.dao, env.cfg)
        pv = await svc.render_round_preview_chart(9, "顶级联赛")
        assert _png_ok(pv), pv
        im = Image.open(pv)
        try:
            w, h = im.size
        finally:
            im.close()
        # 单场预告图高度 = content*2 + 10（无底部多余 pad）
        assert h == 406, f"预告图高度应为紧致 406，实为 {w}x{h}"
        rp = await svc.render_round_chart(9, "顶级联赛")
        assert _png_ok(rp), rp
    finally:
        await env.teardown()


async def test_preview_chart_weather_block_present():
    """预告图必须包含两个区块（对阵表 + 天气预报），天气块不能被挤出画布。"""
    env = await TestEnv().setup()
    try:
        for team, inf in [("利物浦", 150.0), ("巴塞罗那", 120.0),
                          ("纽卡斯尔联", 130.0), ("勒沃库森", 110.0)]:
            await env.stadium_service.import_attributes(team, influence=inf)
        await env.fixture_service.import_fixtures(
            "顶级9 利物浦 巴塞罗那 W12 D6 15:00\n顶级9 纽卡斯尔联 勒沃库森 W12 D6 20:00\n"
        )
        await env.fixture_service.set_weather(9, "利物浦", "晴", "顶级联赛")
        svc = ChartService(env.db, env.dao, env.cfg)
        pv = await svc.render_round_preview_chart(9, "顶级联赛")
        assert _png_ok(pv), pv
        assert _header_band_count(pv, ORANGE) >= 2, "预告图应含对阵表 + 天气两个表头色带"
    finally:
        await env.teardown()


async def test_chart_cancelled_match():
    """取消场次：轮次图能出（观众 -、不算场均）、全取消场均=0；走势图含 0 点。"""
    env = await TestEnv().setup()
    try:
        for team, inf in [("利物浦", 150.0), ("巴塞罗那", 120.0), ("纽卡斯尔联", 130.0)]:
            await env.stadium_service.import_attributes(team, influence=inf)
        await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n1 纽卡斯尔联 勒沃库森"
        )
        await env.fixture_service.set_weather(1, "纽卡斯尔联", "晴")
        await env.fixture_service.record_results(1, "利物浦 取消\n纽卡斯尔联 胜")
        svc = ChartService(env.db, env.dao, env.cfg)
        p1 = await svc.render_round_chart(1)
        assert _png_ok(p1), p1
        # 全取消轮次：场均 0、不除零，图仍能出
        await env.fixture_service.import_fixtures("2 巴塞罗那 利物浦")
        await env.fixture_service.record_results(2, "巴塞罗那 取消")
        p2 = await svc.render_round_chart(2)
        assert _png_ok(p2), p2
        st2 = await env.fixture_service.round_stats(2)
        assert any("取消" in ln for ln in st2["lines"]), st2
        assert st2["totals"]["attendance"] == 0, st2["totals"]
        # 走势图含取消（0 标记）
        pt = await svc.render_season_chart()
        assert _png_ok(pt), pt
    finally:
        await env.teardown()