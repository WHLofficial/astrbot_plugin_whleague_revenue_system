"""DAO：5 张核心表（matches/stadium/league_state/admins/plugin_config）读写。

只保留展示插件用到的读写；财务/冠名/市场/事件/档期/命名轮次等旧面已随
schema v15 的 16 张旧表一起下线。
"""

from datetime import datetime

from ..services.formula import competition_texts


class StadiumDAO:
    def __init__(self, db_manager):
        self._db = db_manager

    # ─── 基础设施 ─────────────────────────────────────────

    @staticmethod
    def _now_str() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 服务层公开入口（不再跨层调私有 _now_str）
    now_str = _now_str

    async def set_config(self, key: str, value: str) -> None:
        """写配置 kv（v15 起 plugin_config 无 updated_at 列）。"""
        await self._db.execute(
            "INSERT INTO plugin_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def get_config(self, key: str) -> str | None:
        row = await self._db.fetchone("SELECT value FROM plugin_config WHERE key=?", (key,))
        return row["value"] if row else None

    async def get_all_config(self) -> list:
        return await self._db.fetchall("SELECT key, value FROM plugin_config")

    # ─── 管理员 ───────────────────────────────────────────

    async def is_admin(self, qq: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM admins WHERE qq=?", (str(qq),)
        )
        return row is not None

    async def add_admin(self, qq: str, added_by: str) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO admins (qq, added_by) VALUES (?, ?)",
            (str(qq), added_by),
        )

    async def remove_admin(self, qq: str) -> None:
        await self._db.execute("DELETE FROM admins WHERE qq=?", (str(qq),))

    async def count_admins(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) AS n FROM admins")
        return int(row["n"]) if row else 0

    # ─── 赛季状态 ─────────────────────────────────────────

    async def get_league_state(self):
        return await self._db.fetchone(
            "SELECT * FROM league_state WHERE id=1"
        )

    async def set_league_state(self, season_number: int, season_label: str) -> None:
        """设置当前赛季（v15：league_state 只有 season_number/season_label）。"""
        await self._db.execute(
            "INSERT INTO league_state (id, season_number, season_label) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET season_number=excluded.season_number, "
            "season_label=excluded.season_label, updated_at=datetime('now','localtime')",
            (int(season_number), season_label),
        )

    async def season_label(self, season: int) -> str:
        """展示用赛季标签（v3：赛季命名已下线，统一 S{n}）。"""
        return f"S{int(season)}"

    # ─── 球场（只读展示；容量/名称来自 club） ─────────────

    async def get_stadium(self, team_name: str):
        return await self._db.fetchone(
            "SELECT * FROM stadium WHERE team_name=?", (team_name,)
        )

    async def list_stadiums(self) -> list:
        return await self._db.fetchall("SELECT * FROM stadium ORDER BY team_name")

    # ─── matches（赛程/赛果同步面） ───────────────────────

    async def list_matches_by_season(self, season: int) -> list:
        """取一季全部赛程行（v15 列集，含 tour 绑定与赛果），供全量同步比对。"""
        cur = await self._db.execute(
            "SELECT id, season_number, tournament_id, competition, round_no, leg, stage_name, "
            "home_team, away_team, home_score, away_score, pen_home, pen_away, result, score, "
            "tour_status, walkover_side, weather, wx_coef, week_no, day_no, match_time, "
            "tour_match_id, synced_at FROM matches "
            "WHERE season_number=? ORDER BY round_no, leg, id",
            (season,),
        )
        try:
            return await cur.fetchall()
        finally:
            await cur.close()

    async def upsert_match(
        self, season: int, tournament_id: int, competition: str, round_no: int,
        home_team: str, away_team: str, leg: int = 0, stage_name: str = "",
        home_score: int | None = None, away_score: int | None = None,
        pen_home: int | None = None, pen_away: int | None = None,
        result: str | None = None, score: str | None = None,
        tour_status: str = "pending", walkover_side: str | None = None,
        week_no: int | None = None, day_no: int | None = None,
        match_time: str | None = None, tour_match_id: int | None = None,
        synced_at: str | None = None, match_id: int | None = None,
    ) -> int:
        """赛程行 upsert（tour 同步入口）：显式 match_id > tour_match_id > 唯一键。

        命中已有行时覆盖 leg/比分/点球/result/score/tour_status/walkover/轮次/
        时刻/tour_match_id/synced_at（stage_name 仅在传非空值时覆盖）；不触碰
        weather/wx_coef（天气由插件本地管理）。返回 match id。
        match_id 由引擎先做归一队名匹配后显式传入，跳过自匹配。
        """
        row = None
        if match_id is not None:
            row = await self._db.fetchone("SELECT id FROM matches WHERE id=?", (match_id,))
        if row is None and tour_match_id is not None:
            row = await self._db.fetchone(
                "SELECT id FROM matches WHERE tour_match_id=?", (tour_match_id,)
            )
        if row is None:
            row = await self._db.fetchone(
                "SELECT id FROM matches WHERE season_number=? AND tournament_id=? "
                "AND round_no=? AND leg=? AND home_team=? AND away_team=?",
                (season, tournament_id, round_no, leg, home_team, away_team),
            )
        if row is not None:
            await self._db.execute(
                "UPDATE matches SET round_no=?, leg=?, "
                "stage_name=COALESCE(NULLIF(?, ''), stage_name), "
                "home_score=?, away_score=?, pen_home=?, pen_away=?, result=?, score=?, "
                "tour_status=?, walkover_side=?, week_no=?, day_no=?, match_time=?, "
                "tour_match_id=COALESCE(?, tour_match_id), synced_at=COALESCE(?, synced_at) "
                "WHERE id=?",
                (round_no, leg, stage_name, home_score, away_score, pen_home, pen_away,
                 result, score, tour_status, walkover_side, week_no, day_no, match_time,
                 tour_match_id, synced_at, row["id"]),
            )
            return int(row["id"])
        cur = await self._db.execute(
            "INSERT INTO matches (season_number, tournament_id, competition, round_no, leg, "
            "stage_name, home_team, away_team, home_score, away_score, pen_home, pen_away, "
            "result, score, tour_status, walkover_side, week_no, day_no, match_time, "
            "tour_match_id, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?)",
            (season, tournament_id, competition, round_no, leg, stage_name, home_team,
             away_team, home_score, away_score, pen_home, pen_away, result, score,
             tour_status, walkover_side, week_no, day_no, match_time, tour_match_id,
             synced_at),
        )
        try:
            return int(cur.lastrowid)
        finally:
            await cur.close()

    async def apply_match_sync_ops(self, ops: list, season: int) -> dict:
        """事务执行一个赛事的赛程同步写批（单赛事独立事务，失败回滚）。

        ops 项：{"kind": "update", "id": int, "row": {展示字段}} 或
        {"kind": "insert", "row": {...}}。返回 {"created": int, "updated": int}。
        """
        async def work(conn):
            created = updated = 0
            for op in ops:
                row = op["row"]
                if op["kind"] == "update":
                    await conn.execute(
                        "UPDATE matches SET round_no=?, leg=?, stage_name=?, "
                        "home_team=?, away_team=?, home_score=?, away_score=?, "
                        "pen_home=?, pen_away=?, result=?, score=?, tour_status=?, "
                        "walkover_side=?, "
                        # tour 未排期（week/day/time 为 NULL）不覆盖本地时刻：
                        # CSV 导入等本地快照保留，tour 排期后非空值照常覆盖
                        "week_no=COALESCE(?, week_no), day_no=COALESCE(?, day_no), "
                        "match_time=COALESCE(?, match_time), "
                        "tour_match_id=?, synced_at=? WHERE id=? AND season_number=?",
                        (
                            row["round_no"], row["leg"], row["stage_name"],
                            row["home_team"], row["away_team"], row["home_score"],
                            row["away_score"], row["pen_home"], row["pen_away"],
                            row["result"], row["score"], row["tour_status"],
                            row["walkover_side"], row["week_no"], row["day_no"],
                            row["match_time"], row["tour_match_id"], row["synced_at"],
                            op["id"], season,
                        ),
                    )
                    updated += 1
                else:
                    await conn.execute(
                        "INSERT INTO matches (season_number, tournament_id, competition, "
                        "round_no, leg, stage_name, home_team, away_team, home_score, "
                        "away_score, pen_home, pen_away, result, score, tour_status, "
                        "walkover_side, week_no, day_no, match_time, tour_match_id, "
                        "synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                        "?, ?, ?, ?, ?, ?)",
                        (
                            season, row["tournament_id"], row["competition"],
                            row["round_no"], row["leg"], row["stage_name"],
                            row["home_team"], row["away_team"], row["home_score"],
                            row["away_score"], row["pen_home"], row["pen_away"],
                            row["result"], row["score"], row["tour_status"],
                            row["walkover_side"], row["week_no"], row["day_no"],
                            row["match_time"], row["tour_match_id"], row["synced_at"],
                        ),
                    )
                    created += 1
            return {"created": created, "updated": updated}

        return await self._db.execute_transaction(work)

    async def get_round_matches(
        self, season: int, round_no: int, competition: str | None = None
    ) -> list:
        """按赛季 + 轮次（可选赛事）取全部赛程行（v15：无 window 过滤）。

        赛事按等价文本集匹配：「联赛」迁移行与「顶级联赛」新行同轮可并存，
        顶级联赛查询两者都命中（competition_texts）。
        """
        texts = competition_texts(competition)
        if texts:
            marks = ",".join("?" for _ in texts)
            return await self._db.fetchall(
                f"SELECT * FROM matches WHERE season_number=? AND round_no=? "
                f"AND competition IN ({marks}) "
                "ORDER BY tournament_id, leg, id",
                (season, round_no, *texts),
            )
        return await self._db.fetchall(
            "SELECT * FROM matches WHERE season_number=? AND round_no=? "
            "ORDER BY tournament_id, leg, id",
            (season, round_no),
        )

    # ─── 天气 ─────────────────────────────────────────────

    async def set_match_weather(
        self, season: int, tournament_id: int, round_no: int,
        weather: str, wx_coef: float | None = None,
    ) -> int:
        """轮级写天气（预报/覆盖）。wx_coef 非空时一并固化；为空保留原系数（COALESCE）。

        返回受影响行数。
        """
        cur = await self._db.execute(
            "UPDATE matches SET weather=?, wx_coef=COALESCE(?, wx_coef) "
            "WHERE season_number=? AND tournament_id=? AND round_no=?",
            (weather, wx_coef, season, tournament_id, round_no),
        )
        try:
            return cur.rowcount or 0
        finally:
            await cur.close()

    async def set_match_weather_by_id(
        self, match_id: int, weather: str, wx_coef: float | None = None,
    ) -> None:
        """按行写天气（预报逐行抽签/手动覆盖单队）。wx_coef 非空一并固化。"""
        await self._db.execute(
            "UPDATE matches SET weather=?, wx_coef=COALESCE(?, wx_coef) WHERE id=?",
            (weather, wx_coef, match_id),
        )

    async def fill_wx_coef(
        self, season: int, tournament_id: int, round_no: int, coef: float
    ) -> int:
        """补抽落库（轮级）：仅当 wx_coef 仍为 NULL 时写入，返回受影响行数（0=已固化，幂等）。"""
        cur = await self._db.execute(
            "UPDATE matches SET wx_coef=? WHERE season_number=? AND tournament_id=? "
            "AND round_no=? AND wx_coef IS NULL",
            (coef, season, tournament_id, round_no),
        )
        try:
            return cur.rowcount or 0
        finally:
            await cur.close()

    async def list_weathered_matches(self) -> list:
        """全部已预报天气的场次（按赛季/赛事/轮次排序），供天气推送逐轮遍历。"""
        return await self._db.fetchall(
            "SELECT * FROM matches WHERE weather IS NOT NULL AND weather<>'' "
            "ORDER BY season_number, tournament_id, round_no, leg, id"
        )
