# WHL revenue 插件 v3.0.0：展示插件设计

- 状态：已定稿，待实施
- 日期：2026-11-15
- 仓库：`C:\Users\bhdjb\whlPointSystem\AstrBot\data\plugins\astrbot_plugin_whleague_revenue_system`
- 目标版本：`3.0.0`，schema `15`

## 1. 目标与不变量

把插件从「经营模拟器」收敛为「WHL 展示插件」：只保留赛程/赛果、天气、三张图、帮助。赛程/赛果以 tour 为唯一真源；赛季由 club 当前赛季自动驱动；上座/球场从 club 只读；天气仍由插件生成并推送 club。

不变量：

1. 只保留 **8 条命令、4 个服务、5 张表**。
2. 不改 tour / club 代码；只调用它们的公开 API。
3. 不实现自动 cron 同步；`/主场赛程同步` 手动触发。
4. 不做财务、经营、事件、冠名、市场、设施、档期、身份绑定、历史财务。
5. 不删除/不修改生产库；迁移只在本地和生产副本演练。
6. 未经用户明确指令，不 `git commit`、不 `git push`、不部署服务器。
7. 不改 `C:\Users\bhdjb\whlProgram\WHL-tournament-management-system`（tour 仓）的任何文件。

## 2. 最终命令（8 条）

| 命令 | 权限 | 行为 |
|---|---|---|
| `/主场赛程同步` | 管理 | 拉 club 当前赛季 + tour 三赛事，补建/更新赛程、赛果、leg、点球、弃权 |
| `/主场天气 <轮次>` | 管理 | 按轮抽天气/系数；已有天气保留，显示「已定」 |
| `/主场天气覆盖 <轮次> <主队> <天气>` | 管理 | 手改天气并重抽系数 |
| `/主场天气推送 [预览]` | 管理 | 只推当前赛季已有天气，不做旧账订正 |
| `/主场轮次预告图 <轮次>` | 所有人 | 周/天/星期/时间/主客/球场/天气；已赛显示比分 |
| `/主场轮次统计图 <轮次>` | 所有人 | 已赛比分/上座/球场；上座缺失显示 `—`，不丢行 |
| `/主场赛季走势图 [顶级\|次级]` | 所有人 | 积分线 + 联赛场均上座柱双轴；次级标注「不含带入分」 |
| `/主场帮助` | 所有人 | 上述 8 条命令说明 |

轮次解析：`顶级12` / `次级11` / `冠军3` / 纯数字。不再用 `round_names` 做 token→round 分配；`round_no` 直接使用 tour 轮次。

## 3. 数据流

```
club /api/seasons/current
  -> season (e.g. 9), tournaments [{tournamentId:1, competitionType:"league_premier"}, ...]
  -> plugin league_state(season_number=9, season_label="S9")

tour /api/public/tournaments/{id}
tour /api/public/tournaments/{id}/matches
  -> plugin matches（赛程/赛果/leg/点球/弃权/轮空）

plugin 本地天气
  -> /主场天气推送 -> club /api/internal/weather-sync

club /api/fixtures?tournament_id=&round=
  -> 上座/球场（只读，图表用）
```

### 3.1 club 当前赛季

- `GET {club_sync_url}/api/seasons/current`，公开、无登录。
- 返回 `{season:{season,status}|null, window, tournaments:[{id,tournamentId,competitionType}]}`。
- `season` 取 `preparing`/`running`，优先 `running`；`window` 是全局最新，**不能**当赛季窗口。
- 映射：`league_premier -> 顶级联赛 / tournament_id 1`，`league_second -> 次级联赛 / 2`，`champions_cup -> 冠军杯 / 3`。以 club 返回的 `tournamentId` 为准，不硬编码。
- 缓存 10 分钟；失败时用 `league_state` 缓存；没有缓存则报错「无法确定当前赛季」。
- 删除 `tour_sync_season`、`club_sync_season`、`season_names`。

## 4. Schema v15

### 4.1 保留 5 张表

