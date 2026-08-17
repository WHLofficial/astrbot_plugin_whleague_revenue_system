"""赛程导入、天气预报、赛果录入、轮次与赛季统计。

按轮次驱动：管理员导入赛程 → 轮前掷天气 → 轮后录入胜平负，
录入赛果时立即按 S8 三维修正公式计算该场上座与票房并记账。
"""

import re

from astrbot.api import logger

from . import formula
from .stadium_service import StadiumService

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

_RESULT_ALIASES = {
    "胜": "W",
    "w": "W",
    "win": "W",
    "平": "D",
    "d": "D",
    "draw": "D",
    "负": "L",
    "l": "L",
    "loss": "L",
}


class FixtureError(Exception):
    pass


def parse_fixture_lines(text: str) -> list[tuple[int, str, str]]:
    """解析赛程文本行。每行三字段：轮次 主队 客队（| 逗号 空白分隔）。"""
    out = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = re.split(r"[|\t,，]+", line)
        if len(parts) < 3:
            parts = line.split()
        if len(parts) < 3:
            raise FixtureError(f"无法解析行: {line}（需为：轮次 主队 客队）")
        try:
            round_no = int(parts[0].strip())
        except ValueError:
            raise FixtureError(f"轮次需为数字: {line}")
        if round_no < 1:
            raise FixtureError(f"轮次需为正数: {line}")
        home = parts[1].strip()
        away = parts[2].strip()
        if not home or not away or home == away:
            raise FixtureError(f"主客队非法: {line}")
        out.append((round_no, home, away))
    return out


def parse_result_lines(text: str) -> list[tuple[str, str]]:
    """解析赛果文本行。每行：主队 胜/平/负（或 W/D/L）。"""
    out = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = re.split(r"[\s|\t,，]+", line)
        if len(parts) < 2:
            raise FixtureError(f"无法解析行: {line}（需为：主队 胜/平/负）")
        home = parts[0].strip()
        result_key = parts[-1].strip().lower()
        result = _RESULT_ALIASES.get(result_key)
        if result is None:
            raise FixtureError(f"赛果需为 胜/平/负（W/D/L）: {line}")
        out.append((home, result))
    return out


