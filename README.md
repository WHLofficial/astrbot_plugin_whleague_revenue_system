# WHL 展示插件（astrbot_plugin_whleague_revenue_system）

WHL 展示插件 v3.0.0：只保留**赛程/赛果、天气、三张图、帮助**。

- 赛程/赛果以 **tour** 为唯一真源（`/主场赛程同步` 手动触发，不自动轮询）；
- 赛季由 **club 当前赛季**自动驱动（`/api/seasons/current`）；
- 上座/球场从 club **只读**（`/api/fixtures`），本地不记账、不结算；
- 天气由插件本地抽签固化，**只推已有天气**到 club，不做任何旧账订正；
- 不实现财务、经营、事件、冠名、市场、设施、档期、身份绑定、历史财务。

**环境要求与兼容性**：AstrBot `>=4,<5`（已在 **4.28.0** 验证）；Python ≥3.12；
依赖 `aiosqlite`、`Pillow`、`aiohttp`（见 `requirements.txt`，随插件自动安装）；平台 `aiocqhttp`。

---

## 一、命令（恰 8 条）

### 玩家（全体群友可查）

| 命令 | 行为 |
|---|---|
| `/主场帮助` | 按权限分级显示命令（玩家段/管理段） |
| `/主场轮次预告图 <轮次>` | 单轮对阵+天气+球场；已赛行追加比分列（弃权显示「弃权」） |
| `/主场轮次统计图 <轮次>` | 已赛比分与上座（club `/api/fixtures` 按 matchId 映射；缺失显示 `—`，不丢行） |
| `/主场赛季走势图 <顶级\|次级>` | 左轴按轮累计积分（W=3/D=1/L=0，弃权不计）× 右轴当轮场均上座柱；次级标注「不含带入分」 |

### 管理（管理员）

| 命令 | 行为 |
|---|---|
| `/主场赛程同步` | club 当前赛季 → tour 三赛事全量拉取：赛程/赛果/leg/点球/弃权/轮空/TBD，`tour_match_id` 优先匹配、归一队名唯一键兜底；未排期行也补建；每赛事独立事务，报告 partial |
| `/主场天气 <轮次>` | 抽整轮天气并固化系数（已有天气保留，标「已定」） |
| `/主场天气覆盖 <轮次> <主队> <天气>` | 手改单场天气并按新类型重抽系数 |
| `/主场天气推送 [预览]` | 只推已有天气到 club（`{season, competition, roundNo, items}`，HMAC 签名）；预览=只算不发；**不订正任何旧账** |

**轮次支持前缀**：`顶级12` / `次级11` / `冠军3` / 纯数字（默认全部赛事）。不再使用
`round_names` 做 token→round 分配，`round_no` 直接使用 tour 轮次。

## 二、数据流

```
club /api/seasons/current
  → season（preparing/running，优先 running；window 一律不当赛季窗口用）
  → 映射 tournamentId（league_premier→顶级联赛、league_second→次级联赛、champions_cup→冠军杯）
  → 插件 league_state(season_number=9, season_label="S9")（失败回落本地 league_state）

tour /api/public/tournaments/{id} + /{id}/matches
  → stage offset：按阶段顺序累计前面各阶段实际最大轮次
    （顶级 round_robin 1–22；次级 base 1–14 / final 15–20 / playoff elim 21–22；
      冠军杯 group 1–6 / elim 7+；淘汰赛同样有偏移，不再丢弃）
  → 两回合每 leg 一行；CPU 后缀归一后匹配、展示保留原名；
    轮空（note 含「轮空」）不落库；对手未定/占位按「轮空/对手未定/淘汰赛未定」分类跳过
  → 只 finished 落终局比分；live 仅更新状态；小组/联赛点球仍按平局、淘汰赛点球定胜负；
    walkoverSide → result='C'、score='弃权'；tour 改判时每次覆盖本地

plugin 本地天气 → /主场天气推送 → club /api/internal/weather-sync（只推已有天气，不订正）

club /api/fixtures?tournament_id=&round=
  → 统计图/走势图按 matchId 映射上座（只读，10 分钟缓存，缺失显示 —）
```

## 三、配置（11 键）

