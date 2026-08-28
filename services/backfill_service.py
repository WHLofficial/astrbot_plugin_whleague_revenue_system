"""主场统一补差（v2.8.0）：战绩系数修正 + 死忠重定基，一条命令完成。

战绩成分（延续 v2.5.1）：修复「近 3 场不足 3 场未中性化」的历史数据——
旧码在历史场次 1~2 场时直接按实际积分查表，只有 0 场才兜底中性 4 分；
新规则改为「非取消场次不足 3 场一律按中性 4 分」（formula.effective_form_pts）。
- 上座与票房：对本赛季每个主场回放录入时的球队全史非取消主场数 n，
  n∈{1,2} 才受影响（n=0 旧码本就中性，n≥3 新旧一致）；
  旧分按 0 场兜底 4 / 否则实际积分复现，上座按 新系数/旧系数 等比缩放并钳到现容量，
  票务与商业收入改按实际入库上座等比缩放（v2.7.0 按系数缩放，在容量钳死时
  会出现上座不变而收入增加的谬账，v2.8.0 顺修）；转播收入与上座无关不动。
  注：容量钳制用现值而非当年值——设施升级只影响 broadcast 及账外的建设券，
  对 ticket/commercial 无影响，此处差异可忽略。

满座微降（v2.9.2 新增）：满座封顶从「恰好=容量」改为容量×random(0.985, 0.999)
（formula.SELL_OUT_FILL，每场独立随机），历史恰好=现容量的已录非取消场次
（任意赛季）统一回修：上座降至容量×random(0.985, 0.999)，票务与商业收入
按新上座等比缩放，转播不动。口径限制：只识别「上座==现容量」的满座，
扩容前的旧满座不在列（与战绩补差用现容量钳制同口径）；战绩修正后恰好
钳到容量的场次一并微降，修正后低于容量的场次视为不再满座、不重复处理。

死忠重定基（v2.8.0 新增）：死忠目标从「影响力×20」线性改为饱和阶梯
（formula.fans_target_table 分段线性），历史死忠由旧线性规则演化而来，
逐队一次性重定基到 round(新目标)：与现值差 <1 的跳过，≥1 的直接改写
stadium.fans_diehards（不动流水，不追算中间窗口）。

兼容：v2.7.0 的战绩补差标记（LEGACY_MARKER_KEY）视为战绩成分已完成，
本次跳过战绩只做死忠重定基；旧标记是补差流水口径的关键——scan 无法凭
库内数值区分「旧规则录入」与「新规则录入」的 n∈{1,2} 场次，伪阳性的
排除依赖部署时序（部署后、下一次窗口结算前执行）。

幂等（v2.9.3 改为成分级）：战绩成分只要任一补差标记存在即跳过，任何模式
（含强制）都不重复执行——重跑会二次缩放，且部署后新规则录入的赛季初
n∈{1,2} 场次全是伪阳性；死忠重定基天然幂等，每次执行都重算（差 <1 跳过）；
满座微降普通模式只捞「上座==现容量」的未处理旧满座，强制模式（force）
重抽所有处于容量×(0.985~0.999) 区间的场次（含已微降过的）。落库后写入
统一标记（MARKER_KEY，含 forced 标志）；已执行过且三成分均无待处理项时
apply 拒绝执行。
"""

from __future__ import annotations

import json
import random

from . import formula
from .formula import NEUTRAL_FORM_PTS, SELL_OUT_FILL

MARKER_KEY = "backfill_home_unified_done"
LEGACY_MARKER_KEY = "backfill_form_neutral_done"


class BackfillError(Exception):
    pass


