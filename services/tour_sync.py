"""tour 全量同步引擎（v3）：赛程/赛果/leg/点球/弃权 以 tour 为唯一真源。

数据流（设计 §5）：
- club /api/seasons/current 给出当前赛季与三个 tournamentId（club_sync.fetch_current_season）；
- 逐赛事 GET /api/public/tournaments/{id}（stages）与 /{id}/matches（每赛事 2 个 GET）；
- stage offset 按阶段顺序累计前面各阶段的实际最大轮次（淘汰赛同样有偏移，不再丢弃）；
- 行映射：两回合每 leg 一行（leg 空按 0，进唯一键）；CPU 后缀归一后匹配、展示保留
  tour 原名；note 含「轮空」不落库；对手未定/占位不落库并分类计入 skipped；
- 匹配写入：tour_match_id 优先，归一队名唯一键兜底；未排期行也补建（时刻为空）；
  主客与本地相反不猜（记 conflicts，不覆盖不补建）；每赛事独立事务，单赛事失败
  不影响其他（status=partial）；
- tour 未排期（week/day/time 为 NULL）不覆盖本地时刻（CSV 导入等本地快照保留）；
- 结果：只 finished 落终局比分；live 仅更新 tour_status；result 主队视角 W/D/L/C；
  小组/联赛点球仍按平局（pen 只展示），淘汰赛点球定胜负；walkoverSide → result='C'、
  score='弃权'；tour 改判时 finished 每次覆盖本地比分/结果；两回合不本地算总比分；
- 本地 round_no（= stage offset + tour.round）与 club 定位键（tour stage 内 m.round）
  的换算表随同步落 kv（tour_round_map），天气推送/上座拉取按换算后的轮次定位。
"""

import json
import re
from datetime import datetime

from astrbot.api import logger

PUBLIC_TOURNAMENT = "/api/public/tournaments"
_TIMEOUT_SECONDS = 15.0

# 本地 round_no → tour stage 内轮次 的换算表（plugin_config kv；JSON 键一律字符串）
TOUR_ROUND_MAP_KEY = "tour_round_map"

# 本地 competition 名（club 当前赛季映射给出，此处仅作展示域校验）
COMPETITIONS = ("顶级联赛", "次级联赛", "冠军杯")

_CPU_RE = re.compile(r"[（(]\s*cpu\s*[)）]", re.IGNORECASE)

# 同步行展示字段（比对与写库共用；比对时排除 synced_at 本身）
_SYNC_FIELDS = (
    "tournament_id", "round_no", "leg", "stage_name", "home_team", "away_team",
    "home_score", "away_score", "pen_home", "pen_away", "result", "score",
    "tour_status", "walkover_side", "week_no", "day_no", "match_time",
    "tour_match_id",
)


class TourSyncError(Exception):
    pass


def normalize_team(name) -> str:
    """队名归一：去掉 (CPU)/(（CPU）) 后缀与多余空白（tour 部分队名带后缀）。"""
    s = _CPU_RE.sub("", str(name or ""))
    return re.sub(r"\s+", " ", s).strip()


def stage_offsets(stages: list, matches: list) -> dict:
    """按 stage 顺序累计前面各阶段的实际最大轮次：stageId → offset。

    round_no = offset + tour.round。次级 base→final→playoff 的 offset 为
    0/14/20（base 实际 14 轮、final 6 轮），冠军杯 group→elim 为 0/6，
    顶级单阶段为 0；淘汰赛同样获得偏移（v3 起淘汰赛不再整段丢弃）。
    """
    max_round: dict = {}
    for m in matches or []:
        sid = m.get("stageId")
        r = m.get("round")
        if sid is None or r is None:
            continue
        try:
            r = int(r)
        except (TypeError, ValueError):
            continue
        max_round[sid] = max(max_round.get(sid, 0), r)

    offsets: dict = {}
    acc = 0
    for s in sorted(stages or [], key=lambda x: (x.get("sortOrder") or 0)):
        sid = s.get("id")
        if sid is not None:
            offsets[sid] = acc
        acc += max_round.get(sid, 0)
    return offsets


