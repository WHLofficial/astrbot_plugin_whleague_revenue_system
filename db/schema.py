"""schema v15：展示插件自包含结构（5 张核心表）+ v12–v14 迁移。

v15 起不再保留 v1–v14 的增量迁移链：
- 全新安装直接建 5 张表（matches / stadium / league_state / admins / plugin_config）。
- 存量库仅支持 v12–v14 → v15 一次性重建；version < 12 报错要求先升到 v2.14，
  version > 15 或非法值直接报错，不再静默改写。
迁移流程：PRAGMA foreign_keys=OFF → BEGIN IMMEDIATE → 重建 matches/stadium/league_state
→ 删 16 张旧表 → 写 schema_version=15 → COMMIT → PRAGMA foreign_keys=ON；失败回滚。
"""

import re

from astrbot.api import logger

SCHEMA_VERSION = 15

SQL_CREATE_TABLES = r"""
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_number INTEGER NOT NULL,
    tournament_id INTEGER NOT NULL,
    competition TEXT NOT NULL,          -- 顶级联赛/次级联赛/冠军杯（展示文本，与 tournament_id 并存）
    round_no INTEGER NOT NULL,
    leg INTEGER NOT NULL DEFAULT 0,     -- 0=单场，1/2=两回合
    stage_name TEXT NOT NULL DEFAULT '',
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_score INTEGER,
    away_score INTEGER,
    pen_home INTEGER,
    pen_away INTEGER,
    result TEXT,                        -- W/D/L/C（主队视角）
    score TEXT,                         -- 展示串：2:1 / 2:1（点球 4:2）/ 弃权
    tour_status TEXT NOT NULL DEFAULT 'pending',
    walkover_side TEXT,                 -- home/away
    weather TEXT,
    wx_coef REAL,
    week_no INTEGER,
    day_no INTEGER,
    match_time TEXT,
    tour_match_id INTEGER,
    synced_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(season_number, tournament_id, round_no, leg, home_team, away_team)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_matches_tour ON matches(tour_match_id) WHERE tour_match_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_matches_round ON matches(season_number, tournament_id, round_no, leg);
CREATE INDEX IF NOT EXISTS idx_matches_home ON matches(home_team);

CREATE TABLE IF NOT EXISTS stadium (
    team_name TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS league_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    season_number INTEGER NOT NULL,
    season_label TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qq TEXT NOT NULL UNIQUE,
    added_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS plugin_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# v15 迁移后删除的 16 张旧表（财务/经营/事件/冠名/市场/设施/档期/身份/赛季命名）
_V15_DROP_TABLES = (
    "market_offers",
    "event_choices",
    "events_log",
    "window_summaries",
    "window_fans_log",
    "revenue_transactions",
    "club_balance",
    "naming_rights",
    "brand_pool",
    "market_rounds",
    "event_pool",
    "stadium_facilities",
    "venue_bookings",
    "influence_history",
    "season_names",
    "round_names",
)

# 存量赛季号映射：v15 仅支持 2 -> 9（其余报错待人工确认）
_V15_SEASON_MAP = {2: 9}

# competition 文本 -> tournament_id；「联赛」为 v2 旧默认文本，落库归一为「顶级联赛」
# （v3 读路径/club 映射统一用「顶级联赛」，不归一则迁移行在轮次图/走势图/天气全查空）
_V15_COMPETITION_MAP = {
    "联赛": 1,
    "顶级联赛": 1,
    "次级联赛": 2,
    "冠军杯": 3,
}
_V15_COMPETITION_TEXT_NORMALIZE = {"联赛": "顶级联赛"}

_MATCHES_DDL_V15 = """
CREATE TABLE matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_number INTEGER NOT NULL,
    tournament_id INTEGER NOT NULL,
    competition TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    leg INTEGER NOT NULL DEFAULT 0,
    stage_name TEXT NOT NULL DEFAULT '',
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_score INTEGER,
    away_score INTEGER,
    pen_home INTEGER,
    pen_away INTEGER,
    result TEXT,
    score TEXT,
    tour_status TEXT NOT NULL DEFAULT 'pending',
    walkover_side TEXT,
    weather TEXT,
    wx_coef REAL,
    week_no INTEGER,
    day_no INTEGER,
    match_time TEXT,
    tour_match_id INTEGER,
    synced_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(season_number, tournament_id, round_no, leg, home_team, away_team)
)
"""

_SCORE_RE = re.compile(r"^\s*(\d+)\s*[:：]\s*(\d+)")
_PEN_RE = re.compile(r"点球\s*(\d+)\s*[:：]\s*(\d+)")


def parse_score_text(score: str | None) -> tuple[int | None, int | None, int | None, int | None]:
    """解析展示串 score -> (home_score, away_score, pen_home, pen_away)。

    支持 '2:1' 与 '2:1（点球 4:2）'（全/半角括号与冒号）；「弃权」或无法解析时
    返回全 None（比分不猜）。
    """
    if score is None:
        return None, None, None, None
    text = str(score).strip()
    if not text or "弃权" in text:
        return None, None, None, None
    m = _SCORE_RE.match(text)
    if not m:
        return None, None, None, None
    home, away = int(m.group(1)), int(m.group(2))
    pen = _PEN_RE.search(text)
    if pen:
        return home, away, int(pen.group(1)), int(pen.group(2))
    return home, away, None, None


async def init_schema(db_manager):
    db = db_manager.conn
    async with db_manager.lock:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='plugin_config'"
        )
        try:
            has_config = await cur.fetchone() is not None
        finally:
            await cur.close()

        if not has_config:
            # 全新安装：直建 5 表 + 版本号 + league_state 种子
            await db.executescript(SQL_CREATE_TABLES)
            await db.commit()
            await db.execute(
                "INSERT INTO plugin_config (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            await db.execute(
                "INSERT OR IGNORE INTO league_state (id, season_number, season_label) "
                "VALUES (1, 1, 'S1')"
            )
            await db.commit()
            logger.info("Database schema initialized (version %d).", SCHEMA_VERSION)
            return

        cur = await db.execute("SELECT value FROM plugin_config WHERE key='schema_version'")
        try:
            row = await cur.fetchone()
        finally:
            await cur.close()

        if row is None or row["value"] is None:
            raise RuntimeError(
                "plugin_config 存在但缺少 schema_version，库处于未知状态："
                "v15 拒绝自动改写，请恢复备份后重试。"
            )
        raw = str(row["value"]).strip()
        try:
            current = int(raw)
        except ValueError:
            raise RuntimeError(
                f"schema_version 非法值 {row['value']!r}：v15 起不再静默改写，"
                "请人工修复或恢复备份。"
            )
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库 schema 版本 {current} 高于插件支持的 {SCHEMA_VERSION}："
                "v15 起不再静默降级，请先升级插件。"
            )
        if current < 12:
            raise RuntimeError(
                f"数据库 schema 版本 {current} 过旧（v15 自包含迁移仅支持 v12–v14）："
                "请先升级插件到 2.14 完成旧版迁移，再升级到当前版本。"
            )
        if current == SCHEMA_VERSION:
            # 幂等补齐（IF NOT EXISTS），不重写版本号
            await db.executescript(SQL_CREATE_TABLES)
            await db.commit()
            return
        # 12 <= current < 15
        await _migrate_v15(db, current)
        logger.info("Database schema migrated %d -> %d.", current, SCHEMA_VERSION)


async def _table_columns(db, table: str) -> set:
    cur = await db.execute(f"PRAGMA table_info({table})")
    try:
        rows = await cur.fetchall()
        return {r["name"] for r in rows}
    finally:
        await cur.close()


async def _check_v15_season(db) -> int:
    """校验存量赛季号并返回映射后的当前赛季（不支持则报错列出待人工确认）。"""
    cur = await db.execute("SELECT DISTINCT season_number FROM matches")
    try:
        rows = await cur.fetchall()
    finally:
        await cur.close()
    seasons = sorted({int(r["season_number"]) for r in rows})
    unsupported = [s for s in seasons if s not in _V15_SEASON_MAP]
    if unsupported:
        raise RuntimeError(
            f"schema v15 迁移仅支持 season_number 映射 2→9，"
            f"发现待人工确认的赛季号: {unsupported}；迁移已中止，库未改动。"
        )
    mapped = {_V15_SEASON_MAP[s] for s in seasons}
    if not mapped:
        # 空 matches：沿用 league_state 的赛季号（同样必须可映射）
        cur = await db.execute("SELECT season_number FROM league_state WHERE id=1")
        try:
            srow = await cur.fetchone()
        finally:
            await cur.close()
        if srow is not None:
            state_season = int(srow["season_number"])
            if state_season not in _V15_SEASON_MAP:
                raise RuntimeError(
                    f"schema v15 迁移仅支持 season_number 映射 2→9，"
                    f"league_state.season_number={state_season} 待人工确认；迁移已中止，库未改动。"
                )
            return _V15_SEASON_MAP[state_season]
        return 9
    if len(mapped) > 1:
        raise RuntimeError(f"matches 存在多个赛季号 {sorted(mapped)}，待人工确认；迁移已中止。")
    return mapped.pop()


async def _migrate_v15(db, current_version: int) -> None:
    """v12–v14 -> v15 一次性重建。全程单事务，失败回滚（版本不推进，可重试）。"""
    await db.execute("PRAGMA foreign_keys=OFF")
    try:
        await db.execute("BEGIN IMMEDIATE")
        try:
            season = await _check_v15_season(db)

            # 1) 重建 matches：window_seq/attendance 等旧列丢弃，补 tournament_id/leg/stage_name/比分列
            await db.execute("ALTER TABLE matches RENAME TO matches_old_v15")
            await db.execute(_MATCHES_DDL_V15)
            old_cols = await _table_columns(db, "matches_old_v15")

            def old_col(name: str, default: str = "NULL") -> str:
                return name if name in old_cols else default

            cur = await db.execute(
                "SELECT id, season_number, competition, round_no, home_team, away_team, "
                "weather, "
                f"{old_col('wx_coef')} AS wx_coef, result, score, week_no, day_no, match_time, "
                f"{old_col('tour_match_id')} AS tour_match_id, created_at FROM matches_old_v15 ORDER BY id"
            )
            try:
                old_rows = await cur.fetchall()
            finally:
                await cur.close()

            unknown_competitions: dict[str, int] = {}
            for r in old_rows:
                comp = str(r["competition"] or "").strip()
                tid = _V15_COMPETITION_MAP.get(comp)
                if tid is None:
                    unknown_competitions[comp] = unknown_competitions.get(comp, 0) + 1
                    continue
                home_score, away_score, pen_home, pen_away = parse_score_text(r["score"])
                tour_status = "pending" if r["result"] is None else "finished"
                await db.execute(
                    "INSERT INTO matches (id, season_number, tournament_id, competition, round_no, "
                    "leg, stage_name, home_team, away_team, home_score, away_score, pen_home, "
                    "pen_away, result, score, tour_status, walkover_side, weather, wx_coef, "
                    "week_no, day_no, match_time, tour_match_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "NULL, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(r["id"]),
                        _V15_SEASON_MAP[int(r["season_number"])],
                        tid,
                        _V15_COMPETITION_TEXT_NORMALIZE.get(comp, comp),
                        int(r["round_no"]),
                        str(r["home_team"]),
                        str(r["away_team"]),
                        home_score,
                        away_score,
                        pen_home,
                        pen_away,
                        r["result"],
                        r["score"],
                        tour_status,
                        r["weather"],
                        r["wx_coef"],
                        r["week_no"],
                        r["day_no"],
                        r["match_time"],
                        r["tour_match_id"],
                        r["created_at"],
                    ),
                )
            if unknown_competitions:
                raise RuntimeError(
                    f"schema v15 迁移发现待人工确认的 competition 文本: {unknown_competitions}；"
                    "迁移已回滚，库未改动。"
                )

            # 2) tour_match_id 去重：重复保留最小 id，其余置 NULL 并告警
            cur = await db.execute(
                "SELECT tour_match_id, COUNT(*) AS n FROM matches "
                "WHERE tour_match_id IS NOT NULL GROUP BY tour_match_id HAVING n > 1"
            )
            try:
                dup_rows = await cur.fetchall()
            finally:
                await cur.close()
            await db.execute(
                "UPDATE matches SET tour_match_id=NULL WHERE tour_match_id IS NOT NULL AND id NOT IN ("
                "SELECT MIN(id) FROM matches WHERE tour_match_id IS NOT NULL GROUP BY tour_match_id)"
            )
            for dup in dup_rows:
                logger.warning(
                    "schema v15 迁移：tour_match_id=%s 重复 %d 行，仅保留最小 id，其余置 NULL。",
                    dup["tour_match_id"], int(dup["n"]),
                )

            # 3) 旧 matches 索引随 matches_old_v15 一并删除，再建新索引
            await db.execute("DROP TABLE matches_old_v15")
            await db.execute(
                "CREATE UNIQUE INDEX uq_matches_tour ON matches(tour_match_id) "
                "WHERE tour_match_id IS NOT NULL"
            )
            await db.execute(
                "CREATE INDEX idx_matches_round ON matches(season_number, tournament_id, round_no, leg)"
            )
            await db.execute("CREATE INDEX idx_matches_home ON matches(home_team)")

            # 4) 重建 stadium：只保留 team_name/name/capacity
            await db.execute("ALTER TABLE stadium RENAME TO stadium_old_v15")
            await db.execute(
                "CREATE TABLE stadium ("
                "team_name TEXT PRIMARY KEY, "
                "name TEXT NOT NULL, "
                "capacity INTEGER NOT NULL DEFAULT 0)"
            )
            await db.execute(
                "INSERT INTO stadium (team_name, name, capacity) "
                "SELECT team_name, name, capacity FROM stadium_old_v15"
            )
            await db.execute("DROP TABLE stadium_old_v15")

            # 5) 重建 league_state：赛季映射 + S 标签
            await db.execute("DROP TABLE league_state")
            await db.execute(
                "CREATE TABLE league_state ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), "
                "season_number INTEGER NOT NULL, "
                "season_label TEXT NOT NULL, "
                "updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))"
            )
            await db.execute(
                "INSERT INTO league_state (id, season_number, season_label) VALUES (1, ?, ?)",
                (season, f"S{season}"),
            )

            # 6) 删除 16 张旧表
            for name in _V15_DROP_TABLES:
                await db.execute(f"DROP TABLE IF EXISTS {name}")

            # 7) 写 schema_version=15（与数据变更同一事务）
            await db.execute(
                "UPDATE plugin_config SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    finally:
        await db.execute("PRAGMA foreign_keys=ON")