```sql
CREATE TABLE matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  season_number INTEGER NOT NULL,
  tournament_id INTEGER NOT NULL,
  competition TEXT NOT NULL,          -- 顶级联赛/次级联赛/冠军杯
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
CREATE UNIQUE INDEX uq_matches_tour ON matches(tour_match_id) WHERE tour_match_id IS NOT NULL;
CREATE INDEX idx_matches_round ON matches(season_number, tournament_id, round_no, leg);
CREATE INDEX idx_matches_home ON matches(home_team);

CREATE TABLE stadium (
  team_name TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  capacity INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE league_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  season_number INTEGER NOT NULL,
  season_label TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- admins 保持原样，用于 _require_admin 兼容
CREATE TABLE admins (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  qq TEXT NOT NULL UNIQUE,
  added_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- plugin_config 只保留 schema_version + 必要的 kv
CREATE TABLE plugin_config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

### 4.2 迁移 v15

- v15 **自包含**，支持 v12–v14；`version < 12` 报错并提示先升级到 v2.14。
- 不再保留 v1–v14 的 343 行迁移链；`SQL_CREATE_TABLES` 只建 5 张核心表。
- 迁移前：生产库 `VACUUM INTO` 备份；导出 `revenue_transactions`、`club_balance`、`season_names`、`naming_rights` 到 CSV。
- 存量映射：
  - `season_number 2 -> 9`；`league_state` 重建为 `season_number=9, season_label='S9'`。
  - `competition`：`联赛 -> tournament_id 1`，`次级联赛 -> 2`，`冠军杯 -> 3`；`competition` 文本保留。
  - `leg=0`；`stage_name=''`。
  - `result` 保留；`score` 文本解析为 `home_score/away_score`（支持 `2:1`、`2:1（点球 4:2）`）。
  - `tour_status = finished if result else pending`。
  - `weather/wx_coef/week_no/day_no/match_time/tour_match_id/created_at` 原样复制。
  - `attendance/ticket_revenue/commercial/broadcast/window_seq` 丢弃。
- `stadium` 只复制 `team_name/name/capacity`。
- 删除 16 张表：`market_offers`、`event_choices`、`events_log`、`window_summaries`、`window_fans_log`、`revenue_transactions`、`club_balance`、`naming_rights`、`brand_pool`、`market_rounds`、`event_pool`、`stadium_facilities`、`venue_bookings`、`influence_history`、`season_names`、`round_names`。
- 迁移流程：`PRAGMA foreign_keys=OFF` → `BEGIN IMMEDIATE` → 重建 matches/stadium/league_state → 删旧表 → 写 schema_version=15 → `COMMIT` → `PRAGMA foreign_keys=ON`。失败回滚。
- `tour_match_id` 重复时保留最小 id，其余置 NULL，并记录 warning。
- `schema_version` 非法/高于 15 直接报错，不再静默改写。
- 迁移后核对：`schema_version=15`、表数=5、matches=136、season=9、tournament_id 分布 66/40/30。

## 5. tour 同步

### 5.1 拉取

- `GET /api/public/tournaments/{id}`：stages/groups/entries。
- `GET /api/public/tournaments/{id}/matches`：全部比赛。
- 每个赛事独立事务；一个赛事失败不影响其他赛事，报告 partial。

### 5.2 stage offset

按 stage 顺序累计前一阶段最大轮次：

- 顶级：round_robin，1–22。
- 次级：base 1–14；final 15–20；playoff elim 21–22。
- 冠军杯：group 1–6；elim 7+。

`round_no = offset + tour.round`。不要用 `stage_offsets` 旧逻辑（elim 返回 None）。

### 5.3 匹配

- `tour_match_id` 优先。
- 无 id 时用 `(season_number, tournament_id, round_no, leg, home_team, away_team)` 兜底。
- CPU 后缀 `(CPU)`/`（CPU）` 归一后匹配；展示保留原名。
- 轮空（note 含「轮空」）不落库。
- 对手未定/placeholder 不落库，计入 skipped。
- 两回合每 leg 一行；`leg` 进唯一键。
- `leg` 为空时用 0。

### 5.4 结果

- 只写 `finished`；`live` 只更新 `tour_status`，不落终局比分。
- `result` 主队视角：胜 W、平 D、负 L、弃权 C。
- 小组/联赛点球仍按平局；淘汰赛用点球定胜负。
- 两回合不本地算总比分，每 leg 独立。
- `walkoverSide` 记弃权方；`result='C'`；`score` 展示「弃权」。
- tour 改判时 finished 每次覆盖本地比分/结果。
- `score` 展示串：普通 `2:1`；点球 `2:1（点球 4:2）`；弃权 `弃权`。

### 5.5 补建与报告

- 不再依赖 `league_state.window_seq`。
- 所有联赛/杯赛行都补建，未排期行也建（week/day/time 为空），天气可先抽。
- 报告字段：`fetched/created/updated/unchanged/pending/skipped/missing_tour/conflicts`。
- 命令回显前 5 条明细；`skipped` 按「轮空/对手未定/淘汰赛未定」分类。
- 手动触发，7 个 GET，1–3 秒，幂等；不加 cron。

## 6. 天气

- `forecast_round(round_no, competition)`：按 `season_number + tournament_id + round_no` 查 matches；已有 weather 保留；缺的抽 `weather + wx_coef`。
- `set_weather(round_no, team, weather)`：手改天气并重抽系数。
- `sync_weather(preview)`：
  - 当前 club 赛季；
  - 按赛事/轮分组；
  - 只推已有天气；
  - payload `{season, competition, roundNo, items:[{home,away,weather,coef}]}`；
  - **删除** `_settlement_inputs`、`apply_weather_correction`、`ensure_stadium`、旧账订正、attendance/ticket/commercial/broadcast 读写。
- `club_sync_season` 配置删除；season 用 club current。

## 7. 三张图

### 7.1 轮次预告图

- 本地 `matches` + `stadium`。
- 列：周/天/星期/时间/主队/客队/球场/天气。
- 已赛加比分列；不调用 club fixtures。

### 7.2 轮次统计图

- 本地 `matches` + club `/api/fixtures?tournament_id=&round=`。
- 按 `matchId` 映射上座；缺失显示 `—`，不丢行。
- 无已赛则提示「还没有赛果」。
- 不按 window 过滤。

### 7.3 赛季走势图

- 支持 `顶级` / `次级`；冠军杯不支持。
- 左轴：按轮累计积分（W=3/D=1/L=0）。
- 右轴：当轮联赛场均上座柱。
- 次级从 0 开始，图上标注「不含带入分」。
- club fixtures 每轮一次，内存缓存 10 分钟；缺失轮次柱为 0/空。
- 文件名 `season_s9_顶级联赛.png`。

### 7.4 查询

- `get_round_matches(season, round_no, competition=None)` 去掉 `window_seq`。
- 删除 `_resolve_round` 的当前窗口优先逻辑；直接按 season + round + competition 查。
- 删除 `get_round_matches_latest`。
- 文件名不再带 window：`round_s9_顶级联赛_r12.png`、`preview_...`。

## 8. 模块与文件

### 8.1 保留

- `services/tour_sync.py`：重写。
- `services/fixture_service.py` → `services/weather_service.py`：只留天气。
- `services/chart_service.py`：三图重做。
- `services/club_sync.py`：天气推送。
- `services/backup_service.py`：每日 04:00 备份。
- `db/dao.py`：只留 matches/stadium/league_state/admins/plugin_config 读写。
- `handlers/admin.py`：4 条管理命令。
- `handlers/player.py`：帮助 + 三图。
- `main.py`：4 个服务、8 条命令、cron 备份。
- `utils/help_text.py`、`utils/security.py`（仅保留仍用的）、`config/defaults.py`、`metadata.yaml`、`_conf_schema.json`、`README.md`、`CHANGELOG.md`、`requirements.txt`。

### 8.2 删除

- 服务：`event_engine/event_effects/llm_writer/brand_service/market_service/stadium_service/window_service/fans_service/file_import_service/backfill_service/negotiation_bridge`。
- `main.py`：群文件监听 `on_group_file`、状态监听、`_get_provider`、bridge。
- 命令：所有经营/财务/事件/冠名/市场/设施/档期/身份/导入/结算/推进/配置/管理增删/发放/改名/赛季命名。
- 表：16 张（见 §4.2）。
- 测试：s2/s3/s4/s5/s7/s9/s10/s12/s14/s15/s16；s1/s8/s18/s19/s6/s13/s17 改写保留。

## 9. 测试

- `tests/common.py`：`TestEnv` 只装 4 个服务（Weather/TourSync/Backup/Chart），不再 import 已删服务。
- `s0_schema`：fresh DB、v12/v13→15、幂等、非法版本、表数=5、season 2→9。
- `s1_formula`：只测天气/系数。
- `s8_charts`：三图；上座缺失 `—`；次级积分；无 window 过滤。
- `s18_club_sync`：只推 payload，不订正旧账。
- `s19_tour_sync`：club season、stage offset、leg、点球、弃权、轮空、TBD、tour_match_id 优先、改判覆盖。
- `s6_handlers`：8 条命令、权限。
- `s13_infra`：备份/cron；群文件钩子已删。
- `s17_framework_compat`：版本 3.0.0。
- 目标：`python -m tests.run_all` 0 failed。

## 10. 实施顺序

| 阶段 | 人日 | 内容 | 退出条件 |
|---|---:|---|---|
| W0 | 0.25 | 本地备份、确认 schema 13/14 | 基线测试绿 |
| W1 | 0.5 | 死代码/旧别名/失效 import | 全量测试仍绿 |
| W2 | 1.0 | schema v15 + 迁移 + 副本演练 | 136 行迁移成功，表数=5 |
| W3 | 1.5 | club season + tour 全量同步 | 三赛事行数对齐，R12 可查 |
| W4 | 1.0 | 三图重做 | 三图正确 |
| W5 | 1.5 | 下线运营/身份/财务/赛季命名 | 命令=8，启动无 ImportError |
| W6 | 1.0 | 测试/文档/配置/版本 | tests.run_all 0 failed |
| W7 | 0.75 | 副本全流程 + 恢复演练 | 同步→天气→三图全通 |

## 11. 验收清单

- [ ] `python -m tests.run_all` 全绿。
- [ ] 插件启动无 ImportError；`/主场帮助` 只列 8 条命令。
- [ ] 本地 v13 副本迁移到 v15：matches=136，season=9，表数=5。
- [ ] `/主场赛程同步` 后顶级 R12 六场可见，tour_match_id/leg/比分正确。
- [ ] 轮次统计图在上座缺失时不丢行，显示 `—`。
- [ ] 赛季走势图顶级+次级可出图，次级标注不含带入分。
- [ ] 天气推送 payload 含 season/competition/roundNo/items，且不读写财务表。
- [ ] `git status` 仅包含本设计预期的改动，不包含 tour 仓改动。
- [ ] 未执行 git commit/push/deploy。

## 12. 非目标

- 自动定时同步、自动天气推送。
- 在插件内恢复财务/经营/事件/身份功能。
- 修改 tour/club 的生产数据或代码。
- 迁移/部署生产服务器（由用户另行指令）。
