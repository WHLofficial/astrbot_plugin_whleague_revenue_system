"""赛程导入、天气预报、赛果录入、轮次与赛季统计。

按轮次驱动：管理员导入赛程 → 轮前掷天气 → 轮后录入胜平负，
录入赛果时立即按 S8 三维修正公式计算该场上座与票房并记账。
"""

import re

from astrbot.api import logger

from . import formula
from .file_import_service import parse_fixture_file, parse_result_file
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
    "取消": "C",
    "c": "C",
    "cancel": "C",
    "cancelled": "C",
}

def _parse_schedule_extra(extra) -> tuple[int | None, int | None, str | None]:
    """解析赛程行末可选字段：周 天 时间（顺序固定，见 formula.parse_schedule_fields）。"""
    try:
        return formula.parse_schedule_fields(extra)
    except ValueError as e:
        raise FixtureError(str(e))


class FixtureError(Exception):
    pass


def parse_fixture_lines(text: str, cfg: dict) -> list[tuple]:
    """解析赛程文本行。每行：轮次 主队 客队 [周] [天] [时间]。

    轮次支持文字前缀识别赛事（formula.parse_round_token）。
    返回 (轮次, 赛事, 主队, 客队, 周, 天, 时间)。
    """
    out = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = re.split(r"[|\t,，]+", line)
        if len(parts) < 3:
            parts = line.split()
        if len(parts) < 3:
            raise FixtureError(f"无法解析行: {line}（需为：轮次 主队 客队 [周] [天] [时间]）")
        try:
            competition, round_no = formula.parse_round_token(cfg, parts[0].strip())
        except ValueError as e:
            raise FixtureError(f"轮次非法: {line}（{e}）")
        home = parts[1].strip()
        away = parts[2].strip()
        if not home or not away or home == away:
            raise FixtureError(f"主客队非法: {line}")
        try:
            week, day, match_time = _parse_schedule_extra(parts[3:])
        except FixtureError as e:
            raise FixtureError(f"{line}: {e}")
        out.append((round_no, competition, home, away, week, day, match_time))
    return out


