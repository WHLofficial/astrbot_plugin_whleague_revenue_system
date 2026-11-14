"""赛程时刻同步：从 tour 公开接口只读拉取开球时刻，写回本地快照。

方向固定为 tour → 插件。tour 是时刻的唯一真源，插件不再接受人工录入时刻
（`/主场赛程导入` 的周/天/时间字段仍解析，但直接丢弃），本地只保留一份副本，
断网或 tour 未排期时预报图仍可用上次快照出图。

轮次映射（插件轮次 → tour 轮次）：
- 顶级联赛：一一对应；
- 次级联赛：预赛阶段一一对应，决赛阶段（冲刺/挑战组）叠加预赛的轮次数
  （S9 是 14 ⇒ 插件 15–20 = tour 1–6）；
- 冠军杯：小组赛一一对应；淘汰赛暂不同步（两回合共用同一轮次号，本地无 leg 列）。
"""

import re

from astrbot.api import logger

PUBLIC_TOURNAMENTS = "/api/public/tournaments"
_TIMEOUT_SECONDS = 15.0

# 赛事名前缀 → 本地 competition 名（插件 matches.competition 的取值）
COMPETITIONS = ("顶级联赛", "次级联赛", "冠军杯")

_CPU_RE = re.compile(r"[（(]\s*cpu\s*[)）]", re.IGNORECASE)


class TourSyncError(Exception):
    pass


def normalize_team(name) -> str:
    """队名归一：去掉 (CPU)/(（CPU）) 后缀与多余空白（tour 部分队名带后缀）。"""
    s = _CPU_RE.sub("", str(name or ""))
    return re.sub(r"\s+", " ", s).strip()


def competition_of(tournament_name) -> str | None:
    """从 tour 赛事名（如「S9 | 顶级联赛」）认出本地赛事名；认不出返回 None。"""
    name = str(tournament_name or "")
    for comp in COMPETITIONS:
        if comp in name:
            return comp
    return None


def stage_offsets(stages: list, matches: list) -> dict:
    """算每个 stage 的轮次偏移：stageId → 偏移（None = 该阶段不同步）。

    偏移只对「决赛阶段」有意义：它要叠加基础阶段的轮次数，才能和插件轮次对齐。
    淘汰赛返回 None（两回合同轮次号，本地没有 leg 列可区分）。
    """
    def cfg_of(stage):
        cfg = stage.get("config") or {}
        return cfg if isinstance(cfg, dict) else {}

    base_rounds = 0
    for stage in stages:
        if stage.get("kind") == "round_robin" and not cfg_of(stage).get("source"):
            rounds = [int(m["round"]) for m in matches
                      if m.get("stageId") == stage.get("id") and m.get("round") is not None]
            base_rounds = max(rounds, default=0)

    out = {}
    for stage in stages:
        sid = stage.get("id")
        kind = stage.get("kind")
        if kind == "elim":
            out[sid] = None
        elif kind == "round_robin" and cfg_of(stage).get("source"):
            out[sid] = base_rounds
        else:
            out[sid] = 0
    return out


