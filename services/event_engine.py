"""随机事件引擎：管理员触发式分配，每队每次触发最多一条。

触发即生效：金额/维护直接入账本，死忠百分比立即调整（受上限钳制），
上座修正写入球场 next_attendance_mod（对下一场主场比赛生效）。
文案由 llm_writer 生成，失败回退模板文案。
"""

import json
import random

from . import formula


class EventError(Exception):
    pass


# 默认事件库（22 条，8 类）：模板文案为 LLM 兜底
DEFAULT_EVENTS = [
    {"event_id": "storm_buzz", "name": "暴雨滂沱", "category": "天气衍生", "weight": 8,
     "conditions": {}, "effects": {"attendance_mod": 0.85}, "template": "暴雨突袭，{stadium} 门前的长队湿了一半，{team} 球迷热情不减。"},
    {"event_id": "pitch_disease", "name": "草皮病害", "category": "场地故障", "weight": 8,
     "conditions": {}, "effects": {"maintenance": 3.0}, "template": "球场的草皮最近越长越秃，{team} 紧急加急养护，多花了一笔维护费。"},
    {"event_id": "light_fail", "name": "灯光故障", "category": "场地故障", "weight": 6,
     "conditions": {}, "effects": {"attendance_mod": 0.9, "money": -0.5}, "template": "比赛日灯光跳闸两小时，{stadium} 现场一片手电筒海，部分观众提前离场。"},
    {"event_id": "roof_leak", "name": "顶棚漏水", "category": "场地故障", "weight": 6,
     "conditions": {}, "effects": {"maintenance": 1.5}, "template": "{stadium} 的顶棚在雨夜漏了水，维修队连夜开工。"},
    {"event_id": "fan_clash", "name": "球迷冲突", "category": "球迷舆情", "weight": 5,
     "conditions": {}, "effects": {"money": -3.0, "fans_pct": -0.02}, "template": "客队球迷与主队球迷在 {stadium} 外发生冲突，{team} 被处以罚款。"},
    {"event_id": "tifo_viral", "name": "TIFO出圈", "category": "球迷舆情", "weight": 7,
     "conditions": {}, "effects": {"fans_pct": 0.03, "money": 1.0}, "template": "{team} 球迷的巨型 TIFO 刷爆社交平台，{stadium} 一夜出圈。"},
    {"event_id": "new_wave", "name": "新球迷浪潮", "category": "球迷舆情", "weight": 6,
     "conditions": {}, "effects": {"fans_pct": 0.04}, "template": "社区推广见效，一群年轻人把 {stadium} 当成了周末打卡地。"},
    {"event_id": "merch_hit", "name": "周边爆款", "category": "商业机会", "weight": 7,
     "conditions": {}, "effects": {"money": 4.0}, "template": "{team} 新年款围巾脱销，周边商品盈利大涨。"},
    {"event_id": "scalper_raid", "name": "黄牛泛滥", "category": "商业机会", "weight": 5,
     "conditions": {}, "effects": {"money": -2.0}, "template": "黄牛把 {team} 主场球票炒到三倍，俱乐部配合警方清理，损失了部分收入。"},
    {"event_id": "food_fest", "name": "球场美食节", "category": "商业机会", "weight": 6,
     "conditions": {}, "effects": {"money": 2.0}, "template": "{stadium} 美食节开了 18 个小吃摊，球迷边吃边看，营业额创纪录。"},
    {"event_id": "doc_film", "name": "纪录片取景", "category": "媒体", "weight": 5,
     "conditions": {}, "effects": {"money": 1.5}, "template": "有纪录片团队进驻 {stadium}，为 {team} 拍了一整个比赛日。"},
    {"event_id": "extra_broadcast", "name": "追加转播", "category": "媒体", "weight": 5,
     "conditions": {}, "effects": {"money": 2.0}, "template": "{team} 的主场比赛被追加为全国转播场次，转播分成到账。"},
    {"event_id": "bad_press", "name": "负面报道", "category": "媒体", "weight": 5,
     "conditions": {}, "effects": {"fans_pct": -0.02}, "template": "一篇关于 {team} 的更衣室传闻登上头条，部分球迷表示要冷静观望。"},
    {"event_id": "guest_ghost", "name": "演唱会嘉宾鸽了", "category": "档期联动", "weight": 5,
     "conditions": {"requires_activity": "concert"}, "effects": {"money": -2.0}, "template": "原定在 {stadium} 开唱的嘉宾临时鸽了，退票潮来袭。"},
    {"event_id": "concert_pack", "name": "档期爆满加场", "category": "档期联动", "weight": 5,
     "conditions": {"requires_activity": "concert"}, "effects": {"money": 4.0}, "template": "{stadium} 演唱会门票秒空，主办方当场宣布加场一场。"},
    {"event_id": "brand_crisis", "name": "冠名品牌危机", "category": "冠名联动", "weight": 4,
     "conditions": {"requires_naming": True}, "effects": {"money": -2.0}, "template": "冠名品牌出事了，{team} 的球场广告位被下架整改，损失了当期冠名收入。"},
    {"event_id": "brand_anniv", "name": "冠名周年庆", "category": "冠名联动", "weight": 4,
     "conditions": {"requires_naming": True}, "effects": {"money": 3.0}, "template": "冠名品牌在 {stadium} 办周年嘉年华，赠送了 {team} 一笔营销赞助。"},
    {"event_id": "relic_found", "name": "球场挖出文物", "category": "意外之财", "weight": 2,
     "conditions": {}, "effects": {"money": 6.0}, "template": "施工队在 {stadium} 地下挖到疑似文物，随后文旅部门送来一笔补偿金。"},
    {"event_id": "subsidy", "name": "政府补贴", "category": "意外之财", "weight": 3,
     "conditions": {}, "effects": {"money": 4.0}, "template": "{team} 获评城市标杆俱乐部，{stadium} 拿到了政府补贴。"},
    {"event_id": "derby_buzz", "name": "德比热度", "category": "商业机会", "weight": 6,
     "conditions": {}, "effects": {"attendance_mod": 1.15, "money": 1.0}, "template": "德比大战将至，{stadium} 的球票一票难求，气氛提前被点燃。"},
    {"event_id": "vip_luxury", "name": "VIP礼遇升级", "category": "商业机会", "weight": 4,
     "conditions": {}, "effects": {"money": 2.5}, "template": "{stadium} 的 VIP 包厢推出香槟套餐，包厢收入水涨船高。"},
    {"event_id": "security_break", "name": "安保存漏洞", "category": "场地故障", "weight": 3,
     "conditions": {}, "effects": {"money": -2.5, "fans_pct": -0.01}, "template": "安检口被曝出漏洞，{team} 加急整改并缴纳了罚款。"},
]


