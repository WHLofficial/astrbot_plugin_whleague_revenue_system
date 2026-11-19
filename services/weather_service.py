"""天气服务（v3）：轮次预报、手动覆盖、推送 club。只推已有天气，不订正旧账。

设计 §6：
- forecast_round：按 当前赛季 + 轮次（可选赛事）查 matches，已有 weather 保留
  并标记「已定」（existing=True），缺的逐行抽 weather + wx_coef 固化；
- set_weather：手改单队天气并按新类型重抽系数固化；
- sync_weather：取 club 当前赛季（league_state 即 club 同步写入的赛季），
  按 (season, competition, round_no) 分组，只推已有天气，payload
  {season, competition, roundNo, items:[{home, away, weather, coef}]}；
  preview 只算不发不写。不做结算输入、旧账订正、球场补建，不碰任何经营/财务
  字段（v3 起结算与上座都在 club 侧，本地只管天气）。
"""

from astrbot.api import logger

from . import formula
from .club_sync import ClubSyncClient
from .tour_sync import effective_round, load_round_map

_WEATHER_ALIASES = {
    "晴": formula.WX_SUNNY,
    "sunny": formula.WX_SUNNY,
    "多云": formula.WX_CLOUDY,
    "cloudy": formula.WX_CLOUDY,
    "阴": formula.WX_CLOUDY,
    "雨": formula.WX_RAIN,
    "rain": formula.WX_RAIN,
    "雪": formula.WX_SNOW,
    "snow": formula.WX_SNOW,
}


class WeatherError(Exception):
    pass


class WeatherService:
    """天气：预报 / 覆盖 / 推送（club_sync.push_round 负责 HMAC 签名发送）。"""

    def __init__(self, dao, cfg: dict):
        self._dao = dao
        self._cfg = cfg

    async def _current_season(self) -> int:
        state = await self._dao.get_league_state()
        if state is None or state["season_number"] is None:
            raise WeatherError("无法确定当前赛季（本地 league_state 为空，先执行 /主场赛程同步）")
        return int(state["season_number"])

    async def forecast_round(self, round_no: int, competition: str | None = None) -> dict:
        """轮次预报：已有天气保留并标「已定」，缺的逐行抽天气+系数固化。"""
        season = await self._current_season()
        matches = await self._dao.get_round_matches(season, round_no, competition)
        if not matches:
            raise WeatherError(f"第 {round_no} 轮({competition or '全部赛事'})没有赛程，先执行 /主场赛程同步")
        results = []
        for m in matches:
            if m["weather"]:
                # 已定：保留原天气与原系数
                results.append({
                    "home": m["home_team"], "weather": m["weather"],
                    "coef": m["wx_coef"], "existing": True,
                })
                continue
            weather, coef = formula.roll_weather_with_coef(self._cfg)
            await self._dao.set_match_weather_by_id(m["id"], weather, coef)
            results.append({
                "home": m["home_team"], "weather": weather,
                "coef": coef, "existing": False,
            })
        drawn = sum(1 for r in results if not r["existing"])
        return {
            "round_no": round_no,
            "competition": competition or "全部赛事",
            "matches": results,
            "total": len(results),
            "drawn": drawn,
            "already_set": len(results) - drawn,
        }

    async def set_weather(self, round_no: int, home_team: str, weather_text: str,
                          competition: str | None = None) -> dict:
        """手动覆盖单队天气：按新类型重抽系数固化（旧系数属于旧类型区间，不保留）。"""
        weather = _WEATHER_ALIASES.get(str(weather_text or "").strip().lower())
        if weather is None:
            raise WeatherError(f"未知天气: {weather_text}（晴/多云/雨/雪）")
        season = await self._current_season()
        matches = await self._dao.get_round_matches(season, round_no, competition)
        for m in matches:
            if m["home_team"] == home_team:
                coef = round(formula.weather_coef(self._cfg, weather), 4)
                await self._dao.set_match_weather_by_id(m["id"], weather, coef)
                return {"home": home_team, "weather": weather, "coef": coef}
        raise WeatherError(f"第 {round_no} 轮({competition or '全部赛事'})无主队「{home_team}」的比赛")

    async def sync_weather(self, preview: bool = False) -> dict:
        """推送已预报天气到 club：只推当前赛季、按赛事/轮分组、只推已有天气。

        设计 §2/§6：推送范围 = 当前 club 赛季（league_state 即 club 同步写入的赛季），
        旧赛季已预报场次不推（推了只会覆盖 club 旧季记录或全部 unmatched）。
        roundNo 用换算后的 tour stage 内轮次（club 按 m.round + 主客队名定位，
        本地 round_no 带 stage offset，直接推次级 15+/冠军杯 7+ 必 unmatched）。
        preview 只算不发不写。
        """
        client = ClubSyncClient(self._cfg)
        if not client.enabled:
            return {
                "status": "bypass",
                "reason": "未配置 club_sync_url / club_sync_secret，天气推送旁路",
            }
        season = await self._current_season()
        all_rows = await self._dao.list_weathered_matches()
        rows = [m for m in all_rows if int(m["season_number"]) == season]
        skipped_other_season = len(all_rows) - len(rows)
        if not rows:
            raise WeatherError(f"S{season} 没有已预报天气的场次，先执行 /主场天气 <轮次>")

        round_map = await load_round_map(self._dao)
        groups: dict[tuple, list] = {}
        for m in rows:
            coef = m["wx_coef"]
            if coef is None:
                # 迁移旧行系数未固化：推送时按类型现抽展示值（v3 无本地结算，无需订正口径）
                coef = round(formula.weather_coef(self._cfg, m["weather"]), 4)
            push_round_no = effective_round(round_map, m["tournament_id"], m["round_no"])
            # club 只认 顶级联赛/次级联赛/冠军杯：迁移行「联赛」归一后推送
            competition = formula.normalize_competition(str(m["competition"]))
            key = (int(m["season_number"]), competition, int(push_round_no))
            groups.setdefault(key, []).append({
                "home": m["home_team"], "away": m["away_team"],
                "weather": m["weather"], "coef": coef,
            })

        rounds = []
        push_ok = push_error = 0
        for (row_season, competition, round_no), items in sorted(groups.items()):
            if preview:
                push = {"status": "preview"}
            else:
                push = await client.push_round(row_season, competition, round_no, items)
                if push.get("status") == "ok":
                    push_ok += 1
                else:
                    push_error += 1
                    logger.warning(
                        f"天气推送失败（{competition} 第 {round_no} 轮）: {push.get('error')}"
                    )
            rounds.append({
                "season": row_season, "competition": competition,
                "round_no": round_no, "matches": len(items), "push": push,
            })
        status = "preview" if preview else ("ok" if push_error == 0 else "error")
        return {
            "status": status,
            "preview": preview,
            "rounds": rounds,
            "totals": {
                "rounds": len(rounds),
                "matches": sum(r["matches"] for r in rounds),
                "push_ok": push_ok,
                "push_error": push_error,
                "skipped_other_season": skipped_other_season,
            },
        }