def map_tour_matches(tournament_name, stages, matches) -> dict:
    """tour 的一个赛事 → 待同步行 + 跳过原因统计。

    未排时刻的联赛场次也会返回（week/day/time 为 None）：tour 是赛程结构真源，
    本地缺行时要能补建，只是暂无时刻可写。
    """
    competition = competition_of(tournament_name)
    if competition is None:
        raise TourSyncError(f"认不出赛事「{tournament_name}」对应本地哪一项（需含 顶级联赛/次级联赛/冠军杯）")
    offsets = stage_offsets(stages, matches)
    rows, skipped = [], {"淘汰赛": 0, "未排时刻": 0, "对手未定": 0, "未知阶段": 0}
    for m in matches:
        offset = offsets.get(m.get("stageId"))
        if offset is None:
            skipped["淘汰赛"] += 1
            continue
        if offset == "?":
            skipped["未知阶段"] += 1
            continue
        home = normalize_team(m.get("homeTeamName"))
        away = normalize_team(m.get("awayTeamName"))
        if not home or not away:
            skipped["对手未定"] += 1
            continue
        week, day, time = m.get("kickoffWeek"), m.get("kickoffDay"), m.get("kickoffTime")
        scheduled = bool(week and day and time)
        if not scheduled:
            skipped["未排时刻"] += 1
        rows.append({
            "competition": competition,
            "round_no": int(m["round"]) + int(offset),
            "home": home,
            "away": away,
            "week": int(week) if scheduled else None,
            "day": int(day) if scheduled else None,
            "time": str(time) if scheduled else None,
            "scheduled": scheduled,
            "tour_match_id": int(m["id"]),
            "status": m.get("status"),
        })
    return {"competition": competition, "rows": rows, "skipped": skipped}


def _key(competition, round_no, home, away) -> tuple:
    return (str(competition), int(round_no), normalize_team(home), normalize_team(away))


def plan_kickoff_sync(tour_rows: list, local_rows: list) -> dict:
    """比对 tour 行与本地行，给出要写的更新与各类落空清单（纯函数，不碰库）。

    已完赛/已有结果的场次不覆盖时刻（记 conflict），主客相反也不猜（记 conflict）。
    tour 尚未排期的场次只记 pending，不写 NULL 覆盖本地快照。
    """
    local = {}
    for raw in local_rows:
        row = dict(raw)  # sqlite3.Row 没有 .get
        local[_key(row["competition"], row["round_no"], row["home_team"], row["away_team"])] = row

    updates, unchanged, missing_local, conflicts, pending = [], [], [], [], []
    seen = set()
    for t in tour_rows:
        key = _key(t["competition"], t["round_no"], t["home"], t["away"])
        row = local.get(key)
        if row is None:
            reversed_key = _key(t["competition"], t["round_no"], t["away"], t["home"])
            if reversed_key in local:
                seen.add(reversed_key)
                conflicts.append({**t, "reason": "主客与本地相反，未覆盖"})
            else:
                missing_local.append(t)
            continue
        seen.add(key)
        if not t.get("scheduled", t.get("time") is not None):
            pending.append(t)
            continue
        same = (row.get("week_no") == t["week"] and row.get("day_no") == t["day"]
                and row.get("match_time") == t["time"])
        if row.get("result"):
            if not same:
                conflicts.append({**t, "reason": f"已完赛（{row['result']}），时刻不覆盖"})
            continue
        if same and row.get("tour_match_id") == t["tour_match_id"]:
            unchanged.append(t)
            continue
        updates.append({**t, "match_id": row["id"]})

    missing_tour = [
        {"competition": r["competition"], "round_no": r["round_no"],
         "home_team": r["home_team"], "away_team": r["away_team"]}
        for key, r in local.items() if key not in seen
    ]
    return {
        "updates": updates,
        "unchanged": unchanged,
        "missing_local": missing_local,
        "missing_tour": missing_tour,
        "conflicts": conflicts,
        "pending": pending,
    }


