"""效果键注册表：随机事件效果的唯一事实源。

每个效果键一个 EffectSpec：apply（落账/落状态+备注）、undo（取消事件时反演）、
describe（概率表/结算描述文案）、clamp（LLM/手写效果钳制）。四处消费方
（event_engine 应用、_outcome_desc 描述、cancel_team_event 反演、
llm_writer 钳制）统一遍历 EFFECT_SPECS，新增键只需：
加 Spec + config 钳幅 + LLM prompt 语义 + 测试（接入指南见 EVENT_KEYS.md）。

顺序即契约：注册表内顺序决定备注拼接与概率表字段的固定次序，
调整顺序属于破坏性变更（玩家可读文案会变）。
"""

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import formula
from .brand_service import _snapshot, build_packages, variant_packages


@dataclass
class ApplyContext:
    """一次效果应用的共享上下文（stadium/naming 惰性取一次并缓存）。"""

    dao: object
    cfg: dict
    team: str
    season: int
    window_seq: int
    label: str
    apply_state: bool = True
    created_ids: list | None = None
    raw: dict | None = None  # effects 源字典引用，供 apply 增补发现状态（见 enrich）
    notes: list = field(default_factory=list)
    _stadium: object | None = field(default=None, repr=False)
    _naming: object | None = field(default=None, repr=False)

    async def stadium(self):
        if self._stadium is None:
            self._stadium = await self.dao.get_stadium(self.team)
        return self._stadium

    async def naming(self):
        if self._naming is None:
            self._naming = await self.dao.get_active_naming(self.team)
        return self._naming

    def enrich(self, key: str, payload) -> None:
        """把 apply 期发现的状态（取消了哪些档位/原天气/报价号）原位写回 effects 源字典。

        引擎在 apply 后才把 effects 序列化进 effect_json / 选择 outcome，因此增补
        会随之落盘，取消反演时由此取回精确状态；redo 时 apply_state=False 不落状态
        也不增补，重算后持久化的仍是普通值。
        """
        if self.raw is not None:
            self.raw[key] = payload


@dataclass
class CancelContext:
    """取消事件的反演上下文（notes/warnings 由调用方汇总输出）。"""

    dao: object
    cfg: dict
    team: str
    season: int
    window_seq: int
    event_name: str
    notes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    _stadium: object | None = field(default=None, repr=False)

    async def stadium(self):
        if self._stadium is None:
            self._stadium = await self.dao.get_stadium(self.team)
        return self._stadium


@dataclass(frozen=True)
class EffectSpec:
    """单个效果键的定义。coerce 把 effects 里的原始值转成 apply/describe 用的类型。"""

    key: str
    apply: object  # async (ApplyContext, value) -> None
    undo: object  # async (CancelContext, value) -> None
    describe: object  # (value, cfg=None) -> str，空串表示该键无可读描述
    clamp: object  # (raw_value, clamps: dict) -> 存储值；None 表示剔除该键
    default: object = 0.0  # effects 缺键时的取值（attendance_mod 为 1.0）
    coerce: object = float
    named_tx: bool = False  # True：撤销需按「事件名（维护）」匹配删资金流水


# ─── apply ────────────────────────────────────────────────


async def _apply_money(ctx: ApplyContext, value: float) -> None:
    if not value:
        return
    (tx_id,) = await ctx.dao.record_entries(
        ctx.team, ctx.season, ctx.window_seq, [("event", value, ctx.label, None)],
    )
    if ctx.created_ids is not None:
        ctx.created_ids.append(tx_id)
    ctx.notes.append(f"资金 {'+' if value > 0 else ''}{value:.1f}M")


async def _apply_maintenance(ctx: ApplyContext, value: float) -> None:
    if not value:
        return
    (tx_id,) = await ctx.dao.record_entries(
        ctx.team, ctx.season, ctx.window_seq,
        [("event", -value, f"{ctx.label}（维护）", None)],
    )
    if ctx.created_ids is not None:
        ctx.created_ids.append(tx_id)
    ctx.notes.append(f"维护 −{value:.1f}M")


async def _apply_fans(ctx: ApplyContext, value: float) -> None:
    if not value:
        return
    if ctx.apply_state:
        s = await ctx.stadium()
        fans = min(max(s["fans_diehards"] * (1.0 + value), 0.0),
                   float(ctx.cfg.get("fans_cap", 10000)))
        await ctx.dao.update_fans(ctx.team, fans)
        ctx.notes.append(f"死忠 {'+' if value > 0 else ''}{value * 100:.1f}%")
    else:
        ctx.notes.append(f"死忠 {value * 100:+.1f}%（重算跳过）")


