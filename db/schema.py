from astrbot.api import logger

SCHEMA_VERSION = 6

SQL_CREATE_TABLES = r"""

CREATE TABLE IF NOT EXISTS stadium (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    capacity INTEGER NOT NULL DEFAULT 12000,
    tier INTEGER NOT NULL DEFAULT 0,
    influence REAL NOT NULL DEFAULT 90,
    fans_diehards REAL NOT NULL DEFAULT 1800,
    next_attendance_mod REAL NOT NULL DEFAULT 1.0,
    free_rename_used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_stadium_tier ON stadium(tier);

CREATE TABLE IF NOT EXISTS stadium_facilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name TEXT NOT NULL REFERENCES stadium(team_name),
    facility_key TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(team_name, facility_key)
);

CREATE TABLE IF NOT EXISTS club_balance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name TEXT NOT NULL UNIQUE,
    balance REAL NOT NULL DEFAULT 0,
    build_credit REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS league_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    season_number INTEGER NOT NULL DEFAULT 1,
    window_seq INTEGER NOT NULL DEFAULT 1,
    current_round INTEGER NOT NULL DEFAULT 0,
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS matches (
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

CREATE INDEX IF NOT EXISTS idx_matches_round ON matches(season_number, window_seq, round_no);
CREATE INDEX IF NOT EXISTS idx_matches_home ON matches(home_team);

CREATE TABLE IF NOT EXISTS influence_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name TEXT NOT NULL,
    season_number INTEGER NOT NULL,
    window_seq INTEGER NOT NULL,
    influence REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(team_name, season_number, window_seq)
);

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qq TEXT NOT NULL UNIQUE,
    added_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS naming_rights (
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

CREATE TABLE IF NOT EXISTS brand_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    heat REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'builtin',
    status TEXT NOT NULL DEFAULT 'adopted',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS venue_bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name TEXT NOT NULL,
    season_number INTEGER NOT NULL,
    window_seq INTEGER NOT NULL,
    slot_no INTEGER NOT NULL,
    activity_type TEXT NOT NULL,
    booked_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(team_name, season_number, window_seq, slot_no)
);

CREATE TABLE IF NOT EXISTS revenue_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name TEXT NOT NULL,
    season_number INTEGER NOT NULL,
    window_seq INTEGER NOT NULL,
    round_no INTEGER,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_tx_team ON revenue_transactions(team_name, season_number, window_seq);
CREATE INDEX IF NOT EXISTS idx_tx_kind ON revenue_transactions(kind);

CREATE TABLE IF NOT EXISTS event_pool (
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

CREATE TABLE IF NOT EXISTS events_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name TEXT NOT NULL,
    season_number INTEGER NOT NULL,
    window_seq INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    effect_json TEXT NOT NULL DEFAULT '{}',
    text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_events_log ON events_log(team_name, season_number, window_seq);

CREATE TABLE IF NOT EXISTS event_choices (
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

CREATE INDEX IF NOT EXISTS idx_event_choices ON event_choices(season_number, window_seq);

CREATE TABLE IF NOT EXISTS window_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_number INTEGER NOT NULL,
    window_seq INTEGER NOT NULL,
    tx_ids TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(season_number, window_seq)
);

CREATE TABLE IF NOT EXISTS plugin_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 命名轮次登记：同一赛季内「轮次名」唯一对应一个轮次号（同名即为同一轮）
CREATE TABLE IF NOT EXISTS round_names (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_number INTEGER NOT NULL,
    token TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(season_number, token)
);

"""