def final_result(m: dict, is_elim: bool) -> dict:
    """tour 一行 → 比分/结果/状态落库值（纯函数）。

    - 未完赛（pending/live）：比分与 result 不落，tour_status 原样（live 仅更新状态）；
    - 弃权（walkoverSide home/away/both）：result='C'、score='弃权'、比分不落；
    - finished：round_robin/group 点球仍按平局（pen 只展示），elim 点球定胜负；
    - score 展示串：'2:1' / '2:1（点球 4:2）' / '弃权'。
    """
    status = str(m.get("status") or "pending")
    tour_status = status if status in ("pending", "live", "finished") else "pending"
    out = {
        "home_score": None, "away_score": None, "pen_home": None, "pen_away": None,
        "result": None, "score": None, "tour_status": tour_status,
    }
    if tour_status != "finished":
        return out
    walkover = m.get("walkoverSide")
    if walkover in ("home", "away", "both"):
        out.update({"result": "C", "score": "弃权", "walkover_side": walkover})
        return out
    h, a = m.get("scoreHome"), m.get("scoreAway")
    if h is None or a is None:
        # 数据异常（完赛无比分）：不猜，保留 finished 状态与空比分
        return out
    h, a = int(h), int(a)
    ph = int(m["penHome"]) if m.get("penHome") is not None else None
    pa = int(m["penAway"]) if m.get("penAway") is not None else None
    if is_elim and ph is not None and pa is not None:
        result = "W" if ph > pa else ("L" if ph < pa else "D")
    else:
        result = "W" if h > a else ("L" if h < a else "D")
    text = f"{h}:{a}"
    if ph is not None and pa is not None:
        text += f"（点球 {ph}:{pa}）"
    out.update({
        "home_score": h, "away_score": a, "pen_home": ph, "pen_away": pa,
        "result": result, "score": text,
    })
    return out


def map_tournament_rows(tournament_id: int, competition: str, stages: list, matches: list) -> dict:
    """tour 一个赛事的 matches → 待落库行 + 跳过分类（轮空/对手未定/淘汰赛未定）。

    未排时刻的行也返回（week/day/time 为 None，tour 是赛程结构真源，本地缺行要补建）。
    round_map：{round_no: tour stage 内轮次}——club weather-sync / fixtures 按
    tour 的 m.round 定位，推送/拉取需用换算后的轮次（设计 §5.2 offset 的逆映射）。
    """
    offsets = stage_offsets(stages, matches)
    elim_ids = {s.get("id") for s in stages or [] if s.get("kind") == "elim"}
    rows, skipped = [], {"轮空": 0, "对手未定": 0, "淘汰赛未定": 0}
    round_map: dict = {}
    for m in matches or []:
        sid = m.get("stageId")
        r = m.get("round")
        if sid is None or sid not in offsets or r is None:
            # 阶段/轮次缺失的异常行：无处挂靠，不落库
            skipped["对手未定"] += 1
            continue
        note = str(m.get("note") or "")
        if "轮空" in note:
            skipped["轮空"] += 1
            continue
        home = m.get("homeTeamName")
        away = m.get("awayTeamName")
        if m.get("homePlaceholder") or m.get("awayPlaceholder"):
            skipped["淘汰赛未定"] += 1
            continue
        if not home or not away:
            skipped["对手未定"] += 1
            continue
        week, day, mtime = m.get("kickoffWeek"), m.get("kickoffDay"), m.get("kickoffTime")
        scheduled = week is not None and day is not None and mtime
        leg = int(m["leg"]) if m.get("leg") is not None else 0
        res = final_result(m, is_elim=sid in elim_ids)
        walkover = m.get("walkoverSide")
        round_no = int(r) + offsets[sid]
        round_map[round_no] = int(r)
        rows.append({
            "tour_match_id": int(m["id"]),
            "tournament_id": int(tournament_id),
            "competition": competition,
            "round_no": round_no,
            "leg": leg,
            "stage_name": str(m.get("stageName") or ""),
            "home_team": str(home).strip(),
            "away_team": str(away).strip(),
            "home_score": res["home_score"],
            "away_score": res["away_score"],
            "pen_home": res["pen_home"],
            "pen_away": res["pen_away"],
            "result": res["result"],
            "score": res["score"],
            "tour_status": res["tour_status"],
            "walkover_side": walkover if walkover in ("home", "away", "both") else None,
            "week_no": int(week) if scheduled else None,
            "day_no": int(day) if scheduled else None,
            "match_time": str(mtime) if scheduled else None,
            "scheduled": scheduled,
        })
    return {"rows": rows, "skipped": skipped, "round_map": round_map}


def _sync_equal(local, row: dict) -> bool:
    """本地行与 tour 行的展示字段是否一致（队名按原文比对，CPU 改名视为变化）。

    week/day/time 三项在 tour 未排期（NULL）时不比对：本地快照（CSV 导入等）
    保留，不算差异——否则每次同步都会虚报 update 且被 COALESCE 吞掉。
    """
    for f in _SYNC_FIELDS:
        if f in ("week_no", "day_no", "match_time") and row.get(f) is None:
            continue
        if local[f] != row[f]:
            return False
    return True