async def _apply_attendance(ctx: ApplyContext, value: float) -> None:
    if value == 1.0:
        return
    if ctx.apply_state:
        s = await ctx.stadium()
        await ctx.dao.update_attendance_mod(ctx.team, s["next_attendance_mod"] * value)
        ctx.notes.append(f"下一场上座 {'+' if value > 1 else ''}{value * 100:.0f}%")
    else:
        ctx.notes.append(f"下一场上座 ×{value:.2f}（重算跳过）")


async def _apply_satisfaction(ctx: ApplyContext, value: float) -> None:
    # 不在此处落库：由行情演化按权重并入满意度信号，避免与上座/战绩信号双重计账
    if not value:
        return
    if await ctx.naming():
        ctx.notes.append(f"品牌满意度 {'+' if value > 0 else ''}{value:.2f}（计入品牌评估）")


async def _apply_brand_heat(ctx: ApplyContext, value: float) -> None:
    if not value:
        return
    naming = await ctx.naming()
    if not naming:
        return
    if ctx.apply_state:
        rules = formula.parse_json_object(ctx.cfg.get("market_heat_config", "{}"))
        low = float(rules.get("clamp_low", 0.5))
        high = float(rules.get("clamp_high", 1.5))
        brand = await ctx.dao.get_brand_by_name(naming["brand"])
        if brand:
            heat = min(max(float(brand["heat"]) + value, low), high)
            await ctx.dao.update_brand_heat(int(brand["id"]), round(heat, 3))
        ctx.notes.append(f"品牌热度 {'+' if value > 0 else ''}{value:.2f}")
    else:
        ctx.notes.append(f"品牌热度 {'+' if value > 0 else ''}{value:.2f}（重算跳过）")


async def _apply_build_credit(ctx: ApplyContext, value: float) -> None:
    # 沿建设券通道直改 club_balance.build_credit 列（不带余额流水，与返还记账解耦）
    if not value:
        return
    if ctx.apply_state:
        await ctx.dao.apply_balance(ctx.team, 0.0, value)
        ctx.notes.append(f"建设券 {'+' if value > 0 else ''}{value:.1f}M")
    else:
        ctx.notes.append(f"建设券 {value:+.1f}M（重算跳过）")


async def _apply_influence(ctx: ApplyContext, value: float) -> None:
    # 直改 influence 基准值（决策记录 3）：消费端读 live 值自动跟随，不另立修正列
    if not value:
        return
    s = await ctx.stadium()
    if not s:
        return
    if ctx.apply_state:
        await ctx.dao.update_influence(ctx.team, max(0.0, float(s["influence"]) + value))
        ctx.notes.append(f"影响力 {'+' if value > 0 else ''}{value:.1f}")
    else:
        ctx.notes.append(f"影响力 {value:+.1f}（重算跳过）")


async def _apply_signals(ctx: ApplyContext, value: dict) -> None:
    # 信号只出备注、不落状态：实际数值由消费方结算时从事件日志重读（同 satisfaction 先例），
    # 因此无需 apply_state 区分，重算天然同源
    if not isinstance(value, dict):
        return
    for name, v in value.items():
        spec = SIGNAL_DEFAULTS.get(str(name))
        if spec is None:
            continue
        v = float(v)
        if spec["type"] == SIGNAL_MULT:
            ctx.notes.append(f"{spec['label']} ×{v:g}")
        else:
            ctx.notes.append(f"{spec['label']} {'+' if v > 0 else ''}{v:g}")


def _signed_delta(raw) -> int:
    """delta 收敛为 ±1（0/非法→0）；结构键「仅 ±1」的统一口径。"""
    try:
        d = int(raw)
    except (TypeError, ValueError):
        return 0
    return 1 if d > 0 else (-1 if d < 0 else 0)


async def _next_unplayed_home(dao, team: str):
    """该队下一场未赛主场（按 season/window/round 升序首个 result 为空的场次）。"""
    for m in await dao.get_home_matches_all(team):
        if m["result"] is None:
            return m
    return None


def _offer_expire_at(cfg: dict) -> str | None:
    """上门报价 TTL（与市场 _expire_at 同键同格式）。"""
    ttl = int(cfg.get("market_offer_ttl_hours", 72))
    if ttl <= 0:
        return None
    return (datetime.now() + timedelta(hours=ttl)).strftime("%Y-%m-%d %H:%M:%S")


