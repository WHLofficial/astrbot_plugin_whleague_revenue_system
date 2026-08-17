"""窗口末结算：维护费 + 档期兑现 + 冠名费 + 死忠演化 → 账本 → 窗口摘要。

自然成本纪律：维护按容量收、票房只按实际上座，超前扩建每窗口自然流血；
无显式惩罚条目。事件在结算外由管理员独立触发。
"""

from astrbot.api import logger

from . import formula


class SettleError(Exception):
    pass


class WindowService:
    def __init__(self, db, dao, cfg, fans_service, brand_service):
        self._db = db
        self._dao = dao
        self._cfg = cfg
        self._fans_service = fans_service
        self._brand_service = brand_service

    async def settle(self, force: bool = False) -> dict:
        state = await self._dao.get_league_state()
        if not state:
            raise SettleError("赛季状态未初始化")
        season, window_seq = int(state["season_number"]), int(state["window_seq"])
        if await self._dao.has_window_summary(season, window_seq) and not force:
            raise SettleError(f"第 {season} 赛季窗口 {window_seq} 已结算（可加「强制」重算）")

        stadiums = await self._dao.list_stadiums()
        if not stadiums:
            raise SettleError("没有已建球场的球队")

        lines = []
        for s in stadiums:
            team = s["team_name"]
            await self._dao.ensure_balance(team, float(self._cfg.get("start_funds", 50.0)))
            parts = []

            # 1) 维护费（按容量收，S9 公式；场次 = 窗口内安排的主场场次）
            home_matches = await self._dao.get_home_matches_window(team, season, window_seq)
            maintenance = formula.tier_maintenance(self._cfg, s["tier"], s["capacity"], len(home_matches))
            if maintenance > 0:
                await self._dao.apply_balance(team, -maintenance)
                await self._dao.add_transaction(team, season, window_seq, "maintenance", -maintenance,
                                                note=f"半赛季维护（{len(home_matches)} 场）")
                parts.append(f"维护 −{maintenance:.2f}M")

            # 2) 档期活动兑现
            for booking in await self._dao.get_bookings(team, season, window_seq):
                facilities = await self._dao.get_facilities(team)
                act = formula.activity_income(
                    self._cfg, booking["activity_type"],
                    pitch_level=facilities.get(formula.FACILITY_PITCH, 0),
                    youth_level=facilities.get(formula.FACILITY_YOUTH, 0),
                )
                if act["income"] > 0:
                    await self._dao.apply_balance(team, act["income"])
                    await self._dao.add_transaction(team, season, window_seq, "activity", act["income"],
                                                    note=f"{booking['activity_type']}（档位{booking['slot_no']}）")
                    parts.append(f"活动 +{act['income']:.2f}M")
                if act["extra_maintenance"] > 0:
                    await self._dao.apply_balance(team, -act["extra_maintenance"])
                    await self._dao.add_transaction(team, season, window_seq, "maintenance",
                                                    -act["extra_maintenance"], note="演唱会草皮损坏")
                    parts.append(f"草皮维修 −{act['extra_maintenance']:.2f}M")

            # 3) 冠名费 + 到期/主动解约
            naming = await self._dao.get_active_naming(team)
            if naming:
                fee = naming["fee_per_window"]
                await self._dao.apply_balance(team, fee)
                await self._dao.add_transaction(team, season, window_seq, "naming", fee,
                                                note=f"{naming['brand']} 冠名费")
                parts.append(f"冠名 +{fee:.2f}M")
                await self._dao.tick_naming(team)
                await self._dao.expire_namings()

            # 4) 死忠演化（记录变动前数值，供品牌解约判断）
            fans_before = s["fans_diehards"]
            fans_details = await self._fans_service.evolve(season, window_seq)
            fans_after = fans_before
            for fd in fans_details:
                if fd["team"] == team:
                    fans_after = fd["after"]
            if fans_after != fans_before:
                delta = round(fans_after - fans_before)
                parts.append(f"死忠 {fans_before:,.0f}→{fans_after:,.0f} ({delta:+d})")
            await self._brand_service.maybe_brand_terminate(team, season, window_seq, fans_before, fans_after)

            # 5) 事件回顾
            events = await self._dao.get_window_events(team, season, window_seq)
            for ev in events:
                parts.append(f"事件·{ev['text'] or ev['event_id']}")

            balance = await self._dao.get_balance(team)
            parts.append(f"余额 {balance['balance']:.2f}M" if balance else "余额 0M")
            lines.append(f"· {team}：{'，'.join(parts) if parts else '无变动'}")

        await self._dao.add_window_summary(season, window_seq)
        return {"season": season, "window_seq": window_seq, "lines": lines}