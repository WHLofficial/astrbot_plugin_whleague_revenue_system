"""窗口末结算：维护费 + 档期兑现 + 冠名费 + 死忠演化 + 选择事件兑现 → 账本 → 窗口摘要。

自然成本纪律：维护按容量收、票房只按实际上座，超前扩建每窗口自然流血；
无显式惩罚条目。即发事件在结算外由管理员独立触发，选择型事件在结算内
统一兑现（已定按概率掷骰、未定按最差结果兜底）。
「强制」重算 = 撤销上次结算创建的流水（按 window_summaries 记录的 ID）并重算余额，
重置选择事件后随本次结算重新兑现；死忠演化是状态变更（无历史快照），重算不重复执行。
"""

import json

from astrbot.api import logger

from . import formula


class SettleError(Exception):
    pass


class WindowService:
    def __init__(self, db, dao, cfg, fans_service, brand_service, event_engine):
        self._db = db
        self._dao = dao
        self._cfg = cfg
        self._fans_service = fans_service
        self._brand_service = brand_service
        self._event_engine = event_engine

    async def settle(self, force: bool = False) -> dict:
        state = await self._dao.get_league_state()
        if not state:
            raise SettleError("赛季状态未初始化")
        season, window_seq = int(state["season_number"]), int(state["window_seq"])
        was_settled = await self._dao.has_window_summary(season, window_seq)
        if was_settled and not force:
            raise SettleError(f"第 {season} 赛季窗口 {window_seq} 已结算（可加「强制」撤销重算）")

        stadiums = await self._dao.list_stadiums()
        if not stadiums:
            raise SettleError("没有已建球场的球队")

        redo = was_settled and force
        if redo:
            # 强制重算：只撤销上次结算创建的流水（ID 记录在 window_summaries.tx_ids），
            # 再按剩余流水重算余额；赛果门票/事件/改名等非结算流水不受影响。
            summary = await self._dao.get_window_summary(season, window_seq)
            try:
                tx_ids = json.loads(summary["tx_ids"] or "[]") if summary else []
            except (ValueError, TypeError):
                tx_ids = []
            removed = await self._dao.delete_transactions_by_ids(tx_ids)
            for s in stadiums:
                await self._dao.recompute_balance(s["team_name"])
            logger.info(f"Force re-settle window {season}-{window_seq}: removed {removed} transactions.")
            # 重置该窗口已结算的选择事件，随本次结算重新兑现
            await self._dao.reset_choices_for_redo(season, window_seq)

        # 事件选择结算：已定按选项概率掷骰、未定按最差结果兜底；流水并入可撤销集合
        choice_res = await self._event_engine.settle_choices(season, window_seq)
        created_ids: list[int] = list(choice_res["tx_ids"])
        lines = []

        # 死忠演化每窗口仅执行一轮（一次调用作用于全部球队），重算时跳过
        fans_details = [] if redo else await self._fans_service.evolve(season, window_seq)
        fans_by_team = {fd["team"]: fd for fd in fans_details}
        for s in stadiums:
            team = s["team_name"]
            await self._dao.ensure_balance(team, float(self._cfg.get("start_funds", 50.0)))
            parts = []

            # 1) 维护费（按容量收，S9 公式；场次 = 窗口内安排的主场场次）
            home_matches = await self._dao.get_home_matches_window(team, season, window_seq)
            maintenance = formula.tier_maintenance(self._cfg, s["tier"], s["capacity"], len(home_matches))
            if maintenance > 0:
                await self._dao.apply_balance(team, -maintenance)
                created_ids.append(await self._dao.add_transaction(
                    team, season, window_seq, "maintenance", -maintenance,
                    note=f"半赛季维护（{len(home_matches)} 场）"))
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
                    created_ids.append(await self._dao.add_transaction(
                        team, season, window_seq, "activity", act["income"],
                        note=f"{booking['activity_type']}（档位{booking['slot_no']}）"))
                    parts.append(f"活动 +{act['income']:.2f}M")
                if act["extra_maintenance"] > 0:
                    await self._dao.apply_balance(team, -act["extra_maintenance"])
                    created_ids.append(await self._dao.add_transaction(
                        team, season, window_seq, "maintenance",
                        -act["extra_maintenance"], note="演唱会草皮损坏"))
                    parts.append(f"草皮维修 −{act['extra_maintenance']:.2f}M")

            # 3) 冠名费 + 到期（重算时不重复扣窗口数）
            naming = await self._dao.get_active_naming(team)
            if naming:
                fee = naming["fee_per_window"]
                await self._dao.apply_balance(team, fee)
                created_ids.append(await self._dao.add_transaction(
                    team, season, window_seq, "naming", fee,
                    note=f"{naming['brand']} 冠名费"))
                parts.append(f"冠名 +{fee:.2f}M")
                if not redo:
                    await self._dao.tick_naming(team)

            # 4) 死忠演化（记录变动前后数值，供品牌解约判断）
            fd = fans_by_team.get(team)
            if fd:
                fans_before, fans_after = fd["before"], fd["after"]
                if fans_after != fans_before:
                    delta = round(fans_after - fans_before)
                    parts.append(f"死忠 {fans_before:,.0f}→{fans_after:,.0f} ({delta:+d})")
            else:
                fans_before = fans_after = s["fans_diehards"]
            await self._brand_service.maybe_brand_terminate(team, season, window_seq, fans_before, fans_after)

            # 5) 事件回顾
            events = await self._dao.get_window_events(team, season, window_seq)
            for ev in events:
                parts.append(f"事件·{ev['text'] or ev['event_id']}")

            balance = await self._dao.get_balance(team)
            parts.append(f"余额 {balance['balance']:.2f}M" if balance else "余额 0M")
            lines.append(f"· {team}：{'，'.join(parts) if parts else '无变动'}")

        await self._dao.expire_namings()
        await self._dao.add_window_summary(season, window_seq)
        await self._dao.save_window_summary_tx_ids(season, window_seq, json.dumps(created_ids))
        return {"season": season, "window_seq": window_seq, "lines": lines}