async def _apply_facility(ctx: ApplyContext, value: dict) -> None:
    if not isinstance(value, dict) or not value.get("key"):
        return
    key = str(value["key"])
    delta = _signed_delta(value.get("delta"))
    if not delta:
        return
    name = formula.FACILITY_NAMES.get(key, key)
    if not ctx.apply_state:
        ctx.notes.append(f"设施 {name} {delta:+d} 级（重算跳过）")
        return
    max_level = len(formula.facility_upgrade_costs(ctx.cfg))
    cur = int(await ctx.dao.get_facility_level(ctx.team, key) or 0)
    new = min(max(cur + delta, 0), max_level)
    # 无论是否生效都回写实际 delta（落空=0），取消反演按实际值对称执行
    ctx.enrich("facility", {"key": key, "delta": new - cur})
    if new == cur:
        ctx.notes.append(f"设施 {name} 已在{'最低' if delta < 0 else '最高'}档，{delta:+d} 级落空")
        return
    await ctx.dao.upsert_facility(ctx.team, key, new)
    ctx.notes.append(f"设施 {name} {cur}→{new} 级")


async def _apply_booking_cancel(ctx: ApplyContext, value) -> None:
    n = int(value.get("count", 0)) if isinstance(value, dict) else 0
    if n <= 0:
        return
    if not ctx.apply_state:
        ctx.notes.append(f"档期取消×{n}（重算跳过）")
        return
    bookings = await ctx.dao.get_bookings(ctx.team, ctx.season, ctx.window_seq)
    targets = sorted(bookings, key=lambda r: int(r["slot_no"]), reverse=True)[:n]
    if not targets:
        ctx.notes.append("本窗无已订档期，取消落空")
        return
    cancelled = []
    for b in targets:
        slot = int(b["slot_no"])
        await ctx.dao.delete_booking(ctx.team, ctx.season, ctx.window_seq, slot)
        cancelled.append({"slot_no": slot, "activity_type": b["activity_type"],
                          "booked_by": b["booked_by"] or ""})
    ctx.enrich("booking_cancel", {"count": len(cancelled), "cancelled": cancelled})
    names = formula.activity_names(ctx.cfg)
    detail = "、".join(f"档{c['slot_no']} {names.get(c['activity_type'], c['activity_type'])}"
                       for c in cancelled)
    ctx.notes.append(f"档期取消×{len(cancelled)}（{detail}）")


async def _apply_booking_gift(ctx: ApplyContext, value) -> None:
    if not isinstance(value, dict) or not value.get("type"):
        return
    act = str(value["type"])
    label = formula.activity_names(ctx.cfg).get(act, act)
    if not ctx.apply_state:
        ctx.notes.append(f"档期赠送：{label}（重算跳过）")
        return
    max_slots = int(ctx.cfg.get("activity_slots", 2))
    used = {int(r["slot_no"]) for r in
            await ctx.dao.get_bookings(ctx.team, ctx.season, ctx.window_seq)}
    slot = next((i for i in range(1, max_slots + 1) if i not in used), 0)
    if not slot:
        ctx.notes.append(f"本窗档期已满，赠送「{label}」落空")
        return
    await ctx.dao.add_booking(ctx.team, ctx.season, ctx.window_seq, slot, act, "事件")
    ctx.enrich("booking_gift", {"type": act, "slot_no": slot})
    ctx.notes.append(f"档期赠送：{label}（档{slot}）")


async def _apply_weather_set(ctx: ApplyContext, value) -> None:
    if not isinstance(value, dict) or not value.get("weather"):
        return
    weather = str(value["weather"])
    if not ctx.apply_state:
        ctx.notes.append(f"主场天气改为「{weather}」（重算跳过）")
        return
    match = await _next_unplayed_home(ctx.dao, ctx.team)
    if match is None:
        ctx.notes.append(f"暂无未赛主场，「{weather}」落空")
        return
    old = match["weather"]
    await ctx.dao.set_match_weather(int(match["id"]), weather)
    ctx.enrich("weather_set", {"weather": weather, "match_id": int(match["id"]), "old": old})
    ctx.notes.append(f"第{match['window_seq']}窗第{match['round_no']}轮主场天气改为「{weather}」")


