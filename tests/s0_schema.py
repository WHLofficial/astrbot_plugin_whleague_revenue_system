"""数据库结构：v4→v5 迁移（事件类型/选项列 + 事件选择表）与幂等。"""

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


async def test_v4_to_v5_migration():
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
            assert row["value"] == "5"
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
            # 重复初始化幂等（不重复加列/重建）
            await init_schema(db)
            row2 = await db.fetchone(
                "SELECT value FROM plugin_config WHERE key='schema_version'"
            )
            assert row2["value"] == "5"
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
    finally:
        await env.teardown()