def parse_result_lines(text: str) -> list[tuple[str, str, str | None]]:
    """解析赛果文本行。每行：主队 胜/平/负（或 W/D/L）[比分]，或 主队 <比分>。

    比分（2-1 / 0-0PK2-4 等）自动推导主队胜平负并保留原文为比分。
    """
    out = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = re.split(r"[\s|\t,，]+", line)
        if len(parts) < 2:
            raise FixtureError(f"无法解析行: {line}（需为：主队 胜/平/负 [比分]，或 主队 比分）")
        home = parts[0].strip()
        result_key = parts[1].strip().lower()
        result = _RESULT_ALIASES.get(result_key)
        if result is not None:
            score = " ".join(p.strip() for p in parts[2:] if p.strip()).strip() or None
        else:
            result = formula.result_from_score(result_key)
            if result is None:
                raise FixtureError(
                    f"赛果需为 胜/平/负（W/D/L）或比分（如 2-1 / 0-0PK2-4）: {line}"
                )
            score = " ".join(p.strip() for p in parts[1:] if p.strip()).strip() or None
        out.append((home, result, score))
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
        text = await self._normalize_round_tokens(text)
        rows = parse_fixture_lines(text, self._cfg)
        if not rows:
            raise FixtureError("没有可导入的赛程行")
        state = await self.get_state()
        season, window_seq = state["season_number"], state["window_seq"]

        known = await self._stadium_service.list_known_teams()
        errors = []
        imported, skipped = 0, 0
        for round_no, competition, home, away, week, day, match_time in rows:
            if home not in known or away not in known:
                errors.append(f"第{round_no}轮({competition}) {home} vs {away}: 未知球队（先导入属性或已建队）")
                skipped += 1
                continue
            for team in (home, away):
                await self._stadium_service.ensure_stadium(team)
            inserted = await self._dao.add_match(season, window_seq, round_no, home, away,
                                                 competition, week_no=week, day_no=day,
                                                 match_time=match_time)
            imported += inserted
            if not inserted:
                errors.append(f"第{round_no}轮({competition}) {home} vs {away}: 重复赛程，已忽略")
        return {"imported": imported, "skipped": skipped, "errors": errors[:10],
                "season": season, "window_seq": window_seq}

    async def import_fixtures_file(self, path: str) -> dict:
        """从 xlsx/csv 文件导入赛程（文件 → 归一化 → 复用 import_fixtures）。"""
        parsed = await parse_fixture_file(self._cfg, path)
        if not parsed["lines"]:
            detail = f"（{parsed['errors'][0]}）" if parsed["errors"] else ""
            raise FixtureError(f"文件中没有可导入的赛程行{detail}")
        result = await self.import_fixtures("\n".join(parsed["lines"]))
        result["file_errors"] = parsed["errors"]
        return result

    # ─── 命名轮次（同名即为同一轮） ────────────────────────

    async def resolve_round_arg(self, token) -> tuple[str, int]:
        """解析命令里的轮次参数：带数字走 parse_round_token；纯文字按登记表取轮次号。

        纯文字轮次与导入时同名登记恒为同一号（同一赛季内），供天气/赛果/统计使用。
        """
        s = str(token or "").strip()
        if not s:
            raise ValueError("轮次不能为空")
        comp, rest = formula.split_competition(self._cfg, s)
        digits = re.sub(r"\D", "", rest)
        if digits:
            round_no = int(digits)
            if round_no < 1:
                raise ValueError(f"轮次需为正数: {token}")
            return comp, round_no
        season = (await self.get_state())["season_number"]
        round_no = await self._dao.add_named_round(season, s)
        return comp, round_no

    async def _normalize_round_tokens(self, text: str) -> str:
        """赛程文本首列的纯文字轮次改写为「前缀+轮次号」（同名登记，跨导入恒同号）。

        带数字的轮次原样保留；空 token 的行不动（沿用既有报错）。
        """
        season = (await self.get_state())["season_number"]
        out = []
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            parts = re.split(r"[|\t,，]+", line)
            if len(parts) < 3:
                parts = line.split()
            if not parts or not parts[0].strip():
                out.append(raw_line)
                continue
            token = parts[0].strip()
            comp, rest = formula.split_competition(self._cfg, token)
            if re.sub(r"\D", "", rest):
                out.append(raw_line)
                continue
            round_no = await self._dao.add_named_round(season, token)
            prefix = token[: len(token) - len(rest)]
            parts[0] = f"{prefix}{round_no}" if prefix else str(round_no)
            out.append(" ".join(p.strip() for p in parts))
        return "\n".join(out)

    # ─── 天气预报 ─────────────────────────────────────────

    async def forecast_round(self, round_no: int, competition: str | None = None) -> dict:
        state = await self.get_state()
        matches = await self._dao.get_round_matches(
            state["season_number"], state["window_seq"], round_no, competition
        )
        if not matches:
            raise FixtureError(f"第 {round_no} 轮({competition or '联赛'})没有赛程")
        results = []
        for m in matches:
            if m["weather"]:
                results.append({"home": m["home_team"], "weather": m["weather"], "existing": True})
                continue
            weather = formula.roll_weather(self._cfg)
            await self._dao.set_match_weather(m["id"], weather)
            results.append({"home": m["home_team"], "weather": weather, "existing": False})
        return {"round_no": round_no, "competition": competition or "联赛", "matches": results}

    async def set_weather(self, round_no: int, home_team: str, weather_text: str,
                          competition: str | None = None) -> dict:
        weather = _WEATHER_ALIASES.get(weather_text.strip().lower())
        if weather is None:
            raise FixtureError(f"未知天气: {weather_text}（晴/多云/雨/雪）")
        state = await self.get_state()
        matches = await self._dao.get_round_matches(
            state["season_number"], state["window_seq"], round_no, competition
        )
        for m in matches:
            if m["home_team"] == home_team:
                await self._dao.set_match_weather(m["id"], weather)
                return {"home": home_team, "weather": weather}
        raise FixtureError(f"第 {round_no} 轮({competition or '联赛'})无主队「{home_team}」的比赛")

    # ─── 赛果录入 ─────────────────────────────────────────

    async def record_results(self, round_no: int, text: str,
                             competition: str | None = None) -> dict:
        rows = parse_result_lines(text)
        if not rows:
            raise FixtureError("没有可录入的赛果")
        state = await self.get_state()
        season, window_seq = state["season_number"], state["window_seq"]
        matches = await self._dao.get_round_matches(season, window_seq, round_no, competition)
        by_home = {m["home_team"]: m for m in matches}
        if not by_home:
            raise FixtureError(f"第 {round_no} 轮({competition or '联赛'})没有赛程")

        results = []
        for home, result, score in rows:
            m = by_home.get(home)
            if m is None:
                raise FixtureError(f"第{round_no}轮无主队「{home}」的比赛")
            if m["result"]:
                raise FixtureError(f"第{round_no}轮「{home}」赛果已录入，不能重复")
            detail = await self._record_one(m, result, season, window_seq, score)
            results.append(detail)
        return {"round_no": round_no, "count": len(results), "results": results}

    async def record_results_file(self, round_no: int, path: str,
                                  competition: str | None = None) -> dict:
        """从 xlsx/csv 文件录入赛果（文件 → 归一化 → 复用 record_results）。"""
        parsed = await parse_result_file(self._cfg, path)
        if not parsed["lines"]:
            detail = f"（{parsed['errors'][0]}）" if parsed["errors"] else ""
            raise FixtureError(f"文件中没有可录入的赛果行{detail}")
        result = await self.record_results(round_no, "\n".join(parsed["lines"]), competition)
        result["file_errors"] = parsed["errors"]
        return result

    async def _record_one(self, match, result: str, season: int, window_seq: int,
                          score: str | None = None) -> dict:
        home, away = match["home_team"], match["away_team"]
        if result == "C":
            # 比赛取消：无观众、无收益、不记任何流水
            await self._dao.set_match_result(match["id"], "C", None, 0.0, 0.0, 0.0, score)
            return {
                "home": home, "away": away, "result": "C", "score": score,
                "weather": match["weather"], "form_pts": 0, "attendance": None,
                "ticket": 0.0, "commercial": 0.0, "broadcast": 0.0, "total": 0.0,
            }
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
        await self._dao.set_match_result(match["id"], result, att, ticket, commercial, broadcast, score)
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
            "home": home, "away": away, "result": result, "score": score,
            "weather": match["weather"],
            "form_pts": form_pts, "attendance": att,
            "ticket": ticket, "commercial": commercial, "broadcast": broadcast, "total": round(total, 4),
        }

    # ─── 统计 ─────────────────────────────────────────────

    async def round_stats(self, round_no: int, competition: str | None = None) -> dict:
        state = await self.get_state()
        matches = await self._dao.get_round_matches(
            state["season_number"], state["window_seq"], round_no, competition
        )
        totals = {"attendance": 0, "ticket": 0.0}
        lines = []
        for m in matches:
            if m["result"] == "C":
                lines.append(f"· {m['home_team']} vs {m['away_team']}：比赛取消（不计入合计）")
                continue
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