async def _apply_offer_spawn(ctx: ApplyContext, value) -> None:
    n = int(value.get("count", 0)) if isinstance(value, dict) else 0
    if n <= 0:
        return
    if not ctx.apply_state:
        ctx.notes.append("品牌上门递价（重算跳过）")
        return
    rounds = await ctx.dao.list_open_rounds()
    if not rounds:
        ctx.notes.append("当前无开放招商轮次，品牌上门落空")
        return
    s = await ctx.stadium()
    if not s:
        return
    candidates = [b for b in await ctx.dao.list_brands("adopted")
                  if await ctx.dao.count_active_namings_by_brand(b["brand"]) == 0]
    if not candidates:
        ctx.notes.append("暂无可上门的未签约品牌")
        return
    weights = [max(float(b["heat"]), 0.0) for b in candidates]
    brand = (random.choices(candidates, weights=weights, k=1)[0] if sum(weights) > 0
             else random.choice(candidates))
    fee = formula.naming_fee(ctx.cfg, float(s["capacity"]), float(s["fans_diehards"]),
                             float(brand["heat"]))
    fee = round(fee * formula.industry_factor(ctx.cfg, brand["industry"] or "通用"), 3)
    packages = (variant_packages(ctx.cfg, fee, brand["packages_json"] or "{}")
                or build_packages(ctx.cfg, fee))
    package = packages[0]  # 稳健结构（与先到先得公开挂价同口径）
    offer_id = await ctx.dao.add_market_offer(
        int(rounds[-1]["id"]), int(brand["id"]), ctx.team, int(package["pkg_no"]),
        float(package["amount"]), int(package["windows"]),
        json.dumps(_snapshot(package), ensure_ascii=False), _offer_expire_at(ctx.cfg))
    if not offer_id:
        # INSERT OR IGNORE 撞 (round, brand, team) 唯一键：该队本轮已有该品牌的
        # 待处理报价，本次上门不重复建价（撤销也无状态可回退，不 enrich）
        ctx.notes.append(f"品牌「{brand['brand']}」已有待处理的上门报价，本次落空")
        return
    ctx.enrich("offer_spawn", {"offer_id": int(offer_id)})
    ctx.notes.append(f"品牌「{brand['brand']}」上门递价：{float(package['amount']):g}M/窗 × "
                     f"{package['windows']} 窗（{package['pkg_name']}，/主场签约 可签）")


# ─── undo（取消事件反演；幂等，动不了的记 warning） ─────────


async def _undo_noop(ctx: CancelContext, value) -> None:
    return None


async def _undo_money(ctx: CancelContext, value: float) -> None:
    if not value:
        return
    if await ctx.dao.delete_latest_event_tx(ctx.team, ctx.season, ctx.window_seq,
                                            ctx.event_name):
        ctx.notes.append(f"资金流水已撤销（{value:+.1f}M）")
    else:
        ctx.warnings.append("未找到对应资金流水（可能已被处理过），跳过")


async def _undo_maintenance(ctx: CancelContext, value: float) -> None:
    if not value:
        return
    if await ctx.dao.delete_latest_event_tx(ctx.team, ctx.season, ctx.window_seq,
                                            f"{ctx.event_name}（维护）"):
        ctx.notes.append(f"维护流水已撤销（−{value:.1f}M）")
    else:
        ctx.warnings.append("未找到对应维护流水（可能已被处理过），跳过")


async def _undo_fans(ctx: CancelContext, value: float) -> None:
    if not value:
        return
    s = await ctx.stadium()
    factor = 1.0 + value
    if s and factor > 0:
        cap = float(ctx.cfg.get("fans_cap", 10000))
        raw = s["fans_diehards"] / factor
        fans = min(max(raw, 0.0), cap)
        await ctx.dao.update_fans(ctx.team, fans)
        ctx.notes.append(f"死忠已按原比例回退（{value * 100:+.1f}%）")
        if abs(fans - raw) > 1e-9:
            ctx.warnings.append("死忠回退触及 0/上限边界，存在微小误差")
    else:
        ctx.warnings.append("死忠比例异常或球队无球场，跳过回退")


async def _undo_attendance(ctx: CancelContext, value: float) -> None:
    if value == 1.0:
        return
    s = await ctx.stadium()
    if s and s["next_attendance_mod"] != 1.0:
        await ctx.dao.update_attendance_mod(ctx.team, s["next_attendance_mod"] / value)
        ctx.notes.append(f"上座修正已回退（×{value:.2f}）")
    else:
        ctx.warnings.append("上座修正已随赛果消耗（或不存在），跳过回退")


async def _undo_build_credit(ctx: CancelContext, value: float) -> None:
    # 反向加减（行不存在时 apply_balance 为无操作）；已花掉的部分会透支为负，
    # deduct_build_credit 只扣不抵，负余额不会透支建设
    if not value:
        return
    await ctx.dao.apply_balance(ctx.team, 0.0, -value)
    ctx.notes.append(f"建设券已反向调整（{value:+.1f}M）")


