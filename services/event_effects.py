"""效果键注册表：随机事件效果的唯一事实源。

每个效果键一个 EffectSpec：apply（落账/落状态+备注）、undo（取消事件时反演）、
describe（概率表/结算描述文案）、clamp（LLM/手写效果钳制）。四处消费方
（event_engine 应用、_outcome_desc 描述、cancel_team_event 反演、
llm_writer 钳制）统一遍历 EFFECT_SPECS，新增键只需：
加 Spec + config 钳幅 + LLM prompt 语义 + 测试（接入指南见 EVENT_KEYS.md）。

顺序即契约：注册表内顺序决定备注拼接与概率表字段的固定次序，
调整顺序属于破坏性变更（玩家可读文案会变）。
"""

from dataclasses import dataclass, field

from . import formula


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
    describe: object  # (value) -> str，空串表示该键无可读描述
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


# ─── describe（概率表/结算描述，与备注口径一致但为独立文案） ─


def _desc_money(v: float) -> str:
    return f"资金{v:+.1f}M" if v else ""


def _desc_maintenance(v: float) -> str:
    return f"维护−{v:.1f}M" if v else ""


def _desc_fans(v: float) -> str:
    return f"死忠{v * 100:+.1f}%" if v else ""


def _desc_attendance(v: float) -> str:
    return f"上座×{v:.2f}" if v != 1.0 else ""


def _desc_satisfaction(v: float) -> str:
    return f"品牌满意度{v:+.2f}" if v else ""


def _desc_brand_heat(v: float) -> str:
    return f"品牌热度{v:+.2f}" if v else ""


def _desc_build_credit(v: float) -> str:
    return f"建设券{v:+.1f}M" if v else ""


def _desc_influence(v: float) -> str:
    return f"影响力{v:+.1f}" if v else ""


def _desc_signals(value) -> str:
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
)
