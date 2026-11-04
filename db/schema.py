from astrbot.api import logger

SCHEMA_VERSION = 14

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
    wx_coef REAL,
    result TEXT,
    score TEXT,
    week_no INTEGER,
    day_no INTEGER,
    match_time TEXT,
    tour_match_id INTEGER,
    kickoff_source TEXT,
    attendance INTEGER,
    ticket_revenue REAL,
    commercial REAL,
    broadcast REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(season_number, window_seq, round_no, competition, home_team, away_team)
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
    -- 套餐快照（'{}'=legacy 老公式合同）；品牌满意度（行情体系）
    package_json TEXT NOT NULL DEFAULT '{}',
    satisfaction REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS brand_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    heat REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'builtin',
    status TEXT NOT NULL DEFAULT 'adopted',
    -- 档位（头部/新兴/口碑）、行业、招商卖点、套餐变体、手动锁档
    tier TEXT NOT NULL DEFAULT '口碑',
    industry TEXT NOT NULL DEFAULT '通用',
    pitch TEXT NOT NULL DEFAULT '',
    packages_json TEXT NOT NULL DEFAULT '',
    tier_locked INTEGER NOT NULL DEFAULT 0,
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
    soft_conditions INTEGER NOT NULL DEFAULT 0,
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

-- 命名轮次登记：同一赛季内「赛事+轮次文本」唯一对应一个轮次号（同名即为同一轮，按（赛季,赛事）导入顺序定序）
CREATE TABLE IF NOT EXISTS round_names (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_number INTEGER NOT NULL,
    competition TEXT NOT NULL DEFAULT '联赛',
    token TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(season_number, competition, token)
);

-- 赛季命名：纯展示标签（身份仍是 league_state.season_number 整数，查询过滤一律用序号）
CREATE TABLE IF NOT EXISTS season_names (
    season_number INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 冠名权市场：招商期（每窗口一轮，开启时的模式快照）
CREATE TABLE IF NOT EXISTS market_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_number INTEGER NOT NULL,
    window_seq INTEGER NOT NULL,
    bid_mode TEXT NOT NULL DEFAULT '明标',
    status TEXT NOT NULL DEFAULT 'open',
    opened_by TEXT NOT NULL DEFAULT '',
    opened_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    settled_at TEXT,
    UNIQUE(season_number, window_seq)
);

-- 品牌报价（先到先得挂价 team_name 存 ''；package_json 为套餐/档位/性格参数快照）
CREATE TABLE IF NOT EXISTS market_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES market_rounds(id),
    brand_id INTEGER NOT NULL REFERENCES brand_pool(id),
    team_name TEXT NOT NULL DEFAULT '',
    package_no INTEGER NOT NULL DEFAULT 1,
    amount REAL NOT NULL,
    windows INTEGER NOT NULL,
    package_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    expire_at TEXT,
    UNIQUE(round_id, brand_id, team_name)
);

CREATE INDEX IF NOT EXISTS idx_market_offers_round ON market_offers(round_id, status);