async def _undo_influence(ctx: CancelContext, value: float) -> None:
    if not value:
        return
    s = await ctx.stadium()
    if s:
        raw = float(s["influence"]) - value
        await ctx.dao.update_influence(ctx.team, max(0.0, raw))
        ctx.notes.append(f"影响力已回退（{value:+.1f}）")
        if raw < 0:
            ctx.warnings.append("影响力回退触及 0 下界，存在微小误差")
    else:
        ctx.warnings.append("球队无球场，跳过影响力回退")


async def _undo_facility(ctx: CancelContext, value) -> None:
    if not isinstance(value, dict) or not value.get("key"):
        return
    delta = _signed_delta(value.get("delta"))
    if not delta:
        return
    key = str(value["key"])
    name = formula.FACILITY_NAMES.get(key, key)
    max_level = len(formula.facility_upgrade_costs(ctx.cfg))
    cur = int(await ctx.dao.get_facility_level(ctx.team, key) or 0)
    new = min(max(cur - delta, 0), max_level)
    if new == cur:
        ctx.warnings.append(f"设施 {name} 等级已被手动调整，无法按原路径回退")
        return
    await ctx.dao.upsert_facility(ctx.team, key, new)
    ctx.notes.append(f"设施 {name} 已回退（{cur}→{new} 级）")


async def _undo_booking_cancel(ctx: CancelContext, value) -> None:
    # 恢复 apply 时记下的具体档位（enrich 进 effect_json 的发现状态）
    if not isinstance(value, dict):
        return
    cancelled = value.get("cancelled")
    if not isinstance(cancelled, list) or not cancelled:
        return
    used = {int(r["slot_no"]) for r in
            await ctx.dao.get_bookings(ctx.team, ctx.season, ctx.window_seq)}
    restored = 0
    for c in cancelled:
        try:
            slot = int(c.get("slot_no"))
        except (TypeError, ValueError):
            continue
        if slot in used:
            ctx.warnings.append(f"档{slot} 已被重新占用，跳过恢复")
            continue
        await ctx.dao.add_booking(ctx.team, ctx.season, ctx.window_seq, slot,
                                  str(c.get("activity_type") or "idle"),
                                  str(c.get("booked_by") or ""))
        used.add(slot)
        restored += 1
    if restored:
        ctx.notes.append(f"已恢复被取消的档期×{restored}")


async def _undo_booking_gift(ctx: CancelContext, value) -> None:
    if not isinstance(value, dict):
        return
    try:
        slot = int(value.get("slot_no"))
    except (TypeError, ValueError):
        return
    act = str(value.get("type") or "")
    current = next((r for r in await ctx.dao.get_bookings(ctx.team, ctx.season, ctx.window_seq)
                    if int(r["slot_no"]) == slot), None)
    if current is None:
        ctx.warnings.append(f"档{slot} 赠送档期已不存在，跳过撤销")
    elif current["activity_type"] == act and (current["booked_by"] or "") == "事件":
        await ctx.dao.delete_booking(ctx.team, ctx.season, ctx.window_seq, slot)
        ctx.notes.append(f"赠送档期已撤销（档{slot}）")
    else:
        ctx.warnings.append(f"档{slot} 已被其他安排替换，跳过撤销")


async def _undo_weather_set(ctx: CancelContext, value) -> None:
    if not isinstance(value, dict):
        return
    try:
        match_id = int(value.get("match_id"))
    except (TypeError, ValueError):
        return
    weather = str(value.get("weather") or "")
    match = await ctx.dao.get_match(match_id)
    if match is None:
        ctx.warnings.append("场次已不存在，跳过天气回退")
    elif match["result"] is not None:
        ctx.warnings.append("该场已录赛果，天气不回退")
    elif (match["weather"] or "") != weather:
        ctx.warnings.append("天气已被其他调整覆盖，跳过回退")
    else:
        await ctx.dao.set_match_weather(match_id, value.get("old"))
        ctx.notes.append(f"主场天气已回退（原「{value.get('old') or '未设定'}」）")


