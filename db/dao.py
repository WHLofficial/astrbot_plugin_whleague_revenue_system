from datetime import datetime


class StadiumDAO:
    def __init__(self, db_manager):
        self._db = db_manager

    # ─── 基础设施 ─────────────────────────────────────────

    @staticmethod
    def _now_str() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def set_config(self, key: str, value: str) -> None:
        await self._db.execute(
            "INSERT INTO plugin_config (key, value, updated_at) VALUES (?, ?, datetime('now','localtime')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now','localtime')",
            (key, value),
        )

    async def get_all_config(self) -> list:
        return await self._db.fetchall("SELECT key, value FROM plugin_config")

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

    # ─── 赛季状态 ─────────────────────────────────────────

    async def get_league_state(self):
        return await self._db.fetchone(
            "SELECT * FROM league_state WHERE id=1"
        )

    async def update_league_state(self, season, window_seq, current_round, updated_by) -> None:
        await self._db.execute(
            "UPDATE league_state SET season_number=?, window_seq=?, current_round=?, "
            "updated_by=?, updated_at=datetime('now','localtime') WHERE id=1",
            (season, window_seq, current_round, updated_by),
        )

    # ─── 球场 ─────────────────────────────────────────────

    async def get_stadium(self, team_name: str):
        return await self._db.fetchone(
            "SELECT * FROM stadium WHERE team_name=?", (team_name,)
        )

    async def list_stadiums(self) -> list:
        return await self._db.fetchall("SELECT * FROM stadium ORDER BY team_name")

    async def create_stadium(
        self, team_name: str, capacity: int, tier: int, influence: float, fans: float
    ) -> None:
        await self._db.execute(
            "INSERT INTO stadium (team_name, name, capacity, tier, influence, fans_diehards) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (team_name, f"{team_name}主场", capacity, tier, influence, fans),
        )

    async def update_stadium_attrs(
        self, team_name: str, capacity: int | None = None, tier: int | None = None
    ) -> None:
        sets = ["updated_at=datetime('now','localtime')"]
        params = []
        if capacity is not None:
            sets.append("capacity=?")
            params.append(capacity)
        if tier is not None:
            sets.append("tier=?")
            params.append(tier)
        params.append(team_name)
        await self._db.execute(
            f"UPDATE stadium SET {', '.join(sets)} WHERE team_name=?", params
        )

    async def update_influence(self, team_name: str, influence: float) -> None:
        await self._db.execute(
            "UPDATE stadium SET influence=?, updated_at=datetime('now','localtime') WHERE team_name=?",
            (influence, team_name),
        )

    async def update_fans(self, team_name: str, fans: float) -> None:
        await self._db.execute(
            "UPDATE stadium SET fans_diehards=?, updated_at=datetime('now','localtime') WHERE team_name=?",
            (fans, team_name),
        )

    async def update_stadium_name(self, team_name: str, name: str) -> None:
        await self._db.execute(
            "UPDATE stadium SET name=?, updated_at=datetime('now','localtime') WHERE team_name=?",
            (name, team_name),
        )

    async def mark_rename_used(self, team_name: str) -> None:
        await self._db.execute(
            "UPDATE stadium SET free_rename_used=1, updated_at=datetime('now','localtime') WHERE team_name=?",
            (team_name,),
        )

    async def update_attendance_mod(self, team_name: str, mod: float) -> None:
        await self._db.execute(
            "UPDATE stadium SET next_attendance_mod=?, updated_at=datetime('now','localtime') WHERE team_name=?",
            (mod, team_name),
        )

    async def reset_attendance_mod(self, team_name: str) -> None:
        await self._db.execute(
            "UPDATE stadium SET next_attendance_mod=1.0, updated_at=datetime('now','localtime') WHERE team_name=?",
            (team_name,),
        )

    # ─── 子设施 ───────────────────────────────────────────

    async def get_facilities(self, team_name: str) -> dict:
        rows = await self._db.fetchall(
            "SELECT facility_key, level FROM stadium_facilities WHERE team_name=?",
            (team_name,),
        )
        return {r["facility_key"]: r["level"] for r in rows}

    async def get_facility_level(self, team_name: str, facility_key: str) -> int:
        row = await self._db.fetchone(
            "SELECT level FROM stadium_facilities WHERE team_name=? AND facility_key=?",
            (team_name, facility_key),
        )
        return row["level"] if row else 0

    async def upsert_facility(self, team_name: str, facility_key: str, level: int) -> None:
        await self._db.execute(
            "INSERT INTO stadium_facilities (team_name, facility_key, level, updated_at) "
            "VALUES (?, ?, ?, datetime('now','localtime')) "
            "ON CONFLICT(team_name, facility_key) DO UPDATE SET level=excluded.level, "
            "updated_at=datetime('now','localtime')",
            (team_name, facility_key, level),
        )

    # ─── 余额 ─────────────────────────────────────────────

    async def get_balance(self, team_name: str):
        return await self._db.fetchone(
            "SELECT * FROM club_balance WHERE team_name=?", (team_name,)
        )

    async def ensure_balance(self, team_name: str, start_funds: float) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO club_balance (team_name, balance, build_credit) VALUES (?, ?, 0)",
            (team_name, start_funds),
        )

    async def apply_balance(self, team_name: str, amount: float, build_credit_amount: float = 0.0) -> None:
        if build_credit_amount:
            await self._db.execute(
                "UPDATE club_balance SET balance=balance+?, build_credit=build_credit+?, "
                "updated_at=datetime('now','localtime') WHERE team_name=?",
                (amount, build_credit_amount, team_name),
            )
        else:
            await self._db.execute(
                "UPDATE club_balance SET balance=balance+?, updated_at=datetime('now','localtime') WHERE team_name=?",
                (amount, team_name),
            )

    async def deduct_build_credit(self, team_name: str, amount: float) -> float:
        """用建设券抵扣建设支出，返回实际抵扣金额。"""
        if amount <= 0:
            return 0.0
        row = await self._db.fetchone(
            "SELECT build_credit FROM club_balance WHERE team_name=?", (team_name,)
        )
        credit = row["build_credit"] if row else 0.0
        used = min(credit, amount)
        if used > 0:
            await self._db.execute(
                "UPDATE club_balance SET build_credit=build_credit-?, updated_at=datetime('now','localtime') WHERE team_name=?",
                (used, team_name),
            )
        return used

    # ─── 账务流水 ─────────────────────────────────────────

    async def add_transaction(
        self, team_name: str, season: int, window_seq: int, kind: str,
        amount: float, note: str = "", round_no: int | None = None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO revenue_transactions (team_name, season_number, window_seq, round_no, kind, amount, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (team_name, season, window_seq, round_no, kind, amount, note),
        )

    async def list_transactions(
        self, team_name: str, season: int | None = None, window_seq: int | None = None,
        limit: int = 100,
    ) -> list:
        sql = "SELECT * FROM revenue_transactions WHERE team_name=?"
        params = [team_name]
        if season is not None:
            sql += " AND season_number=?"
            params.append(season)
        if window_seq is not None:
            sql += " AND window_seq=?"
            params.append(window_seq)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return await self._db.fetchall(sql, params)

    # ─── 赛程/赛果 ────────────────────────────────────────

    async def add_match(
        self, season: int, window_seq: int, round_no: int, home_team: str, away_team: str,
        competition: str = "联赛", week_no: int | None = None,
        day_no: int | None = None, match_time: str | None = None,
    ) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO matches (season_number, window_seq, round_no, competition, "
            "home_team, away_team, week_no, day_no, match_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (season, window_seq, round_no, competition, home_team, away_team,
             week_no, day_no, match_time),
        )

    async def get_round_matches(
        self, season: int, window_seq: int, round_no: int, competition: str | None = None
    ) -> list:
        if competition:
            return await self._db.fetchall(
                "SELECT * FROM matches WHERE season_number=? AND window_seq=? AND round_no=? "
                "AND competition=? ORDER BY id",
                (season, window_seq, round_no, competition),
            )
        return await self._db.fetchall(
            "SELECT * FROM matches WHERE season_number=? AND window_seq=? AND round_no=? ORDER BY id",
            (season, window_seq, round_no),
        )
        return await self._db.fetchall(
            "SELECT * FROM matches WHERE season_number=? AND window_seq=? AND round_no=? ORDER BY id",
            (season, window_seq, round_no),
        )

    async def get_window_matches(self, season: int, window_seq: int) -> list:
        return await self._db.fetchall(
            "SELECT * FROM matches WHERE season_number=? AND window_seq=? ORDER BY round_no, id",
            (season, window_seq),
        )

    async def get_home_matches_window(self, team_name: str, season: int, window_seq: int) -> list:
        return await self._db.fetchall(
            "SELECT * FROM matches WHERE home_team=? AND season_number=? AND window_seq=? ORDER BY id",
            (team_name, season, window_seq),
        )

    async def get_home_matches_all(self, team_name: str) -> list:
        return await self._db.fetchall(
            "SELECT * FROM matches WHERE home_team=? ORDER BY season_number, window_seq, round_no",
            (team_name,),
        )

    async def get_home_matches_season(self, team_name: str, season: int) -> list:
        return await self._db.fetchall(
            "SELECT * FROM matches WHERE home_team=? AND season_number=? "
            "ORDER BY window_seq, round_no, id",
            (team_name, season),
        )

    async def get_last_results(self, team_name: str, limit: int = 3) -> list:
        return await self._db.fetchall(
            "SELECT * FROM matches WHERE home_team=? AND result IS NOT NULL "
            "ORDER BY season_number DESC, window_seq DESC, round_no DESC LIMIT ?",
            (team_name, limit),
        )

    async def set_match_weather(self, match_id: int, weather: str) -> None:
        await self._db.execute("UPDATE matches SET weather=? WHERE id=?", (weather, match_id))

    async def set_match_result(
        self, match_id: int, result: str, attendance: int,
        ticket_revenue: float, commercial: float, broadcast: float, score: str | None = None,
    ) -> None:
        await self._db.execute(
            "UPDATE matches SET result=?, score=?, attendance=?, ticket_revenue=?, commercial=?, broadcast=? WHERE id=?",
            (result, score, attendance, ticket_revenue, commercial, broadcast, match_id),
        )

    # ─── 影响力历史 ───────────────────────────────────────

    async def add_influence_history(
        self, team_name: str, season: int, window_seq: int, influence: float
    ) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO influence_history (team_name, season_number, window_seq, influence) "
            "VALUES (?, ?, ?, ?)",
            (team_name, season, window_seq, influence),
        )

    # ─── 冠名 ─────────────────────────────────────────────

    async def get_active_naming(self, team_name: str):
        return await self._db.fetchone(
            "SELECT * FROM naming_rights WHERE team_name=? AND status='active'", (team_name,)
        )

    async def add_naming(
        self, team_name: str, brand: str, heat: float, fee_per_window: float,
        windows_total: int, season: int, window_seq: int,
    ) -> None:
        await self._db.execute(
            "INSERT INTO naming_rights (team_name, brand, heat, fee_per_window, windows_total, "
            "windows_remaining, status, signed_season, signed_window) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (team_name, brand, heat, fee_per_window, windows_total, windows_total, season, window_seq),
        )

    async def tick_naming(self, team_name: str) -> None:
        await self._db.execute(
            "UPDATE naming_rights SET windows_remaining=windows_remaining-1 "
            "WHERE team_name=? AND status='active'",
            (team_name,),
        )

    async def expire_namings(self) -> None:
        await self._db.execute(
            "UPDATE naming_rights SET status='expired' WHERE status='active' AND windows_remaining<=0"
        )

    async def terminate_naming(self, team_name: str) -> None:
        await self._db.execute(
            "UPDATE naming_rights SET status='terminated', windows_remaining=0 "
            "WHERE team_name=? AND status='active'",
            (team_name,),
        )

    # ─── 品牌池 ───────────────────────────────────────────

    async def add_brand(self, brand: str, heat: float, source: str, status: str = "adopted") -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO brand_pool (brand, heat, source, status) VALUES (?, ?, ?, ?)",
            (brand, heat, source, status),
        )

    async def list_brands(self, status: str | None = None) -> list:
        if status is None:
            return await self._db.fetchall("SELECT * FROM brand_pool ORDER BY id")
        return await self._db.fetchall(
            "SELECT * FROM brand_pool WHERE status=? ORDER BY id", (status,)
        )

    async def adopt_brand(self, brand_id: int) -> None:
        await self._db.execute(
            "UPDATE brand_pool SET status='adopted' WHERE id=? AND status='pending'", (brand_id,)
        )

    async def discard_brand(self, brand_id: int) -> None:
        await self._db.execute(
            "UPDATE brand_pool SET status='discarded' WHERE id=? AND status='pending'", (brand_id,)
        )

    # ─── 档期 ─────────────────────────────────────────────

    async def add_booking(
        self, team_name: str, season: int, window_seq: int, slot_no: int,
        activity_type: str, booked_by: str,
    ) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO venue_bookings (team_name, season_number, window_seq, slot_no, activity_type, booked_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (team_name, season, window_seq, slot_no, activity_type, booked_by),
        )

    async def get_bookings(self, team_name: str, season: int, window_seq: int) -> list:
        return await self._db.fetchall(
            "SELECT * FROM venue_bookings WHERE team_name=? AND season_number=? AND window_seq=? ORDER BY slot_no",
            (team_name, season, window_seq),
        )

    async def clear_bookings(self, season: int, window_seq: int) -> None:
        await self._db.execute(
            "DELETE FROM venue_bookings WHERE season_number=? AND window_seq=?", (season, window_seq)
        )

    # ─── 事件池/日志 ──────────────────────────────────────

    async def upsert_event(self, event_id: str, name: str, category: str, weight: int,
                           conditions_json: str, effects_json: str, template: str,
                           source: str, status: str) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO event_pool (event_id, name, category, weight, conditions_json, "
            "effects_json, template, source, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, name, category, weight, conditions_json, effects_json, template, source, status),
        )

    async def list_events(self, status: str | None = None) -> list:
        if status is None:
            return await self._db.fetchall("SELECT * FROM event_pool ORDER BY id")
        return await self._db.fetchall(
            "SELECT * FROM event_pool WHERE status=? ORDER BY id", (status,)
        )

    async def adopt_event(self, event_id: int) -> None:
        await self._db.execute(
            "UPDATE event_pool SET status='adopted' WHERE id=? AND status='pending'", (event_id,)
        )

    async def discard_event(self, event_id: int) -> None:
        await self._db.execute(
            "UPDATE event_pool SET status='discarded' WHERE id=? AND status='pending'", (event_id,)
        )

    async def add_event_log(
        self, team_name: str, season: int, window_seq: int, event_id: str,
        effect_json: str, text: str,
    ) -> None:
        await self._db.execute(
            "INSERT INTO events_log (team_name, season_number, window_seq, event_id, effect_json, text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (team_name, season, window_seq, event_id, effect_json, text),
        )

    async def update_event_log_text(self, team_name: str, event_id: str, text: str) -> None:
        await self._db.execute(
            "UPDATE events_log SET text=? WHERE id = "
            "(SELECT id FROM events_log WHERE team_name=? AND event_id=? "
            "ORDER BY id DESC LIMIT 1)",
            (text, team_name, event_id),
        )

    async def get_window_events(self, team_name: str, season: int, window_seq: int) -> list:
        return await self._db.fetchall(
            "SELECT * FROM events_log WHERE team_name=? AND season_number=? AND window_seq=? ORDER BY id",
            (team_name, season, window_seq),
        )

    async def fetch_event_by_id(self, event_id: str):
        return await self._db.fetchone(
            "SELECT * FROM event_pool WHERE event_id=?", (event_id,)
        )

    async def next_event_counter(self) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM event_pool"
        )
        return (row["n"] if row else 0) + 1

    # ─── 窗口结算 ─────────────────────────────────────────

    async def has_window_summary(self, season: int, window_seq: int) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM window_summaries WHERE season_number=? AND window_seq=?", (season, window_seq)
        )
        return row is not None

    async def add_window_summary(self, season: int, window_seq: int) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO window_summaries (season_number, window_seq) VALUES (?, ?)",
            (season, window_seq),
        )