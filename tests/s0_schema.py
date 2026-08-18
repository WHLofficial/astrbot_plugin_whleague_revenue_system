"""数据库结构：v4→v6 / v5→v6 迁移（事件列 + 选择表 + 命名轮次表）与幂等。"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.db.connection import DatabaseManager  # noqa: E402
from astrbot_plugin_whleague_revenue_system.db.schema import _table_columns, init_schema  # noqa: E402


async def _make_v4_db(path: str) -> None:
    """构造一个真实的 v4 结构数据库（event_pool 无新列、无 event_choices）。"""
    import aiosqlite

    conn = await aiosqlite.connect(path)
    try:
        await conn.executescript(
            """
            CREATE TABLE event_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '通用',
                weight INTEGER NOT NULL DEFAULT 10,
                conditions_json TEXT NOT NULL DEFAULT '{}',
                effects_json TEXT NOT NULL DEFAULT '{}',
                template TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'builtin',
                status TEXT NOT NULL DEFAULT 'adopted',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE plugin_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            INSERT INTO plugin_config (key, value) VALUES ('schema_version', '4');
            INSERT INTO event_pool (event_id, name, category, weight, conditions_json, effects_json, template, source, status)
            VALUES ('v4_legacy', '旧版即发事件', '通用', 5, '{}', '{"money":1}', 't', 'builtin', 'adopted');
            """
        )
        await conn.commit()
    finally:
        await conn.close()


async def _make_v5_db(path: str) -> None:
    """构造一个真实的 v5 结构数据库（事件列/选择表齐全、无命名轮次表）。"""
    import aiosqlite

    conn = await aiosqlite.connect(path)
    try:
        await conn.executescript(
            """
            CREATE TABLE event_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '通用',
                weight INTEGER NOT NULL DEFAULT 10,
                conditions_json TEXT NOT NULL DEFAULT '{}',
                effects_json TEXT NOT NULL DEFAULT '{}',
                options_json TEXT NOT NULL DEFAULT '{}',
                event_type TEXT NOT NULL DEFAULT 'instant',
                template TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'builtin',
                status TEXT NOT NULL DEFAULT 'adopted',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE event_choices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name TEXT NOT NULL,
                season_number INTEGER NOT NULL,
                window_seq INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                choice_no INTEGER,
                resolved INTEGER NOT NULL DEFAULT 0,
                outcome TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(team_name, season_number, window_seq, event_id)
            );
            CREATE TABLE plugin_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            INSERT INTO plugin_config (key, value) VALUES ('schema_version', '5');
            """
        )
        await conn.commit()
    finally:
        await conn.close()


async def test_v4_to_latest_migration():
    tmp = tempfile.TemporaryDirectory()
    try:
        path = str(Path(tmp.name) / "old.db")
        await _make_v4_db(path)
        db = DatabaseManager(path)
        await db.init()
        try:
            await init_schema(db)
            row = await db.fetchone(
                "SELECT value FROM plugin_config WHERE key='schema_version'"
            )
            assert row["value"] == "7"
            # 事件池新增列
            cols = await _table_columns(db.conn, "event_pool")
            assert {"event_type", "options_json"} <= cols, cols
            # 旧行保留且缺省为即发型
            ev = await db.fetchone("SELECT * FROM event_pool WHERE event_id='v4_legacy'")
            assert ev["event_type"] == "instant"
            assert ev["options_json"] == "{}"
            # 选择表存在且结构完整
            cc = await _table_columns(db.conn, "event_choices")
            assert {"team_name", "season_number", "window_seq",
                    "event_id", "choice_no", "resolved", "outcome"} <= cc, cc
            # 命名轮次登记表存在
            rn = await _table_columns(db.conn, "round_names")
            assert {"season_number", "token", "round_no"} <= rn, rn
            # 重复初始化幂等（不重复加列/重建）
            await init_schema(db)
            row2 = await db.fetchone(
                "SELECT value FROM plugin_config WHERE key='schema_version'"
            )
            assert row2["value"] == "7"
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_v5_to_latest_migration():
    """v5 库迁移到当前版本：版本号更新、round_names 表可用。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        path = str(Path(tmp.name) / "v5.db")
        await _make_v5_db(path)
        db = DatabaseManager(path)
        await db.init()
        try:
            await init_schema(db)
            row = await db.fetchone(
                "SELECT value FROM plugin_config WHERE key='schema_version'"
            )
            assert row["value"] == "7"
            cols = await _table_columns(db.conn, "round_names")
            assert {"season_number", "token", "round_no"} <= cols, cols
            # 登记 API 可用：同名同号、递增分配
            n1 = await db.fetchone(
                "INSERT INTO round_names (season_number, token, round_no) VALUES (1, '顶级', 1) "
                "ON CONFLICT(season_number, token) DO NOTHING RETURNING round_no"
            )
            assert n1 is not None and n1["round_no"] == 1
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def _make_v6_db(path: str) -> None:
    """构造 v6 结构数据库：matches 唯一键为同轮次同赛事同主队（无 away_team）。"""
    import aiosqlite

    conn = await aiosqlite.connect(path)
    try:
        await conn.executescript(
            """
            CREATE TABLE matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_number INTEGER NOT NULL,
                window_seq INTEGER NOT NULL,
                round_no INTEGER NOT NULL,
                competition TEXT NOT NULL DEFAULT '联赛',
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                weather TEXT,
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
                UNIQUE(season_number, window_seq, round_no, competition, home_team)
            );
            CREATE TABLE round_names (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_number INTEGER NOT NULL,
                token TEXT NOT NULL,
                round_no INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(season_number, token)
            );
            CREATE TABLE plugin_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            INSERT INTO plugin_config (key, value) VALUES ('schema_version', '6');
            INSERT INTO matches (season_number, window_seq, round_no, competition, home_team, away_team)
            VALUES (1, 1, 1, '顶级联赛', '利物浦', '巴塞罗那');
            """
        )
        await conn.commit()
    finally:
        await conn.close()


