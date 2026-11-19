"""数据库结构 v15：fresh 5 表、v12–v14 合成库自包含迁移、幂等、非法版本报错、dao 核心读写。

s0 只依赖 DatabaseManager + init_schema + StadiumDAO（不装配将亡服务）。
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests.common  # noqa: E402,F401  (import 副作用即 install_stubs)
from tests.common import run_module  # noqa: E402

from astrbot_plugin_whleague_revenue_system.db.connection import DatabaseManager  # noqa: E402
from astrbot_plugin_whleague_revenue_system.db.dao import StadiumDAO  # noqa: E402
from astrbot_plugin_whleague_revenue_system.db.schema import (  # noqa: E402
    SCHEMA_VERSION,
    _table_columns,
    init_schema,
    parse_score_text,
)

CORE_TABLES = {"matches", "stadium", "league_state", "admins", "plugin_config"}
DROPPED_TABLES = {
    "market_offers", "event_choices", "events_log", "window_summaries",
    "window_fans_log", "revenue_transactions", "club_balance", "naming_rights",
    "brand_pool", "market_rounds", "event_pool", "stadium_facilities",
    "venue_bookings", "influence_history", "season_names", "round_names",
}
MATCHES_COLUMNS = {
    "id", "season_number", "tournament_id", "competition", "round_no", "leg",
    "stage_name", "home_team", "away_team", "home_score", "away_score",
    "pen_home", "pen_away", "result", "score", "tour_status", "walkover_side",
    "weather", "wx_coef", "week_no", "day_no", "match_time", "tour_match_id",
    "synced_at", "created_at",
}
DROPPED_MATCH_COLUMNS = {
    "window_seq", "attendance", "ticket_revenue", "commercial", "broadcast",
    "kickoff_source",
}

_LEGACY_TABLES_DDL = """
CREATE TABLE league_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    season_number INTEGER NOT NULL DEFAULT 1,
    window_seq INTEGER NOT NULL DEFAULT 1,
    current_round INTEGER NOT NULL DEFAULT 0,
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE stadium (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    capacity INTEGER NOT NULL DEFAULT 12000,
    tier INTEGER NOT NULL DEFAULT 0,
    influence REAL NOT NULL DEFAULT 90,
    fans_diehards REAL NOT NULL DEFAULT 1800,
    next_attendance_mod REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qq TEXT NOT NULL UNIQUE,
    added_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE plugin_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE market_offers (id INTEGER PRIMARY KEY);
CREATE TABLE event_choices (id INTEGER PRIMARY KEY);
CREATE TABLE events_log (id INTEGER PRIMARY KEY);
CREATE TABLE window_summaries (id INTEGER PRIMARY KEY);
CREATE TABLE window_fans_log (id INTEGER PRIMARY KEY);
CREATE TABLE revenue_transactions (id INTEGER PRIMARY KEY);
CREATE TABLE club_balance (id INTEGER PRIMARY KEY);
CREATE TABLE naming_rights (id INTEGER PRIMARY KEY);
CREATE TABLE brand_pool (id INTEGER PRIMARY KEY);
CREATE TABLE market_rounds (id INTEGER PRIMARY KEY);
CREATE TABLE event_pool (id INTEGER PRIMARY KEY);
CREATE TABLE stadium_facilities (id INTEGER PRIMARY KEY);
CREATE TABLE venue_bookings (id INTEGER PRIMARY KEY);
CREATE TABLE influence_history (id INTEGER PRIMARY KEY);
CREATE TABLE season_names (id INTEGER PRIMARY KEY);
CREATE TABLE round_names (id INTEGER PRIMARY KEY);
"""


def _matches_ddl(extra_cols: str) -> str:
    """v12–v14 旧 matches 结构：window_seq 键 + 财务列；extra_cols 为 v13/v14 增列。"""
    return f"""
    CREATE TABLE matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        season_number INTEGER NOT NULL,
        window_seq INTEGER NOT NULL,
        round_no INTEGER NOT NULL,
        competition TEXT NOT NULL DEFAULT '联赛',
        home_team TEXT NOT NULL,
        away_team TEXT NOT NULL,
        weather TEXT,
        {extra_cols}
        result TEXT,
        score TEXT,
        week_no INTEGER,
        day_no INTEGER,
        match_time TEXT,
        attendance INTEGER,
        ticket_revenue REAL,
        commercial REAL,
        broadcast REAL,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        UNIQUE(season_number, window_seq, round_no, competition, home_team, away_team)
    )
    """


_LEGACY_SEED = """
INSERT INTO plugin_config (key, value) VALUES ('schema_version', '{version}');
INSERT INTO league_state (id, season_number, window_seq, current_round, updated_by)
VALUES (1, 2, 1, 0, 'legacy');
INSERT INTO stadium (team_name, name, capacity, tier, influence, fans_diehards)
VALUES ('利物浦', '安菲尔德', 54000, 1, 95.0, 2000.0),
       ('巴塞罗那', '诺坎普', 99000, 1, 93.0, 2200.0);
