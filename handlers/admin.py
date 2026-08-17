"""管理命令：赛程/天气/赛果/统计、属性导入、事件与品牌（含 LLM 设计）、结算、配置。"""

from collections.abc import AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import MessageEventResult
from astrbot.api.message_components import File

from ..config.defaults import validate_and_cast
from ..services import formula
from ..services.brand_service import BrandError
from ..services.event_engine import EventError
from ..services.file_import_service import FileImportError, cleanup_file
from ..services.fixture_service import FixtureError
from ..services.stadium_service import StadiumError
from ..services.window_service import SettleError
from ..utils.security import format_m, parse_int, parse_qq, parse_qq_arg

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
            return await coro
        except (StadiumError, FixtureError, EventError, BrandError, SettleError,
                FileImportError, ValueError) as e:
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"Admin handler error: {e}")
            return {"error": "操作失败，已记录错误"}

    async def _guard(self, event) -> str | None:
        """权限门禁：无权限时返回错误文案，调用方 yield 后 return。"""
        if not await self._require_admin(event):
            return "你没有权限执行此操作"
        return None

    async def _get_attachment(self, event) -> str | None:
        """从消息链中提取第一个文件附件，返回下载后的本地路径；无附件返回 None。"""
        try:
            messages = event.get_messages()
        except Exception:
            return None
        for comp in messages:
            if isinstance(comp, File):
                try:
                    path = await comp.get_file()
                except Exception as e:
                    logger.warning(f"附件下载失败: {e}")
                    return None
                if path:
                    return path
        return None

    # ─── 赛程 ─────────────────────────────────────────────

    async def import_fixtures(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        path = await self._get_attachment(event)
        parts = event.get_message_str().split(maxsplit=1)
        if path is None and len(parts) < 2:
            yield event.plain_result("用法: /主场赛程导入\n每行：轮次 主队 客队 [周] [天] [时间]\n例：1 利物浦 巴塞罗那 W12 D6 15:00（或附带 xlsx/csv 文件）")
            return
        if path:
            try:
                result = await self._run(
                    event, self._plugin.fixture_service.import_fixtures_file(path)
                )
            finally:
                cleanup_file(path)
        else:
            result = await self._run(event, self._plugin.fixture_service.import_fixtures(parts[1]))
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        lines = [f"📋 已导入第 {result['season']} 赛季窗口 {result['window_seq']} 赛程 {result['imported']} 场（跳过 {result['skipped']}）"]
        for e in list(result.get("file_errors", [])) + list(result.get("errors", [])):
            lines.append(f"⚠️ {e}")
        yield event.plain_result("\n".join(lines))

    async def forecast_weather(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /主场天气 <轮次>（或 /主场天气 <轮次> <主队> <天气> 覆盖）\n轮次支持前缀：顶级9/次级11/冠军3（默认联赛）")
            return
        try:
            competition, round_no = formula.parse_round_token(self._plugin.config_cache, parts[1])
        except ValueError as e:
            yield event.plain_result(str(e))
            return
        if len(parts) >= 4:
            result = await self._run(
                event,
                self._plugin.fixture_service.set_weather(round_no, parts[2], parts[3], competition),
            )
            if "error" in result:
                yield event.plain_result(result["error"])
                return
            yield event.plain_result(f"☀️ 第 {round_no} 轮({competition})「{result['home']}」天气改为 {result['weather']}")
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

    async def record_results(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split(maxsplit=2)
        if len(parts) < 2:
            yield event.plain_result("用法: /主场赛果 <轮次>\n每行：主队 胜/平/负 [比分]（如 利物浦 胜 2-1；或附带 xlsx/csv 文件）\n轮次支持前缀：顶级9/次级11/冠军3（默认联赛）")
            return
        try:
            competition, round_no = formula.parse_round_token(self._plugin.config_cache, parts[1])
        except ValueError as e:
            yield event.plain_result(str(e))
            return
        path = await self._get_attachment(event)
        if path is None and len(parts) < 3:
            yield event.plain_result("用法: /主场赛果 <轮次>\n每行：主队 胜/平/负 [比分]（如 利物浦 胜 2-1；或附带 xlsx/csv 文件）\n轮次支持前缀：顶级9/次级11/冠军3（默认联赛）")
            return
        if path:
            try:
                result = await self._run(
                    event, self._plugin.fixture_service.record_results_file(round_no, path, competition)
                )
            finally:
                cleanup_file(path)
        else:
            result = await self._run(
                event, self._plugin.fixture_service.record_results(round_no, parts[2], competition)
            )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        lines = [f"⚽ 第 {round_no} 轮({competition})赛果已录入（{result['count']} 场）"]
        for r in result["results"]:
            wx = {"晴": "☀️", "多云": "⛅", "雨": "🌧️", "雪": "❄️"}.get(r["weather"] or "", "")
            score_txt = f" {r['score']}" if r.get("score") else ""
            lines.append(
                f"· {r['home']} {r['result']}{score_txt} {r['away']} {wx} | 上座 {r['attendance']:,}"
                f" | 票 {r['ticket']:.2f}M 商 {r['commercial']:.2f}M 转 {r['broadcast']:.2f}M"
            )
        for e in result.get("file_errors", []):
            lines.append(f"⚠️ {e}")
        yield event.plain_result("\n".join(lines))

    async def round_stats(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /主场轮次统计 <轮次>\n轮次支持前缀：顶级9/次级11/冠军3（默认联赛）")
            return
        try:
            competition, round_no = formula.parse_round_token(self._plugin.config_cache, parts[1])
        except ValueError as e:
            yield event.plain_result(str(e))
            return
        result = await self._run(event, self._plugin.fixture_service.round_stats(round_no, competition))
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        lines = [f"📊 第 {result['round_no']} 轮({competition})统计（赛季{result['season']} 窗口{result['window_seq']}）"]
        lines.extend(result["lines"])
        t = result["totals"]
        lines.append(f"合计: 上座 {t['attendance']:,} / 票房 {t['ticket']:.2f}M")
        yield event.plain_result("\n".join(lines))

    async def advance_window(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        result = await self._run(
            event, self._plugin.fixture_service.advance_window(event.get_sender_id())
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result(f"✅ 已进入第 {result['season_number']} 赛季窗口 {result['window_seq']}")

    async def advance_season(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        result = await self._run(
            event, self._plugin.fixture_service.advance_season(event.get_sender_id())
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result(f"✅ 已进入第 {result['season_number']} 赛季窗口 {result['window_seq']}")

    # ─── 属性导入 ─────────────────────────────────────────

    async def import_attributes(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /主场属性 <队名> [影响力] [容量] [等级]（- 表示不改）")
            return
        team = parts[1]
        influence = capacity = tier = None
        try:
            if len(parts) >= 3 and parts[2] != "-":
                influence = float(parts[2])
            if len(parts) >= 4 and parts[3] != "-":
                capacity = parse_int(parts[3], min_val=1)
            if len(parts) >= 5 and parts[4] != "-":
                tier = parse_int(parts[4], min_val=0)
        except ValueError:
            yield event.plain_result("参数格式错误：影响力为数字，容量/等级为整数")
            return
        result = await self._run(
            event, self._plugin.stadium_service.import_attributes(team, influence, capacity, tier)
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        lines = [f"✅ {result['team']} 属性已更新"]
        lines.extend(f"· {n}" for n in result["notes"])
        yield event.plain_result("\n".join(lines))

    async def import_attributes_batch(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split(maxsplit=1)
        path = await self._get_attachment(event)
        if path is None and len(parts) < 2:
            yield event.plain_result("用法: /主场属性导入\n每行：队名 影响力 容量 等级（或附带 xlsx/csv 文件）")
            return
        if path:
            try:
                result = await self._run(
                    event, self._plugin.stadium_service.import_attributes_file(path)
                )
            finally:
                cleanup_file(path)
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
                influence = float(fields[1]) if len(fields) >= 2 and fields[1] != "-" else None
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
            result = await self._run(
                event, self._plugin.event_engine.trigger_team(parts[1], season, window_seq)
            )
            if "error" in result:
                yield event.plain_result(result["error"])
                return
            hit = result["hits"][0]
            yield event.plain_result(f"🎲 {hit['team']} 触发事件：{hit['text'] or hit['event']}（{'；'.join(hit['notes'])}）")
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
            lines.append(f"· {hit['team']}: {hit['text'] or hit['event']}（{'；'.join(hit['notes'])}）")
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
            lines.append(f"· {r['id']} [{r['status']}] {r['name']}（{r['category']} w{r['weight']}）{r['effects_json']}")
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
        parts = event.get_message_str().split(maxsplit=4)
        if len(parts) < 5:
            yield event.plain_result("用法: /主场事件写 <名称> <类别> <权重> <effectsJSON> [模板文案]")
            return
        try:
            weight = parse_int(parts[3], min_val=1, max_val=100)
        except ValueError:
            yield event.plain_result("权重需为 1-100 整数")
            return
        result = await self._run(
            event,
            self._plugin.event_engine.add_custom(parts[1], parts[2], weight, parts[4]),
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
        lines = [f"🧾 第 {result['season']} 赛季窗口 {result['window_seq']} 结算"]
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
        secrets = {"tier_table", "weather_ranges", "form_coef_table", "facility_effects", "activity_config"}
        for key in sorted(self._plugin.config_cache):
            val = self._plugin.config_cache[key]
            if key in secrets and val:
                lines.append(f"· {key}: <内部参数，已隐藏>")
            else:
                lines.append(f"· {key}: {val}")
        yield event.plain_result("\n".join(lines))

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