async def init_schema(db_manager):
    db = db_manager.conn
    async with db_manager.lock:
        await db.executescript(SQL_CREATE_TABLES)
        await db.commit()

    cur = await db.execute("SELECT value FROM plugin_config WHERE key='schema_version'")
    row = await cur.fetchone()
    await cur.close()
    if row is None:
        await db.execute(
            "INSERT INTO plugin_config (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        await db.execute(
            "INSERT OR IGNORE INTO league_state (id, season_number, window_seq, current_round) VALUES (1, 1, 1, 0)"
        )
        await db.commit()
        logger.info("Database schema initialized (version %d).", SCHEMA_VERSION)
    else:
        try:
            current = int(row["value"])
        except (ValueError, TypeError):
            logger.warning(
                "Invalid schema_version value %r, rewriting to %d.",
                row["value"],
                SCHEMA_VERSION,
            )
            await db.execute(
                "UPDATE plugin_config SET value=?, updated_at=datetime('now','localtime') WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
            await db.commit()
            current = SCHEMA_VERSION
        if current < SCHEMA_VERSION:
            await _migrate(db, current)
            await db.execute(
                "UPDATE plugin_config SET value=?, updated_at=datetime('now','localtime') WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
            await db.commit()
            logger.info("Database schema migrated %d -> %d.", current, SCHEMA_VERSION)


async def _table_columns(db, table: str) -> set:
    cur = await db.execute(f"PRAGMA table_info({table})")
    try:
        rows = await cur.fetchall()
        return {r["name"] for r in rows}
    finally:
        await cur.close()


async def _migrate(db, current_version: int):
    """增量迁移：仅在目标列缺失时执行，保证可重复运行。"""
    if current_version < 1:
        await db.execute(
            "INSERT OR IGNORE INTO league_state (id, season_number, window_seq, current_round) VALUES (1, 1, 1, 0)"
        )
        await db.commit()
    if current_version < 2:
        cols = await _table_columns(db, "matches")
        if "competition" not in cols:
            # 重建 matches 表：新增 competition 列并纳入唯一键（同轮次多赛事可共存）
            await db.executescript(
                """
                ALTER TABLE matches RENAME TO matches_old;
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
                INSERT INTO matches
                    (id, season_number, window_seq, round_no, competition, home_team,
                     away_team, weather, result, attendance, ticket_revenue,
                     commercial, broadcast, created_at)
                SELECT id, season_number, window_seq, round_no, '联赛', home_team,
                       away_team, weather, result, attendance, ticket_revenue,
                       commercial, broadcast, created_at
                FROM matches_old;
                DROP TABLE matches_old;
                CREATE INDEX IF NOT EXISTS idx_matches_round ON matches(season_number, window_seq, round_no);
                CREATE INDEX IF NOT EXISTS idx_matches_home ON matches(home_team);
                """
            )
            await db.commit()
            logger.info("Migrated matches table: added competition column.")
    if current_version < 3:
        cols = await _table_columns(db, "matches")
        for col, ddl in (
            ("score", "ALTER TABLE matches ADD COLUMN score TEXT"),
            ("week_no", "ALTER TABLE matches ADD COLUMN week_no INTEGER"),
            ("day_no", "ALTER TABLE matches ADD COLUMN day_no INTEGER"),
            ("match_time", "ALTER TABLE matches ADD COLUMN match_time TEXT"),
        ):
            if col not in cols:
                await db.execute(ddl)
        await db.commit()
        logger.info("Migrated matches table: added score/week/day/time columns.")
    if current_version < 4:
        cols = await _table_columns(db, "window_summaries")
        if "tx_ids" not in cols:
            # 记录结算创建的流水 ID，供强制重算时精确撤销
            await db.execute(
                "ALTER TABLE window_summaries ADD COLUMN tx_ids TEXT NOT NULL DEFAULT ''"
            )
        await db.commit()
        logger.info("Migrated window_summaries table: added tx_ids column.")
    if current_version < 5:
        cols = await _table_columns(db, "event_pool")
        for col, ddl in (
            ("event_type", "ALTER TABLE event_pool ADD COLUMN event_type TEXT NOT NULL DEFAULT 'instant'"),
            ("options_json", "ALTER TABLE event_pool ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}'"),
        ):
            if col not in cols:
                await db.execute(ddl)
        # 选择型事件的玩家决策表（choice_no=NULL 表示尚未定夺，结算按最差兜底）
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS event_choices (
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
            CREATE INDEX IF NOT EXISTS idx_event_choices ON event_choices(season_number, window_seq);
            """
        )
        await db.commit()
        logger.info("Migrated event tables: event_type/options_json + event_choices table.")
    if current_version < 6:
        # 命名轮次登记表：纯文字轮次（如「顶级」「小组赛」）在同一赛季同名恒同号
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS round_names (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_number INTEGER NOT NULL,
                token TEXT NOT NULL,
                round_no INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(season_number, token)
            );
            """
        )
        await db.commit()
        logger.info("Migrated round_names table for named rounds.")