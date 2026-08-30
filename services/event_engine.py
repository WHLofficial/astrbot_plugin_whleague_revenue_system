"""随机事件引擎：管理员触发式分配，每队每次触发最多一条。

触发即生效：金额/维护直接入账本，死忠百分比立即调整（受上限钳制），
上座修正写入球场 next_attendance_mod（对下一场主场比赛生效）。
文案由 llm_writer 生成，失败回退模板文案。
"""

import json
import math
import random
from datetime import datetime, timedelta

from . import formula
from .llm_writer import _fill_template
from ..utils.security import parse_choice_no


class EventError(Exception):
    pass


# 默认事件库（22 条：6 即发 + 16 选择）。
# · 即发型（event_type=instant）：触发即按 effects 记账。
# · 选择型（event_type=choice）：触发只广播选项进入待定，玩家任选一项操作；
#   每项操作下有「正面/负面概率表」（outcomes），结算时已定按概率掷骰、
#   未定按最差结果（净额最小）兜底。取值均在 LLM 钳制范围内。
DEFAULT_EVENTS = [
    # ─── 即发型（6 条，触发即生效） ─────────────────────────
    {"event_id": "storm_buzz", "name": "暴雨滂沱", "category": "天气衍生", "weight": 8,
     "event_type": "instant", "conditions": {}, "effects": {"attendance_mod": 0.85},
     "template": "暴雨突袭，{stadium} 门前的长队湿了一半，{team} 球迷热情不减。"},
    {"event_id": "tifo_viral", "name": "TIFO出圈", "category": "球迷舆情", "weight": 7,
     "event_type": "instant", "conditions": {}, "effects": {"fans_pct": 0.03, "money": 1.0},
     "template": "{team} 球迷的巨型 TIFO 刷爆社交平台，{stadium} 一夜出圈。"},
    {"event_id": "bad_press", "name": "负面报道", "category": "媒体", "weight": 5,
     "event_type": "instant", "conditions": {}, "effects": {"fans_pct": -0.02},
     "template": "一篇关于 {team} 的更衣室传闻登上头条，部分球迷表示要冷静观望。"},
    {"event_id": "relic_found", "name": "球场挖出文物", "category": "意外之财", "weight": 2,
     "event_type": "instant", "conditions": {}, "effects": {"money": 6.0},
     "template": "施工队在 {stadium} 地下挖到疑似文物，随后文旅部门送来一笔补偿金。"},
    {"event_id": "subsidy", "name": "政府补贴", "category": "意外之财", "weight": 3,
     "event_type": "instant", "conditions": {}, "effects": {"money": 4.0},
     "template": "{team} 获评城市标杆俱乐部，{stadium} 拿到了政府补贴。"},
    {"event_id": "security_break", "name": "安保存漏洞", "category": "场地故障", "weight": 3,
     "event_type": "instant", "conditions": {}, "effects": {"money": -2.5, "fans_pct": -0.01},
     "template": "安检口被曝出漏洞，{team} 加急整改并缴纳了罚款。"},
    # ─── 选择型（16 条，玩家任选一项操作） ───────────────────
    {"event_id": "pitch_disease", "name": "草皮病害", "category": "场地故障", "weight": 8,
     "event_type": "choice", "conditions": {},
     "template": "球场的草皮最近越长越秃，{team} 需要决定怎么处理。",
     "options": [
         {"no": 1, "name": "整块翻新草皮", "desc": "一步到位，代价高",
          "outcomes": [{"w": 60, "effects": {"money": -3.0}},
                       {"w": 40, "effects": {"maintenance": 4.0, "attendance_mod": 0.9}}]},
         {"no": 2, "name": "局部修补省钱", "desc": "花小钱赌一把",
          "outcomes": [{"w": 50, "effects": {"money": -1.0, "fans_pct": 0.01}},
                       {"w": 50, "effects": {"maintenance": 3.0, "attendance_mod": 0.85}}]},
     ]},
    {"event_id": "light_fail", "name": "灯光故障", "category": "场地故障", "weight": 6,
     "event_type": "choice", "conditions": {},
     "template": "比赛日灯光跳闸两小时，{stadium} 现场一片手电筒海，怎么补救?",
     "options": [
         {"no": 1, "name": "连夜抢修灯光", "desc": "保证夜场正常",
          "outcomes": [{"w": 70, "effects": {"money": -2.0}},
                       {"w": 30, "effects": {"maintenance": 2.0, "attendance_mod": 0.9}}]},
         {"no": 2, "name": "租移动照明车", "desc": "临时顶上",
          "outcomes": [{"w": 50, "effects": {"money": -0.5, "attendance_mod": 1.05}},
                       {"w": 50, "effects": {"money": -3.5, "attendance_mod": 0.95}}]},
     ]},
    {"event_id": "roof_leak", "name": "顶棚漏水", "category": "场地故障", "weight": 6,
     "event_type": "choice", "conditions": {},
     "template": "{stadium} 的顶棚在雨夜漏了水，维修队给出两套方案。",
     "options": [
         {"no": 1, "name": "赶在雨前修顶", "desc": "花钱买保险",
          "outcomes": [{"w": 70, "effects": {"money": -2.0}},
                       {"w": 30, "effects": {"maintenance": 3.0, "attendance_mod": 0.9}}]},
         {"no": 2, "name": "先遮再商议", "desc": "省钱拖修",
          "outcomes": [{"w": 50, "effects": {"money": -0.5, "fans_pct": 0.01}},
                       {"w": 50, "effects": {"maintenance": 4.0, "attendance_mod": 0.85}}]},
     ]},
    {"event_id": "fan_clash", "name": "球迷冲突", "category": "球迷舆情", "weight": 5,
     "event_type": "choice", "conditions": {},
     "template": "客队球迷与主队球迷在 {stadium} 外发生冲突，{team} 要尽快表态。",
     "options": [
         {"no": 1, "name": "高调道歉并处罚", "desc": "平息舆论，成本高",
          "outcomes": [{"w": 60, "effects": {"money": -4.0, "fans_pct": 0.02}},
                       {"w": 40, "effects": {"money": -6.0, "fans_pct": -0.01}}]},
         {"no": 2, "name": "低调冷处理", "desc": "省事但风险大",
          "outcomes": [{"w": 40, "effects": {"money": -1.0}},
                       {"w": 60, "effects": {"money": -5.0, "fans_pct": -0.03}}]},
     ]},
    {"event_id": "new_wave", "name": "新球迷浪潮", "category": "球迷舆情", "weight": 6,
     "event_type": "choice", "conditions": {},
     "template": "社区推广见效，一群年轻人把 {stadium} 当成了周末打卡地，怎么接住?",
     "options": [
         {"no": 1, "name": "办球迷开放日", "desc": "小投入拉口碑",
          "outcomes": [{"w": 70, "effects": {"fans_pct": 0.04, "money": -0.5}},
                       {"w": 30, "effects": {"fans_pct": 0.01, "money": -1.5}}]},
         {"no": 2, "name": "推出低价学生票", "desc": "薄利多销搏长期",
          "outcomes": [{"w": 50, "effects": {"fans_pct": 0.05, "money": -1.0}},
                       {"w": 50, "effects": {"money": 0.5, "fans_pct": -0.01}}]},
     ]},
    {"event_id": "merch_hit", "name": "周边爆款", "category": "商业机会", "weight": 7,
     "event_type": "choice", "conditions": {},
     "template": "{team} 新年款围巾脱销，周边商品盈利大涨，要不要趁机加码?",
     "options": [
         {"no": 1, "name": "加班加单补货", "desc": "趁热度冲一波销售",
          "outcomes": [{"w": 60, "effects": {"money": 5.0, "maintenance": 1.0}},
                       {"w": 40, "effects": {"money": -1.5, "maintenance": 3.0}}]},
         {"no": 2, "name": "线上限量抽签", "desc": "饥饿营销保口碑",
          "outcomes": [{"w": 50, "effects": {"money": 4.0, "fans_pct": 0.02}},
                       {"w": 50, "effects": {"money": 0.5, "fans_pct": -0.02}}]},
     ]},
    {"event_id": "scalper_raid", "name": "黄牛泛滥", "category": "商业机会", "weight": 5,
     "event_type": "choice", "conditions": {},
     "template": "黄牛把 {team} 主场球票炒到三倍，俱乐部打算清理。",
     "options": [
         {"no": 1, "name": "实名购票+人脸入场", "desc": "动真格清理黄牛",
          "outcomes": [{"w": 60, "effects": {"money": -2.0, "fans_pct": 0.03}},
                       {"w": 40, "effects": {"maintenance": 4.0, "fans_pct": 0.01}}]},
         {"no": 2, "name": "与票务平台合作", "desc": "技术封堵，成本中等",
          "outcomes": [{"w": 70, "effects": {"money": -1.0, "fans_pct": 0.02}},
                       {"w": 30, "effects": {"money": -3.0, "fans_pct": -0.02}}]},
     ]},
    {"event_id": "food_fest", "name": "球场美食节", "category": "商业机会", "weight": 6,
     "event_type": "choice", "conditions": {},
     "template": "{stadium} 美食节开了 18 个小吃摊，怎么运营赚得更多?",
     "options": [
         {"no": 1, "name": "自营加放15个摊", "desc": "摊租全收",
          "outcomes": [{"w": 60, "effects": {"money": 3.0}},
                       {"w": 40, "effects": {"money": -1.0, "maintenance": 1.5}}]},
         {"no": 2, "name": "免铺租换流量", "desc": "让利引流",
          "outcomes": [{"w": 60, "effects": {"fans_pct": 0.03, "money": 1.0}},
                       {"w": 40, "effects": {"money": -2.0}}]},
     ]},
    {"event_id": "doc_film", "name": "纪录片取景", "category": "媒体", "weight": 5,
     "event_type": "choice", "conditions": {},
     "template": "有纪录片团队进驻 {stadium}，为 {team} 拍一个比赛日，怎么谈?",
     "options": [
         {"no": 1, "name": "免费借景", "desc": "换口碑曝光",
          "outcomes": [{"w": 70, "effects": {"fans_pct": 0.03, "money": -1.0}},
                       {"w": 30, "effects": {"fans_pct": 0.01, "money": -2.5}}]},
         {"no": 2, "name": "收取拍摄场地费", "desc": "明码标价",
          "outcomes": [{"w": 50, "effects": {"money": 2.0}},
                       {"w": 50, "effects": {"money": -0.5, "fans_pct": -0.01}}]},
     ]},
    {"event_id": "extra_broadcast", "name": "追加转播", "category": "媒体", "weight": 5,
     "event_type": "choice", "conditions": {},
     "template": "{team} 的主场比赛被追加为全国转播场次，转播分成怎么最大化?",
     "options": [
         {"no": 1, "name": "追加投入信号制作", "desc": "自掏腰包提画质",
          "outcomes": [{"w": 70, "effects": {"money": 4.0, "maintenance": 0.5}},
                       {"w": 30, "effects": {"money": -1.5, "maintenance": 2.0}}]},
         {"no": 2, "name": "按现行制式上", "desc": "零成本佛系",
          "outcomes": [{"w": 60, "effects": {"money": 1.5}},
                       {"w": 40, "effects": {"money": -0.5}}]},
     ]},
    {"event_id": "guest_ghost", "name": "演唱会嘉宾鸽了", "category": "档期联动", "weight": 5,
     "event_type": "choice", "conditions": {"requires_activity": "concert"},
     "template": "原定在 {stadium} 开唱的嘉宾临时鸽了，退票潮来袭。",
     "options": [
         {"no": 1, "name": "紧急补位嘉宾", "desc": "高价临时请人",
          "outcomes": [{"w": 60, "effects": {"money": 2.0, "maintenance": 1.0}},
                       {"w": 40, "effects": {"money": -3.0, "maintenance": 2.0}}]},
         {"no": 2, "name": "全额退票+补偿", "desc": "花钱保口碑",
          "outcomes": [{"w": 50, "effects": {"money": -4.0, "fans_pct": 0.03}},
                       {"w": 50, "effects": {"money": -6.0, "fans_pct": -0.02}}]},
     ]},
    {"event_id": "concert_pack", "name": "档期爆满加场", "category": "档期联动", "weight": 5,
     "event_type": "choice", "conditions": {"requires_activity": "concert"},
     "template": "{stadium} 演唱会门票秒空，主办方问要不要加场。",
     "options": [
         {"no": 1, "name": "加开一场", "desc": "吃满热度",
          "outcomes": [{"w": 60, "effects": {"money": 5.0, "maintenance": 1.5}},
                       {"w": 40, "effects": {"money": -1.0, "maintenance": 4.0}}]},
         {"no": 2, "name": "不加场，卖贵一点", "desc": "物以稀为贵",
          "outcomes": [{"w": 60, "effects": {"money": 3.0, "fans_pct": -0.01}},
                       {"w": 40, "effects": {"money": -1.0}}]},
     ]},
    {"event_id": "brand_crisis", "name": "冠名品牌危机", "category": "冠名联动", "weight": 4,
     "event_type": "choice", "conditions": {"requires_naming": True},
     "template": "冠名品牌出事了，{team} 的球场广告位被下架整改。",
     "options": [
         {"no": 1, "name": "声援品牌共渡难关", "desc": "留人情换长约",
          "outcomes": [{"w": 60, "effects": {"money": -3.0, "fans_pct": 0.02}},
                       {"w": 40, "effects": {"money": -4.0, "fans_pct": -0.02}}]},
         {"no": 2, "name": "紧急换广告位", "desc": "切割风险",
          "outcomes": [{"w": 50, "effects": {"money": -4.0, "fans_pct": 0.02}},
                       {"w": 50, "effects": {"money": 1.0}}]},
     ]},
    {"event_id": "brand_anniv", "name": "冠名周年庆", "category": "冠名联动", "weight": 4,
     "event_type": "choice", "conditions": {"requires_naming": True},
     "template": "冠名品牌在 {stadium} 办周年嘉年华，赠送 {team} 一笔营销赞助。",
     "options": [
         {"no": 1, "name": "全力赞助庆典", "desc": "借势营销",
          "outcomes": [{"w": 70, "effects": {"money": 4.0, "fans_pct": 0.02, "maintenance": 0.5}},
                       {"w": 30, "effects": {"money": -1.0, "fans_pct": -0.01}}]},
         {"no": 2, "name": "只提供场地", "desc": "稳赚不亏",
          "outcomes": [{"w": 60, "effects": {"money": 2.5}},
                       {"w": 40, "effects": {"money": -0.5, "maintenance": 1.0}}]},
     ]},
    {"event_id": "derby_buzz", "name": "德比热度", "category": "商业机会", "weight": 6,
     "event_type": "choice", "conditions": {},
     "template": "德比大战将至，{stadium} 的球票一票难求，气氛提前被点燃。",
     "options": [
         {"no": 1, "name": "加急印限量球衣", "desc": "抢德比财",
          "outcomes": [{"w": 60, "effects": {"money": 6.0, "maintenance": 1.0}},
                       {"w": 40, "effects": {"money": -2.0, "maintenance": 2.0}}]},
         {"no": 2, "name": "提高包厢价格", "desc": "趁热抬价",
          "outcomes": [{"w": 50, "effects": {"money": 4.0, "fans_pct": -0.01}},
                       {"w": 50, "effects": {"money": -1.5, "fans_pct": -0.02}}]},
     ]},
    {"event_id": "vip_luxury", "name": "VIP礼遇升级", "category": "商业机会", "weight": 4,
     "event_type": "choice", "conditions": {},
     "template": "{stadium} 的 VIP 包厢推出香槟套餐，要不要借机升级?",
     "options": [
         {"no": 1, "name": "升级包厢软装", "desc": "高投入高回报",
          "outcomes": [{"w": 60, "effects": {"money": 4.0, "maintenance": 2.0}},
                       {"w": 40, "effects": {"money": -3.0, "maintenance": 3.0}}]},
         {"no": 2, "name": "与豪华酒店联名", "desc": "借名头少投入",
          "outcomes": [{"w": 60, "effects": {"money": 3.0, "fans_pct": 0.01}},
                       {"w": 40, "effects": {"money": -1.5, "fans_pct": -0.01}}]},
     ]},
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
                json.dumps(ev.get("effects") or {}, ensure_ascii=False),
                ev["template"], source="builtin", status="adopted",
                event_type=ev.get("event_type", "instant"),
                options_json=json.dumps(ev.get("options") or [], ensure_ascii=False),
            )

    # ─── 触发 ─────────────────────────────────────────────

    async def trigger_all(self, season: int, window_seq: int, updated_by: str = "") -> dict:
        """全员分配：每队独立命中（默认 40%），命中队抽 1 条符合条件的。

        同一事件在一次分配内最多被 event_max_occurrences 支球队抽中
        （默认 2），达到上限后从后续球队候选中剔除。
        即发型立即生效；选择型进入待定并返回广播文案。
        """
        stadiums = await self._dao.list_stadiums()
        if not stadiums:
            raise EventError("没有已建球场的球队")
        events_pool = await self._dao.list_events("adopted")
        try:
            hit_prob = float(self._cfg.get("event_hit_probability", 0.4))
        except (TypeError, ValueError):
            hit_prob = 0.4
        if not math.isfinite(hit_prob) or hit_prob < 0.0:
            hit_prob = 0.4
        hit_prob = min(1.0, hit_prob)
        max_occ = int(self._cfg.get("event_max_occurrences", 2))
        picked: dict[str, int] = {}
        hits = []
        capped = 0
        for s in stadiums:
            if random.random() >= hit_prob:
                continue
            excluded = {eid for eid, n in picked.items() if n >= max_occ}
            bookings = [b["activity_type"] for b in
                        await self._dao.get_bookings(s["team_name"], season, window_seq)]
            cap_out = {}
            ev = await self._pick(s, events_pool, bookings, excluded=excluded, cap_out=cap_out)
            if ev is None:
                if cap_out.get("yes"):
                    capped += 1
                continue
            hits.append(await self._dispatch(ev, s["team_name"], season, window_seq))
            picked[ev["event_id"]] = picked.get(ev["event_id"], 0) + 1
        await self._decorate(hits)
        return {"triggered": len(hits), "hits": hits, "capped": capped}

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
        row = await self._dispatch(ev, team_name, season, window_seq)
        await self._decorate([row])
        return {"triggered": 1, "hits": [row]}

    async def _dispatch(self, event, team_name: str, season: int, window_seq: int) -> dict:
        """按事件类型分流：即发立即生效，选择型进入待定并广播选项。"""
        if str(event["event_type"] or "instant") == "choice":
            return await self._trigger_choice(event, team_name, season, window_seq)
        return await self._apply(event, team_name, season, window_seq)

    async def _pick(self, stadium, events_pool, bookings: list[str],
                    excluded: set | None = None, cap_out: dict | None = None):
        """条件过滤 + 权重抽取一条。

        excluded 非空时先剔除此集合内的事件（同事件分配上限用）；
        结果为「有符合条件但全部被排除」时，cap_out["yes"] 置 True
        （供 trigger_all 统计因上限未中彩的队数）。
        """
        if excluded is None:
            excluded = set()
        has_naming = await self._dao.get_active_naming(stadium["team_name"]) is not None
        eligible = [
            e for e in events_pool
            if e["event_id"] not in excluded
            and await self._condition_ok(e, stadium, has_naming, bookings)
        ]
        if not eligible:
            if cap_out is not None and excluded:
                cap_out["yes"] = any([
                    await self._condition_ok(e, stadium, has_naming, bookings)
                    for e in events_pool
                ])
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

    def _event_effects(self, event) -> dict:
        try:
            raw = json.loads(event["effects_json"] or "{}")
        except (ValueError, TypeError):
            raw = {}
        return raw if isinstance(raw, dict) else {}

    def _event_options(self, event) -> list:
        try:
            raw = json.loads(event["options_json"] or "[]")
        except (ValueError, TypeError):
            raw = []
        return raw if isinstance(raw, list) else []

    async def _apply_effects(self, team_name: str, season: int, window_seq: int,
                             effects: dict, label: str,
                             created_ids: list[int] | None = None,
                             apply_state: bool = True) -> list[str]:
        """应用一组效果（资金/维护/死忠/上座修正），返回中文备注行。

        资金与维护各计一条 event 流水；created_ids 非空时收集流水 ID，
        供结算把选择结果流水并入可撤销集合。
        apply_state=False 时跳过死忠/上座等持久状态类效果（强制重算只重记账目，
        与死忠演化「重算不重复状态」一致），备注标注「重算跳过」。
        """
        stadium = await self._dao.get_stadium(team_name)
        await self._dao.ensure_balance(team_name, float(self._cfg.get("start_funds", 50.0)))
        notes = []
        money = float(effects.get("money", 0.0))
        maintenance = float(effects.get("maintenance", 0.0))
        fans_pct = float(effects.get("fans_pct", 0.0))
        attendance_mod = float(effects.get("attendance_mod", 1.0))

        if money:
            (tx_id,) = await self._dao.record_entries(
                team_name, season, window_seq, [("event", money, label, None)],
            )
            if created_ids is not None:
                created_ids.append(tx_id)
            notes.append(f"资金 {'+' if money > 0 else ''}{money:.1f}M")
        if maintenance:
            (tx_id,) = await self._dao.record_entries(
                team_name, season, window_seq,
                [("event", -maintenance, f"{label}（维护）", None)],
            )
            if created_ids is not None:
                created_ids.append(tx_id)
            notes.append(f"维护 −{maintenance:.1f}M")
        if fans_pct:
            if apply_state:
                fans = min(max(stadium["fans_diehards"] * (1.0 + fans_pct), 0.0),
                           float(self._cfg.get("fans_cap", 10000)))
                await self._dao.update_fans(team_name, fans)
                notes.append(f"死忠 {'+' if fans_pct > 0 else ''}{fans_pct * 100:.1f}%")
            else:
                notes.append(f"死忠 {fans_pct * 100:+.1f}%（重算跳过）")
        if attendance_mod != 1.0:
            if apply_state:
                mod = stadium["next_attendance_mod"] * attendance_mod
                await self._dao.update_attendance_mod(team_name, mod)
                notes.append(f"下一场上座 {'+' if attendance_mod > 1 else ''}{attendance_mod * 100:.0f}%")
            else:
                notes.append(f"下一场上座 ×{attendance_mod:.2f}（重算跳过）")
        return notes

    async def _apply(self, event, team_name: str, season: int, window_seq: int) -> dict:
        """即发型：立即按 effects 记账并写日志。"""
        effects = self._event_effects(event)
        notes = await self._apply_effects(team_name, season, window_seq, effects, event["name"])
        await self._dao.add_event_log(team_name, season, window_seq, event["event_id"],
                                      json.dumps(effects, ensure_ascii=False), event["name"])
        return {"team": team_name, "event": event["name"], "event_id": event["event_id"],
                "notes": notes}

    async def _trigger_choice(self, event, team_name: str, season: int, window_seq: int) -> dict:
        """选择型：仅创建待定选择并写广播文案，不记账（结算时才兑现）。"""
        await self._dao.add_event_choice(team_name, season, window_seq, event["event_id"])
        options = self._event_options(event)
        stadium = await self._dao.get_stadium(team_name)
        broadcast = await self._build_broadcast(event, team_name,
                                                stadium["name"] if stadium else team_name, options)
        await self._dao.add_event_log(team_name, season, window_seq, event["event_id"],
                                      "{}", broadcast)
        return {"team": team_name, "event": event["name"], "event_id": event["event_id"],
                "type": "choice", "broadcast": broadcast, "options": options}

    async def _build_broadcast(self, event, team_name: str, stadium_name: str, options: list) -> str:
        """生成面向球员的广播：LLM 叙述段 + 确定性选项列表（不显示概率）。

        叙述只讲氛围（失败回退模板），选项由数据生成保证编号/说明准确，
        玩家按「队名 事件名 选项号」回复。
        """
        if self._llm is None:
            narrative = _fill_template(event["template"] or event["name"], team_name, stadium_name)
        else:
            narrative = await self._llm.event_broadcast(event, team_name, stadium_name)
        lines = [
            f"🎲 事件「{event['name']}」（{event['category']}）@ {team_name}·{stadium_name}",
            narrative,
            "请在窗口结算前任选一项操作（回复：队名 事件名 选项号）：",
        ]
        for opt in options:
            desc = opt.get("desc") or ""
            lines.append(f"{self._num(opt['no'])} {opt['name']}"
                         + (f"（{desc}）" if desc else ""))
        return "\n".join(lines)

    @staticmethod
    def _num(n) -> str:
        return {1: "①", 2: "②", 3: "③", 4: "④"}.get(int(n), f"{n}.")

    def _roll_option(self, opt: dict) -> dict | None:
        """按选项结果的权重掷骰选一条 outcome；选项无结果时返回 None。"""
        outcomes = opt.get("outcomes") or []
        if not outcomes:
            return None
        weights = [max(1, int(o.get("w", 1))) for o in outcomes]
        return random.choices(outcomes, weights=weights, k=1)[0]

    def _worst_option(self, options: list) -> tuple[dict | None, dict | None]:
        """全部选项所有结果里净额最小（money−maintenance）的一条，确定性平局取低序号。

        无结果的选项不参与；所有选项均无结果时返回 (None, None)。
        """
        best = None
        for i, opt in enumerate(options):
            for j, out in enumerate(opt.get("outcomes") or []):
                effects = out.get("effects") or {}
                net = float(effects.get("money", 0.0)) - float(effects.get("maintenance", 0.0))
                key = (net, opt.get("no", i + 1), j)
                if best is None or key < best[0]:
                    best = (key, opt, out)
        if best is None:
            return None, None
        return best[1], best[2]

    async def _decorate(self, hits: list[dict]) -> None:
        """为即发命中事件生成文案（LLM，超限/失败回退模板）写入日志。

        选择型事件保留广播文案，不作 LLM 装饰（选项与概率必须确定呈现）。
        """
        if not hits or self._llm is None:
            return
        max_calls = int(self._cfg.get("llm_max_calls", 8))
        for i, hit in enumerate(hits):
            event_row = await self._dao.fetch_event_by_id(hit["event_id"])
            if event_row is None:
                continue
            if str(event_row["event_type"] or "instant") == "choice":
                hit["text"] = hit.get("broadcast", "")
                continue
            stadium = await self._dao.get_stadium(hit["team"])
            stadium_name = stadium["name"] if stadium else hit["team"]
            if i < max_calls:
                text = await self._llm.event_text(event_row, hit["team"], stadium_name,
                                                  context="；".join(hit["notes"]))
            else:
                text = _fill_template(event_row["template"] or event_row["name"],
                                      hit["team"], stadium_name)
            await self._dao.update_event_log_text(
                hit["team"], hit["event_id"], text
            )
            hit["text"] = text

    # ─── 结算 ─────────────────────────────────────────────

    async def settle_choices(self, season: int, window_seq: int,
                             apply_state: bool = True, tx_sink: list | None = None,
                             on_batch=None) -> dict:
        """结算当前窗口全部待定选择：已定按概率掷骰、未定按最差兜底。

        返回的 tx_ids 并入窗口结算的可撤销集合；每个已结事件写一行结算短文案
        （LLM，超限/失败回退确定性摘要）到 events_log.text。
        apply_state=False（强制重算）只重记账目，跳过死忠/上座状态应用。
        tx_sink：外部共享列表，流水 ID 实时并入（供结算增量持久化）；on_batch：
        每结完一条选择后的回调（把已累计的 ID 落盘，中途崩溃可精确撤销）。
        """
        rows = await self._dao.get_unresolved_choices(season, window_seq)
        return await self._settle_rows(rows, apply_state=apply_state,
                                       tx_sink=tx_sink, on_batch=on_batch)

    async def settle_now(self, season: int, window_seq: int,
                         include_undecided: bool = False) -> dict:
        """独立结算（管理员指令）：默认只结已导入选择；include_undecided 连未定一并兜底。

        与窗口结算解耦——流水是普通 event 流水，不写入 window_summaries.tx_ids，
        之后窗口「强制」重算不会误删；已结算的选择窗口结算会跳过。
        """
        rows = await self._dao.get_unresolved_choices(season, window_seq)
        if not include_undecided:
            rows = [c for c in rows if c["choice_no"] is not None]
        return await self._settle_rows(rows, apply_state=True)

    async def _settle_rows(self, rows, apply_state: bool, tx_sink: list | None = None,
                           on_batch=None) -> dict:
        resolved = []
        tx_ids: list[int] = tx_sink if tx_sink is not None else []
        llm_max = int(self._cfg.get("llm_max_calls", 8))
        used = 0
        for c in rows:
            use_llm = used < llm_max
            used += 1
            resolved.append(await self._resolve_choice(c, tx_ids, apply_state=apply_state,
                                                       use_llm=use_llm))
            if on_batch is not None:
                await on_batch()
        return {"resolved": resolved, "tx_ids": tx_ids}

    async def _resolve_choice(self, c, tx_ids: list[int], apply_state: bool, use_llm: bool) -> dict:
        """结算单条选择事件：已定掷骰/未定最差 → 生效 → 记 resolved → 写结算短文案。"""
        season, window_seq = int(c["season_number"]), int(c["window_seq"])
        event = await self._dao.fetch_event_by_id(c["event_id"])
        options = self._event_options(event) if event else []
        if not options:
            await self._dao.mark_choice_resolved(c["id"], '{"skipped": true}')
            name = event["name"] if event else c["event_id"]
            await self._dao.update_event_log_text(c["team_name"], c["event_id"],
                                                  f"「{name}」无选项信息，跳过")
            return {"team": c["team_name"], "event": name,
                    "auto": True, "skipped": True, "notes": []}
        auto = c["choice_no"] is None
        opt = None
        if not auto:
            opt = next((o for o in options if o["no"] == c["choice_no"]), None)
            auto = opt is None  # 选项号无效同样按最差兜底
        if auto:
            opt, outcome = self._worst_option(options)
            if opt is None:
                # 全部选项均无结果：与无选项同样跳过，不中断整批结算
                name = event["name"] if event else c["event_id"]
                await self._dao.mark_choice_resolved(c["id"], '{"skipped": true}')
                await self._dao.update_event_log_text(
                    c["team_name"], c["event_id"], f"「{name}」选项均无结果，跳过")
                return {"team": c["team_name"], "event": name,
                        "auto": True, "skipped": True, "notes": []}
        else:
            outcome = self._roll_option(opt)
        effects = (outcome or {}).get("effects") or {}
        notes = await self._apply_effects(c["team_name"], season, window_seq,
                                          effects, f"{event['name']}·{opt['name']}",
                                          tx_ids, apply_state=apply_state)
        if outcome is None:
            notes.append("选项无结果配置，按无效果结算")
        if auto:
            how = "自动最差（选项号无效）" if c["choice_no"] is not None else "自动最差（未收到选择）"
        else:
            how = f"选{opt['no']} {opt['name']}"
        await self._dao.mark_choice_resolved(c["id"], json.dumps(
            {"option": opt["no"], "option_name": opt["name"], "auto": auto,
             "effects": effects}, ensure_ascii=False))
        text = await self._result_text(event, c["team_name"], how, notes, use_llm)
        await self._dao.update_event_log_text(c["team_name"], c["event_id"], text)
        return {"team": c["team_name"], "event": event["name"],
                "option": opt["name"], "auto": auto, "notes": notes, "text": text}

    async def _result_text(self, event, team: str, how: str, notes, use_llm: bool) -> str:
        """结算短文案：LLM（受调用上限约束）；失败/超限回退确定性摘要行。"""
        fallback = f"「{event['name']}」{how} → {'；'.join(notes) if notes else '无变化'}"
        if use_llm and self._llm is not None:
            try:
                text = await self._llm.event_result(event, team, how, notes)
            except Exception:
                text = ""
            if text:
                return text
        return fallback

    # ─── 选择录入（管理员收集球员回应后导入） ───────────────

    async def record_choice(self, team_name: str, season: int, window_seq: int,
                            event_name: str, choice_no: int) -> dict:
        """为指定球队的某个待定选择填入选项号（按事件名匹配）。"""
        choices = await self._dao.list_event_choices(season, window_seq)
        target = None
        for c in choices:
            if c["team_name"] != team_name:
                continue
            event = await self._dao.fetch_event_by_id(c["event_id"])
            if event is not None and event["name"] == event_name:
                target, event_row = c, event
                break
        if target is None:
            raise EventError(f"球队「{team_name}」在当前窗口没有名为「{event_name}」的待定选择事件")
        if target["resolved"]:
            raise EventError(f"「{event_name}」已完成结算，无法修改选择")
        options = self._event_options(event_row)
        if not (1 <= choice_no <= len(options)):
            raise EventError(f"选项号需在 1~{len(options)} 之间（{event_name} 共 {len(options)} 项）")
        await self._dao.set_event_choice(team_name, season, window_seq, target["event_id"], choice_no)
        opt = next(o for o in options if o["no"] == choice_no)
        return {"team": team_name, "event": event_name, "choice_no": choice_no,
                "option": opt["name"]}

    async def import_choices(self, text: str) -> list[dict]:
        """批量导入选择：每行「队名 事件名 选项号」，逐条记结果（含错误行）。"""
        state = await self._dao.get_league_state()
        season = state["season_number"] if state else 1
        window_seq = state["window_seq"] if state else 1
        results = []
        for raw in text.splitlines():
            fields = raw.strip().split()
            if len(fields) < 3:
                continue
            # 末位 token 为选项号，中间归事件名——事件名可含空格
            team, ev_name, opt_raw = fields[0], " ".join(fields[1:-1]), fields[-1]
            try:
                choice_no = parse_choice_no(opt_raw)
            except ValueError:
                results.append({"team": team, "event": ev_name, "ok": False,
                                "error": "选项号需为数字或 ①②③④"})
                continue
            try:
                r = await self.record_choice(team, season, window_seq, ev_name, choice_no)
                results.append({"team": team, "event": ev_name, "choice_no": choice_no,
                                "option": r["option"], "ok": True})
            except EventError as e:
                results.append({"team": team, "event": ev_name, "ok": False, "error": str(e)})
        return results

    async def choices_summary(self, season: int, window_seq: int) -> list[dict]:
        """当前窗口全部选择事件的状态（含事件名/已选操作/是否结算）。"""
        out = []
        for c in await self._dao.list_event_choices(season, window_seq):
            event = await self._dao.fetch_event_by_id(c["event_id"])
            name = event["name"] if event else c["event_id"]
            opt_name = ""
            if c["choice_no"] is not None and event is not None:
                for o in self._event_options(event):
                    if o["no"] == c["choice_no"]:
                        opt_name = o["name"]
                        break
            out.append({"team": c["team_name"], "event": name,
                        "choice_no": c["choice_no"], "option": opt_name,
                        "resolved": bool(c["resolved"]), "outcome": c["outcome"]})
        return out

    # ─── 取消分配（管理员撤销本窗口分配给球队的事件） ───────

    async def list_team_events(self, team_name: str, season: int, window_seq: int) -> list[dict]:
        """该队本窗口已分配的事件（类型 + 状态），供取消命令列出可选项。"""
        rows = []
        logs = await self._dao.get_window_events(team_name, season, window_seq)
        choices = [c for c in await self._dao.list_event_choices(season, window_seq)
                   if c["team_name"] == team_name]
        for c in choices:
            event = await self._dao.fetch_event_by_id(c["event_id"])
            name = event["name"] if event else c["event_id"]
            if c["resolved"]:
                status = "已结算"
            elif c["choice_no"] is not None:
                status = "已选未结"
            else:
                status = "待定"
            rows.append({"event": name, "event_id": c["event_id"],
                         "kind": "选择", "status": status})
        choice_ids = {c["event_id"] for c in choices}
        for lg in logs:
            if lg["event_id"] in choice_ids:
                continue
            event = await self._dao.fetch_event_by_id(lg["event_id"])
            name = event["name"] if event else lg["event_id"]
            rows.append({"event": name, "event_id": lg["event_id"],
                         "kind": "即发", "status": "已生效"})
        return rows

    async def cancel_team_event(self, team_name: str, ref: str,
                                season: int, window_seq: int) -> dict:
        """取消本窗口分配给该队的指定事件（每次取消最新一条实例）。

        选择型：未定/已选未结 → 删待定行与广播日志（触发时未动账，无需回退，
        可重新触发）；已结算 → 拒绝（引导 /主场结算 强制 重算）。
        即发型：按该实例日志的 effect_json 回退——删对应最新 event 流水并重算余额、
        死忠按原比例反漴（乘法可交换，多事件叠加同样正确；触及 0/上限钳制有微小误差）、
        上座修正未被消耗（当前值≠1）才回退，已随赛果消耗则跳过并提示。
        """
        logs = await self._dao.get_window_events(team_name, season, window_seq)
        choices = [c for c in await self._dao.list_event_choices(season, window_seq)
                   if c["team_name"] == team_name]
        event_id = await self._resolve_event_ref(ref, logs, choices)
        if event_id is None:
            avail = await self.list_team_events(team_name, season, window_seq)
            if avail:
                listing = "、".join(f"{r['event']}({r['kind']}·{r['status']})" for r in avail)
                raise EventError(f"球队「{team_name}」本窗口没有匹配「{ref}」的事件。现有：{listing}")
            raise EventError(f"球队「{team_name}」本窗口没有分配任何事件")

        event = await self._dao.fetch_event_by_id(event_id)
        name = event["name"] if event else event_id

        choice = next((c for c in choices if c["event_id"] == event_id), None)
        if choice is not None:
            if choice["resolved"]:
                raise EventError(f"「{name}」已结算，无法取消（如需重算可用 /主场结算 强制）")
            await self._dao.delete_event_choice(team_name, season, window_seq, event_id)
            removed_logs = await self._dao.delete_event_logs(team_name, season, window_seq, event_id)
            return {"kind": "choice", "event": name, "event_id": event_id,
                    "removed_logs": removed_logs}

        rows = [lg for lg in logs if lg["event_id"] == event_id]
        if not rows:
            raise EventError(f"球队「{team_name}」本窗口没有「{name}」的即发记录")
        latest = rows[-1]  # get_window_events 按 id 升序 → 取最新一条实例
        try:
            effects = json.loads(latest["effect_json"] or "{}")
        except (ValueError, TypeError):
            effects = {}
        if not isinstance(effects, dict):
            effects = {}
        notes, warnings = [], []
        money = float(effects.get("money", 0.0) or 0.0)
        maintenance = float(effects.get("maintenance", 0.0) or 0.0)
        fans_pct = float(effects.get("fans_pct", 0.0) or 0.0)
        attendance_mod = float(effects.get("attendance_mod", 1.0) or 1.0)

        if (money or maintenance) and event is None:
            warnings.append("事件已不在事件池，资金流水无法按名匹配，请手动核对账目")
        else:
            if money:
                if await self._dao.delete_latest_event_tx(team_name, season, window_seq, name):
                    notes.append(f"资金流水已撤销（{money:+.1f}M）")
                else:
                    warnings.append("未找到对应资金流水（可能已被处理过），跳过")
            if maintenance:
                if await self._dao.delete_latest_event_tx(
                        team_name, season, window_seq, f"{name}（维护）"):
                    notes.append(f"维护流水已撤销（−{maintenance:.1f}M）")
                else:
                    warnings.append("未找到对应维护流水（可能已被处理过），跳过")
            if money or maintenance:
                await self._dao.recompute_balance(team_name)

        if fans_pct:
            stadium = await self._dao.get_stadium(team_name)
            factor = 1.0 + fans_pct
            if stadium and factor > 0:
                cap = float(self._cfg.get("fans_cap", 10000))
                raw = stadium["fans_diehards"] / factor
                fans = min(max(raw, 0.0), cap)
                await self._dao.update_fans(team_name, fans)
                notes.append(f"死忠已按原比例回退（{fans_pct * 100:+.1f}%）")
                if abs(fans - raw) > 1e-9:
                    warnings.append("死忠回退触及 0/上限边界，存在微小误差")
            else:
                warnings.append("死忠比例异常或球队无球场，跳过回退")

        if attendance_mod != 1.0:
            stadium = await self._dao.get_stadium(team_name)
            if stadium and stadium["next_attendance_mod"] != 1.0:
                await self._dao.update_attendance_mod(
                    team_name, stadium["next_attendance_mod"] / attendance_mod)
                notes.append(f"上座修正已回退（×{attendance_mod:.2f}）")
            else:
                warnings.append("上座修正已随赛果消耗（或不存在），跳过回退")

        await self._dao.delete_event_log_by_id(latest["id"])
        return {"kind": "instant", "event": name, "event_id": event_id,
                "occurrences": len(rows) - 1, "notes": notes, "warnings": warnings}

    async def cancel_team_events(self, team_name: str, season: int, window_seq: int) -> dict:
        """取消该队本窗口最近一次分配的全部事件（以事件生成时间为准）。

        events_log / event_choices 的 created_at 取该队最新时间戳，落在
        [最新时间戳−批次窗口, 最新时间戳] 内的行视为同一次分配（窗口默认 60 秒，
        可配 event_batch_window_seconds，0=仅同一秒）：即发型逐条复用
        cancel_team_event 回退数值，选择型未定/已选未结删除待定行与日志；
        已结算跳过并提示。窗口外的更早批次保留（可继续用事件名/id 逐条取消）。
        """
        logs = await self._dao.get_window_events(team_name, season, window_seq)
        choices = [c for c in await self._dao.list_event_choices(season, window_seq)
                   if c["team_name"] == team_name]
        if not logs and not choices:
            raise EventError(f"球队「{team_name}」本窗口没有分配任何事件")
        latest = max([lg["created_at"] for lg in logs]
                     + [c["created_at"] for c in choices])
        window = int(self._cfg.get("event_batch_window_seconds", 60))
        return await self._cancel_batch(
            team_name, season, window_seq,
            self._batch_rows(logs, latest, window),
            self._batch_rows(choices, latest, window),
        )

    async def cancel_latest_assignment(self, season: int, window_seq: int) -> dict:
        """回退全局最近一次分配的全部事件（按生成时间取全窗口最新一批）。

        全窗口 created_at 最大值即为最近一次分配时刻，落在 [该时刻−批次窗口,
        该时刻] 内的行（可跨多队）视为同一次分配，逐队复用 _cancel_batch：
        即发回退数值、选择型未定/已选未结删除，已结算跳过；窗口外批次保留。
        若最近一次分配后隔了超过窗口时长又单队触发，全局最新一批就是该单队
        批次（按时间规则的自然结果）。
        """
        logs = await self._dao.get_window_events_all(season, window_seq)
        choices = await self._dao.list_event_choices(season, window_seq)
        if not logs and not choices:
            raise EventError("本窗口没有分配任何事件")
        latest = max([lg["created_at"] for lg in logs]
                     + [c["created_at"] for c in choices])
        window = int(self._cfg.get("event_batch_window_seconds", 60))
        teams = sorted({lg["team_name"] for lg in logs}
                       | {c["team_name"] for c in choices})
        total = {"cancelled": 0, "instant": 0, "choice": 0,
                 "skipped": [], "lines": []}
        for team in teams:
            batch_logs = self._batch_rows(
                [lg for lg in logs if lg["team_name"] == team], latest, window)
            batch_choices = self._batch_rows(
                [c for c in choices if c["team_name"] == team], latest, window)
            if not batch_logs and not batch_choices:
                continue
            r = await self._cancel_batch(team, season, window_seq,
                                         batch_logs, batch_choices)
            total["cancelled"] += r["cancelled"]
            total["instant"] += r["instant"]
            total["choice"] += r["choice"]
            total["skipped"].extend(f"{team}·{n}" for n in r["skipped"])
            total["lines"].extend(f"{team}：{ln}" for ln in r["lines"])
        return total

    def _batch_rows(self, rows: list, latest: str, window: int) -> list:
        """取一批：created_at 落在 [latest−window 秒, latest] 内的行（0=仅同一秒）。

        时间解析失败的行按「与 latest 完全同秒」的旧行为兜底。
        """
        if window <= 0:
            return [r for r in rows if r["created_at"] == latest]
        try:
            cutoff = datetime.fromisoformat(latest) - timedelta(seconds=window)
        except (ValueError, TypeError):
            return [r for r in rows if r["created_at"] == latest]
        out = []
        for r in rows:
            try:
                dt = datetime.fromisoformat(r["created_at"])
            except (ValueError, TypeError):
                if r["created_at"] == latest:
                    out.append(r)
                continue
            if dt >= cutoff:
                out.append(r)
        return out

    async def _cancel_batch(self, team_name: str, season: int, window_seq: int,
                            batch_logs: list, batch_choices: list) -> dict:
        """取消指定一批事件（已按时间戳过滤好的行）：即发逐条回退、选择待定移除、已结算跳过。"""
        choice_ids = {c["event_id"] for c in batch_choices}
        skipped: list[str] = []
        lines: list[str] = []
        instant = choice = 0
        for c in batch_choices:
            event = await self._dao.fetch_event_by_id(c["event_id"])
            name = event["name"] if event else c["event_id"]
            if c["resolved"]:
                skipped.append(name)
                continue
            await self._dao.delete_event_choice(team_name, season, window_seq, c["event_id"])
            await self._dao.delete_event_logs(team_name, season, window_seq, c["event_id"])
            choice += 1
            lines.append(f"选择「{name}」（待定已移除，未动账目，可重新触发）")
        for eid in sorted({lg["event_id"] for lg in batch_logs} - choice_ids):
            event = await self._dao.fetch_event_by_id(eid)
            name = event["name"] if event else eid
            notes, warnings = [], []
            for _ in [lg for lg in batch_logs if lg["event_id"] == eid]:
                try:
                    r = await self.cancel_team_event(team_name, eid, season, window_seq)
                except EventError as exc:
                    warnings.append(str(exc))
                    break
                for n in r.get("notes") or []:
                    if n not in notes:
                        notes.append(n)
                for w in r.get("warnings") or []:
                    if w not in warnings:
                        warnings.append(w)
            instant += 1
            line = f"即发「{name}」"
            if notes:
                line += "：" + "；".join(notes)
            lines.append(line)
            for w in warnings:
                lines.append(f"⚠️ {w}")
        return {"cancelled": instant + choice, "instant": instant, "choice": choice,
                "skipped": skipped, "lines": lines}

    async def _resolve_event_ref(self, ref, logs, choices) -> str | None:
        """引用解析：先按事件 id 直配，再按池中事件名匹配（fetch 不限状态）。"""
        s = str(ref or "").strip()
        if not s:
            return None
        ids = {lg["event_id"] for lg in logs} | {c["event_id"] for c in choices}
        if s in ids:
            return s
        for eid in sorted(ids):
            event = await self._dao.fetch_event_by_id(eid)
            if event is not None and event["name"] == s:
                return eid
        return None

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
                "{}", json.dumps(d.get("effects") or {}, ensure_ascii=False),
                d["template"], source="llm", status="pending",
                event_type=d.get("event_type", "instant"),
                options_json=json.dumps(d.get("options") or [], ensure_ascii=False),
            )
            added.append({
                "id": event_id, "name": d["name"], "event_type": d.get("event_type", "instant"),
                "effects": d.get("effects") or {}, "options": d.get("options") or [],
            })
        return added

    async def adopt(self, event_id: int) -> None:
        rows = await self._dao.list_events("pending")
        target = next((r for r in rows if r["id"] == event_id), None)
        if target is None:
            raise EventError("待采纳事件不存在（事件通过 主场事件列表 查看 id）")
        # 重名防护：与已采纳事件同名则拒绝（流水 note 与选择录入都按名匹配；
        # 仅看 adopted——pending 草稿不可触发，同名草稿间不算冲突）
        if any(r["name"] == target["name"] and r["event_id"] != target["event_id"]
               for r in await self._dao.list_events("adopted")):
            raise EventError(f"事件名「{target['name']}」与已采纳事件重名，无法采纳（可丢弃后换名重写）")
        await self._dao.adopt_event(event_id)

    async def discard(self, event_id: int) -> None:
        rows = await self._dao.list_events("pending")
        if not any(r["id"] == event_id for r in rows):
            raise EventError("待处理事件不存在")
        await self._dao.discard_event(event_id)

    # ─── 管理员手写 ───────────────────────────────────────

    async def add_custom(self, name: str, category: str, weight: int,
                         effects_text: str, event_type: str = "instant",
                         options_text: str = "") -> None:
        """手写自定义事件。

        即发型：effects_text 为 JSON 文本（money/fans_pct/maintenance/attendance_mod）；
        选择型：options_text 为选项 JSON 数组文本（结构同 LLM 草稿，效果自动钳制）。
        """
        from .llm_writer import _clamp_effects, _clamp_event

        name = str(name or "").strip()
        # 重名防护：与已采纳事件同名即拒绝（流水 note 与选择录入都按名匹配，
        # 同名不同 id 会造成歧义/误删；手写直接入池，故对标已采纳集合）
        if any(r["name"] == name for r in await self._dao.list_events("adopted")):
            raise EventError(f"事件名「{name}」已存在（与已采纳事件重名会导致按名匹配歧义），请换名")
        money_clamp = float(self._cfg.get("event_money_clamp", 8.0))
        fans_clamp = float(self._cfg.get("event_fans_clamp", 0.05))
        maintenance_clamp = float(self._cfg.get("event_maintenance_clamp", 5.0))
        event_id = f"custom_{await self._dao.next_event_counter()}"
        if event_type.lower() == "choice":
            try:
                raw_options = json.loads(options_text)
            except (ValueError, TypeError):
                raise EventError("选择事件需要 options 为 JSON 数组文本")
            try:
                d = _clamp_event(
                    {"event_type": "choice", "name": name, "category": category,
                     "weight": weight, "template": name, "options": raw_options},
                    money_clamp, fans_clamp, maintenance_clamp,
                )
            except ValueError as e:
                raise EventError(str(e))
            await self._dao.upsert_event(
                event_id, d["name"], d["category"], d["weight"],
                "{}", "{}", d["template"], source="custom", status="adopted",
                event_type="choice", options_json=json.dumps(d["options"], ensure_ascii=False),
            )
            return
        import json as _json

        try:
            effects = _json.loads(effects_text)
        except (ValueError, TypeError):
            raise EventError("effects 需为 JSON 文本，如 {\"money\": 3}（money/fans_pct/maintenance/attendance_mod）")
        if not isinstance(effects, dict):
            raise EventError("effects 需为 JSON 对象")
        # 即发效果与 LLM 草稿同强度钳制：越界取边界、非数值直接拒绝，
        # 防止触发时 float() 中断整个分配且无流水可撤销
        try:
            effects = _clamp_effects(effects, money_clamp, fans_clamp, maintenance_clamp)
        except (ValueError, TypeError) as e:
            raise EventError(f"effects 含非法数值（money/fans_pct/maintenance/attendance_mod 需为数字）: {e}")
        await self._dao.upsert_event(
            event_id, name.strip(), category.strip() or "自定义", weight,
            "{}", _json.dumps(effects, ensure_ascii=False), name.strip(),
            source="custom", status="adopted",
        )