class FixtureService:
    def __init__(self, db, dao, cfg, stadium_service: StadiumService | None = None):
        self._db = db
        self._dao = dao
        self._cfg = cfg
        self._stadium_service = stadium_service

    # ─── 赛季状态 ─────────────────────────────────────────

    async def get_state(self):
        state = await self._dao.get_league_state()
        if not state:
            await self._dao.update_league_state(1, 1, 0, "")
            state = await self._dao.get_league_state()
        return state

    async def advance_window(self, updated_by: str) -> dict:
        state = await self.get_state()
        season = state["season_number"]
        window_seq = state["window_seq"] + 1
        await self._dao.update_league_state(season, window_seq, 0, updated_by)
        return {"season_number": season, "window_seq": window_seq}

    async def advance_season(self, updated_by: str) -> dict:
        season = (await self.get_state())["season_number"] + 1
        await self._dao.update_league_state(season, 1, 0, updated_by)
        return {"season_number": season, "window_seq": 1}

    # ─── 赛程导入 ─────────────────────────────────────────

    async def import_fixtures(self, text: str) -> dict:
        rows = parse_fixture_lines(text)
        if not rows:
            raise FixtureError("没有可导入的赛程行")
        state = await self.get_state()
        season, window_seq = state["season_number"], state["window_seq"]

        known = await self._stadium_service.list_known_teams()
        errors = []
        imported, skipped = 0, 0
        for round_no, home, away in rows:
            if home not in known or away not in known:
                errors.append(f"第{round_no}轮 {home} vs {away}: 未知球队（先导入属性或已建队）")
                skipped += 1
                continue
            for team in (home, away):
                await self._stadium_service.ensure_stadium(team)
            await self._dao.add_match(season, window_seq, round_no, home, away)
            imported += 1
        return {"imported": imported, "skipped": skipped, "errors": errors[:10],
                "season": season, "window_seq": window_seq}

    # ─── 天气预报 ─────────────────────────────────────────

    async def forecast_round(self, round_no: int) -> dict:
        state = await self.get_state()
        matches = await self._dao.get_round_matches(
            state["season_number"], state["window_seq"], round_no
        )
        if not matches:
            raise FixtureError(f"第 {round_no} 轮没有赛程")
        results = []
        for m in matches:
            if m["weather"]:
                results.append({"home": m["home_team"], "weather": m["weather"], "existing": True})
                continue
            weather = formula.roll_weather(self._cfg)
            await self._dao.set_match_weather(m["id"], weather)
            results.append({"home": m["home_team"], "weather": weather, "existing": False})
        return {"round_no": round_no, "matches": results}

    async def set_weather(self, round_no: int, home_team: str, weather_text: str) -> dict:
        weather = _WEATHER_ALIASES.get(weather_text.strip().lower())
        if weather is None:
            raise FixtureError(f"未知天气: {weather_text}（晴/多云/雨/雪）")
        state = await self.get_state()
        matches = await self._dao.get_round_matches(
            state["season_number"], state["window_seq"], round_no
        )
        for m in matches:
            if m["home_team"] == home_team:
                await self._dao.set_match_weather(m["id"], weather)
                return {"home": home_team, "weather": weather}
        raise FixtureError(f"第 {round_no} 轮无主队「{home_team}」的比赛")

    # ─── 赛果录入 ─────────────────────────────────────────

    async def record_results(self, round_no: int, text: str) -> dict:
        rows = parse_result_lines(text)
        if not rows:
            raise FixtureError("没有可录入的赛果")
        state = await self.get_state()
        season, window_seq = state["season_number"], state["window_seq"]
        matches = await self._dao.get_round_matches(season, window_seq, round_no)
        by_home = {m["home_team"]: m for m in matches}
        if not by_home:
            raise FixtureError(f"第 {round_no} 轮没有赛程")

        results = []
        for home, result in rows:
            m = by_home.get(home)
            if m is None:
                raise FixtureError(f"第{round_no}轮无主队「{home}」的比赛")
            if m["result"]:
                raise FixtureError(f"第{round_no}轮「{home}」赛果已录入，不能重复")
            detail = await self._record_one(m, result, season, window_seq)
            results.append(detail)
        return {"round_no": round_no, "count": len(results), "results": results}

    async def _record_one(self, match, result: str, season: int, window_seq: int) -> dict:
        home, away = match["home_team"], match["away_team"]
        stadium = await self._stadium_service.ensure_stadium(home)
        away_stadium = await self._dao.get_stadium(away)
        away_influence = (
            away_stadium["influence"]
            if away_stadium else float(self._cfg.get("default_influence", 90.0))
        )
        form_pts = 4  # 无历史赛果时按中性（S8 默认 Pts=4）
        recent = await self._dao.get_last_results(home, 3)
        if recent:
            form_pts = formula.form_pts([r["result"] for r in recent])

        facilities = await self._dao.get_facilities(home)
        commercial_level = facilities.get(formula.FACILITY_COMMERCIAL, 0)
        broadcast_level = facilities.get(formula.FACILITY_BROADCAST, 0)

        att = formula.attendance(
            self._cfg,
            fans=stadium["fans_diehards"],
            capacity=stadium["capacity"],
            tier=stadium["tier"],
            weather=match["weather"],
            form_pts_value=form_pts,
            home_influence=stadium["influence"],
            away_influence=away_influence,
            next_attendance_mod=stadium["next_attendance_mod"],
        )
        ticket, commercial, broadcast = formula.match_revenues(
            self._cfg, att, commercial_level, broadcast_level
        )
        await self._dao.set_match_result(match["id"], result, att, ticket, commercial, broadcast)
        if stadium["next_attendance_mod"] != 1.0:
            await self._dao.reset_attendance_mod(home)

        total = ticket + commercial + broadcast
        await self._dao.ensure_balance(home, float(self._cfg.get("start_funds", 50.0)))
        await self._dao.apply_balance(home, total)
        for kind, amount in (("ticket", ticket), ("commercial", commercial), ("broadcast", broadcast)):
            if amount <= 0:
                continue
            await self._dao.add_transaction(
                home, season, window_seq, kind, amount,
                note=f"第{match['round_no']}轮 主{home} vs 客{away}", round_no=match["round_no"],
            )
        return {
            "home": home, "away": away, "result": result, "weather": match["weather"],
            "form_pts": form_pts, "attendance": att,
            "ticket": ticket, "commercial": commercial, "broadcast": broadcast, "total": round(total, 4),
        }

    # ─── 统计 ─────────────────────────────────────────────

    async def round_stats(self, round_no: int) -> dict:
        state = await self.get_state()
        matches = await self._dao.get_round_matches(
            state["season_number"], state["window_seq"], round_no
        )
        totals = {"attendance": 0, "ticket": 0.0}
        lines = []
        for m in matches:
            if m["attendance"] is None:
                lines.append(f"· {m['home_team']} vs {m['away_team']}：未录赛果")
                continue
            totals["attendance"] += m["attendance"]
            totals["ticket"] += m["ticket_revenue"] or 0
            wx = m["weather"] or "?"
            lines.append(
                f"· {m['home_team']} vs {m['away_team']} {wx} {'胜' if m['result']=='W' else '平' if m['result']=='D' else '负'}"
                f" | 上座 {m['attendance']:,} | 票 {m['ticket_revenue'] or 0:.2f}M"
                f" + 商 {m['commercial'] or 0:.2f}M + 转 {m['broadcast'] or 0:.2f}M"
            )
        return {
            "round_no": round_no,
            "season": state["season_number"],
            "window_seq": state["window_seq"],
            "lines": lines,
            "totals": totals,
        }

    async def season_stats(self) -> dict:
        stadiums = await self._dao.list_stadiums()
        rows = []
        grand = {"attendance": 0, "ticket": 0.0}
        for s in stadiums:
            matches = await self._dao.get_home_matches_all(s["team_name"])
            played = [m for m in matches if m["attendance"] is not None]
            if not played:
                continue
            att_list = [m["attendance"] for m in played]
            ticket_sum = sum(m["ticket_revenue"] or 0 for m in played)
            grand["attendance"] += sum(att_list)
            grand["ticket"] += ticket_sum
            rows.append({
                "team": s["team_name"],
                "home_count": len(played),
                "total": sum(att_list),
                "max": max(att_list),
                "min": min(att_list),
                "avg": int(sum(att_list) / len(att_list)),
                "ticket": round(ticket_sum, 2),
            })
        rows.sort(key=lambda r: r["ticket"], reverse=True)
        return {"rows": rows, "grand": grand}