async def _undo_offer_spawn(ctx: CancelContext, value) -> None:
    if not isinstance(value, dict):
        return
    offer_id = value.get("offer_id")
    try:
        offer_id = int(offer_id)
    except (TypeError, ValueError):
        return
    if not offer_id:
        return
    offer = await ctx.dao.get_market_offer(offer_id)
    if offer is None:
        ctx.warnings.append("上门报价已过期清理，跳过撤销")
    elif offer["status"] != "pending":
        ctx.warnings.append("上门报价已被签约，不撤销")
    else:
        await ctx.dao.delete_market_offer(offer_id)
        ctx.notes.append("上门报价已撤销")


# ─── describe（概率表/结算描述，与备注口径一致但为独立文案） ─


def _desc_money(v: float, cfg=None) -> str:
    return f"资金{v:+.1f}M" if v else ""


def _desc_maintenance(v: float, cfg=None) -> str:
    return f"维护−{v:.1f}M" if v else ""


def _desc_fans(v: float, cfg=None) -> str:
    return f"死忠{v * 100:+.1f}%" if v else ""


def _desc_attendance(v: float, cfg=None) -> str:
    return f"上座×{v:.2f}" if v != 1.0 else ""


def _desc_satisfaction(v: float, cfg=None) -> str:
    return f"品牌满意度{v:+.2f}" if v else ""


def _desc_brand_heat(v: float, cfg=None) -> str:
    return f"品牌热度{v:+.2f}" if v else ""


def _desc_build_credit(v: float, cfg=None) -> str:
    return f"建设券{v:+.1f}M" if v else ""


def _desc_influence(v: float, cfg=None) -> str:
    return f"影响力{v:+.1f}" if v else ""


def _desc_signals(value, cfg=None) -> str:
    if not isinstance(value, dict) or not value:
        return ""
    parts = []
    for name, v in value.items():
        spec = SIGNAL_DEFAULTS.get(str(name))
        if spec is None:
            continue
        v = float(v)
        if spec["type"] == SIGNAL_MULT:
            parts.append(f"{spec['label']}×{v:g}")
        else:
            parts.append(f"{spec['label']}{'+' if v > 0 else ''}{v:g}")
    return " / ".join(parts)


def _desc_facility(value, cfg=None) -> str:
    if not isinstance(value, dict) or not value.get("key"):
        return ""
    d = _signed_delta(value.get("delta"))
    if not d:
        return ""
    label = formula.FACILITY_NAMES.get(str(value["key"]), str(value["key"]))
    return f"设施{label}{d:+d}级"


def _desc_booking_cancel(value, cfg=None) -> str:
    n = int(value.get("count", 0)) if isinstance(value, dict) else 0
    return f"档期取消×{n}" if n > 0 else ""


def _desc_booking_gift(value, cfg=None) -> str:
    if not isinstance(value, dict) or not value.get("type"):
        return ""
    act = str(value["type"])
    names = formula.activity_names(cfg) if cfg else {}
    return f"赠档期:{names.get(act, act)}"


def _desc_weather_set(value, cfg=None) -> str:
    if not isinstance(value, dict) or not value.get("weather"):
        return ""
    return f"天气→{value['weather']}"


def _desc_offer_spawn(value, cfg=None) -> str:
    n = int(value.get("count", 0)) if isinstance(value, dict) else 0
    return "品牌上门" if n > 0 else ""


# ─── clamp（LLM/手写钳制；raw 值来自 JSON，非数值抛错拒绝） ──


def _clamp_money(v, clamps: dict):
    return round(max(-clamps["money"], min(clamps["money"], float(v))), 3)


def _clamp_fans(v, clamps: dict):
    return round(max(-clamps["fans"], min(clamps["fans"], float(v))), 4)


def _clamp_maintenance(v, clamps: dict):
    # 维护只允许支出（0 ~ 上限），不允许负维护退款
    return round(max(0.0, min(clamps["maintenance"], float(v))), 3)


def _clamp_attendance(v, clamps: dict):
    v = float(v)
    if v == 1.0:
        return None
    return round(max(0.5, min(2.0, v)), 3)


def _clamp_signed(v, clamps: dict, name: str, default: float):
    """对称区间钳制，round 2（与满意度/热度 {:+.2f} 展示口径一致）；0 视为无效果不落键。"""
    c = float(clamps.get(name, default))
    v = round(max(-c, min(c, float(v))), 2)
    return v if v else None


def _clamp_satisfaction(v, clamps: dict):
    # 实际生效仍以球队有冠名品牌为前提（apply 阶段判命名()），钳制只限强度
    return _clamp_signed(v, clamps, "satisfaction", 0.5)


def _clamp_brand_heat(v, clamps: dict):
    # apply 阶段另有 market_heat_config clamp_low/high 终钳，此处限单次变化幅度
    return _clamp_signed(v, clamps, "brand_heat", 0.3)