-- 每窗每队死忠快照（对赌奖金 redo 重算的增长口径来源）
CREATE TABLE IF NOT EXISTS window_fans_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_number INTEGER NOT NULL,
    window_seq INTEGER NOT NULL,
    team_name TEXT NOT NULL,
    fans_before REAL NOT NULL,
    fans_after REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(season_number, window_seq, team_name)
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
            # 显式事务包裹（SQLite DDL 可回滚）：失败时旧表完好，下次启动可重试，避免残留空表丢数据
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "ALTER TABLE matches RENAME TO matches_old"
                )
                await db.execute(
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
                    )
                    """
                )
                await db.execute(
                    """
                    INSERT INTO matches
                        (id, season_number, window_seq, round_no, competition, home_team,
                         away_team, weather, result, attendance, ticket_revenue,
                         commercial, broadcast, created_at)
                    SELECT id, season_number, window_seq, round_no, '联赛', home_team,
                           away_team, weather, result, attendance, ticket_revenue,
                           commercial, broadcast, created_at
                    FROM matches_old
                    """
                )
                await db.execute("DROP TABLE matches_old")
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_matches_round ON matches(season_number, window_seq, round_no)"
                )
                await db.execute("CREATE INDEX IF NOT EXISTS idx_matches_home ON matches(home_team)")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
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
    if current_version < 7:
        # 去重粒度收窄为「完整对阵」：唯一键加入 away_team，同主队窗口内可多场（重复对阵/双回合）
        # 显式事务包裹（DDL 可回滚）：失败时旧表完好、下次启动可重试，避免残留空表丢数据
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute("ALTER TABLE matches RENAME TO matches_old")
            await db.execute(
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
                    UNIQUE(season_number, window_seq, round_no, competition, home_team, away_team)
                )
                """
            )
            await db.execute(
                """
                INSERT INTO matches
                    (id, season_number, window_seq, round_no, competition, home_team,
                     away_team, weather, result, score, week_no, day_no, match_time,
                     attendance, ticket_revenue, commercial, broadcast, created_at)
                SELECT id, season_number, window_seq, round_no, competition, home_team,
                       away_team, weather, result, score, week_no, day_no, match_time,
                       attendance, ticket_revenue, commercial, broadcast, created_at
                FROM matches_old
                """
            )
            await db.execute("DROP TABLE matches_old")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_matches_round ON matches(season_number, window_seq, round_no)"
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_matches_home ON matches(home_team)")
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        logger.info("Migrated matches table: unique on full fixture (home+away).")
    if current_version < 8:
        # 轮次改为按（赛季, 赛事）导入顺序定序：round_names 增加 competition 列并纳入唯一键。
        # 旧映射（全局编号）作废重建；已导入 matches 的轮次号不重算。事务包裹，失败回滚可重试。
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute("DROP TABLE IF EXISTS round_names")
            await db.execute(
                """
                CREATE TABLE round_names (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    season_number INTEGER NOT NULL,
                    competition TEXT NOT NULL DEFAULT '联赛',
                    token TEXT NOT NULL,
                    round_no INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    UNIQUE(season_number, competition, token)
                )
                """
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        logger.info("Migrated round_names: per (season, competition) import-order numbering.")
    if current_version < 9:
        # 赛季命名表：每赛季一条展示名（有名只显名，无名回退 第N赛季）；CREATE IF NOT EXISTS 幂等
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS season_names (
                season_number INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            """
        )
        await db.commit()
        logger.info("Migrated season_names table for nameable seasons.")
    if current_version < 10:
        # 冠名权市场：brand_pool 档位/行业/卖点/套餐/锁档，naming_rights 套餐快照/满意度，
        # 新表 market_rounds/market_offers。存量品牌 tier 一律置「口碑」（迁移拿不到配置里的
        # 配额比例，不猜）；首次招商期按热度排名自动校准。
        cols = await _table_columns(db, "brand_pool")
        for col, ddl in (
            ("tier", "ALTER TABLE brand_pool ADD COLUMN tier TEXT NOT NULL DEFAULT '口碑'"),
            ("industry", "ALTER TABLE brand_pool ADD COLUMN industry TEXT NOT NULL DEFAULT '通用'"),
            ("pitch", "ALTER TABLE brand_pool ADD COLUMN pitch TEXT NOT NULL DEFAULT ''"),
            ("packages_json", "ALTER TABLE brand_pool ADD COLUMN packages_json TEXT NOT NULL DEFAULT ''"),
            ("tier_locked", "ALTER TABLE brand_pool ADD COLUMN tier_locked INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in cols:
                await db.execute(ddl)
        # 内置品牌行业映射（仅补默认值，不覆盖已有行业）
        await db.execute(
            """
            UPDATE brand_pool SET industry = CASE brand
                WHEN '麒麟生物' THEN '医疗'
                WHEN '阿迪达斯' THEN '运动'
                WHEN '亚马逊' THEN '科技'
                WHEN '可口可乐' THEN '饮食'
                WHEN '海底捞' THEN '饮食'
                WHEN '星海通讯' THEN '科技'
                WHEN 'CVS Health' THEN '医疗'
                ELSE industry
            END
            WHERE source='builtin' AND industry='通用'
            """
        )
        cols = await _table_columns(db, "naming_rights")
        for col, ddl in (
            ("package_json", "ALTER TABLE naming_rights ADD COLUMN package_json TEXT NOT NULL DEFAULT '{}'"),
            ("satisfaction", "ALTER TABLE naming_rights ADD COLUMN satisfaction REAL NOT NULL DEFAULT 1.0"),
        ):
            if col not in cols:
                await db.execute(ddl)
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_number INTEGER NOT NULL,
                window_seq INTEGER NOT NULL,
                bid_mode TEXT NOT NULL DEFAULT '明标',
                status TEXT NOT NULL DEFAULT 'open',
                opened_by TEXT NOT NULL DEFAULT '',
                opened_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                settled_at TEXT,
                UNIQUE(season_number, window_seq)
            );

            CREATE TABLE IF NOT EXISTS market_offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER NOT NULL REFERENCES market_rounds(id),
                brand_id INTEGER NOT NULL REFERENCES brand_pool(id),
                team_name TEXT NOT NULL DEFAULT '',
                package_no INTEGER NOT NULL DEFAULT 1,
                amount REAL NOT NULL,
                windows INTEGER NOT NULL,
                package_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                expire_at TEXT,
                UNIQUE(round_id, brand_id, team_name)
            );

            CREATE INDEX IF NOT EXISTS idx_market_offers_round ON market_offers(round_id, status);
            """
        )
        await db.commit()
        logger.info("Migrated naming market: brand_pool tiers + market_rounds/market_offers.")

    if current_version < 11:
        # 每窗每队死忠快照：对赌奖金 redo 重算的增长口径来源（v11 前旧窗无快照，
        # redo 回退现状行为）
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS window_fans_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_number INTEGER NOT NULL,
                window_seq INTEGER NOT NULL,
                team_name TEXT NOT NULL,
                fans_before REAL NOT NULL,
                fans_after REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(season_number, window_seq, team_name)
            );
            """
        )
        await db.commit()
        logger.info("Migrated window_fans_log: per-team diehard snapshot per window.")

    if current_version < 12:
        # 事件池软条件开关：置 1 时条件不满足按权重衰减而非硬剔除（动态权重）
        cols = await _table_columns(db, "event_pool")
        if "soft_conditions" not in cols:
            await db.execute(
                "ALTER TABLE event_pool ADD COLUMN soft_conditions INTEGER NOT NULL DEFAULT 0"
            )
        await db.commit()
        logger.info("Migrated event_pool: added soft_conditions column.")

    if current_version < 13:
        # 天气系数固化：预报/覆盖时抽定的 wx_coef 落库，结算与平台推送按同系数走。
        # NULL = v13 前的旧场次（系数未存过），结算回落现抽，推送时补抽落库。
        cols = await _table_columns(db, "matches")
        if "wx_coef" not in cols:
            await db.execute("ALTER TABLE matches ADD COLUMN wx_coef REAL")
        await db.commit()
        logger.info("Migrated matches: added wx_coef column.")

    if current_version < 14:
        # 开球时刻改由 tour 公开接口提供（P3-C）：本地降级为快照。
        # tour_match_id 是绑定锚点；kickoff_source 记来源（tour / 旧本地数据为空）。
        cols = await _table_columns(db, "matches")
        for col, ddl in (
            ("tour_match_id", "ALTER TABLE matches ADD COLUMN tour_match_id INTEGER"),
            ("kickoff_source", "ALTER TABLE matches ADD COLUMN kickoff_source TEXT"),
        ):
            if col not in cols:
                await db.execute(ddl)
        await db.commit()
        logger.info("Migrated matches: added tour_match_id / kickoff_source columns.")
