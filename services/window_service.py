"""窗口末结算：维护费 + 档期兑现 + 冠名费 + 死忠演化 + 选择事件兑现 → 账本 → 窗口摘要。

自然成本纪律：维护按容量收、票房只按实际上座，超前扩建每窗口自然流血；
无显式惩罚条目。即发事件在结算外由管理员独立触发，选择型事件在结算内
统一兑现（已定按概率掷骰、未定按最差结果兜底）。
「强制」重算 = 撤销上次结算创建的流水（按 window_summaries 记录的 ID）并重算余额，
重置选择事件后随本次结算重新兑现；死忠演化是状态变更（无历史快照），重算不重复执行。
结算权经 claim_window_summary 原子认领（标记先于扣费创建），流水 ID 逐批增量持久化：
中途崩溃后强制重算可按已记录 ID 精确撤销，不会重复扣费。
"""

import json

from astrbot.api import logger

from . import formula
from .event_effects import collect_window_signals, mult_factor, step_value


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

    async def _tier_profile(self, team: str) -> dict:
        """品牌档位性格（实时档位，改档即生效）；无生效冠名或档位缺失返回空。"""
        naming = await self._dao.get_active_naming(team)
        if not naming:
            return {}
        brand = next((b for b in await self._dao.list_brands()
                      if b["brand"] == naming["brand"]), None)
        tier = (brand["tier"] if brand else "") or "口碑"
        profiles = formula.parse_json_object(self._cfg.get("market_tier_profiles", {}))
        profile = profiles.get(tier)
        return profile if isinstance(profile, dict) else {}

    async def settle(self, force: bool = False) -> dict:
        state = await self._dao.get_league_state()
        if not state:
            raise SettleError("赛季状态未初始化")
        season, window_seq = int(state["season_number"]), int(state["window_seq"])
        was_settled = await self._dao.has_window_summary(season, window_seq)
        if was_settled and not force:
            season_label = await self._dao.season_label(season)
            raise SettleError(f"{season_label} 窗口 {window_seq} 已结算（可加「强制」撤销重算）")

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
            # 撤销后清空标记内 ID，随本次结算重新增量记录
            await self._dao.save_window_summary_tx_ids(season, window_seq, "[]")
        else:
            # 原子认领结算权：并发/重复结算在此拦下（标记先于任何扣费创建）
            claimed = await self._dao.claim_window_summary(season, window_seq)
            if claimed == 0:
                season_label = await self._dao.season_label(season)
                raise SettleError(f"{season_label} 窗口 {window_seq} 已结算（可加「强制」撤销重算）")

        async def _persist_tx_ids() -> None:
            # 增量持久化：每笔流水创建后立即落盘，中途崩溃后强制重算按已记录 ID 精确撤销
            await self._dao.save_window_summary_tx_ids(season, window_seq, json.dumps(created_ids))

        async def _record_tx(team: str, kind: str, amount: float, note: str) -> int:
            # 余额增量与流水同事务落盘，杜绝「流水已记、余额未动」的中间态
            (tx_id,) = await self._dao.record_entries(
                team, season, window_seq, [(kind, amount, note, None)],
            )
            created_ids.append(tx_id)
            await _persist_tx_ids()
            return tx_id

        # 事件选择结算：已定按选项概率掷骰、未定按最差结果兜底；流水并入可撤销集合。
        # 强制重算（redo）只重记账目类效果，跳过死忠/上座等持久状态应用（避免复利叠加）。
        created_ids: list[int] = []
        await self._event_engine.settle_choices(season, window_seq,
                                                apply_state=not redo,
                                                tx_sink=created_ids, on_batch=_persist_tx_ids)

        # 事件信号（signals 键）：乘数型消费维护费/冠名费，步进型随死忠演化消费（redo 跳过）。
        # 只读事件日志不落状态——首结与重算同源，重算的乘数自动重新应用
        signals = await collect_window_signals(self._dao, season, window_seq)
        sig_cfg = formula.parse_json_object(self._cfg.get("event_signal_config", "{}"))
        lines = []

        # 死忠演化每窗口仅执行一轮（一次调用作用于全部球队），重算时跳过；
        # 冠名死忠加成随演化应用，状态类随重算一起跳过
        fans_buffs = {}
        mood_by_team = {}
        if not redo:
            for s0 in stadiums:
                team0 = s0["team_name"]
                buff = float((await self._tier_profile(team0)).get("fans_buff", 0.0) or 0.0)
                if buff:
                    fans_buffs[team0] = buff
                mood = step_value(signals, "fan_mood", team0)
                if mood:
                    mood_by_team[team0] = mood
        fans_details = [] if redo else await self._fans_service.evolve(
            season, window_seq, fans_buffs=fans_buffs, mood_by_team=mood_by_team or None)
        fans_by_team = {fd["team"]: fd for fd in fans_details}
        for s in stadiums:
            team = s["team_name"]
            await self._dao.ensure_balance(team, float(self._cfg.get("start_funds", 50.0)))
            parts = []

            # 1) 维护费（按容量收，S9 公式；场次 = 窗口内安排的主场场次；
            #    upkeep 信号乘数消费：Π(事件值) 终钳，重算同源重应用）
            home_matches = await self._dao.get_home_matches_window(team, season, window_seq)
            maintenance = formula.tier_maintenance(self._cfg, s["tier"], s["capacity"], len(home_matches))
            upkeep_f = mult_factor(signals, "upkeep", team,
                                   float(sig_cfg.get("upkeep_low", 0.5)),
                                   float(sig_cfg.get("upkeep_high", 2.0)))
            if upkeep_f != 1.0:
                maintenance = round(maintenance * upkeep_f, 3)
            if maintenance > 0:
                await _record_tx(team, "maintenance", -maintenance,
                                 note=f"半赛季维护（{len(home_matches)} 场）")
                parts.append(f"维护 −{maintenance:.2f}M")

            # 2) 档期活动兑现（头部品牌上座加成按实时档位性格乘收入）
            attend_buff = float((await self._tier_profile(team)).get("attend_buff", 0.0) or 0.0)
            for booking in await self._dao.get_bookings(team, season, window_seq):
                facilities = await self._dao.get_facilities(team)
                act = formula.activity_income(
                    self._cfg, booking["activity_type"],
                    pitch_level=facilities.get(formula.FACILITY_PITCH, 0),
                    youth_level=facilities.get(formula.FACILITY_YOUTH, 0),
                )
                if act["income"] > 0:
                    income = round(act["income"] * (1.0 + attend_buff), 3)
                    await _record_tx(team, "activity", income,
                                     note=f"{booking['activity_type']}（档位{booking['slot_no']}）")
                    parts.append(f"活动 +{income:.2f}M")
                if act["extra_maintenance"] > 0:
                    await _record_tx(team, "maintenance", -act["extra_maintenance"],
                                     note="演唱会草皮损坏")
                    parts.append(f"草皮维修 −{act['extra_maintenance']:.2f}M")

            fd = fans_by_team.get(team)

            # 3) 冠名费 + 到期 + 对赌奖金（重算时不重复扣窗口数；奖金为纯流水可重算）
            naming = await self._dao.get_active_naming(team)
            if naming:
                fee = float(naming["fee_per_window"])
                fee_f = mult_factor(signals, "fee_mod", team,
                                    float(sig_cfg.get("fee_mod_low", 0.5)),
                                    float(sig_cfg.get("fee_mod_high", 2.0)))
                if fee_f != 1.0:
                    fee = round(fee * fee_f, 3)
                await _record_tx(team, "naming", fee, note=f"{naming['brand']} 冠名费")
                parts.append(f"冠名 +{fee:.2f}M")
                if not redo:
                    await self._dao.tick_naming(team)
                try:
                    package = json.loads(naming["package_json"] or "{}")
                except (ValueError, TypeError):
                    package = {}
                if isinstance(package, dict) and package.get("pkg_no") == 3:
                    attend_rate = await self._fans_service.window_attend_rate(
                        team, season, window_seq, s["capacity"])
                    # 增长口径以死忠快照为准：首次结算与 redo 同源（redo 时 evolve
                    # 跳过、fd 为空，快照仍在）；无快照（v11 前旧窗）回退现状
                    log = await self._dao.get_window_fans_log(season, window_seq, team)
                    if log is not None:
                        before = float(log["fans_before"])
                        after = float(log["fans_after"])
                    else:
                        before = float(s["fans_diehards"])
                        after = float(fd["after"]) if fd else before
                    growth = (after - before) / before if before > 0 else 0.0
                    bonus = formula.bet_bonus_due(package, attend_rate, growth)
                    if bonus > 0:
                        await _record_tx(team, "naming", bonus,
                                         note=f"{naming['brand']} 对赌奖金（上座/死忠达线）")
                        parts.append(f"对赌奖金 +{bonus:.2f}M")

            # 4) 死忠演化展示
            if fd and fd["after"] != fd["before"]:
                delta = round(fd["after"] - fd["before"])
                parts.append(f"死忠 {fd['before']:,.0f}→{fd['after']:,.0f} ({delta:+d})")

            # 事件回顾
            events = await self._dao.get_window_events(team, season, window_seq)
            for ev in events:
                parts.append(f"事件·{ev['text'] or ev['event_id']}")

            balance = await self._dao.get_balance(team)
            parts.append(f"余额 {balance['balance']:.2f}M" if balance else "余额 0M")
            lines.append(f"· {team}：{'，'.join(parts) if parts else '无变动'}")

        # 行情演化（状态类，重算整步跳过）：满意度三信号步进，跌破下限品牌解约
        if not redo:
            attend_rates = {}
            for s in stadiums:
                attend_rates[s["team_name"]] = await self._fans_service.window_attend_rate(
                    s["team_name"], season, window_seq, s["capacity"])
            term_lines, _ = await self._brand_service.evolve_sentiment(
                season, window_seq, attend_rates)
            lines.extend(term_lines)

        await self._dao.expire_namings()
        await _persist_tx_ids()
        return {"season": season, "window_seq": window_seq, "lines": lines}