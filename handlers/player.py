"""玩家命令：主场查看、赛季统计、命名、档期活动、冠名、财务。"""

from collections.abc import AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import MessageEventResult

from ..services.brand_service import BrandError
from ..services.chart_service import ChartError
from ..services import formula
from ..services.stadium_service import StadiumError
from ..utils.security import format_m

_FACILITY_DISPLAY = {
    "commercial": "商业区",
    "broadcast": "灯光转播",
    "pitch": "草皮",
    "youth": "青训中心",
    "medical": "医疗中心",
}

_TIER_NAMES = ["社区级", "地区级", "大区级", "国家级", "国际级"]

_ACTIVITY_HINT = "演唱会/电竞/球迷开放日/青训营/空置"


class PlayerHandler:
    def __init__(self, plugin):
        self._plugin = plugin

    async def _run(self, event, coro):
        try:
            return await coro
        except (StadiumError, BrandError, ChartError, ValueError, IndexError) as e:
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"Player handler error: {e}")
            return {"error": "操作失败，已记录错误"}

    async def _my_team(self, event) -> str:
        bridge = self._plugin.bridge
        if bridge is None or not await bridge.is_available():
            raise StadiumError("谈判系统不可用，无法确认你的球队")
        binding = await bridge.get_binding_by_qq(event.get_sender_id())
        if not binding:
            raise StadiumError("你尚未绑定球队，请先在谈判系统 /绑定球队")
        return binding["team_name"]

    @staticmethod
    def _tier_name(tier: int) -> str:
        return _TIER_NAMES[tier] if 0 <= tier < len(_TIER_NAMES) else f"等级{tier}"

    # ─── 查看 ─────────────────────────────────────────────

    async def my_stadium(self, event) -> AsyncGenerator[MessageEventResult, None]:
        try:
            team = await self._my_team(event)
        except StadiumError as e:
            yield event.plain_result(str(e))
            return
        result = await self._run(event, self._build_view(team))
        yield event.plain_result(result if isinstance(result, str) else str(result))

    async def view_stadium(self, event) -> AsyncGenerator[MessageEventResult, None]:
        parts = event.get_message_str().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法: /主场信息 <队名>")
            return
        team = parts[1].strip()
        result = await self._run(event, self._build_view(team))
        yield event.plain_result(result if isinstance(result, str) else str(result))

    async def _build_view(self, team: str) -> str:
        dao = self._plugin.dao
        stadium = await dao.get_stadium(team)
        if not stadium:
            raise StadiumError(f"球队「{team}」还没有球场，请联系管理员发放")
        facilities = await dao.get_facilities(team)
        balance = await dao.get_balance(team)
        naming = await dao.get_active_naming(team)
        state = await dao.get_league_state()
        display_name = stadium["name"]
        if naming:
            display_name = f"{naming['brand']}·{stadium['name']}"
        fac_lines = "，".join(
            f"{_FACILITY_DISPLAY.get(k, k)}{v}级"
            for k, v in sorted(facilities.items()) if v > 0
        ) or "无"
        lines = [
            f"🏟 {display_name}",
            f"· 球队: {team}",
            f"· 等级: {stadium['tier']}级 {self._tier_name(stadium['tier'])} | 容量: {stadium['capacity']:,} 座",
            f"· 影响力: {stadium['influence']:g} | 死忠球迷: {stadium['fans_diehards']:,.0f}",
            f"· 设施: {fac_lines}",
        ]
        if naming:
            lines.append(f"· 冠名: {naming['brand']}（剩 {naming['windows_remaining']} 窗口，{naming['fee_per_window']}M/窗口）")
        if balance:
            lines.append(f"· 余额: {format_m(balance['balance'])}M（建设券 {format_m(balance['build_credit'])}M）")
        if state:
            lines.append(f"· 赛季 {state['season_number']} 窗口 {state['window_seq']} 轮次 {state['current_round']}")
        return "\n".join(lines)

    async def season_stats(self, event) -> AsyncGenerator[MessageEventResult, None]:
        try:
            stats = await self._plugin.fixture_service.season_stats()
        except Exception as e:
            logger.error(f"Season stats error: {e}")
            yield event.plain_result("统计失败，已记录错误")
            return
        if not stats["rows"]:
            yield event.plain_result("暂无已录入赛果的上座数据")
            return
        lines = ["📊 赛季上座统计（S8 汇总样式）", "队名 | 赛 | 总上座 | 最高 | 最低 | 平均 | 总票房(M)"]
        for r in stats["rows"]:
            lines.append(
                f"{r['team']} | {r['home_count']} | {r['total']:,} | {r['max']:,} | {r['min']:,} | {r['avg']:,} | {r['ticket']}"
            )
        g = stats["grand"]
        lines.append(f"合计: {g['attendance']:,} 人次 / {g['ticket']:.2f}M 票房")
        yield event.plain_result("\n".join(lines))

    # ─── 统计图（全体群友可查） ───────────────────────────

    async def round_chart(self, event) -> AsyncGenerator[MessageEventResult, None]:
        parts = event.get_message_str().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法: /主场轮次统计图 <轮次>\n轮次支持前缀：顶级9/次级11/冠军3（默认联赛）")
            return
        try:
            competition, round_no = formula.parse_round_token(self._plugin.config_cache, parts[1])
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
        result = await self._run(event, self._plugin.chart_service.render_season_chart())
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.image_result(result)

    async def preview_chart(self, event) -> AsyncGenerator[MessageEventResult, None]:
        parts = event.get_message_str().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法: /主场轮次预告图 <轮次>\n上半为对阵、下半为天气预报\n轮次支持前缀：顶级9/次级11/冠军3（默认联赛）")
            return
        try:
            competition, round_no = formula.parse_round_token(self._plugin.config_cache, parts[1])
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

    # ─── 改名 ─────────────────────────────────────────────

    async def rename(self, event) -> AsyncGenerator[MessageEventResult, None]:
        parts = event.get_message_str().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法: /球场命名 <名称>（首次免费，之后 2M）")
            return
        try:
            team = await self._my_team(event)
        except StadiumError as e:
            yield event.plain_result(str(e))
            return
        result = await self._run(
            event, self._plugin.stadium_service.rename(team, parts[1])
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        fee_txt = f"（花费 {result['fee']}M）" if result["fee"] else "（首次免费）"
        yield event.plain_result(f"✅ 球场已命名为「{result['new']}」{fee_txt}")

    # ─── 档期活动 ─────────────────────────────────────────

    async def book_activity(self, event) -> AsyncGenerator[MessageEventResult, None]:
        parts = event.get_message_str().split()
        if len(parts) < 2:
            yield event.plain_result(f"用法: /球场活动 <类型> [档位1-2]（类型: {_ACTIVITY_HINT}）")
            return
        try:
            team = await self._my_team(event)
        except StadiumError as e:
            yield event.plain_result(str(e))
            return
        activity_type = self._resolve_activity(parts[1])
        if activity_type is None:
            yield event.plain_result(f"未知活动类型: {parts[1]}（{_ACTIVITY_HINT}）")
            return
        slot = int(parts[2]) if len(parts) >= 3 else 1
        max_slots = int(self._plugin.config_cache.get("activity_slots", 2))
        if not (1 <= slot <= max_slots):
            yield event.plain_result(f"档位需在 1~{max_slots} 之间")
            return
        state = await self._plugin.dao.get_league_state()
        result = await self._run(
            event,
            self._book(team, activity_type, slot, state),
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result(f"✅ 已预订本窗口档位{slot}: {result['name']}")

    async def _book(self, team: str, activity_type: str, slot: int, state) -> dict:
        dao = self._plugin.dao
        await self._plugin.stadium_service.ensure_stadium(team)
        season = state["season_number"] if state else 1
        window_seq = state["window_seq"] if state else 1
        await dao.add_booking(team, season, window_seq, slot, activity_type, "")
        from ..services import formula

        return {"name": formula.activity_names(self._plugin.config_cache).get(activity_type, activity_type)}

    @staticmethod
    def _resolve_activity(raw: str) -> str | None:
        aliases = {
            "concert": "concert", "演唱会": "concert", "音乐节": "concert",
            "esports": "esports", "电竞": "esports", "电竞赛事": "esports",
            "open_day": "open_day", "开放日": "open_day", "球迷开放日": "open_day", "球迷日": "open_day",
            "youth_camp": "youth_camp", "青训": "youth_camp", "夏令营": "youth_camp", "青训营": "youth_camp",
            "idle": "idle", "空置": "idle", "无": "idle",
        }
        return aliases.get(raw.strip().lower())

    # ─── 冠名 ─────────────────────────────────────────────

    async def sign_naming(self, event) -> AsyncGenerator[MessageEventResult, None]:
        parts = event.get_message_str().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法: /冠名 <品牌>（可用 /主场信息 查看品牌列表提示）")
            return
        try:
            team = await self._my_team(event)
        except StadiumError as e:
            yield event.plain_result(str(e))
            return
        state = await self._plugin.dao.get_league_state()
        season = state["season_number"] if state else 1
        window_seq = state["window_seq"] if state else 1
        result = await self._run(
            event, self._plugin.brand_service.sign(team, parts[1], season, window_seq)
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result(
            f"✅ 「{result['brand']}」冠名 {result['team']}！{result['fee_per_window']}M/窗口 × {result['windows']} 窗口"
        )

    async def terminate_naming(self, event) -> AsyncGenerator[MessageEventResult, None]:
        try:
            team = await self._my_team(event)
        except StadiumError as e:
            yield event.plain_result(str(e))
            return
        state = await self._plugin.dao.get_league_state()
        season = state["season_number"] if state else 1
        window_seq = state["window_seq"] if state else 1
        result = await self._run(
            event, self._plugin.brand_service.terminate(team, season, window_seq, initiated_by="team")
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        penalty = f"，赔付 {result['penalty']}M" if result["penalty"] else ""
        yield event.plain_result(f"✅ 已与「{result['brand']}」解约{penalty}")

    # ─── 财务 ─────────────────────────────────────────────

    async def my_finance(self, event) -> AsyncGenerator[MessageEventResult, None]:
        parts = event.get_message_str().split()
        try:
            team = await self._my_team(event)
        except StadiumError as e:
            yield event.plain_result(str(e))
            return
        state = await self._plugin.dao.get_league_state()
        season = int(state["season_number"]) if state else 1
        window_seq = None
        if len(parts) >= 2 and parts[1].isdigit():
            window_seq = int(parts[1])
        rows = await self._plugin.dao.list_transactions(
            team, season=season, window_seq=window_seq, limit=50
        )
        if not rows:
            yield event.plain_result("本窗口暂无流水")
            return
        balance = await self._plugin.dao.get_balance(team)
        lines = [f"💳 {team} 财务（赛季{season}" + (f" 窗口{window_seq}" if window_seq else "") + "）"]
        total = 0.0
        for r in reversed(rows):
            total += r["amount"]
            lines.append(
                f"{'🟢' if r['amount'] >= 0 else '🔴'} {r['kind']} {format_m(r['amount'])}M | {r['note']}"
            )
        if balance:
            lines.append(f"余额 {format_m(balance['balance'])}M（建设券 {format_m(balance['build_credit'])}M）")
        yield event.plain_result("\n".join(lines))