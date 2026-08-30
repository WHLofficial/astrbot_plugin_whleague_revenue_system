"""管理命令：赛程/天气/赛果/统计、赛季推进与命名、属性导入、事件与品牌（含 LLM 设计）、结算、配置。"""

import json
from collections.abc import AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import MessageEventResult

from ..config.defaults import validate_and_cast
from ..services.backfill_service import BackfillError
from ..services.brand_service import BrandError
from ..services.event_engine import EventError
from ..services.file_import_service import FileImportError
from ..services.fixture_service import FixtureError
from ..services.stadium_service import StadiumError
from ..services.formula import SELL_OUT_FILL
from ..services.window_service import SettleError
from ..utils.security import format_int, format_m, parse_float, parse_int, parse_qq, parse_qq_arg

_FACILITY_ALIASES = {
    "商业区": "commercial", "商业": "commercial", "commercial": "commercial",
    "灯光转播": "broadcast", "转播": "broadcast", "灯光": "broadcast", "broadcast": "broadcast",
    "草皮": "pitch", "草坪": "pitch", "pitch": "pitch",
    "青训中心": "youth", "青训": "youth", "youth": "youth",
    "医疗中心": "medical", "医疗": "medical", "medical": "medical",
}


class AdminHandler:
    def __init__(self, plugin):
        self._plugin = plugin

    async def _require_admin(self, event) -> bool:
        if event.is_admin():
            return True
        return await self._plugin.dao.is_admin(event.get_sender_id())

    async def _run(self, event, coro):
        try:
            result = await coro
            # 服务成功但返回 None（采纳/丢弃/自定义等）统一归一为空 dict，
            # 避免上层 `"error" in None`；list（选择列表/批量导入）原样保留
            return result if result is not None else {}
        except (StadiumError, FixtureError, EventError, BrandError, SettleError,
                FileImportError, BackfillError, ValueError) as e:
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"Admin handler error: {e}")
            return {"error": "操作失败，已记录错误"}

    async def _guard(self, event) -> str | None:
        """权限门禁：无权限时返回错误文案，调用方 yield 后 return。"""
        if not await self._require_admin(event):
            return "你没有权限执行此操作"
        return None

    @staticmethod
    def _import_files_hint(service) -> str:
        """imports 目录现有可导入文件提示（命令无参/缺参时引导用）。"""
        names = service.list_import_files()
        return "、".join(names) if names else "（空，把 .csv/.xlsx 放进该目录后传文件名）"

    # ─── 赛程 ─────────────────────────────────────────────

    async def import_fixtures(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        svc = self._plugin.fixture_service
        parts = event.get_message_str().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result(
                "用法: /主场赛程导入 <赛程文件.csv/xlsx> 或直接粘贴赛程\n"
                "每行：轮次 主队 客队 [周] [天] [时间]；例：1 利物浦 巴塞罗那 W12 D6 15:00\n"
                f"📁 imports 目录当前有：{self._import_files_hint(svc)}"
            )
            return
        try:
            path = svc.resolve_import_file(parts[1])
        except FileImportError as e:
            yield event.plain_result(str(e))
            return
        if path is not None:
            result = await self._run(event, svc.import_fixtures_file(path))
        else:
            result = await self._run(event, svc.import_fixtures(parts[1]))
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        season_label = await self._plugin.dao.season_label(result['season'])
        lines = [f"📋 已导入 {season_label} 窗口 {result['window_seq']} 赛程 {result['imported']} 场（跳过 {result['skipped']}）"]
        for e in list(result.get("file_errors", [])) + list(result.get("errors", [])):
            lines.append(f"⚠️ {e}")
        yield event.plain_result("\n".join(lines))

    async def forecast_weather(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        if len(parts) >= 4:
            yield event.plain_result(
                "查询整轮预报只带轮次；要覆盖单场天气请用 /主场天气覆盖 <轮次> <主队> <天气>"
            )
            return
        if len(parts) < 2:
            yield event.plain_result("用法: /主场天气 <轮次>\n轮次支持前缀：顶级9/次级11/冠军3（默认联赛）")
            return
        try:
            competition, round_no = await self._plugin.fixture_service.resolve_round_arg(parts[1])
        except ValueError as e:
            yield event.plain_result(str(e))
            return
        result = await self._run(event, self._plugin.fixture_service.forecast_round(round_no, competition))
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        icons = {"晴": "☀️", "多云": "⛅", "雨": "🌧️", "雪": "❄️"}
        lines = [f"🌤 第 {round_no} 轮({competition})天气预报"]
        for m in result["matches"]:
            mark = "（已定）" if m["existing"] else ""
            lines.append(f"· {m['home']}: {icons.get(m['weather'], '')}{m['weather']}{mark}")
        yield event.plain_result("\n".join(lines))

    async def weather_override(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        if len(parts) < 4:
            yield event.plain_result("用法: /主场天气覆盖 <轮次> <主队> <天气>（晴/多云/雨/雪）")
            return
        try:
            competition, round_no = await self._plugin.fixture_service.resolve_round_arg(parts[1])
        except ValueError as e:
            yield event.plain_result(str(e))
            return
        result = await self._run(
            event,
            self._plugin.fixture_service.set_weather(round_no, parts[2], parts[3], competition),
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result(f"☀️ 第 {round_no} 轮({competition})「{result['home']}」天气改为 {result['weather']}")

    async def record_results(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        svc = self._plugin.fixture_service
        parts = event.get_message_str().split(maxsplit=2)
        if len(parts) < 2:
            yield event.plain_result("用法: /主场赛果录入 <轮次> [赛果文本或赛果文件.csv/xlsx]\n每行：主队 胜/平/负 [比分] 或 主队 取消（如 利物浦 胜 2-1）\n轮次支持前缀：顶级9/次级11/冠军3（默认联赛）；文件来自 imports 目录")
            return
        try:
            competition, round_no = await svc.resolve_round_arg(parts[1])
        except ValueError as e:
            yield event.plain_result(str(e))
            return
        if len(parts) < 3:
            yield event.plain_result(
                "用法: /主场赛果录入 <轮次> [赛果文本或赛果文件.csv/xlsx]\n"
                "每行：主队 胜/平/负 [比分] 或 主队 取消（如 利物浦 胜 2-1）；文件来自 imports 目录\n"
                f"📁 imports 目录当前有：{self._import_files_hint(svc)}"
            )
            return
        try:
            path = svc.resolve_import_file(parts[2])
        except FileImportError as e:
            yield event.plain_result(str(e))
            return
        if path is not None:
            result = await self._run(event, svc.record_results_file(round_no, path, competition))
        else:
            result = await self._run(event, svc.record_results(round_no, parts[2], competition))
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        lines = [f"⚽ 第 {round_no} 轮({competition})赛果已录入（{result['count']} 场）"]
        for r in result["results"]:
            if r["result"] == "C":
                lines.append(f"· {r['home']} vs {r['away']}：比赛取消（不计入合计）")
                continue
            wx = {"晴": "☀️", "多云": "⛅", "雨": "🌧️", "雪": "❄️"}.get(r["weather"] or "", "")
            result_cn = {"W": "胜", "D": "平", "L": "负", "C": "取消"}.get(r["result"], r["result"])
            score_txt = f" {r['score']}" if r.get("score") else ""
            lines.append(
                f"· {r['home']} {result_cn}{score_txt} {r['away']} {wx} | 上座 {r['attendance']:,}"
                f" | 票 {r['ticket']:.2f}M 商 {r['commercial']:.2f}M 转 {r['broadcast']:.2f}M"
            )
        for e in result.get("file_errors", []):
            lines.append(f"⚠️ {e}")
        yield event.plain_result("\n".join(lines))

    async def advance(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        # 供状态监听方（如成长插件）定位提醒回发的会话
        self._plugin.last_advance_session = event.unified_msg_origin
        parts = event.get_message_str().split(maxsplit=2)
        if len(parts) < 2:
            yield event.plain_result(
                "用法: /主场推进 <窗口|赛季> [赛季名称]（推进到下一窗口或下一赛季；赛季可在推进时直接命名）"
            )
            return
        kind = parts[1].strip().lower()
        svc = self._plugin.fixture_service
        if kind in ("窗口", "window", "w"):
            coro = svc.advance_window(event.get_sender_id())
        elif kind in ("赛季", "season", "s"):
            coro = svc.advance_season(
                event.get_sender_id(), parts[2].strip() if len(parts) >= 3 else None
            )
        else:
            yield event.plain_result("参数需为 窗口 或 赛季（如 /主场推进 窗口）")
            return
        result = await self._run(event, coro)
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        season_label = await self._plugin.dao.season_label(result["season_number"])
        yield event.plain_result(f"✅ 已进入 {season_label} 窗口 {result['window_seq']}")

    async def name_season(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            yield event.plain_result("用法: /主场赛季命名 <名称>（给当前赛季命名，可反复改名；≤30 字）")
            return
        result = await self._run(
            event, self._plugin.fixture_service.name_season(parts[1], event.get_sender_id())
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        if result["old"]:
            yield event.plain_result(f"✅ 已将本赛季由「{result['old']}」改名为「{result['new']}」")
        else:
            yield event.plain_result(f"✅ 本赛季已命名为「{result['new']}」")

    # ─── 属性导入 ─────────────────────────────────────────

    async def import_attributes_batch(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        svc = self._plugin.stadium_service
        parts = event.get_message_str().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result(
                "用法: /主场属性导入 <属性文件.csv/xlsx> 或直接粘贴\n"
                "每行：队名 影响力 容量 等级（- 表示不改；单行即单队更新）\n"
                f"📁 imports 目录当前有：{self._import_files_hint(svc)}"
            )
            return
        try:
            path = svc.resolve_import_file(parts[1])
        except FileImportError as e:
            yield event.plain_result(str(e))
            return
        if path is not None:
            result = await self._run(event, svc.import_attributes_file(path))
            if "error" in result:
                yield event.plain_result(result["error"])
                return
            lines_out = [f"📄 文件导入属性 {result['imported']} 队"]
            for r in result["results"]:
                if r["ok"]:
                    lines_out.append(f"✅ {r['team']}: " + "；".join(r["notes"]))
                else:
                    lines_out.append(f"⚠️ {r['team']}: {r['error']}")
            for e in result.get("errors", []):
                lines_out.append(f"⚠️ {e}")
            yield event.plain_result("\n".join(lines_out) if lines_out else "没有可导入的行")
            return
        lines_out = []
        for raw in parts[1].splitlines():
            fields = raw.strip().split()
            if len(fields) < 2:
                continue
            try:
                influence = parse_float(fields[1]) if len(fields) >= 2 and fields[1] != "-" else None
                capacity = parse_int(fields[2], min_val=1) if len(fields) >= 3 and fields[2] != "-" else None
                tier = parse_int(fields[3], min_val=0) if len(fields) >= 4 and fields[3] != "-" else None
                result = await self._run(
                    event,
                    self._plugin.stadium_service.import_attributes(fields[0], influence, capacity, tier),
                )
            except ValueError:
                lines_out.append(f"⚠️ {raw}: 格式错误")
                continue
            if "error" in result:
                lines_out.append(f"⚠️ {fields[0]}: {result['error']}")
            else:
                lines_out.append(f"✅ {fields[0]}: " + "；".join(result["notes"]))
        yield event.plain_result("批量导入结果\n" + "\n".join(lines_out) if lines_out else "没有可导入的行")

    async def import_facility(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        if len(parts) < 4:
            yield event.plain_result("用法: /主场设施 <队名> <设施> <等级0-5>（商业区/灯光转播/草皮/青训中心/医疗中心）")
            return
        key = _FACILITY_ALIASES.get(parts[2].strip().lower())
        if key is None:
            yield event.plain_result(f"未知设施: {parts[2]}")
            return
        try:
            level = parse_int(parts[3], min_val=0, max_val=5)
        except ValueError:
            yield event.plain_result("等级需为 0~5 的整数")
            return
        result = await self._run(
            event, self._plugin.stadium_service.import_facility(parts[1], key, level)
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result(f"✅ {parts[1]} 的「{result['facility']}」已更新为 {result['level']} 级")

    async def rename_stadium(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("用法: /主场改名 <队名> <新名>（免费，改名收归管理员）")
            return
        result = await self._run(
            event, self._plugin.stadium_service.admin_rename(parts[1], parts[2])
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result(
            f"✅ 已将 {parts[1]} 的球场改名为「{result['new']}」（原「{result['old']}」）"
        )

    async def grant_all(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        result = await self._run(event, self._plugin.stadium_service.grant_all())
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result(f"✅ 已为 {result['created']} 支球队发放初始球场（1.2 万座，社区级）")

    # ─── 事件 ─────────────────────────────────────────────

    async def trigger_event(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        state = await self._plugin.dao.get_league_state()
        season = state["season_number"] if state else 1
        window_seq = state["window_seq"] if state else 1
        if len(parts) >= 2:
            event_id = parts[2] if len(parts) >= 3 else None
            result = await self._run(
                event,
                self._plugin.event_engine.trigger_team(parts[1], season, window_seq, event_id=event_id),
            )
            if "error" in result:
                yield event.plain_result(result["error"])
                return
            hit = result["hits"][0]
            if hit.get("type") == "choice":
                yield event.plain_result(
                    (hit.get("broadcast") or hit["event"]) + "\n📤 请收集球员回应后，用 /主场事件选择 统一录入"
                )
            else:
                yield event.plain_result(self._format_hit(hit))
            return
        result = await self._run(
            event, self._plugin.event_engine.trigger_all(season, window_seq)
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        if not result["hits"]:
            yield event.plain_result("🎲 本次触发无人中彩")
            return
        lines = [f"🎲 事件触发：{result['triggered']} 队命中"]
        for hit in result["hits"]:
            lines.append(self._format_hit(hit))
        if result.get("capped"):
            lines.append(f"（另有 {result['capped']} 队因同事件重复上限未抽中）")
        lines.append("📤 选择型事件请收集球员回应后，用 /主场事件选择 多行批量录入（/主场事件选择列表 查看待定）")
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _format_hit(hit) -> str:
        """渲染一条命中：选择型返回多行广播，即发型返回单行简述。"""
        if hit.get("type") == "choice":
            return hit.get("broadcast") or hit["event"]
        notes = hit.get("notes") or []
        line = f"🎲 {hit['team']} 触发事件：{hit.get('text') or hit['event']}"
        if notes:
            line += f"（{'；'.join(notes)}）"
        return line

    async def set_choice(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            yield event.plain_result(
                "用法: /主场事件选择 <队名> <事件名> <选项号>\n"
                "也可多行批量（每行：队名 事件名 选项号，支持 ①②③④；事件名可含空格）\n"
                "例：\n利物浦 周边爆款 1\n巴塞罗那 赞助商考察 ②"
            )
            return
        results = await self._run(event, self._plugin.event_engine.import_choices(parts[1]))
        if isinstance(results, dict):
            yield event.plain_result(results["error"])
            return
        ok = [r for r in results if r["ok"]]
        bad = [r for r in results if not r["ok"]]
        if not ok and not bad:
            yield event.plain_result("没有可录入的选择，每行格式：队名 事件名 选项号（支持 ①②③④）")
            return
        lines = [f"📥 已录入 {len(ok)} 条选择"]
        for r in ok:
            lines.append(f"✅ {r['team']} {r['event']} → 选{r['choice_no']} {r['option']}")
        for r in bad:
            lines.append(f"⚠️ {r['team']} {r['event']}: {r['error']}")
        yield event.plain_result("\n".join(lines))

    async def list_choices(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        state = await self._plugin.dao.get_league_state()
        season = state["season_number"] if state else 1
        window_seq = state["window_seq"] if state else 1
        if len(parts) >= 2:
            try:
                window_seq = parse_int(parts[1], min_val=1)
            except ValueError:
                yield event.plain_result("窗口需为正整数")
                return
        rows = await self._run(event, self._plugin.event_engine.choices_summary(season, window_seq))
        if "error" in rows:
            yield event.plain_result(rows["error"])
            return
        if not rows:
            yield event.plain_result(f"窗口 {window_seq} 没有选择型事件（可用 /主场事件 触发分配）")
            return
        season_label = await self._plugin.dao.season_label(season)
        lines = [f"📋 {season_label} 窗口 {window_seq} 事件选择（{len(rows)} 条）"]
        for r in rows:
            if r["resolved"]:
                lines.append(f"· {r['team']} {r['event']}：✅ 已结算")
            elif r["choice_no"] is not None:
                lines.append(f"· {r['team']} {r['event']}：已选 {r['choice_no']} {r['option']}")
            else:
                lines.append(f"· {r['team']} {r['event']}：⏳ 未定（收集回应用 /主场事件选择）")
        yield event.plain_result("\n".join(lines))

    async def settle_events_now(self, event) -> AsyncGenerator[MessageEventResult, None]:
        """独立事件结算：不等窗口结算，可直接结清已导入选择（加「全部」连未定一并兜底）。"""
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        state = await self._plugin.dao.get_league_state()
        season = state["season_number"] if state else 1
        window_seq = state["window_seq"] if state else 1
        include = len(parts) >= 2 and parts[1] in ("全部", "all")
        result = await self._run(
            event,
            self._plugin.event_engine.settle_now(season, window_seq, include_undecided=include),
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        if not result["resolved"]:
            hint = "没有待结算的选择事件" if include else "没有已导入选择的选择事件"
            yield event.plain_result(f"窗口 {window_seq}：{hint}（用 /主场事件 触发、/主场事件选择 录入）")
            return
        season_label = await self._plugin.dao.season_label(season)
        lines = [f"🎲 {season_label} 窗口 {window_seq} 事件结算 {len(result['resolved'])} 条"]
        for r in result["resolved"]:
            if r.get("skipped"):
                lines.append(f"· {r['team']} {r['event']}：跳过（无选项信息）")
                continue
            how = "自动最差" if r["auto"] else "已选"
            # 优先展示 LLM 生成的结果短文案（已含效果说明）；无内容回退到确定性数据变化行
            text = (r.get("text") or "").strip()
            detail = text or ("；".join(r["notes"]) if r["notes"] else "无变化")
            lines.append(f"· {r['team']} {r['event']}（{how}）：{detail}")
        if not include:
            remaining = await self._plugin.dao.get_unresolved_choices(season, window_seq)
            if remaining:
                lines.append(f"⏳ 另有 {len(remaining)} 条未收到选择（可用 /主场事件结算 全部 按最差兜底，或留到窗口结算自动处理）")
        yield event.plain_result("\n".join(lines))

    async def cancel_event(self, event) -> AsyncGenerator[MessageEventResult, None]:
        """取消本窗口分配给球队的事件：即发回退数值；选择型仅未定/已选未结可取消。"""
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split(maxsplit=2)
        if len(parts) < 2:
            yield event.plain_result(
                "用法: /主场事件取消 <队名> [事件名|事件id|all] ；或 /主场事件取消 all\n"
                "只带队名则列出该队本窗口事件；选择型仅未定/已选未结可取消（已结算用 /主场结算 强制 重算）；"
                "即发型取消时回退资金流水/死忠/上座修正（每次取消最新一条）；"
                "<队名> all 取消该队最近一次分配，仅 all 回退全局最近一次分配（均按事件生成时间）"
            )
            return
        state = await self._plugin.dao.get_league_state()
        season = state["season_number"] if state else 1
        window_seq = state["window_seq"] if state else 1
        svc = self._plugin.event_engine
        if len(parts) == 2 and parts[1].strip().lower() == "all":
            result = await self._run(
                event, svc.cancel_latest_assignment(season, window_seq)
            )
            if "error" in result:
                yield event.plain_result(result["error"])
                return
            lines = [
                f"✅ 已取消最近一次分配的全部事件"
                f"（共 {result['cancelled']} 个：即发 {result['instant']}、选择 {result['choice']}）"
            ]
            for ln in result["lines"]:
                lines.append(f"· {ln}")
            if result["skipped"]:
                lines.append(
                    f"⚠️ 已结算跳过：{'、'.join(result['skipped'])}（如需重算可用 /主场结算 强制）"
                )
            yield event.plain_result("\n".join(lines))
            return
        if len(parts) == 2:
            rows = await svc.list_team_events(parts[1], season, window_seq)
            if not rows:
                yield event.plain_result(f"球队「{parts[1]}」本窗口没有分配任何事件")
                return
            lines = [f"📋 {parts[1]} 本窗口事件（取消用 /主场事件取消 {parts[1]} <事件名|id>）"]
            for r in rows:
                lines.append(f"· {r['event']}（{r['kind']}·{r['status']}，id {r['event_id']}）")
            yield event.plain_result("\n".join(lines))
            return
        if parts[2].strip().lower() == "all":
            result = await self._run(
                event, svc.cancel_team_events(parts[1], season, window_seq)
            )
            if "error" in result:
                yield event.plain_result(result["error"])
                return
            lines = [
                f"✅ 已取消 {parts[1]} 最近一次分配的全部事件"
                f"（共 {result['cancelled']} 个：即发 {result['instant']}、选择 {result['choice']}）"
            ]
            for ln in result["lines"]:
                lines.append(f"· {ln}")
            if result["skipped"]:
                lines.append(
                    f"⚠️ 已结算跳过：{'、'.join(result['skipped'])}（如需重算可用 /主场结算 强制）"
                )
            yield event.plain_result("\n".join(lines))
            return
        result = await self._run(
            event, svc.cancel_team_event(parts[1], parts[2], season, window_seq)
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        team = parts[1]
        if result["kind"] == "choice":
            yield event.plain_result(
                f"✅ 已取消 {team} 的选择事件「{result['event']}」（待定已移除，未动账目，可重新触发）"
            )
            return
        lines = [f"✅ 已取消 {team} 的即发事件「{result['event']}」并回退生效数值"]
        for n in result["notes"]:
            lines.append(f"· {n}")
        for w in result["warnings"]:
            lines.append(f"⚠️ {w}")
        if result["occurrences"] > 0:
            lines.append(f"· 该事件本窗口还有 {result['occurrences']} 条实例（再执行一次取消下一条）")
        yield event.plain_result("\n".join(lines))

    async def generate_events(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split(maxsplit=2)
        try:
            count = parse_int(parts[1], min_val=1, max_val=20) if len(parts) >= 2 else 5
        except ValueError:
            yield event.plain_result("数量需为 1-20 的整数")
            return
        topic = parts[2] if len(parts) >= 3 else ""
        result = await self._run(event, self._plugin.event_engine.generate_drafts(count, topic))
        if "error" in result:
            yield event.plain_result(f"生成失败: {result['error']}")
            return
        lines = [f"🤖 LLM 起草 {len(result)} 个事件（待采纳）"]
        for d in result:
            lines.append(f"· {d['id']} {d['name']} {d['effects']}")
        lines.append("用 /主场事件列表 查看，/主场事件采纳 <id> 或 /主场事件丢弃 <id> 处理")
        yield event.plain_result("\n".join(lines))

    async def list_events(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        status = "pending" if len(parts) >= 2 and parts[1] in ("待定", "pending") else None
        rows = await self._plugin.dao.list_events(status)
        if not rows:
            yield event.plain_result("（空）")
            return
        lines = [f"事件池（{'待定' if status else '全部'} {len(rows)} 条）"]
        for r in rows[-30:]:
            kind = "选择" if r["event_type"] == "choice" else "即发"
            if r["event_type"] == "choice":
                try:
                    n = len(json.loads(r["options_json"] or "[]"))
                except (ValueError, TypeError):
                    n = 0
                extra = f"{n} 个选项"
            else:
                extra = r["effects_json"]
            lines.append(f"· {r['id']} [{r['status']}][{kind}] {r['name']}（{r['category']} w{r['weight']}）{extra}")
        yield event.plain_result("\n".join(lines))

    async def adopt_event(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        try:
            event_id = parse_int(parts[1], min_val=1) if len(parts) >= 2 else None
        except ValueError:
            event_id = None
        if event_id is None:
            yield event.plain_result("用法: /主场事件采纳 <事件id>")
            return
        result = await self._run(event, self._plugin.event_engine.adopt(event_id))
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result("✅ 事件已采纳，进入事件池")

    async def discard_event(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        try:
            event_id = parse_int(parts[1], min_val=1) if len(parts) >= 2 else None
        except ValueError:
            event_id = None
        if event_id is None:
            yield event.plain_result("用法: /主场事件丢弃 <事件id>")
            return
        result = await self._run(event, self._plugin.event_engine.discard(event_id))
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result("✅ 事件已丢弃")

    async def add_custom_event(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split(maxsplit=6)
        if len(parts) < 5:
            yield event.plain_result("用法: /主场事件写 <名称> <类别> <权重> <effectsJSON> [模板文案]\n或 /主场事件写 <名称> <类别> <权重> choice <选项JSON>\n选项JSON：{\"name\":\"操作\",\"desc\":\"…\",\"outcomes\":[{\"w\":60,\"effects\":{\"money\":1}},{\"w\":40,\"effects\":{\"money\":-1}}]}（2~4 个操作）")
            return
        try:
            weight = parse_int(parts[3], min_val=1, max_val=100)
        except ValueError:
            yield event.plain_result("权重需为 1-100 整数")
            return
        if parts[4].lower() in ("choice", "选择"):
            if len(parts) < 6:
                yield event.plain_result("选择事件缺少 <选项JSON>（见用法）")
                return
            result = await self._run(
                event,
                self._plugin.event_engine.add_custom(
                    parts[1], parts[2], weight, "{}", event_type="choice", options_text=parts[5],
                ),
            )
        else:
            result = await self._run(
                event, self._plugin.event_engine.add_custom(parts[1], parts[2], weight, parts[4])
            )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result("✅ 自定义事件已加入事件池")

    # ─── 品牌 ─────────────────────────────────────────────

    async def generate_brands(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split(maxsplit=2)
        try:
            count = parse_int(parts[1], min_val=1, max_val=20) if len(parts) >= 2 else 5
        except ValueError:
            yield event.plain_result("数量需为 1-20 的整数")
            return
        topic = parts[2] if len(parts) >= 3 else ""
        result = await self._run(event, self._plugin.brand_service.generate_drafts(count, topic))
        if "error" in result:
            yield event.plain_result(f"生成失败: {result['error']}")
            return
        lines = [f"🤖 LLM 起草 {len(result)} 个品牌（待采纳）"]
        for d in result:
            lines.append(f"· {d['brand']}（热度 {d['heat']}）")
        lines.append("用 /主场品牌列表 查看，/主场品牌采纳 <id> 或 /主场品牌丢弃 <id> 处理")
        yield event.plain_result("\n".join(lines))

    async def list_brands(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        status = "pending" if len(parts) >= 2 and parts[1] in ("待定", "pending") else None
        rows = await self._plugin.dao.list_brands(status)
        if not rows:
            yield event.plain_result("（空）")
            return
        lines = [f"品牌池（{'待定' if status else '全部'} {len(rows)} 个）"]
        for r in rows[-30:]:
            lines.append(f"· {r['id']} [{r['status']}] {r['brand']}（热度 {r['heat']}，{r['source']}）")
        yield event.plain_result("\n".join(lines))

    async def adopt_brand(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        try:
            brand_id = parse_int(parts[1], min_val=1) if len(parts) >= 2 else None
        except ValueError:
            brand_id = None
        if brand_id is None:
            yield event.plain_result("用法: /主场品牌采纳 <品牌id>")
            return
        result = await self._run(event, self._plugin.brand_service.adopt(brand_id))
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result("✅ 品牌已采纳")

    async def discard_brand(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        try:
            brand_id = parse_int(parts[1], min_val=1) if len(parts) >= 2 else None
        except ValueError:
            brand_id = None
        if brand_id is None:
            yield event.plain_result("用法: /主场品牌丢弃 <品牌id>")
            return
        result = await self._run(event, self._plugin.brand_service.discard(brand_id))
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result("✅ 品牌已丢弃")

    # ─── 结算 ─────────────────────────────────────────────

    async def settle_window(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        force = len(parts) >= 2 and parts[1] in ("强制", "force")
        result = await self._run(event, self._plugin.window_service.settle(force=force))
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        season_label = await self._plugin.dao.season_label(result["season"])
        lines = [f"🧾 {season_label} 窗口 {result['window_seq']} 结算"]
        lines.extend(result["lines"])
        yield event.plain_result("\n".join(lines))

    # ─── 配置 ─────────────────────────────────────────────

    async def set_config(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("用法: /主场设置 <配置键> <值>")
            return
        key, raw = parts[1], parts[2]
        try:
            value = validate_and_cast(key, raw)
        except ValueError as e:
            yield event.plain_result(f"配置无效: {e}")
            return
        self._plugin.config_cache[key] = value
        await self._plugin._persist_config(key, value)
        yield event.plain_result(f"✅ 配置 {key} 已更新")

    async def view_config(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        lines = ["⚙️ 当前配置（部分）"]
        secrets = {"tier_table", "weather_ranges", "form_coef_table", "facility_effects", "activity_config", "fans_target_table"}
        for key in sorted(self._plugin.config_cache):
            val = self._plugin.config_cache[key]
            if key in secrets and val:
                lines.append(f"· {key}: <内部参数，已隐藏>")
            else:
                lines.append(f"· {key}: {val}")
        yield event.plain_result("\n".join(lines))

    async def form_backfill(self, event) -> AsyncGenerator[MessageEventResult, None]:
        """主场补差：战绩系数修正 + 满座微降 + 死忠重定基；默认预览只读，
        「确认」才落库（成分级幂等），「强制」重抽满座区间场次。"""
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        args = event.get_message_str().split()[1:]
        if (
            any(a not in ("确认", "强制") for a in args)
            or args.count("确认") > 1 or args.count("强制") > 1
        ):
            yield event.plain_result("用法: /主场补差 [确认] [强制]")
            return
        confirm = "确认" in args
        force = "强制" in args
        svc = self._plugin.backfill_service
        result = await self._run(
            event, svc.apply(force=force) if confirm else svc.scan(force=force))
        if "error" in result:
            yield event.plain_result(f"❌ {result['error']}")
            return
        yield event.plain_result(self._format_backfill(result))

    @staticmethod
    def _format_backfill(r: dict) -> str:
        lines: list[str] = []
        affected = r["affected"]
        fans = r["fans"]
        sellouts = r.get("sellouts") or []
        force = bool(r.get("force"))
        pending = bool(affected or fans or sellouts)
        if r.get("done") and not pending:
            mk = r.get("marker") or {}
            tail = "、战绩成分已由旧版补差完成" if mk.get("form_skipped") else ""
            body = (f"✅ 主场补差已执行过：赛季 S{mk.get('executed_season')}，"
                    f"{mk.get('matches', 0)} 场票房补差、{mk.get('sellouts', 0)} 场满座微降、"
                    f"{mk.get('fans_teams', 0)} 队死忠重定基{tail}")
            if force:
                return body + "。\n强制扫描未发现可重算项"
            return body + "。无需重复执行；重抽满座区间请发：/主场补差 强制"
        head = f"🧮 主场补差预览（赛季 S{r['season']}，尚未生效）" + ("｜✅ 已落库" if r.get("applied") else "")
        if r.get("done"):
            head += "｜已执行过，本次仅重算待处理项"
        if not pending:
            return head + "\n✅ 未发现需要补差的数据。\n仍要写入完成标记请发：/主场补差 确认"
        lines.append(head)
        if force:
            lo, hi = SELL_OUT_FILL
            lines.append(f"⚠️ 强制模式：满座微降将重抽所有处于容量 {lo * 100:.1f}%~{hi * 100:.1f}%"
                         "区间的场次（含已微降过的）；战绩成分不可强制")
        if r.get("form_done"):
            lines.append("· 战绩成分已执行过（幂等标记），本次跳过")
        elif affected:
            d_money = sum(t["d_ticket"] + t["d_commercial"] for t in r["teams"])
            lines.append(f"受影响 {len(affected)} 场（转播收入不动），票房净差额 {format_m(d_money)} M")
        for a in affected[:30]:
            updown = "↑" if a["d_ticket"] > 0 or a["d_att"] > 0 else "↓"
            lines.append(
                f"· {updown} 第{a['round_no']}轮({a['competition']}) {a['home']} vs {a['away']}："
                f"历史{a['n']}场旧分{a['old_pts']}({a['coef_old']:.2f})→中性4分({a['coef_new']:.2f})，"
                f"上座 {format_int(a['att_old'])}→{format_int(a['att_new'])}，"
                f"票 {format_m(a['d_ticket'])} 商 {format_m(a['d_commercial'])} M"
            )
        if len(affected) > 30:
            lines.append(f"· …其余 {len(affected) - 30} 场从略")
        if r["teams"]:
            lines.append("队伍票房汇总：")
            for t in r["teams"]:
                lines.append(f"· {t['team']}：上座 {t['d_attendance']:+d}，票房净 "
                             f"{format_m(t['d_ticket'] + t['d_commercial'])} M（票 {format_m(t['d_ticket'])}／商 {format_m(t['d_commercial'])}）")
        if sellouts:
            lo, hi = SELL_OUT_FILL
            scope = (f"强制：重抽容量 {lo * 100:.1f}%~{hi * 100:.1f}% 区间内全部场次"
                     if force else "历史恰好=容量的记录")
            lines.append(f"满座微降（{scope}，转播收入不动）{len(sellouts)} 场：")
            for s in sellouts[:30]:
                cap = s["capacity"]
                lines.append(
                    f"· 第{s['round_no']}轮({s['competition']}) {s['home']} vs {s['away']}："
                    f"上座 {format_int(s['att_old'])} → 容量的 {lo * 100:.1f}%~{hi * 100:.1f}%"
                    f"（约 {format_int(int(cap * lo))}~{format_int(int(cap * hi))}）"
                )
            if len(sellouts) > 30:
                lines.append(f"· …其余 {len(sellouts) - 30} 场从略")
        if fans:
            lines.append("死忠重定基（影响力阶梯目标）：")
            for f in fans:
                lines.append(f"· {f['team']}：死忠 {format_int(f['before'])}→{format_int(f['after'])}（{f['delta']:+d}）")
        if not r.get("applied"):
            tip = "/主场补差 确认 强制" if force else "/主场补差 确认"
            lines.append("⚠️ 请在部署后、下一次窗口结算前执行；确认落库请发：" + tip)
        return "\n".join(lines)

    async def add_admin(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /主场添加管理 <QQ>")
            return
        try:
            qq = parse_qq_arg(parts[1]) or parse_qq(parts[1])
        except ValueError as e:
            yield event.plain_result(str(e))
            return
        await self._plugin.dao.add_admin(qq, event.get_sender_id())
        yield event.plain_result(f"✅ 已添加管理 {qq}")

    async def remove_admin(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /主场删除管理 <QQ>")
            return
        try:
            qq = parse_qq_arg(parts[1]) or parse_qq(parts[1])
        except ValueError as e:
            yield event.plain_result(str(e))
            return
        await self._plugin.dao.remove_admin(qq)
        yield event.plain_result(f"✅ 已删除管理 {qq}")