def _clamp_build_credit(v, clamps: dict):
    return _clamp_signed(v, clamps, "build_credit", 5.0)


def _clamp_influence(v, clamps: dict):
    return _clamp_signed(v, clamps, "influence", 10.0)


def _clamp_signals(v, clamps: dict):
    """信号字典钳制：未知名剔除、步进型对称钳、乘数型区间钳、0 值剔除。

    边界读 clamps["signals"]（event_signal_config 解析结果），缺省用 SIGNAL_DEFAULTS 内置值。
    """
    if not isinstance(v, dict) or not v:
        return None
    scfg = clamps.get("signals") or {}
    out = {}
    for name, raw in v.items():
        spec = SIGNAL_DEFAULTS.get(str(name))
        if spec is None:
            continue
        try:
            x = float(raw)
        except (TypeError, ValueError):
            continue
        if spec["type"] == SIGNAL_STEP:
            c = float(scfg.get("fan_mood_clamp", spec["clamp"]))
            x = round(max(-c, min(c, x)), 2)
        else:
            low = float(scfg.get(f"{name}_low", spec["clamp"][0]))
            high = float(scfg.get(f"{name}_high", spec["clamp"][1]))
            x = round(max(low, min(high, x)), 3)
        if x:
            out[str(name)] = x
    return out or None


def _coerce_facility(v):
    return v if isinstance(v, dict) else {}


def _coerce_count(v):
    """计数型标量归一：1 → {"count": 1}（取消反演需兼容 enrich 后的 dict 形态）。"""
    if isinstance(v, dict):
        return v
    try:
        n = int(v)
    except (TypeError, ValueError):
        return {}
    return {"count": n} if n > 0 else {}


def _coerce_type(v):
    if isinstance(v, dict):
        return v
    return {"type": str(v)} if v else {}


def _coerce_weather(v):
    if isinstance(v, dict):
        return v
    return {"weather": str(v)} if v else {}


def _clamp_facility(v, clamps: dict):
    if not isinstance(v, dict) or not v.get("key"):
        return None
    key = str(v["key"])
    allowed = clamps.get("facility_keys")
    if allowed is not None and key not in allowed:
        return None
    d = _signed_delta(v.get("delta"))
    return {"key": key, "delta": d} if d else None


def _clamp_booking_cancel(v, clamps: dict):
    n = int(v.get("count", 0)) if isinstance(v, dict) else _signed_delta(v)
    cap = max(int(float(clamps.get("booking_cancel", 2.0))), 0)
    n = min(max(n, 0), cap)
    return {"count": n} if n > 0 else None


def _clamp_booking_gift(v, clamps: dict):
    raw = v.get("type") if isinstance(v, dict) else v
    if not raw:
        return None
    act = str(raw)
    allowed = clamps.get("activity_keys")
    if allowed is not None and act not in allowed:
        return None
    return {"type": act}


def _clamp_weather_set(v, clamps: dict):
    raw = v.get("weather") if isinstance(v, dict) else v
    if not raw:
        return None
    w = str(raw)
    allowed = clamps.get("weather_keys")
    if allowed is not None and w not in allowed:
        return None
    return {"weather": w}


def _clamp_offer_spawn(v, clamps: dict):
    n = int(v.get("count", 0)) if isinstance(v, dict) else _signed_delta(v)
    return {"count": 1} if n > 0 else None


# ─── 信号总线（signals 键的运行时支撑：采集/聚合，供结算侧消费） ──

SIGNAL_STEP = "step"    # 步进型：消费侧按队 Σ（值即百分点步进）
SIGNAL_MULT = "mult"    # 乘数型：消费侧按队 Π(值) 后终钳（值本身即乘数，0.8=×0.8）

SIGNAL_DEFAULTS: dict = {
    "fan_mood": {"label": "粉丝情绪", "type": SIGNAL_STEP, "clamp": 2.0},
    "upkeep": {"label": "维护负担", "type": SIGNAL_MULT, "clamp": (0.5, 2.0)},
    "fee_mod": {"label": "冠名费", "type": SIGNAL_MULT, "clamp": (0.5, 2.0)},
}


def signal_clamp_config(cfg: dict) -> dict:
    """event_signal_config 解析为钳幅字典（非法/缺省回 {}，钳制走内置值）。"""
    return formula.parse_json_object(cfg.get("event_signal_config", "{}"))


def _coerce_signals(v):
    return v if isinstance(v, dict) else {}