async def test_v6_to_latest_migration():
    """v6 库迁移到 v7：matches 唯一键收窄为完整对阵（home+away）。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        path = str(Path(tmp.name) / "v6.db")
        await _make_v6_db(path)
        db = DatabaseManager(path)
        await db.init()
        try:
            await init_schema(db)
            row = await db.fetchone(
                "SELECT value FROM plugin_config WHERE key='schema_version'"
            )
            assert row["value"] == "7"
            # 迁移后旧数据保留
            rows = await db.fetchall("SELECT * FROM matches")
            assert rows and rows[0]["home_team"] == "利物浦"
            # 语义验证：同轮同赛事同主队、不同客队可并存；完全相同的行仍判重
            async def _ins(home, away) -> int:
                cur = await db.execute(
                    "INSERT OR IGNORE INTO matches (season_number, window_seq, round_no, competition, home_team, away_team) "
                    "VALUES (1, 1, 1, '顶级联赛', ?, ?)",
                    (home, away),
                )
                try:
                    return cur.rowcount or 0
                finally:
                    await cur.close()

            assert await _ins("利物浦", "勒沃库森") == 1
            assert await _ins("利物浦", "巴塞罗那") == 0  # 与旧行完全相同 → 重复跳过
        finally:
            await db.close()
    finally:
        tmp.cleanup()


async def test_fresh_db_has_choice_schema():
    env = await TestEnv().setup()
    try:
        cols = await _table_columns(env.db.conn, "event_pool")
        assert {"event_type", "options_json"} <= cols, cols
        cc = await _table_columns(env.db.conn, "event_choices")
        assert "choice_no" in cc
        # 默认事件池：22 条内置（提交 2 起拆分为 6 即发 + 16 选择）
        rows = await env.dao.list_events("adopted")
        builtin = [r for r in rows if r["source"] == "builtin"]
        assert len(builtin) == 22
        # choice 决策表 API 可用
        await env.dao.add_event_choice("利物浦", 1, 1, "storm_buzz")
        await env.dao.add_event_choice("利物浦", 1, 1, "storm_buzz")  # 幂等
        await env.dao.set_event_choice("利物浦", 1, 1, "storm_buzz", 2)
        c = await env.dao.get_event_choice("利物浦", 1, 1, "storm_buzz")
        assert c["choice_no"] == 2 and c["resolved"] == 0
        pending = await env.dao.get_unresolved_choices(1, 1)
        assert len(pending) == 1
        await env.dao.mark_choice_resolved(c["id"], '{"no":2}')
        pending2 = await env.dao.get_unresolved_choices(1, 1)
        assert len(pending2) == 0
        await env.dao.reset_choices_for_redo(1, 1)
        pending3 = await env.dao.get_unresolved_choices(1, 1)
        assert len(pending3) == 1 and pending3[0]["choice_no"] == 2
        # 命名轮次登记：同名同号、不同名递增、跨赛季分离
        a1 = await env.dao.add_named_round(1, "顶级")
        a2 = await env.dao.add_named_round(1, "顶级")
        b1 = await env.dao.add_named_round(1, "次级")
        assert a1 == a2 == 1 and b1 == 2, (a1, a2, b1)
        assert await env.dao.get_named_round(1, "顶级") == 1
        assert await env.dao.get_named_round(2, "顶级") is None
    finally:
        await env.teardown()