class EventEngine:
    def __init__(self, db, dao, cfg, llm=None):
        self._db = db
        self._dao = dao
        self._cfg = cfg
        self._llm = llm

    async def init_defaults(self) -> None:
        """幂等写入默认事件库（builtin/adopted）。"""
        for ev in DEFAULT_EVENTS:
            await self._dao.upsert_event(
                ev["event_id"], ev["name"], ev["category"], ev["weight"],
                json.dumps(ev["conditions"], ensure_ascii=False),
                json.dumps(ev["effects"], ensure_ascii=False),
                ev["template"], source="builtin", status="adopted",
            )

    # ─── 触发 ─────────────────────────────────────────────

    async def trigger_all(self, season: int, window_seq: int, updated_by: str = "") -> dict:
        """全员触发：每队独立命中（默认 40%），命中队抽 1 条符合条件的。"""
        stadiums = await self._dao.list_stadiums()
        if not stadiums:
            raise EventError("没有已建球场的球队")
        events_pool = await self._dao.list_events("adopted")
        hit_prob = float(self._cfg.get("event_hit_probability", 0.4))
        hits = []
        for s in stadiums:
            if random.random() >= hit_prob:
                continue
            bookings = [b["activity_type"] for b in
                        await self._dao.get_bookings(s["team_name"], season, window_seq)]
            ev = await self._pick(s, events_pool, bookings)
            if ev is None:
                continue
            row = await self._apply(ev, s["team_name"], season, window_seq)
            hits.append(row)
        await self._decorate(hits)
        return {"triggered": len(hits), "hits": hits}

    async def trigger_team(self, team_name: str, season: int, window_seq: int,
                           event_id: str | None = None, updated_by: str = "") -> dict:
        """指定单队触发（event_id 为空则符合条件池内加权抽取）。"""
        s = await self._dao.get_stadium(team_name)
        if not s:
            raise EventError(f"球队「{team_name}」没有球场")
        bookings = [b["activity_type"] for b in
                    await self._dao.get_bookings(team_name, season, window_seq)]
        events_pool = await self._dao.list_events("adopted")
        if event_id:
            ev = next((e for e in events_pool if e["event_id"] == event_id), None)
            if ev is None:
                raise EventError(f"事件 {event_id} 不存在或未采纳")
        else:
            ev = await self._pick(s, events_pool, bookings)
            if ev is None:
                raise EventError("没有符合条件的事件")
        row = await self._apply(ev, team_name, season, window_seq)
        await self._decorate([row])
        return {"triggered": 1, "hits": [row]}

    async def _pick(self, stadium, events_pool, bookings: list[str]):
        """条件过滤 + 权重抽取一条。"""
        has_naming = await self._dao.get_active_naming(stadium["team_name"]) is not None
        eligible = [
            e for e in events_pool
            if await self._condition_ok(e, stadium, has_naming, bookings)
        ]
        if not eligible:
            return None
        weights = [max(1, int(e["weight"])) for e in eligible]
        return random.choices(eligible, weights=weights, k=1)[0]

    async def _condition_ok(self, event, stadium, has_naming: bool, bookings: list[str]) -> bool:
        try:
            cond = json.loads(event["conditions_json"] or "{}")
        except (ValueError, TypeError):
            cond = {}
        tier = stadium["tier"]
        if "min_tier" in cond and tier < int(cond["min_tier"]):
            return False
        if "max_tier" in cond and tier > int(cond["max_tier"]):
            return False
        if "min_capacity" in cond and stadium["capacity"] < int(cond["min_capacity"]):
            return False
        if cond.get("requires_naming") is True and not has_naming:
            return False
        if cond.get("requires_activity"):
            if cond["requires_activity"] not in bookings:
                return False
        return True

    async def _apply(self, event, team_name: str, season: int, window_seq: int) -> dict:
        try:
            effects = json.loads(event["effects_json"] or "{}")
        except (ValueError, TypeError):
            effects = {}
        stadium = await self._dao.get_stadium(team_name)
        await self._dao.ensure_balance(team_name, float(self._cfg.get("start_funds", 50.0)))
        notes = []

        money = float(effects.get("money", 0.0))
        maintenance = float(effects.get("maintenance", 0.0))
        fans_pct = float(effects.get("fans_pct", 0.0))
        attendance_mod = float(effects.get("attendance_mod", 1.0))

        if money:
            await self._dao.apply_balance(team_name, money)
            await self._dao.add_transaction(
                team_name, season, window_seq, "event", money, note=event["name"],
            )
            notes.append(f"资金 {'+' if money > 0 else ''}{money:.1f}M")
        if maintenance:
            await self._dao.apply_balance(team_name, -maintenance)
            await self._dao.add_transaction(
                team_name, season, window_seq, "event", -maintenance,
                note=f"{event['name']}（维护）",
            )
            notes.append(f"维护 +{maintenance:.1f}M")
        if fans_pct:
            fans = min(max(stadium["fans_diehards"] * (1.0 + fans_pct), 0.0),
                       float(self._cfg.get("fans_cap", 10000)))
            await self._dao.update_fans(team_name, fans)
            notes.append(f"死忠 {'+' if fans_pct > 0 else ''}{fans_pct * 100:.1f}%")
        if attendance_mod != 1.0:
            mod = stadium["next_attendance_mod"] * attendance_mod
            await self._dao.update_attendance_mod(team_name, mod)
            notes.append(f"下一场上座 {'+' if attendance_mod > 1 else ''}{attendance_mod * 100:.0f}%")

        effect_snapshot = json.dumps({"money": money, "maintenance": maintenance,
                                      "fans_pct": fans_pct, "attendance_mod": attendance_mod},
                                     ensure_ascii=False)
        await self._dao.add_event_log(team_name, season, window_seq, event["event_id"],
                                      effect_snapshot, event["name"])
        return {"team": team_name, "event": event["name"], "event_id": event["event_id"],
                "notes": notes}

    async def _decorate(self, hits: list[dict]) -> None:
        """为命中事件生成文案（LLM，超限/失败回退模板）写入日志。"""
        if not hits or self._llm is None:
            return
        max_calls = int(self._cfg.get("llm_max_calls", 8))
        for i, hit in enumerate(hits):
            event_row = await self._dao.fetch_event_by_id(hit["event_id"])
            if event_row is None:
                continue
            stadium = await self._dao.get_stadium(hit["team"])
            stadium_name = stadium["name"] if stadium else hit["team"]
            if i < max_calls:
                text = await self._llm.event_text(event_row, hit["team"], stadium_name,
                                                  context="；".join(hit["notes"]))
            else:
                from .llm_writer import _fill_template

                text = _fill_template(event_row["template"] or event_row["name"],
                                      hit["team"], stadium_name)
            await self._dao.update_event_log_text(
                hit["team"], hit["event_id"], text
            )
            hit["text"] = text

    # ─── LLM 设计 ─────────────────────────────────────────

    async def generate_drafts(self, count: int, topic: str = "") -> list[dict]:
        if self._llm is None:
            raise EventError("LLM 不可用")
        drafts = await self._llm.design_events(count, topic)
        added = []
        for d in drafts:
            event_id = f"llm_{await self._dao.next_event_counter()}"
            await self._dao.upsert_event(
                event_id, d["name"], d["category"], d["weight"],
                "{}", json.dumps(d["effects"], ensure_ascii=False),
                d["template"], source="llm", status="pending",
            )
            added.append({"id": event_id, "name": d["name"], "effects": d["effects"]})
        return added

    async def adopt(self, event_id: int) -> None:
        rows = await self._dao.list_events("pending")
        target = next((r for r in rows if r["id"] == event_id), None)
        if target is None:
            raise EventError("待采纳事件不存在（事件通过 主场事件列表 查看 id）")
        await self._dao.adopt_event(event_id)

    async def discard(self, event_id: int) -> None:
        rows = await self._dao.list_events("pending")
        if not any(r["id"] == event_id for r in rows):
            raise EventError("待处理事件不存在")
        await self._dao.discard_event(event_id)

    # ─── 管理员手写 ───────────────────────────────────────

    async def add_custom(self, name: str, category: str, weight: int,
                         effects_text: str) -> None:
        """手写自定义事件（effects_text 为 JSON 文本，如 {"money": 3}）。"""
        import json as _json

        try:
            effects = _json.loads(effects_text)
        except (ValueError, TypeError):
            raise EventError("effects 需为 JSON 文本，如 {\"money\": 3}（money/fans_pct/maintenance/attendance_mod）")
        if not isinstance(effects, dict):
            raise EventError("effects 需为 JSON 对象")
        event_id = f"custom_{await self._dao.next_event_counter()}"
        await self._dao.upsert_event(
            event_id, name.strip(), category.strip() or "自定义", weight,
            "{}", _json.dumps(effects, ensure_ascii=False), name.strip(),
            source="custom", status="adopted",
        )