def plan_sync(rows: list, local_rows: list) -> dict:
    """比对 tour 行与本地行 → 写操作计划（纯函数，不碰库）。

    匹配：tour_match_id 优先；无 id 用归一队名唯一键
    (tournament_id, round_no, leg, 归一主队, 归一客队) 兜底。
    命中且字段一致 → unchanged；命中有差异 → update op（含 match_id）；
    主客与本地相反（同轮同 leg 归一队名对调命中）→ conflicts，不覆盖不补建；
    未命中 → insert op；本地行未被任何 tour 行匹配 → missing_tour。
    """
    by_tmid: dict = {}
    by_key: dict = {}
    for raw in local_rows:
        row = dict(raw)
        tmid = row.get("tour_match_id")
        if tmid is not None:
            by_tmid.setdefault(int(tmid), row)
        key = (
            int(row["tournament_id"]), int(row["round_no"]), int(row["leg"]),
            normalize_team(row["home_team"]), normalize_team(row["away_team"]),
        )
        by_key.setdefault(key, row)

    ops: list = []
    unchanged = 0
    pending = 0
    conflicts: list = []
    matched_ids: set = set()
    for t in rows:
        if not t.get("scheduled"):
            pending += 1
        row = by_tmid.get(t["tour_match_id"])
        if row is None:
            row = by_key.get((
                t["tournament_id"], t["round_no"], t["leg"],
                normalize_team(t["home_team"]), normalize_team(t["away_team"]),
            ))
        if row is None:
            # 主客反不猜：同轮同 leg 主客对调能对上 → 记冲突，不覆盖也不补建
            reversed_row = by_key.get((
                t["tournament_id"], t["round_no"], t["leg"],
                normalize_team(t["away_team"]), normalize_team(t["home_team"]),
            ))
            if reversed_row is not None:
                matched_ids.add(int(reversed_row["id"]))
                conflicts.append({
                    "competition": t["competition"], "round_no": t["round_no"],
                    "leg": t["leg"], "home_team": t["home_team"],
                    "away_team": t["away_team"],
                    "reason": "主客与本地相反，未覆盖",
                })
                continue
            ops.append({"kind": "insert", "row": t})
            continue
        matched_ids.add(int(row["id"]))
        if _sync_equal(row, t):
            unchanged += 1
        else:
            ops.append({"kind": "update", "id": int(row["id"]), "row": t})

    missing_tour = [
        {
            "competition": r["competition"], "round_no": r["round_no"],
            "leg": r["leg"], "home_team": r["home_team"], "away_team": r["away_team"],
        }
        for rid, r in ((int(x["id"]), dict(x)) for x in local_rows)
        if rid not in matched_ids
    ]
    return {
        "ops": ops, "unchanged": unchanged, "pending": pending,
        "missing_tour": missing_tour, "conflicts": conflicts,
    }


async def load_round_map(dao) -> dict:
    """读 kv 换算表 → {tournament_id: {round_no: tour stage 内轮次}}；无表/坏表返回 {}。"""
    try:
        raw = await dao.get_config(TOUR_ROUND_MAP_KEY)
        data = json.loads(raw) if raw else None
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for tid, rounds in data.items():
        try:
            tid_int = int(tid)
        except (TypeError, ValueError):
            continue
        if isinstance(rounds, dict):
            mapping = {}
            for rnd, tour_rnd in rounds.items():
                try:
                    mapping[int(rnd)] = int(tour_rnd)
                except (TypeError, ValueError):
                    continue
            out[tid_int] = mapping
    return out


def effective_round(round_map: dict, tournament_id: int, round_no: int) -> int:
    """本地 round_no（带 stage offset）→ club 定位用的 tour stage 内轮次。

    无换算表/该轮不在表内（v2 迁移行等）按原值返回：顶级 1–22 与冠军杯小组
    1–6 本就与 tour 轮次对齐，回落即正确。
    """
    try:
        tid_map = round_map.get(int(tournament_id)) or {}
        return int(tid_map.get(int(round_no), round_no))
    except (TypeError, ValueError):
        return int(round_no)


def local_rows_for_tournament(local_rows: list, tournament_id: int) -> list:
    """只保留指定赛事的本地行；同步报告按赛事独立比对，避免跨赛事虚报 missing_tour。"""
    tid = int(tournament_id)
    return [r for r in local_rows if int(r["tournament_id"] or 0) == tid]


def _synced_at() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class TourSyncClient:
    """tour 公开接口客户端。base 为空时 enabled=False（旁路，不发请求）。"""

    def __init__(self, cfg: dict):
        self._base = str(cfg.get("tour_api_base", "") or "").strip().rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self._base)

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

    async def fetch_tournament(self, session, tournament_id: int) -> tuple[dict, list]:
        """拉一个赛事的详情（stages）与全部 matches；返回 (detail, matches)。"""
        detail = await self._get(session, f"{PUBLIC_TOURNAMENT}/{tournament_id}")
        data = await self._get(session, f"{PUBLIC_TOURNAMENT}/{tournament_id}/matches")
        return detail, data.get("matches") or []