INSERT INTO admins (qq, added_by) VALUES ('10001', 'owner');
INSERT INTO club_balance (id) VALUES (1);
INSERT INTO round_names (id) VALUES (1);
"""

# 5 行赛程样本：(season, window, round, comp, home, away, weather, wx, result,
#               score, week, day, time, att, ticket, commercial, broadcast, created)
_LEGACY_MATCH_ROWS = [
    (2, 1, 1, "联赛", "利物浦", "巴塞罗那", "晴", 1.05, "W", "2:1",
     1, 2, "2026-09-05 19:30", 12000, 100.0, 5.0, 3.0, "2026-08-01 10:00:00"),
    (2, 1, 2, "联赛", "利物浦", "勒沃库森", "雨", 0.92, "D", "0:0",
     2, 3, "2026-09-12 20:00", 9000, 80.0, 4.0, 2.0, "2026-08-01 10:00:00"),
    (2, 2, 3, "次级联赛", "纽卡斯尔联", "阿贾克斯", None, None, "W",
     "2:1（点球 4:2）", 3, 1, None, None, None, None, None, "2026-08-01 10:00:00"),
    (2, 2, 1, "冠军杯", "利物浦", "皇马", "多云", 0.98, "C", "弃权",
     1, 5, "2026-09-15 21:00", 0, 0.0, 0.0, 0.0, "2026-08-01 10:00:00"),
    (2, 3, 4, "联赛", "巴塞罗那", "尤文图斯", None, None, None, None,
     None, None, None, None, None, None, None, "2026-08-01 10:00:00"),
]


async def _make_legacy_db(path: str, version: int) -> None:
    """构造 v12/v13/v14 结构合成库（含赛程样本与 16 张待删表）。"""
    import aiosqlite

    if version >= 14:
        extra = (
            "wx_coef REAL,\n        tour_match_id INTEGER,\n        "
            "kickoff_source TEXT,\n        "
        )
    elif version >= 13:
        extra = "wx_coef REAL,\n        "
    else:
        extra = ""
    conn = await aiosqlite.connect(path)
    try:
        await conn.executescript(_LEGACY_TABLES_DDL)
        await conn.execute(_matches_ddl(extra))
        await conn.executescript(_LEGACY_SEED.format(version=version))
        for i, r in enumerate(_LEGACY_MATCH_ROWS):
            (season, window, round_no, comp, home, away, weather, wx, result,
             score, week, day, mtime, att, ticket, commercial, broadcast, created) = r
            wx_val = wx if version >= 13 else None
            if version >= 14:
                tour_ids = [501, 501, 502, None, None]
                sources = ["tour", "tour", "tour", None, None]
                await conn.execute(
                    "INSERT INTO matches (season_number, window_seq, round_no, competition, "
                    "home_team, away_team, weather, wx_coef, result, score, week_no, day_no, "
                    "match_time, attendance, ticket_revenue, commercial, broadcast, "
                    "tour_match_id, kickoff_source, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (season, window, round_no, comp, home, away, weather, wx_val, result,
                     score, week, day, mtime, att, ticket, commercial, broadcast,
                     tour_ids[i], sources[i], created),
                )
            elif version >= 13:
                await conn.execute(
                    "INSERT INTO matches (season_number, window_seq, round_no, competition, "
                    "home_team, away_team, weather, wx_coef, result, score, week_no, day_no, "
                    "match_time, attendance, ticket_revenue, commercial, broadcast, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (season, window, round_no, comp, home, away, weather, wx_val, result,
                     score, week, day, mtime, att, ticket, commercial, broadcast, created),
                )
            else:
                await conn.execute(
                    "INSERT INTO matches (season_number, window_seq, round_no, competition, "
                    "home_team, away_team, weather, result, score, week_no, day_no, "
                    "match_time, attendance, ticket_revenue, commercial, broadcast, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (season, window, round_no, comp, home, away, weather, result, score,
                     week, day, mtime, att, ticket, commercial, broadcast, created),
                )
        await conn.commit()
    finally:
        await conn.close()


async def _table_set(db: DatabaseManager) -> set:
    rows = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return {r["name"] for r in rows}


async def _index_names(db: DatabaseManager) -> set:
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='index'")
    return {r["name"] for r in rows}


async def _version_of(db: DatabaseManager):
    row = await db.fetchone("SELECT value FROM plugin_config WHERE key='schema_version'")
    return None if row is None else row["value"]


async def _make_bare_version_db(path: str, version_value: str) -> None:
    import aiosqlite

    conn = await aiosqlite.connect(path)
    try:
        await conn.execute(
            "CREATE TABLE plugin_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO plugin_config (key, value) VALUES ('schema_version', ?)",
            (version_value,),
        )
        await conn.commit()
    finally:
        await conn.close()


async def _expect_runtime_error(db: DatabaseManager, keyword: str) -> str:
    try:
        await init_schema(db)
        raise AssertionError(f"应报错（含 {keyword!r}）")
    except RuntimeError as e:
        assert keyword in str(e), (keyword, str(e))
        return str(e)


async def test_fresh_db_five_tables():
    tmp = tempfile.TemporaryDirectory()
    try:
        db = DatabaseManager(str(Path(tmp.name) / "fresh.db"))
        await db.init()
        try:
            await init_schema(db)
            assert await _table_set(db) == CORE_TABLES
            assert await _version_of(db) == str(SCHEMA_VERSION) == "15"
            cols = await _table_columns(db.conn, "matches")
            assert cols == MATCHES_COLUMNS, cols ^ MATCHES_COLUMNS
            assert not (cols & DROPPED_MATCH_COLUMNS)
            # 索引：partial 唯一 + 轮次 + 主队
            idx = await _index_names(db)
            assert {"uq_matches_tour", "idx_matches_round", "idx_matches_home"} <= idx, idx
            # stadium 三列
            assert await _table_columns(db.conn, "stadium") == {
                "team_name", "name", "capacity",
            }
            # league_state 种子
            row = await db.fetchone("SELECT * FROM league_state WHERE id=1")
            assert row["season_number"] == 1 and row["season_label"] == "S1"
            # plugin_config 无 updated_at 列
            assert await _table_columns(db.conn, "plugin_config") == {"key", "value"}
            # league_state 无 window_seq 列
            assert await _table_columns(db.conn, "league_state") == {
                "id", "season_number", "season_label", "updated_at",
            }
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_fresh_db_idempotent():
    tmp = tempfile.TemporaryDirectory()
    try:
        db = DatabaseManager(str(Path(tmp.name) / "fresh2.db"))
        await db.init()
        try:
            await init_schema(db)
            await init_schema(db)  # 连跑两遍
            assert await _table_set(db) == CORE_TABLES
            assert await _version_of(db) == "15"
            rows = await db.fetchall("SELECT * FROM league_state")
            assert len(rows) == 1 and rows[0]["season_label"] == "S1"
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_v13_db_migration_to_v15():
    tmp = tempfile.TemporaryDirectory()
    try:
        path = str(Path(tmp.name) / "v13.db")
        await _make_legacy_db(path, 13)
        db = DatabaseManager(path)
        await db.init()
        try:
            await init_schema(db)
            assert await _version_of(db) == "15"
            assert await _table_set(db) == CORE_TABLES
            assert not (await _table_set(db)) & DROPPED_TABLES
            cols = await _table_columns(db.conn, "matches")
            assert not (cols & DROPPED_MATCH_COLUMNS), cols & DROPPED_MATCH_COLUMNS
            # 行数不变
            rows = await db.fetchall("SELECT * FROM matches ORDER BY id")
            assert len(rows) == 5
            # season 2→9、leg/stage_name、walkover
            assert all(r["season_number"] == 9 for r in rows)
            assert all(r["leg"] == 0 and r["stage_name"] == "" for r in rows)
            assert all(r["walkover_side"] is None for r in rows)
            # competition→tournament_id；旧文本「联赛」归一为「顶级联赛」
            # （v3 读路径/club 映射统一「顶级联赛」，不归一则迁移行全查空）
            by_key = {
                (r["competition"], r["home_team"], r["away_team"]): r for r in rows
            }
            assert by_key[("顶级联赛", "利物浦", "巴塞罗那")]["tournament_id"] == 1
            assert by_key[("顶级联赛", "利物浦", "勒沃库森")]["tournament_id"] == 1
            assert by_key[("次级联赛", "纽卡斯尔联", "阿贾克斯")]["tournament_id"] == 2
            assert by_key[("冠军杯", "利物浦", "皇马")]["tournament_id"] == 3
            assert by_key[("顶级联赛", "巴塞罗那", "尤文图斯")]["tournament_id"] == 1
            assert not any(r["competition"] == "联赛" for r in rows), "旧文本「联赛」不得残留"
            # score 解析：普通/点球/弃权/空
            r1 = by_key[("顶级联赛", "利物浦", "巴塞罗那")]
            assert (r1["home_score"], r1["away_score"], r1["pen_home"], r1["pen_away"]) == (2, 1, None, None)
            r3 = by_key[("次级联赛", "纽卡斯尔联", "阿贾克斯")]
            assert (r3["home_score"], r3["away_score"], r3["pen_home"], r3["pen_away"]) == (2, 1, 4, 2)
            r4 = by_key[("冠军杯", "利物浦", "皇马")]
            assert r4["home_score"] is None and r4["away_score"] is None
            assert r4["result"] == "C" and r4["score"] == "弃权" and r4["tour_status"] == "finished"
            r5 = by_key[("顶级联赛", "巴塞罗那", "尤文图斯")]
            assert r5["tour_status"] == "pending" and r5["result"] is None
            assert r1["tour_status"] == "finished" and r1["result"] == "W"
            # weather/wx_coef/时刻/created_at 原样保留
            assert r1["weather"] == "晴" and r1["wx_coef"] == 1.05
            assert r1["week_no"] == 1 and r1["day_no"] == 2
            assert r1["match_time"] == "2026-09-05 19:30"
            assert r1["created_at"] == "2026-08-01 10:00:00"
            assert r3["wx_coef"] is None and r3["weather"] is None
            # stadium 只留三列且数据保留
            st = await db.fetchall("SELECT * FROM stadium ORDER BY team_name")
            assert [s["team_name"] for s in st] == ["利物浦", "巴塞罗那"]
            assert st[0]["name"] == "安菲尔德" and st[0]["capacity"] == 54000
            assert st[1]["name"] == "诺坎普" and st[1]["capacity"] == 99000
            # league_state 重建为 S9
            ls = await db.fetchone("SELECT * FROM league_state WHERE id=1")
            assert ls["season_number"] == 9 and ls["season_label"] == "S9"
            # admins 保留
            admins_row = await db.fetchone("SELECT COUNT(*) AS n FROM admins")
            assert admins_row["n"] == 1
            # partial 唯一索引存在
            assert "uq_matches_tour" in await _index_names(db)
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_v12_db_migration_to_v15():
    """v12 库（matches 无 wx_coef/tour_match_id/kickoff_source）迁移兼容缺列。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        path = str(Path(tmp.name) / "v12.db")
        await _make_legacy_db(path, 12)
        db = DatabaseManager(path)
        await db.init()
        try:
            await init_schema(db)
            assert await _version_of(db) == "15"
            assert await _table_set(db) == CORE_TABLES
            rows = await db.fetchall("SELECT * FROM matches")
            assert len(rows) == 5
            assert all(r["wx_coef"] is None and r["tour_match_id"] is None for r in rows)
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_v14_db_migration_dedup_tour_match_id():
    """v14 库：重复 tour_match_id 保留最小 id，其余置 NULL；kickoff_source 丢弃。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        path = str(Path(tmp.name) / "v14.db")
        await _make_legacy_db(path, 14)
        db = DatabaseManager(path)
        await db.init()
        try:
            await init_schema(db)
            assert await _version_of(db) == "15"
            rows = await db.fetchall("SELECT * FROM matches ORDER BY id")
            assert len(rows) == 5
            assert rows[0]["tour_match_id"] == 501, "最小 id 保留绑定"
            assert rows[1]["tour_match_id"] is None, "重复行置 NULL"
            assert rows[2]["tour_match_id"] == 502
            assert rows[3]["tour_match_id"] is None and rows[4]["tour_match_id"] is None
            # 幂等：迁移后再跑 init_schema 不报错、版本不回退
            await init_schema(db)
            assert await _version_of(db) == "15"
            assert await _table_set(db) == CORE_TABLES
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_migration_below_12_raises():
    tmp = tempfile.TemporaryDirectory()
    try:
        path = str(Path(tmp.name) / "v11.db")
        await _make_bare_version_db(path, "11")
        db = DatabaseManager(path)
        await db.init()
        try:
            await _expect_runtime_error(db, "2.14")
            assert await _version_of(db) == "11", "报错后版本不得被改写"
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_migration_above_15_raises():
    tmp = tempfile.TemporaryDirectory()
    try:
        path = str(Path(tmp.name) / "v16.db")
        await _make_bare_version_db(path, "16")
        db = DatabaseManager(path)
        await db.init()
        try:
            await _expect_runtime_error(db, "16")
            assert await _version_of(db) == "16"
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_migration_invalid_version_raises():
    tmp = tempfile.TemporaryDirectory()
    try:
        path = str(Path(tmp.name) / "vbad.db")
        await _make_bare_version_db(path, "not-a-number")
        db = DatabaseManager(path)
        await db.init()
        try:
            await _expect_runtime_error(db, "not-a-number")
            assert await _version_of(db) == "not-a-number", "非法值不得被静默改写"
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_migration_missing_version_raises():
    import aiosqlite

    tmp = tempfile.TemporaryDirectory()
    try:
        path = str(Path(tmp.name) / "nover.db")
        conn = await aiosqlite.connect(path)
        try:
            await conn.execute(
                "CREATE TABLE plugin_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            await conn.commit()
        finally:
            await conn.close()
        db = DatabaseManager(path)
        await db.init()
        try:
            await _expect_runtime_error(db, "schema_version")
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_migration_unsupported_season_rollback():
    """season_number=3 不支持：报错列出待人工确认，且回滚（版本/数据不变）。"""
    import aiosqlite

    tmp = tempfile.TemporaryDirectory()
    try:
        path = str(Path(tmp.name) / "v13_s3.db")
        await _make_legacy_db(path, 13)
        conn = await aiosqlite.connect(path)
        try:
            await conn.execute("UPDATE matches SET season_number=3 WHERE id=1")
            await conn.commit()
        finally:
            await conn.close()
        db = DatabaseManager(path)
        await db.init()
        try:
            await _expect_runtime_error(db, "season_number")
            # 回滚：版本仍 13、16 张旧表仍在、matches 数据完好
            assert await _version_of(db) == "13"
            tables = await _table_set(db)
            assert DROPPED_TABLES <= tables, DROPPED_TABLES - tables
            rows = await db.fetchall("SELECT * FROM matches")
            assert len(rows) == 5
            cols = await _table_columns(db.conn, "matches")
            assert "window_seq" in cols, "回滚后旧列应完好"
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_migration_unknown_competition_rollback():
    """未知 competition 文本：报错待人工确认，且回滚。"""
    import aiosqlite

    tmp = tempfile.TemporaryDirectory()
    try:
        path = str(Path(tmp.name) / "v13_badcomp.db")
        await _make_legacy_db(path, 13)
        conn = await aiosqlite.connect(path)
        try:
            await conn.execute("UPDATE matches SET competition='友谊赛' WHERE id=5")
            await conn.commit()
        finally:
            await conn.close()
        db = DatabaseManager(path)
        await db.init()
        try:
            await _expect_runtime_error(db, "友谊赛")
            assert await _version_of(db) == "13"
            assert DROPPED_TABLES <= await _table_set(db)
            rows = await db.fetchall("SELECT * FROM matches WHERE id=5")
            assert rows[0]["competition"] == "友谊赛"
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_dao_config_kv():
    tmp = tempfile.TemporaryDirectory()
    try:
        db = DatabaseManager(str(Path(tmp.name) / "cfg.db"))
        dao = StadiumDAO(db)
        await db.init()
        try:
            await init_schema(db)
            assert await dao.get_config("nope") is None
            await dao.set_config("k", "v")
            assert await dao.get_config("k") == "v"
            await dao.set_config("k", "v2")
            assert await dao.get_config("k") == "v2"
            all_cfg = {r["key"]: r["value"] for r in await dao.get_all_config()}
            assert all_cfg["k"] == "v2"
            assert all_cfg["schema_version"] == "15"
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_dao_league_state():
    tmp = tempfile.TemporaryDirectory()
    try:
        db = DatabaseManager(str(Path(tmp.name) / "ls.db"))
        dao = StadiumDAO(db)
        await db.init()
        try:
            await init_schema(db)
            row = await dao.get_league_state()
            assert row["season_number"] == 1 and row["season_label"] == "S1"
            await dao.set_league_state(9, "S9")
            row = await dao.get_league_state()
            assert row["season_number"] == 9 and row["season_label"] == "S9"
            assert row["updated_at"] is not None
            await dao.set_league_state(10, "S10")
            rows = await db.fetchall("SELECT * FROM league_state")
            assert len(rows) == 1 and rows[0]["season_number"] == 10
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_dao_match_upsert():
    tmp = tempfile.TemporaryDirectory()
    try:
        db = DatabaseManager(str(Path(tmp.name) / "up.db"))
        dao = StadiumDAO(db)
        await db.init()
        try:
            await init_schema(db)
            # 唯一键插入
            mid = await dao.upsert_match(
                9, 1, "顶级联赛", 12, "利物浦", "巴塞罗那", leg=1,
                stage_name="league_robin", tour_status="pending",
            )
            assert isinstance(mid, int) and mid > 0
            row = await db.fetchone("SELECT * FROM matches WHERE id=?", (mid,))
            assert row["leg"] == 1 and row["stage_name"] == "league_robin"
            # 预置天气，验证 upsert 不触碰
            await db.execute(
                "UPDATE matches SET weather='晴', wx_coef=1.05 WHERE id=?", (mid,)
            )
            # 同唯一键 upsert：覆盖比分/点球/tour_status/walkover/时刻/tour_match_id
            mid2 = await dao.upsert_match(
                9, 1, "顶级联赛", 12, "利物浦", "巴塞罗那", leg=1,
                home_score=2, away_score=1, pen_home=4, pen_away=2,
                result="W", score="2:1（点球 4:2）", tour_status="finished",
                walkover_side=None, week_no=7, day_no=6,
                match_time="2026-11-20 20:00", tour_match_id=901,
                synced_at="2026-11-15 09:00:00",
            )
            assert mid2 == mid
            rows = await db.fetchall("SELECT * FROM matches")
            assert len(rows) == 1
            row = rows[0]
            assert (row["home_score"], row["away_score"]) == (2, 1)
            assert (row["pen_home"], row["pen_away"]) == (4, 2)
            assert row["result"] == "W" and row["tour_status"] == "finished"
            assert row["week_no"] == 7 and row["day_no"] == 6
            assert row["tour_match_id"] == 901 and row["synced_at"] == "2026-11-15 09:00:00"
            assert row["weather"] == "晴" and row["wx_coef"] == 1.05, "天气不得被同步覆盖"
            # tour_match_id 优先匹配：唯一键外的轮次变化仍命中同一行
            mid3 = await dao.upsert_match(
                9, 1, "顶级联赛", 13, "利物浦", "巴塞罗那", leg=1,
                home_score=3, away_score=0, result="W", score="3:0",
                tour_match_id=901,
            )
            assert mid3 == mid
            row = await db.fetchone("SELECT * FROM matches WHERE id=?", (mid,))
            assert row["round_no"] == 13, "tour_match_id 命中后覆盖轮次"
            assert (row["home_score"], row["away_score"]) == (3, 0)
            # 唯一索引：直接插重复 tour_match_id 被拒
            try:
                await db.execute(
                    "INSERT INTO matches (season_number, tournament_id, competition, round_no, "
                    "leg, home_team, away_team, tour_match_id) "
                    "VALUES (9, 1, '顶级联赛', 14, 0, 'X', 'Y', 901)"
                )
                assert False, "重复 tour_match_id 应违反 partial 唯一索引"
            except Exception as e:
                assert "UNIQUE" in str(e), str(e)
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_dao_round_matches_no_window():
    tmp = tempfile.TemporaryDirectory()
    try:
        db = DatabaseManager(str(Path(tmp.name) / "round.db"))
        dao = StadiumDAO(db)
        await db.init()
        try:
            await init_schema(db)
            await dao.upsert_match(9, 1, "顶级联赛", 12, "A", "B")
            await dao.upsert_match(9, 1, "顶级联赛", 12, "C", "D")
            await dao.upsert_match(9, 2, "次级联赛", 12, "E", "F")
            await dao.upsert_match(9, 1, "顶级联赛", 13, "G", "H")
            # 新签名：无 window_seq 参数
            rows = await dao.get_round_matches(9, 12)
            assert len(rows) == 3 and {r["tournament_id"] for r in rows} == {1, 2}
            rows = await dao.get_round_matches(9, 12, "顶级联赛")
            assert len(rows) == 2 and all(r["tournament_id"] == 1 for r in rows)
            rows = await dao.get_round_matches(9, 13)
            assert len(rows) == 1 and rows[0]["home_team"] == "G"
            rows = await dao.get_round_matches(9, 99)
            assert rows == []
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_dao_weather_round_key():
    tmp = tempfile.TemporaryDirectory()
    try:
        db = DatabaseManager(str(Path(tmp.name) / "wx.db"))
        dao = StadiumDAO(db)
        await db.init()
        try:
            await init_schema(db)
            await dao.upsert_match(9, 1, "顶级联赛", 12, "A", "B")
            await dao.upsert_match(9, 1, "顶级联赛", 12, "C", "D", leg=2)
            await dao.upsert_match(9, 1, "顶级联赛", 13, "E", "F")
            # 轮级预报：轮内两行全有天气 + 系数固化
            n = await dao.set_match_weather(9, 1, 12, "晴", 1.05)
            assert n == 2
            # 轮外行不受影响
            row = await db.fetchone("SELECT * FROM matches WHERE home_team='E'")
            assert row["weather"] is None
            # fill_wx_coef：已固化 → 幂等 0
            assert await dao.fill_wx_coef(9, 1, 12, 1.10) == 0
            rows = await db.fetchall("SELECT wx_coef FROM matches WHERE round_no=12")
            assert all(r["wx_coef"] == 1.05 for r in rows)
            # 不带系数覆盖：系数保留
            await dao.set_match_weather(9, 1, 12, "雨")
            rows = await db.fetchall(
                "SELECT weather, wx_coef FROM matches WHERE round_no=12"
            )
            assert all(r["weather"] == "雨" and r["wx_coef"] == 1.05 for r in rows)
            # 空 wx 行补抽（只固化系数，不算已预报天气）
            assert await dao.fill_wx_coef(9, 1, 13, 0.99) == 1
            assert await dao.fill_wx_coef(9, 1, 13, 0.98) == 0, "第二次幂等"
            # 已预报清单：仅 round 12 的两行有天气
            lst = await dao.list_weathered_matches()
            assert len(lst) == 2 and all(m["round_no"] == 12 for m in lst)
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_parse_score_text_unit():
    assert parse_score_text("2:1") == (2, 1, None, None)
    assert parse_score_text("2:1（点球 4:2）") == (2, 1, 4, 2)
    assert parse_score_text("2:1 (点球 4:2)") == (2, 1, 4, 2)
    assert parse_score_text("0:0") == (0, 0, None, None)
    assert parse_score_text("弃权") == (None, None, None, None)
    assert parse_score_text(None) == (None, None, None, None)
    assert parse_score_text("乱串") == (None, None, None, None)


if __name__ == "__main__":
    sys.exit(run_module("tests.s0_schema"))
