"""数据库结构：v4/v5/v6/v9 旧库迁移到最新（事件列 + 选择表 + 命名轮次表 + 赛季命名表 + 冠名权市场）与幂等。"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402
from astrbot_plugin_whleague_revenue_system.db.connection import DatabaseManager  # noqa: E402
from astrbot_plugin_whleague_revenue_system.db.schema import SCHEMA_VERSION, _table_columns, init_schema  # noqa: E402


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
            assert row["value"] == str(SCHEMA_VERSION)
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
            # 赛季命名表存在（v9）
            sn = await _table_columns(db.conn, "season_names")
            assert {"season_number", "name", "updated_by"} <= sn, sn
            # 重复初始化幂等（不重复加列/重建）
            await init_schema(db)
            row2 = await db.fetchone(
                "SELECT value FROM plugin_config WHERE key='schema_version'"
            )
            assert row2["value"] == str(SCHEMA_VERSION)
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
            assert row["value"] == str(SCHEMA_VERSION)
            cols = await _table_columns(db.conn, "round_names")
            assert {"season_number", "competition", "token", "round_no"} <= cols, cols
            sn = await _table_columns(db.conn, "season_names")
            assert {"season_number", "name"} <= sn, sn
            # 登记 API 可用：同名同号、递增分配
            n1 = await db.fetchone(
                "INSERT INTO round_names (season_number, competition, token, round_no) "
                "VALUES (1, '顶级联赛', '顶级', 1) "
                "ON CONFLICT(season_number, competition, token) DO NOTHING RETURNING round_no"
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
            assert row["value"] == str(SCHEMA_VERSION)
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


async def test_matches_rebuild_rollback_on_failure():
    """matches 重建失败时回滚：不重建空表、旧数据完好，版本不推进（下次可重试）。"""
    import aiosqlite

    tmp = tempfile.TemporaryDirectory()
    try:
        path = str(Path(tmp.name) / "v6_rollback.db")
        await _make_v6_db(path)
        conn = await aiosqlite.connect(path)
        try:
            # 预置同名旧表，让 v7 重建的 RENAME 冲突，触发失败路径
            await conn.execute("CREATE TABLE matches_old (id INTEGER PRIMARY KEY)")
            await conn.commit()
        finally:
            await conn.close()
        db = DatabaseManager(path)
        await db.init()
        try:
            try:
                await init_schema(db)
                assert False, "重建应因表名冲突失败"
            except aiosqlite.OperationalError:
                pass
            rows = await db.fetchall("SELECT * FROM matches")
            assert rows and rows[0]["home_team"] == "利物浦", "失败后旧 matches 数据应完好"
            ver = await db.fetchone(
                "SELECT value FROM plugin_config WHERE key='schema_version'"
            )
            assert ver["value"] == "6", "失败后版本号不应推进，可重试迁移"
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
        # 命名轮次登记：按（赛季,赛事）编号——同赛事同名同号、异名递增、不同赛事各从 1 起、跨赛季归一
        a1 = await env.dao.add_named_round(1, "顶级联赛", "顶级")
        a2 = await env.dao.add_named_round(1, "顶级联赛", "顶级")
        s1 = await env.dao.add_named_round(1, "次级联赛", "次级")
        d1 = await env.dao.add_named_round(1, "顶级联赛", "顶级9")
        assert a1 == a2 == 1 and s1 == 1, (a1, a2, s1)
        assert d1 == 2, d1  # 同赛事不同文本递增
        assert await env.dao.get_named_round(1, "顶级联赛", "顶级") == 1
        assert await env.dao.get_named_round(1, "顶级联赛", "未导入") is None
        assert await env.dao.add_named_round(2, "顶级联赛", "顶级") == 1  # 跨赛季归一
        # 赛季命名 DAO：写入/覆写/读取 + 展示标签回退
        assert await env.dao.get_season_name(1) is None
        await env.dao.set_season_name(1, "S8 黄金联赛", "tester")
        await env.dao.set_season_name(1, "S8 黄金联赛·修订", "tester")  # 覆写
        assert await env.dao.get_season_name(1) == "S8 黄金联赛·修订"
        assert await env.dao.season_label(1) == "S8 黄金联赛·修订"
        assert await env.dao.season_label(3) == "第 3 赛季"  # 未命名回退
    finally:
        await env.teardown()


async def _make_v9_db(path: str) -> None:
    """构造一个真实的 v9 结构数据库（naming_rights/brand_pool 无市场列、无 market 表）。"""
    import aiosqlite

    conn = await aiosqlite.connect(path)
    try:
        await conn.executescript(
            """
            CREATE TABLE plugin_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE naming_rights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name TEXT NOT NULL UNIQUE,
                brand TEXT NOT NULL,
                heat REAL NOT NULL,
                fee_per_window REAL NOT NULL,
                windows_total INTEGER NOT NULL,
                windows_remaining INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                signed_season INTEGER NOT NULL,
                signed_window INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE brand_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand TEXT NOT NULL,
                heat REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'builtin',
                status TEXT NOT NULL DEFAULT 'adopted',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            INSERT INTO plugin_config (key, value) VALUES ('schema_version', '9');
            INSERT INTO naming_rights (team_name, brand, heat, fee_per_window, windows_total, windows_remaining, status, signed_season, signed_window)
            VALUES ('利物浦', '麒麟生物', 1.2, 5.0, 4, 2, 'active', 1, 3);
            INSERT INTO brand_pool (brand, heat, source, status) VALUES
                ('麒麟生物', 1.2, 'builtin', 'adopted'),
                ('阿迪达斯', 1.1, 'builtin', 'adopted'),
                ('星际咖啡', 0.9, 'llm', 'adopted');
            """
        )
        await conn.commit()
    finally:
        await conn.close()


async def test_migrate_v9_to_v10_market_schema():
    """v9 库迁移：市场列补齐、builtin 行业映射、market 表就绪、在途合同 legacy 快照、幂等。"""
    import aiosqlite

    tmp = tempfile.TemporaryDirectory()
    db_path = str(Path(tmp.name) / "v9.db")
    await _make_v9_db(db_path)
    db = DatabaseManager(db_path)
    try:
        await db.init()
        await init_schema(db)
        ver = await db.fetchone(
            "SELECT value FROM plugin_config WHERE key='schema_version'"
        )
        assert ver["value"] == str(SCHEMA_VERSION), ver
        bp = await _table_columns(db.conn, "brand_pool")
        assert {"tier", "industry", "pitch", "packages_json", "tier_locked"} <= bp, bp
        nr = await _table_columns(db.conn, "naming_rights")
        assert {"package_json", "satisfaction"} <= nr, nr
        # 在途老合同：legacy 快照 + 中性满意度
        row = await db.fetchone(
            "SELECT package_json, satisfaction FROM naming_rights WHERE team_name='利物浦'"
        )
        assert row["package_json"] == "{}" and row["satisfaction"] == 1.0, row
        # 存量品牌一律口碑（迁移不猜配额，首次招商期自动校准）；builtin 行业映射、llm 品牌保持通用
        brands = {r["brand"]: r for r in await db.fetchall("SELECT * FROM brand_pool")}
        assert all(b["tier"] == "口碑" and b["tier_locked"] == 0 for b in brands.values())
        assert brands["麒麟生物"]["industry"] == "医疗"
        assert brands["阿迪达斯"]["industry"] == "运动"
        assert brands["星际咖啡"]["industry"] == "通用", brands["星际咖啡"]
        # market 表可用；同轮同品牌同队唯一（先到先得挂价 team_name='' 同品牌仅一条）
        await db.execute(
            "INSERT INTO market_rounds (season_number, window_seq, bid_mode) VALUES (1, 1, '明标')"
        )
        await db.execute(
            "INSERT INTO market_offers (round_id, brand_id, team_name, package_no, amount, windows)"
            " VALUES (1, 1, '利物浦', 1, 5.0, 6)"
        )
        try:
            await db.execute(
                "INSERT INTO market_offers (round_id, brand_id, team_name, package_no, amount, windows)"
                " VALUES (1, 1, '利物浦', 2, 5.0, 6)"
            )
            assert False, "同轮同品牌同队重复报价应违反唯一约束"
        except aiosqlite.IntegrityError:
            pass
        # 幂等：再次 init_schema 不报错、版本不回退
        await init_schema(db)
        ver2 = await db.fetchone(
            "SELECT value FROM plugin_config WHERE key='schema_version'"
        )
        assert ver2["value"] == str(SCHEMA_VERSION)
    finally:
        await db.close()
        tmp.cleanup()


async def test_fresh_db_has_market_schema():
    """全新安装：市场列/表就绪，品牌池新列按默认值；配置键注册齐全 + market_bid_mode 白名单。"""
    env = await TestEnv().setup()
    try:
        bp = await _table_columns(env.db.conn, "brand_pool")
        assert {"tier", "industry", "pitch", "packages_json", "tier_locked"} <= bp, bp
        nr = await _table_columns(env.db.conn, "naming_rights")
        assert {"package_json", "satisfaction"} <= nr, nr
        cols = await _table_columns(env.db.conn, "market_rounds")
        assert {"season_number", "window_seq", "bid_mode", "status", "settled_at"} <= cols, cols
        cols = await _table_columns(env.db.conn, "market_offers")
        assert {
            "round_id", "brand_id", "team_name", "package_no", "amount", "windows",
            "package_json", "status", "expire_at",
        } <= cols, cols
        # 品牌池已播种 7 内置品牌，档位默认口碑
        rows = await env.dao.list_brands()
        builtin = [r for r in rows if r["source"] == "builtin"]
        assert len(builtin) == 7
        assert all(r["tier"] == "口碑" for r in builtin)
    finally:
        await env.teardown()

    from astrbot_plugin_whleague_revenue_system.config.defaults import (
        DEFAULT_CONFIG,
        validate_and_cast,
    )

    assert DEFAULT_CONFIG["market_bid_mode"] == "明标"
    assert DEFAULT_CONFIG["market_top_seat_ratio"] == 8
    assert DEFAULT_CONFIG["market_offers_per_brand"] == 3
    assert DEFAULT_CONFIG["market_emerging_slots"] == 2
    assert DEFAULT_CONFIG["market_offer_ttl_hours"] == 72
    assert DEFAULT_CONFIG["naming_pkg_stable_windows"] == 6
    assert DEFAULT_CONFIG["naming_pkg_stable_factor"] == 0.85
    assert DEFAULT_CONFIG["naming_pkg_short_windows"] == 2
    assert DEFAULT_CONFIG["naming_pkg_short_factor"] == 1.25
    assert DEFAULT_CONFIG["naming_pkg_bet_windows"] == 4
    assert DEFAULT_CONFIG["naming_pkg_bet_factor"] == 0.7
    assert DEFAULT_CONFIG["naming_pkg_bet_bonus_rate"] == 0.7
    assert DEFAULT_CONFIG["naming_pkg_bet_attend"] == 0.8
    assert DEFAULT_CONFIG["naming_pkg_bet_fans"] == 0.03
    assert DEFAULT_CONFIG["naming_bet_expected_prob"] == 0.5
    assert DEFAULT_CONFIG["market_satisfy_floor"] == 0.6
    satisfy = json.loads(DEFAULT_CONFIG["market_satisfy_config"])
    assert satisfy["attend_weight"] == 0.5 and satisfy["line_buffer"] == 0.05
    heat = json.loads(DEFAULT_CONFIG["market_heat_config"])
    assert heat["champion"] == 0.10 and heat["clamp_high"] == 1.5
    profiles = json.loads(DEFAULT_CONFIG["market_tier_profiles"])
    assert set(profiles) == {"头部", "新兴", "口碑"}
    for name, prof in profiles.items():
        assert {
            "satisfy_floor", "good_attend", "bad_attend",
            "penalty_mult", "attend_buff", "fans_buff",
        } <= set(prof), (name, prof)
    json.loads(DEFAULT_CONFIG["market_industry_templates"])

    assert validate_and_cast("market_bid_mode", " 暗标 ") == "暗标"
    assert validate_and_cast("market_bid_mode", "先到先得") == "先到先得"
    try:
        validate_and_cast("market_bid_mode", "密封递价")
        assert False, "非法招商模式应被拒绝"
    except ValueError as e:
        assert "明标" in str(e)
