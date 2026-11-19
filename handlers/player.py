"""玩家命令：三张图与帮助（全体群友可查）。"""

from collections.abc import AsyncGenerator

from astrbot.api.event import MessageEventResult

from ..services.chart_service import ChartError
from ..services.formula import parse_round_token
from ..utils.help_text import build_help


class PlayerHandler:
    def __init__(self, plugin):
        self._plugin = plugin

    async def _run(self, event, coro):
        try:
            return await coro
        except (ChartError, ValueError) as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"操作失败：{type(e).__name__}"}

    async def help(self, event) -> AsyncGenerator[MessageEventResult, None]:
        is_admin = event.is_admin() or await self._plugin.dao.is_admin(event.get_sender_id())
        yield event.plain_result(build_help(is_admin))

    # ─── 统计图（全体群友可查） ───────────────────────────

    async def round_chart(self, event) -> AsyncGenerator[MessageEventResult, None]:
        parts = event.get_message_str().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法: /主场轮次统计图 <轮次>\n轮次支持前缀：顶级9/次级11/冠军3")
            return
        try:
            competition, round_no = parse_round_token(self._plugin.config_cache, parts[1])
        except ValueError as e:
            yield event.plain_result(str(e))
            return
        result = await self._run(
            event, self._plugin.chart_service.render_round_chart(round_no, competition)
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.image_result(result)

    async def season_chart(self, event) -> AsyncGenerator[MessageEventResult, None]:
        parts = event.get_message_str().split()
        token = parts[1].strip() if len(parts) >= 2 else ""
        competition = {"顶级": "顶级联赛", "次级": "次级联赛"}.get(token, "")
        if not competition:
            yield event.plain_result("用法: /主场赛季走势图 <顶级|次级>\n冠军杯无积分走势，不支持")
            return
        result = await self._run(event, self._plugin.chart_service.render_season_chart(competition))
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.image_result(result)

    async def preview_chart(self, event) -> AsyncGenerator[MessageEventResult, None]:
        parts = event.get_message_str().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法: /主场轮次预告图 <轮次>\n上半为对阵（已赛含比分）、下半为天气预报\n轮次支持前缀：顶级9/次级11/冠军3")
            return
        try:
            competition, round_no = parse_round_token(self._plugin.config_cache, parts[1])
        except ValueError as e:
            yield event.plain_result(str(e))
            return
        result = await self._run(
            event, self._plugin.chart_service.render_round_preview_chart(round_no, competition)
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.image_result(result)
