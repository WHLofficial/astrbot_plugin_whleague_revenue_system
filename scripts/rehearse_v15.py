#!/usr/bin/env python3
"""v15 迁移演练：只读复制生产库 → 在副本上跑 v15 → 打印核对表。

用法（在插件仓根目录）：
    python scripts/rehearse_v15.py "D:/path/revenue_system.db"
    python scripts/rehearse_v15.py "D:/path/revenue_system.db" --expect-matches 136 --expect-season 9
    python scripts/rehearse_v15.py "D:/path/revenue_system.db" --workdir D:/tmp/rehearsal --keep

安全边界：
- 源库以 SQLite URI ``mode=ro`` 打开，只做 ``Connection.backup()`` 到副本；
- 迁移只作用于副本；脚本前后比对源库 SHA256，不写源库；
- 默认结束后删除副本；失败或 --keep 时保留，方便排查。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = PLUGIN_ROOT.parent
for path in (str(PLUGIN_ROOT), str(PLUGINS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

CORE_TABLES = {"matches", "stadium", "league_state", "admins", "plugin_config"}
OLD_TABLES = {
    "market_offers", "event_choices", "events_log", "window_summaries",
    "window_fans_log", "revenue_transactions", "club_balance", "naming_rights",
    "brand_pool", "market_rounds", "event_pool", "stadium_facilities",
    "venue_bookings", "influence_history", "season_names", "round_names",
}


def _install_runtime_stubs_if_needed() -> None:
    """无 AstrBot 运行时（如仓库根的普通 Python）时安装 tests/stubs。"""
    try:
        from astrbot.api import logger  # noqa: F401
        return
    except Exception:
        pass
    try:
        from tests.stubs import install_stubs
    except Exception as e:  # pragma: no cover - 运行环境缺 tests 时给出明确提示
        raise SystemExit(
            f"无法导入 astrbot.api，也无法加载 tests/stubs.py：{type(e).__name__}: {e}"
        )
    install_stubs()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ro_uri(path: Path) -> str:
    # as_uri() 在 Windows 下是 file:///C:/...，再附加 mode=ro
    return path.resolve().as_uri() + "?mode=ro"


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _scalar(conn: sqlite3.Connection, sql: str, default=None):
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return default
    return default if row is None else row[0]


def _count_col(conn: sqlite3.Connection, table: str, col: str) -> int | None:
    if col not in _columns(conn, table):
        return None
    return int(_scalar(conn, f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL", 0))


def _distribution(conn: sqlite3.Connection, col: str) -> list[tuple]:
    if "matches" not in _table_names(conn) or col not in _columns(conn, "matches"):
        return []
    return list(
        conn.execute(
            f"SELECT {col}, COUNT(*) FROM matches GROUP BY {col} ORDER BY {col}"
        )
    )


def _snapshot(conn: sqlite3.Connection) -> dict:
    version = _scalar(
        conn, "SELECT value FROM plugin_config WHERE key='schema_version'", None
    )
    snap = {
        "tables": _table_names(conn),
        "schema_version": version,
        "matches": _scalar(conn, "SELECT COUNT(*) FROM matches", None),
        "seasons": _distribution(conn, "season_number"),
        "tournaments": _distribution(conn, "tournament_id"),
        "legs": _distribution(conn, "leg"),
        "results": _count_col(conn, "matches", "result"),
        "weather": _count_col(conn, "matches", "weather"),
        "tour_match_ids": _count_col(conn, "matches", "tour_match_id"),
        "stadium": _scalar(conn, "SELECT COUNT(*) FROM stadium", None),
        "admins": _scalar(conn, "SELECT COUNT(*) FROM admins", None),
        "league_state": _scalar(conn, "SELECT COUNT(*) FROM league_state", None),
    }
    try:
        snap["integrity"] = _scalar(conn, "PRAGMA integrity_check", "unknown")
        snap["fk_violations"] = len(list(conn.execute("PRAGMA foreign_key_check")))
    except sqlite3.Error as e:
        snap["integrity"] = f"error: {e}"
        snap["fk_violations"] = -1
    return snap


def _fmt_dist(rows: list[tuple]) -> str:
    return "、".join(f"{k}={v}" for k, v in rows) if rows else "—"


def _print_snapshot(label: str, snap: dict) -> None:
    print(f"\n[{label}]")
    print(f"  tables        : {', '.join(snap['tables']) or '—'}")
    print(f"  schema_version: {snap['schema_version']}")
    print(f"  matches       : {snap['matches']}")
    print(f"  season_number : {_fmt_dist(snap['seasons'])}")
    print(f"  tournament_id : {_fmt_dist(snap['tournaments'])}")
    print(f"  leg           : {_fmt_dist(snap['legs'])}")
    print(
        "  非空计数      : "
        f"result={snap['results']} weather={snap['weather']} "
        f"tour_match_id={snap['tour_match_ids']}"
    )
    print(
        f"  stadium/admin : {snap['stadium']} / {snap['admins']} "
        f"league_state={snap['league_state']}"
    )
    print(f"  integrity     : {snap['integrity']} (fk={snap['fk_violations']})")


def _copy_readonly(src: Path, dst: Path) -> None:
    """SQLite 在线备份 API：源库只读，副本独立。"""
    src_conn = sqlite3.connect(_ro_uri(src), uri=True)
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
        dst_conn.commit()
    finally:
        dst_conn.close()
        src_conn.close()


async def _migrate_copy(path: Path) -> None:
    _install_runtime_stubs_if_needed()
    from astrbot_plugin_whleague_revenue_system.db.connection import DatabaseManager
    from astrbot_plugin_whleague_revenue_system.db.schema import init_schema

    db = DatabaseManager(str(path))
    await db.init()
    try:
        await init_schema(db)
    finally:
        await db.close()


def _check(name: str, ok: bool, detail: str) -> bool:
    print(f"  {'✅' if ok else '❌'} {name}: {detail}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v15 迁移演练（只读源库，只迁移副本）")
    parser.add_argument("source", help="生产 SQLite 文件路径")
    parser.add_argument("--workdir", help="副本目录（默认系统临时目录）")
    parser.add_argument("--keep", action="store_true", help="结束后保留副本")
    parser.add_argument("--expect-matches", type=int, help="期望迁移后 matches 行数，如 136")
    parser.add_argument("--expect-season", type=int, help="期望迁移后 season_number，如 9")
    args = parser.parse_args(argv)

    src = Path(args.source).expanduser().resolve()
    if not src.is_file():
        print(f"❌ 源库不存在：{src}")
        return 2
    if src.stat().st_size == 0:
        print(f"❌ 源库是 0 字节空库：{src}")
        return 2

    workdir = Path(args.workdir).expanduser().resolve() if args.workdir else Path(
        tempfile.mkdtemp(prefix="whl-v15-rehearsal-")
    )
    workdir.mkdir(parents=True, exist_ok=True)
    copy_path = workdir / "revenue_system.rehearsal.db"
    if copy_path.resolve() == src:
        print("❌ 副本路径不能等于源库")
        return 2
    if copy_path.exists():
        copy_path.unlink()

    before_hash = _sha256(src)
    print("=== v15 迁移演练 ===")
    print(f"源库   : {src}")
    print(f"副本   : {copy_path}")
    print(f"SHA256 : {before_hash}")

    try:
        _copy_readonly(src, copy_path)
    except Exception as e:
        print(f"❌ 只读复制失败：{type(e).__name__}: {e}")
        return 1

    with sqlite3.connect(str(copy_path)) as conn:
        before = _snapshot(conn)
    _print_snapshot("迁移前（副本）", before)

    print("\n[迁移] 正在对副本执行 init_schema() …")
    try:
        asyncio.run(_migrate_copy(copy_path))
    except Exception as e:
        print(f"❌ 迁移失败：{type(e).__name__}: {e}")
        print(f"副本保留在：{copy_path}")
        return 1

    with sqlite3.connect(str(copy_path)) as conn:
        after = _snapshot(conn)
    _print_snapshot("迁移后（副本）", after)

    print("\n[校验]")
    ok = True
    ok &= _check("schema_version", str(after["schema_version"]) == "15", str(after["schema_version"]))
    ok &= _check(
        "核心表",
        set(after["tables"]) == CORE_TABLES,
        f"{sorted(after['tables'])}",
    )
    ok &= _check(
        "旧表已删",
        not (set(after["tables"]) & OLD_TABLES),
        f"残留={sorted(set(after['tables']) & OLD_TABLES) or '无'}",
    )
    ok &= _check(
        "matches 行数不变",
        before["matches"] == after["matches"],
        f"{before['matches']} -> {after['matches']}",
    )
    ok &= _check(
        "weather 计数不变",
        before["weather"] == after["weather"],
        f"{before['weather']} -> {after['weather']}",
    )
    ok &= _check(
        "result 计数不变",
        before["results"] == after["results"],
        f"{before['results']} -> {after['results']}",
    )
    if before["tour_match_ids"] is not None:
        ok &= _check(
            "tour_match_id 计数不变",
            before["tour_match_ids"] == after["tour_match_ids"],
            f"{before['tour_match_ids']} -> {after['tour_match_ids']}",
        )
    ok &= _check(
        "tournament_id 无 NULL",
        not any(k is None for k, _ in after["tournaments"]),
        _fmt_dist(after["tournaments"]),
    )
    ok &= _check(
        "leg 无 NULL",
        not any(k is None for k, _ in after["legs"]),
        _fmt_dist(after["legs"]),
    )
    ok &= _check("integrity_check", after["integrity"] == "ok", str(after["integrity"]))
    ok &= _check("foreign_key_check", after["fk_violations"] == 0, str(after["fk_violations"]))

    if args.expect_matches is not None:
        ok &= _check(
            "期望 matches 行数",
            after["matches"] == args.expect_matches,
            f"{after['matches']} (期望 {args.expect_matches})",
        )
    if args.expect_season is not None:
        seasons = [k for k, _ in after["seasons"]]
        ok &= _check(
            "期望 season_number",
            seasons == [args.expect_season],
            f"{seasons} (期望 [{args.expect_season}])",
        )

    after_hash = _sha256(src)
    source_unchanged = before_hash == after_hash
    print(
        f"\n源库 SHA256 {'未变' if source_unchanged else '已变'}：{after_hash}"
    )
    if not source_unchanged:
        print("  ⚠️ 源库在演练期间被外部写入（生产流量）；脚本只以 mode=ro 打开源库，未写源库。")

    if ok and not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
        print("\n结论：✅ 演练通过；副本已清理。")
        return 0
    if ok:
        print(f"\n结论：✅ 演练通过；副本保留在：{copy_path}")
        return 0
    print(f"\n结论：❌ 有校验未通过；副本保留在：{copy_path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
