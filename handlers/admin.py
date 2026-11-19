"""管理命令：赛程同步、天气预报/覆盖/推送（4 条；权限门禁共用）。"""

from collections.abc import AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import MessageEventResult

from ..services.formula import parse_round_token
from ..services.weather_service import WeatherError


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
            return result if result is not None else {}
        except (WeatherError, ValueError) as e:
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"Admin handler error: {e}")
            return {"error": "操作失败，已记录错误"}

    async def _guard(self, event) -> str | None:
        """权限门禁：无权限时返回错误文案，调用方 yield 后 return。"""
        if not await self._require_admin(event):
            return "你没有权限执行此操作"
        return None

    # ─── 赛程同步 ─────────────────────────────────────────

    async def sync_fixture_times(self, event) -> AsyncGenerator[MessageEventResult, None]:
        """从 tour 全量同步赛程/赛果（v3：club 当前赛季驱动三赛事，tour 唯一真源）。"""
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        result = await self._run(event, self._plugin.tour_sync_service.sync())
        status = result.get("status")
        if status == "bypass":
            yield event.plain_result("未配置 tour_api_base，赛程同步未执行（配置后可再试）。")
            return
        if status == "error":
            yield event.plain_result(f"⚠️ {result.get('error', '同步失败')}")
            return

        title = "赛程同步完成" if status == "ok" else "赛程同步部分完成"
        lines = [
            f"📋 {result.get('season_label', '')} {title}：拉取 {result['fetched']} 场，"
            f"补建 {result['created']}，更新 {result['updated']}，未变 {result['unchanged']}"
        ]
        per = result.get("tournaments") or []
        if per:
            lines.append("· " + " / ".join(
                f"{t['competition']} {t.get('fetched', 0)} 场" for t in per
            ))
        skipped = result.get("skipped") or {}
        if any(skipped.values()):
            lines.append("· 跳过：" + "、".join(f"{k} {v}" for k, v in skipped.items() if v))
        if result.get("pending"):
            lines.append(f"· tour 未排期：{result['pending']} 场（已建行，时刻待 tour 排期）")
        for note, items in (
            ("本地多出（tour 无此场）", result.get("missing_tour") or []),
            ("冲突", result.get("conflicts") or []),
        ):
            if not items:
                continue
            shown = items[:5]
            detail = "；".join(
                f"{i.get('competition', '')}R{i.get('round_no', '')}"
                + (f"L{i['leg']}" if i.get("leg") else "")
                + f" {i.get('home_team', '')} vs {i.get('away_team', '')}"
                + (f"（{i['reason']}）" if i.get("reason") else "")
                for i in shown
            )
            more = f" 等 {len(items)} 项" if len(items) > len(shown) else ""
            lines.append(f"· {note}：{detail}{more}")
        for err in result.get("errors") or []:
            lines.append(f"· ⚠️ {err}")
        for line in lines[1:]:
            logger.info(f"schedsync {line}")
        yield event.plain_result(chr(10).join(lines))

    # ─── 天气 ─────────────────────────────────────────────

    async def forecast_weather(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split(maxsplit=1)
        if len(parts) >= 2 and len(parts[1].split()) > 1:
            yield event.plain_result(
                "查询整轮预报只带轮次；要覆盖单场天气请用 /主场天气覆盖 <轮次> <主队> <天气>"
            )
            return
        if len(parts) < 2:
            yield event.plain_result("用法: /主场天气 <轮次>\n轮次支持前缀：顶级9/次级11/冠军3")
            return
        try:
            competition, round_no = parse_round_token(self._plugin.config_cache, parts[1])
        except ValueError as e:
            yield event.plain_result(str(e))
            return
        result = await self._run(event, self._plugin.weather_service.forecast_round(round_no, competition))
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        icons = {"晴": "☀️", "多云": "⛅", "雨": "🌧️", "雪": "❄️"}
        lines = [f"🌤 第 {round_no} 轮({competition or '全部赛事'})天气预报"]
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
            competition, round_no = parse_round_token(self._plugin.config_cache, parts[1])
        except ValueError as e:
            yield event.plain_result(str(e))
            return
        result = await self._run(
            event,
            self._plugin.weather_service.set_weather(round_no, parts[2], parts[3], competition),
        )
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result(
            f"☀️ 第 {round_no} 轮({competition or '全部赛事'})「{result['home']}」天气改为 {result['weather']}"
        )

    async def weather_push(self, event) -> AsyncGenerator[MessageEventResult, None]:
        deny = await self._guard(event)
        if deny:
            yield event.plain_result(deny)
            return
        parts = event.get_message_str().split()
        preview = len(parts) >= 2 and parts[1] in ("预览", "preview")
        if len(parts) >= 2 and not preview:
            yield event.plain_result(
                "用法: /主场天气推送 [预览]\n无参数=推送全部已预报轮次（只推已有天气）；预览=只算不发"
            )
            return
        result = await self._run(event, self._plugin.weather_service.sync_weather(preview))
        if "error" in result:
            yield event.plain_result(result["error"])
            return
        yield event.plain_result(self._format_sync(result))

    @staticmethod
    def _format_sync(result: dict) -> str:
        totals = result["totals"]
        head = "🔍 天气推送预览（只算不发）" if result["preview"] else "🔄 天气推送"
        lines = [
            f"{head}：{totals['rounds']} 轮 / {totals['matches']} 场"
            f" | 推送成功 {totals['push_ok']} 轮 / 失败 {totals['push_error']} 轮",
        ]
        if totals.get("skipped_other_season"):
            lines.append(
                f"· 跳过旧赛季已预报场次：{totals['skipped_other_season']} 场（只推当前赛季）"
            )
        for r in result["rounds"]:
            push = r["push"]
            if push["status"] == "ok":
                resp = push.get("response") or {}
                verdicts = resp.get("verdicts") or []
                summary = resp.get("summary") or {}
                landed = summary.get("pushed", len(verdicts))
                push_txt = f"推送 ok（平台落库 {landed}/{len(verdicts)} 场）"
            elif push["status"] == "preview":
                push_txt = "推送（预览跳过）"
            elif push["status"] == "bypass":
                push_txt = "推送旁路（未配置 club_sync_url/secret）"
            else:
                push_txt = f"推送失败：{push.get('error', '')}"
            lines.append(
                f"· {r['competition']} 第{r['round_no']}轮：{r['matches']} 场"
                f" | {push_txt}"
            )
        if result["preview"]:
            lines.append("预览只组装推送载荷，不写库、不发请求")
        return "\n".join(lines)
