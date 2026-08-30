"""死忠球迷演化（非对称）：涨粉看热度、掉粉看空场。

窗口结算时执行：向本窗口影响力目标靠拢，涨粉系数乘热度（0.6+0.4×上座率），
掉粉系数随空场加速（1+0.8×(1−上座率)），再叠加战绩修正（近3场 Pts）。
"""

from . import formula


class FansService:
    def __init__(self, db, dao, cfg):
        self._db = db
        self._dao = dao
        self._cfg = cfg

    async def window_attend_rate(self, team_name: str, season: int, window_seq: int,
                                 capacity: int) -> float:
        """窗口平均上座率 = 总上座 / (容量 × 已赛场次)；无场次视为中性 1.0。"""
        matches = await self._dao.get_home_matches_window(team_name, season, window_seq)
        played = [m for m in matches if m["attendance"] is not None]
        # 容量非法时无分母可言，按中性处理，避免结算中除零炸掉演化
        if not played or capacity <= 0:
            return 1.0
        total = sum(m["attendance"] for m in played)
        return total / (capacity * len(played))

    async def window_form_pts(self, team_name: str) -> int:
        """近 3 场 Pts；非取消场次不足 3 场时按中性（与赛果录入路径一致）。"""
        results = await self._dao.get_last_results(team_name, 3)
        return formula.effective_form_pts([r["result"] for r in results])

    async def evolve(self, season: int, window_seq: int) -> list[dict]:
        """对所有球场执行一次死忠演化，返回各队变动摘要。"""
        details = []
        stadiums = await self._dao.list_stadiums()
        for s in stadiums:
            team = s["team_name"]
            target = formula.diehard_target(self._cfg, s["influence"])
            attend_rate = await self.window_attend_rate(team, season, window_seq, s["capacity"])
            form_pts = await self.window_form_pts(team)
            facilities = await self._dao.get_facilities(team)
            youth_level = facilities.get(formula.FACILITY_YOUTH, 0)
            new_fans = formula.evolve_fans(
                self._cfg, fans=s["fans_diehards"], target=target,
                attend_rate=attend_rate, youth_level=youth_level, form_pts_value=form_pts,
            )
            delta = round(new_fans - s["fans_diehards"])
            if abs(delta) < 0.5:
                continue
            await self._dao.update_fans(team, new_fans)
            details.append({
                "team": team,
                "before": round(s["fans_diehards"], 1),
                "after": round(new_fans, 1),
                "delta": delta,
                "attend_rate": round(attend_rate, 3),
            })
        return details