class BackfillService:
    def __init__(self, db, dao, cfg):
        self._db = db
        self._dao = dao
        self._cfg = cfg

    # ─── 预览（只读） ─────────────────────────────────────

    async def scan(self, force: bool = False) -> dict:
        state = await self._dao.get_league_state()
        if state is None:
            raise BackfillError("联赛尚未初始化，没有可补差的数据")
        season = int(state["season_number"])

        marker = await self._dao.get_config(MARKER_KEY)
        # 任一标记存在 → 战绩成分已完成，任何模式（含强制）都跳过
        form_done = marker is not None or (
            await self._dao.get_config(LEGACY_MARKER_KEY) is not None
        )

        capacities: dict[str, int] = {}
        fans_now: dict[str, float] = {}
        influences: dict[str, float] = {}
        for s in await self._dao.list_stadiums():
            capacities[s["team_name"]] = s["capacity"]
            fans_now[s["team_name"]] = s["fans_diehards"]
            influences[s["team_name"]] = s["influence"]

        affected: list[dict] = []
        sellouts: list[dict] = []
        team_totals: dict[str, dict] = {}
        for team in sorted(capacities):
            hist = [
                m for m in await self._dao.get_home_matches_all(team)
                if str(m["result"]).strip().upper() != "C"
            ]
            priors: list[str] = []
            for m in hist:
                n = len(priors)
                if (
                    not form_done
                    and int(m["season_number"]) == season
                    and 0 < n < 3
                    and m["attendance"] is not None
                ):
                    old_pts = _legacy_form_pts(n, priors)
                    if old_pts != NEUTRAL_FORM_PTS:
                        affected.append(
                            self._scale(m, team, n, old_pts,
                                        capacities.get(team))
                        )
                # 满座微降：普通模式只捞恰好=现容量的未处理旧满座；
                # 强制模式重抽满座区间内全部场次（含已微降过的）
                if m["attendance"] is not None and capacities.get(team):
                    att = int(m["attendance"])
                    cap = capacities[team]
                    if force:
                        hit = int(cap * SELL_OUT_FILL[0]) <= att <= cap
                    else:
                        hit = att == cap
                    if hit:
                        sellouts.append({
                            "id": m["id"], "season": int(m["season_number"]),
                            "window_seq": m["window_seq"], "round_no": m["round_no"],
                            "competition": m["competition"], "home": team,
                            "away": m["away_team"],
                            "att_old": att,
                            "capacity": cap,
                        })
                priors.append(str(m["result"]).strip().upper())

            for a in (x for x in affected if x["home"] == team):
                t = team_totals.setdefault(
                    team, {"team": team, "d_attendance": 0,
                           "d_ticket": 0.0, "d_commercial": 0.0})
                t["d_attendance"] += a["d_att"]
                t["d_ticket"] += a["d_ticket"]
                t["d_commercial"] += a["d_commercial"]

        fans = []
        for team in sorted(fans_now):
            before = int(round(fans_now[team]))
            after = int(round(formula.diehard_target(
                self._cfg, influences.get(team, 0.0))))
            if abs(after - fans_now[team]) >= 1:
                fans.append({
                    "team": team, "before": before, "after": after,
                    "delta": after - before,
                })

        return {
            "done": marker is not None,
            "marker": json.loads(marker) if marker else None,
            "season": season,
            "form_done": form_done,
            "force": force,
            "affected": affected,
            "sellouts": sellouts,
            "teams": list(team_totals.values()),
            "fans": fans,
        }

    def _scale(self, m: dict, home: str, n: int, old_pts: int,
               capacity) -> dict:
        coef_old = formula.form_coef(self._cfg, old_pts)
        coef_new = formula.form_coef(self._cfg, NEUTRAL_FORM_PTS)
        ratio = coef_new / coef_old if coef_old > 0 else 1.0
        att_old = int(m["attendance"])
        att_new = int(round(att_old * ratio))
        if capacity:
            att_new = min(att_new, capacity)
        # 收入跟随实际入库上座（钳容量时 ratio 折算为 0 增量）
        rev_ratio = att_new / att_old if att_old > 0 else 0.0
        t_old = float(m["ticket_revenue"] or 0.0)
        c_old = float(m["commercial"] or 0.0)
        t_new = round(t_old * rev_ratio, 4)
        c_new = round(c_old * rev_ratio, 4)
        return {
            "id": m["id"], "season": int(m["season_number"]),
            "window_seq": m["window_seq"], "round_no": m["round_no"],
            "competition": m["competition"], "home": home,
            "away": m["away_team"],
            "n": n, "old_pts": old_pts, "coef_old": coef_old,
            "coef_new": coef_new,
            "att_old": att_old, "att_new": att_new,
            "capacity": capacity,
            "d_att": att_new - att_old,
            "t_old": t_old, "t_new": t_new,
            "c_old": c_old, "c_new": c_new,
            "d_ticket": round(t_new - t_old, 4),
            "d_commercial": round(c_new - c_old, 4),
        }

    # ─── 落库 ────────────────────────────────────────────

    async def apply(self, force: bool = False) -> dict:
        data = await self.scan(force=force)
        # 标记存在且三成分均无待处理项 → 拒绝；无标记时零数据仍可落标记
        if data["done"] and not (
            data["affected"] or data["fans"] or data["sellouts"]
        ):
            raise BackfillError(
                "主场补差已执行过且当前无待重算项（战绩成分不可重复执行）；"
                "如需重抽满座区间场次请先发 /主场补差 强制 预览"
            )

        async def work(conn):
            for a in data["affected"]:
                await conn.execute(
                    "UPDATE matches SET attendance=?, ticket_revenue=?, commercial=? WHERE id=?",
                    (a["att_new"], a["t_new"], a["c_new"], a["id"]),
                )
                note_suffix = f"第{a['round_no']}轮 主{a['home']} vs 客{a['away']}｜战绩系数补差"
                for kind, amount in (
                    ("ticket", a["d_ticket"]), ("commercial", a["d_commercial"]),
                ):
                    if amount == 0:
                        continue
                    await conn.execute(
                        "INSERT INTO revenue_transactions "
                        "(team_name, season_number, window_seq, round_no, kind, amount, note) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (a["home"], a["season"], a["window_seq"],
                         a["round_no"], kind, amount, note_suffix),
                    )
            # 满座微降：历史恰好=容量的记录 ∪ 战绩修正后恰好钳到容量的场次
            sellout_jobs = {s["id"]: s for s in data["sellouts"]}
            for a in data["affected"]:
                if a.get("capacity") and a["att_new"] == a["capacity"]:
                    sellout_jobs[a["id"]] = a
            sellout_count = 0
            for jid in sorted(sellout_jobs):
                job = sellout_jobs[jid]
                cur = await conn.execute(
                    "SELECT season_number, window_seq, round_no, attendance, "
                    "ticket_revenue, commercial FROM matches WHERE id=?",
                    (jid,),
                )
                row = await cur.fetchone()
                if row is None or row["attendance"] is None:
                    continue
                att_now = int(row["attendance"])
                cap = int(job["capacity"])
                if att_now <= 0:
                    continue
                if force:
                    if not (int(cap * SELL_OUT_FILL[0]) <= att_now <= cap):
                        continue
                elif att_now != cap:
                    continue
                new_att = int(cap * random.uniform(*SELL_OUT_FILL))
                rev_ratio = new_att / att_now
                t_now = float(row["ticket_revenue"] or 0.0)
                c_now = float(row["commercial"] or 0.0)
                t_new = round(t_now * rev_ratio, 4)
                c_new = round(c_now * rev_ratio, 4)
                await conn.execute(
                    "UPDATE matches SET attendance=?, ticket_revenue=?, commercial=? WHERE id=?",
                    (new_att, t_new, c_new, jid),
                )
                note = f"第{job['round_no']}轮 主{job['home']} vs 客{job['away']}｜满座微降"
                for kind, amount in (
                    ("ticket", round(t_new - t_now, 4)),
                    ("commercial", round(c_new - c_now, 4)),
                ):
                    if amount == 0:
                        continue
                    await conn.execute(
                        "INSERT INTO revenue_transactions "
                        "(team_name, season_number, window_seq, round_no, kind, amount, note) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (job["home"], int(row["season_number"]), row["window_seq"],
                         row["round_no"], kind, amount, note),
                    )
                sellout_count += 1
            touched = {a["home"] for a in data["affected"]} | {
                s["home"] for s in data["sellouts"]
            }
            for team in sorted(touched):
                await conn.execute(
                    "INSERT OR IGNORE INTO club_balance (team_name, balance, build_credit) VALUES (?, 0, 0)",
                    (team,),
                )
                cur = await conn.execute(
                    "SELECT COALESCE(SUM(amount), 0.0) AS s FROM revenue_transactions "
                    "WHERE team_name=? AND kind != 'credit'",
                    (team,),
                )
                row = await cur.fetchone()
                total = row["s"] if row else 0.0
                await conn.execute(
                    "UPDATE club_balance SET balance=?, updated_at=datetime('now','localtime') WHERE team_name=?",
                    (total, team),
                )
            for f in data["fans"]:
                await conn.execute(
                    "UPDATE stadium SET fans_diehards=?, updated_at=datetime('now','localtime') WHERE team_name=?",
                    (f["after"], f["team"]),
                )
            await conn.execute(
                "INSERT INTO plugin_config (key, value, updated_at) VALUES (?, ?, datetime('now','localtime')) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now','localtime')",
                (MARKER_KEY, json.dumps({
                    "executed_season": data["season"],
                    "matches": len(data["affected"]),
                    "sellouts": sellout_count,
                    "fans_teams": len(data["fans"]),
                    "form_skipped": data["form_done"],
                    "forced": force,
                }, ensure_ascii=False)),
            )

        await self._db.execute_transaction(work)
        return {"applied": True, **data}


def _legacy_form_pts(n_recorded: int, priors: list[str]) -> int:
    """复现旧录入路径的取分：仅 0 场兜底中性 4 分，1~2 场按实际积分查表。"""
    if n_recorded == 0:
        return NEUTRAL_FORM_PTS
    return formula.form_pts(priors[:n_recorded])