async def collect_window_signals(dao, season: int, window_seq: int) -> dict:
    """汇总本窗口事件信号：{信号名: {队名: [值, ...]}}。

    来源=即发事件 effect_json.signals（全队）+ 选择结算 outcome.effects.signals（按队）。
    只读事件日志，不落状态——首结与重算同源；未知名/非法值静默跳过。
    """
    collected: dict = {}

    def _feed(team: str, signals) -> None:
        if not isinstance(signals, dict):
            return
        for name, raw in signals.items():
            name = str(name)
            if name not in SIGNAL_DEFAULTS:
                continue
            try:
                collected.setdefault(name, {}).setdefault(team, []).append(float(raw))
            except (TypeError, ValueError):
                continue

    for row in await dao.get_window_events_all(season, window_seq):
        effects = formula.parse_json_object(row["effect_json"])
        _feed(row["team_name"], effects.get("signals"))
    for s in await dao.list_stadiums():
        for ch in await dao.get_window_choice_outcomes(s["team_name"], season, window_seq):
            outcome = formula.parse_json_object(ch["outcome"])
            effects = outcome.get("effects") if isinstance(outcome.get("effects"), dict) else {}
            _feed(s["team_name"], effects.get("signals"))
    return collected


def step_value(signals: dict, name: str, team: str) -> float:
    """步进型聚合：某队某信号 Σ；无信号返回 0.0。"""
    return round(sum((signals.get(name) or {}).get(team) or []), 4)


def mult_factor(signals: dict, name: str, team: str, low: float, high: float) -> float:
    """乘数型聚合：某队某信号 Π(值) 后终钳 [low, high]；无信号返回 1.0。"""
    values = (signals.get(name) or {}).get(team) or []
    if not values:
        return 1.0
    factor = 1.0
    for v in values:
        factor *= float(v)
    return round(min(max(factor, low), high), 4)


EFFECT_SPECS: tuple[EffectSpec, ...] = (
    EffectSpec(key="money", apply=_apply_money, undo=_undo_money,
               describe=_desc_money, clamp=_clamp_money, named_tx=True),
    EffectSpec(key="maintenance", apply=_apply_maintenance, undo=_undo_maintenance,
               describe=_desc_maintenance, clamp=_clamp_maintenance, named_tx=True),
    EffectSpec(key="fans_pct", apply=_apply_fans, undo=_undo_fans,
               describe=_desc_fans, clamp=_clamp_fans),
    EffectSpec(key="attendance_mod", apply=_apply_attendance, undo=_undo_attendance,
               describe=_desc_attendance, clamp=_clamp_attendance, default=1.0),
    EffectSpec(key="satisfaction", apply=_apply_satisfaction, undo=_undo_noop,
               describe=_desc_satisfaction, clamp=_clamp_satisfaction),
    EffectSpec(key="brand_heat", apply=_apply_brand_heat, undo=_undo_noop,
               describe=_desc_brand_heat, clamp=_clamp_brand_heat),
    EffectSpec(key="build_credit", apply=_apply_build_credit, undo=_undo_build_credit,
               describe=_desc_build_credit, clamp=_clamp_build_credit),
    EffectSpec(key="influence", apply=_apply_influence, undo=_undo_influence,
               describe=_desc_influence, clamp=_clamp_influence),
    EffectSpec(key="signals", apply=_apply_signals, undo=_undo_noop,
               describe=_desc_signals, clamp=_clamp_signals, default={}, coerce=_coerce_signals),
    EffectSpec(key="facility", apply=_apply_facility, undo=_undo_facility,
               describe=_desc_facility, clamp=_clamp_facility, default={}, coerce=_coerce_facility),
    EffectSpec(key="booking_cancel", apply=_apply_booking_cancel, undo=_undo_booking_cancel,
               describe=_desc_booking_cancel, clamp=_clamp_booking_cancel, coerce=_coerce_count),
    EffectSpec(key="booking_gift", apply=_apply_booking_gift, undo=_undo_booking_gift,
               describe=_desc_booking_gift, clamp=_clamp_booking_gift, coerce=_coerce_type),
    EffectSpec(key="weather_set", apply=_apply_weather_set, undo=_undo_weather_set,
               describe=_desc_weather_set, clamp=_clamp_weather_set, coerce=_coerce_weather),
    EffectSpec(key="offer_spawn", apply=_apply_offer_spawn, undo=_undo_offer_spawn,
               describe=_desc_offer_spawn, clamp=_clamp_offer_spawn, coerce=_coerce_count),
)