class TourSyncService:
    """tour 全量同步进本地 matches。失败不抛，返回 status 供命令层展示。"""

    def __init__(self, dao, cfg):
        self._dao = dao
        self._cfg = cfg

    async def sync(self) -> dict:
        """club 当前赛季驱动三赛事全量同步。手动触发，幂等，不加 cron。"""
        client = TourSyncClient(self._cfg)
        if not client.enabled:
            return {"status": "bypass", "reason": "未配置 tour_api_base"}

        from .club_sync import ClubSyncClient, CurrentSeasonError
        club = ClubSyncClient(self._cfg)
        try:
            current = await club.fetch_current_season(self._dao)
        except CurrentSeasonError as e:
            return {"status": "error", "error": str(e)}
        tournaments = current.get("tournaments") or []
        if not tournaments:
            return {
                "status": "error",
                "error": "无法确定当前赛季的赛事列表（club 未返回赛事映射，仅本地赛季）",
            }
        season = int(current["season"])
        label = current["season_label"]

        try:
            import aiohttp
        except ImportError:
            return {"status": "error", "error": "缺少 aiohttp 依赖，无法同步（pip install aiohttp）"}

        local_rows = await self._dao.list_matches_by_season(season)
        synced_at = _synced_at()
        fetched = created = updated = unchanged = pending_total = 0
        skipped: dict = {}
        missing_tour: list = []
        conflicts: list = []
        per_tournament: list = []
        errors: list = []
        # 轮次换算表：保留上次成功映射（单赛事本次失败时其旧映射仍可用），成功者覆盖
        round_maps: dict = await load_round_map(self._dao)
        rounds_dirty = False

        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for t in tournaments:
                tid, comp = t["tournament_id"], t["competition"]
                entry = {"competition": comp, "tournament_id": tid}
                try:
                    detail, matches = await client.fetch_tournament(session, tid)
                    mapped = map_tournament_rows(tid, comp, detail.get("stages") or [], matches)
                    local_for_tid = local_rows_for_tournament(local_rows, tid)
                    plan = plan_sync(mapped["rows"], local_for_tid)
                    for op in plan["ops"]:
                        op["row"]["synced_at"] = synced_at
                    counts = await self._dao.apply_match_sync_ops(plan["ops"], season)
                    fetched += len(mapped["rows"])
                    created += counts["created"]
                    updated += counts["updated"]
                    unchanged += plan["unchanged"]
                    pending_total += plan["pending"]
                    for k, v in mapped["skipped"].items():
                        skipped[k] = skipped.get(k, 0) + v
                    missing_tour.extend(plan["missing_tour"])
                    conflicts.extend(plan["conflicts"])
                    if mapped["round_map"]:
                        round_maps[int(tid)] = mapped["round_map"]
                        rounds_dirty = True
                    entry.update({
                        "status": "ok", "fetched": len(mapped["rows"]),
                        "created": counts["created"], "updated": counts["updated"],
                        "unchanged": plan["unchanged"],
                    })
                except Exception as e:  # 单赛事失败不影响其他赛事（partial）
                    logger.error(f"tour 同步 {comp} 失败: {type(e).__name__}: {e}")
                    errors.append(f"{comp}（tournamentId={tid}）：{type(e).__name__}: {e}")
                    entry.update({"status": "error", "error": str(e)})
                per_tournament.append(entry)

        if rounds_dirty:
            # JSON 键只能为字符串：{tid: {round_no: tour_round}} 落 kv（int↔str 由读写两侧转换）
            serialized = {
                str(tid): {str(rnd): tr for rnd, tr in rounds.items()}
                for tid, rounds in round_maps.items()
            }
            try:
                await self._dao.set_config(
                    TOUR_ROUND_MAP_KEY,
                    json.dumps(serialized, ensure_ascii=False, separators=(",", ":")),
                )
            except Exception as e:  # kv 写失败不影响同步结果（换算表回落原值）
                logger.warning(f"tour_round_map kv 写入失败: {type(e).__name__}: {e}")

        if errors and len(errors) >= len(tournaments):
            status = "error"
        elif errors:
            status = "partial"
        else:
            status = "ok"
        return {
            "status": status,
            "season": season,
            "season_label": label,
            "fetched": fetched,
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "pending": pending_total,
            "skipped": skipped,
            "missing_tour": missing_tour,
            "conflicts": conflicts,
            "tournaments": per_tournament,
            "errors": errors,
        }