| 键 | 默认 | 说明 |
|---|---|---|
| `group_whitelist` | `[]` | 允许使用的群 ID 列表，空=全部群 |
| `tour_api_base` | `https://tour.whleague.win` | tour 根地址；留空则赛程同步旁路 |
| `club_sync_url` | `""` | club 根地址；天气推送与上座拉取；留空则不推送、上座显示 `—` |
| `club_sync_secret` | `""` | 天气推送 HMAC 密钥（与平台 `WEATHER_SYNC_SECRET` 相同）；上座拉取不需要 |
| `weather_probabilities` | 四档 | 天气→概率 JSON（加和为 1） |
| `weather_ranges` | 四档 | 天气→系数区间 JSON |
| `competition_aliases` | 三赛事 | 轮次前缀识别 JSON（赛事名→前缀列表） |
| `chart_font_path` | `""` | 图表中文字体路径（留空自动查找：内置→系统→目录扫描） |
| `backup_enabled` | `true` | 每日自动备份开关 |
| `backup_time` | `04:00` | 备份时间（宿主本地时区 HH:MM） |
| `backup_keep_count` | `30` | 备份保留份数 |

## 四、数据与迁移（schema 15）

- 数据库 `data/plugin_data/astrbot_plugin_whleague_revenue_system/revenue_system.db`，
  每日 04:00 自动 `VACUUM` 备份（`backup/` 目录，保留份数可配）。
- **schema v15**：仅 5 张表（`matches` / `stadium` / `league_state` / `admins` / `plugin_config`）。
- 自包含迁移只支持 **v12–v14 → v15**：`season_number 2→9`、`competition` 文本→`tournament_id`
  （联赛→1 / 顶级联赛→1 / 次级联赛→2 / 冠军杯→3）、`leg=0`、比分串解析（`2:1` /
  `2:1（点球 4:2）`）、`tour_status=finished if result else pending`、
  `tour_match_id` 重复保留最小 id；丢弃 window_seq/attendance/ticket_revenue/commercial/broadcast/kickoff_source；
  删除 16 张旧表（market_offers、event_choices、events_log、window_summaries、window_fans_log、
  revenue_transactions、club_balance、naming_rights、brand_pool、market_rounds、event_pool、
  stadium_facilities、venue_bookings、influence_history、season_names、round_names）。
- **升级前务必备份**（脚本自动 `VACUUM INTO` 或手动拷贝库文件）；`version < 12` 需先升到 2.14；
  `version > 15` 或非法值直接报错，不再静默改写。
- 迁移后核对：`schema_version=15`、表数=5、`season=9`、`tournament_id` 分布 66/40/30。

## 五、三张图

- **轮次预告图** `preview_s{N}_{赛事}_r{R}.png`：本地 matches+stadium，不调 club。
- **轮次统计图** `round_s{N}_{赛事}_r{R}.png`：本地已赛行 + club 上座映射，缺失 `—` 不丢行。
- **赛季走势图** `season_s{N}_{赛事}.png`：积分折线（每队）× 场均上座柱双轴；仅顶级/次级。

图表字体：优先 `chart_font_path` → 插件 `fonts/` 内置 → 系统字体 → 目录扫描。

## 六、开发与测试

```
python -m tests.run_all        # 全部测试（8 模块，全绿基线）
python -m tests.s8_charts      # 单模块
```

测试以 stub 桩替代 astrbot 运行时（`tests/stubs.py`），不依赖 AstrBot 进程；
TestEnv 只装配 4 个服务（TourSync/Weather/Backup/Chart）+ 临时 v15 库。

## 七、从 2.x 升级（破坏性变更）

- 命令 **40+ → 8**：赛程导入/赛果录入/推进/结算/事件/品牌/市场/冠名/档期/财务/设施/发放/
  改名/赛季命名/配置/管理等全部下线；账本与经营数据留在旧库备份中，不再迁移。
- 数据库 **schema 15**：删 16 张表（见上）；升级前务必备份。
- 配置 **84 → 11 键**：`club_sync_season`/`tour_sync_season` 及全部经营参数删除；
  赛季号由 club 当前赛季驱动，不再需要赛季换算配置。
- 天气推送**不再订正旧账**：上座/票房结算全部在 club 侧。