class TourSyncClient:
    """tour 公开接口客户端。base 为空时 enabled=False（旁路，不发请求）。"""

    def __init__(self, cfg: dict):
        self._base = str(cfg.get("tour_api_base", "") or "").strip().rstrip("/")
        self._season = str(cfg.get("tour_sync_season", "") or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self._base)

    @property
    def configured_season(self) -> str:
        return self._season

    async def _get(self, session, path: str):
        url = f"{self._base}{path}"
        async with session.get(url) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise TourSyncError(f"GET {path} 失败：HTTP {resp.status} {text[:160]}")
            try:
                import json
                return json.loads(text)
            except ValueError:
                raise TourSyncError(f"GET {path} 返回的不是 JSON")

    async def fetch_rows(self, season_label: str) -> dict:
        """拉本赛季三项赛事的全部时刻行；返回 {rows, tournaments, skipped}。"""
        try:
            import aiohttp
        except ImportError:
            raise TourSyncError("缺少 aiohttp 依赖，无法同步（pip install aiohttp）")

        label = season_label or self._season
        if not label:
            raise TourSyncError("赛季未命名，且未配置 tour_sync_season，无法确定要同步 S 几")

        rows, tournaments, skipped = [], [], {}
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            payload = await self._get(session, PUBLIC_TOURNAMENTS)
            for t in payload.get("tournaments", []):
                name = str(t.get("name") or "")
                if label not in name:
                    continue
                if competition_of(name) is None:
                    skipped[name] = "赛事名无法映射"
                    continue
                detail = await self._get(session, f"/api/public/tournaments/{t['id']}")
                data = await self._get(session, f"/api/public/tournaments/{t['id']}/matches")
                mapped = map_tour_matches(name, detail.get("stages") or [], data.get("matches") or [])
                rows.extend(mapped["rows"])
                tournaments.append(name)
                skipped.update({f"{name}·{k}": v for k, v in mapped["skipped"].items() if v})
        if not tournaments:
            raise TourSyncError(f"tour 上没有找到名字含「{label}」的赛事")
        return {"rows": rows, "tournaments": tournaments, "skipped": skipped}


class TourSyncService:
    """把 tour 时刻同步进本地快照。失败不抛，返回 status=error 供命令层展示。"""

    def __init__(self, dao, cfg):
        self._dao = dao
        self._cfg = cfg

    async def sync(self, season: int) -> dict:
        client = TourSyncClient(self._cfg)
        if not client.enabled:
            return {"status": "bypass", "reason": "未配置 tour_api_base"}
        try:
            label = client.configured_season or await self._dao.get_season_name(season) or ""
            fetched = await client.fetch_rows(label)
            local = await self._dao.list_matches_by_season(season)
            plan = plan_kickoff_sync(fetched["rows"], local)
            created = await self._create_missing(season, plan["missing_local"])
            if created:
                # 新建行没有 id/tour 绑定，重算一次计划把它们的时刻一并写进去
                local = await self._dao.list_matches_by_season(season)
                plan = plan_kickoff_sync(fetched["rows"], local)
            for u in plan["updates"]:
                await self._dao.set_match_kickoff(
                    u["match_id"], u["week"], u["day"], u["time"], u["tour_match_id"], "tour",
                )
            return {
                "status": "ok",
                "season_label": label,
                "tournaments": fetched["tournaments"],
                "fetched": len(fetched["rows"]),
                "created": created,
                "updated": len(plan["updates"]),
                "unchanged": len(plan["unchanged"]),
                "missing_local": plan["missing_local"],
                "missing_tour": plan["missing_tour"],
                "conflicts": plan["conflicts"],
                "pending": plan["pending"],
                "skipped": fetched["skipped"],
            }
        except TourSyncError as e:
            return {"status": "error", "error": str(e)}
        except Exception as e:  # 网络/解析等一律兜住，命令层给中文提示
            logger.error(f"tour 时刻同步失败: {type(e).__name__}: {e}")
            return {"status": "error", "error": f"同步失败：{type(e).__name__}: {e}"}

    async def _create_missing(self, season: int, rows: list) -> int:
        """tour 有、本地没有的联赛场次补建行（含未排时刻的），返回实际新建数。

        用当前窗口号落库：新行属于当前结算窗口；重复跑由 matches 唯一键幂等。
        """
        if not rows:
            return 0
        state = await self._dao.get_league_state()
        window_seq = int(state["window_seq"]) if state else 1
        created = 0
        for t in rows:
            created += await self._dao.add_match(
                season, window_seq, t["round_no"], t["home"], t["away"], t["competition"],
            )
        return created
