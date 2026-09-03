"""LLM 文案与设计：运行时事件文案、事件/品牌草稿生成。

职责边界：LLM 只碰文案和创意草稿，数值效果永远由事件/品牌结构 + 配置钳制决定。
所有调用失败/超时/无 provider 均回退（文案→模板，设计→报错给管理员）。
"""

import asyncio
import json
import re

from astrbot.api import logger

from ..utils.security import sanitize_text
from . import formula
from .event_effects import EFFECT_SPECS, signal_clamp_config

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


class LlmWriter:
    def __init__(self, cfg: dict, get_provider=None):
        self._cfg = cfg
        self._get_provider = get_provider or (lambda: None)

    def enabled(self, flavor: bool = True) -> bool:
        if flavor:
            return bool(self._cfg.get("llm_flavor_enabled", True))
        return bool(self._cfg.get("llm_design_enabled", True))

    async def _ask(self, prompt: str, quiet: bool = False) -> str | None:
        """发起 LLM 调用；失败/空内容返回 None 并分类告警（便于归因）。

        quiet=True 时不打印失败原因（设计类命令重试时避免重复告警）。
        """
        try:
            provider = self._get_provider()
            if provider is None:
                if not quiet:
                    logger.warning(
                        "LLM provider 不可用（未配置，或 AstrBot 未选用可用模型），"
                        "本次调用跳过（prompt %d 字符）。",
                        len(prompt),
                    )
                return None
            timeout = float(self._cfg.get("llm_timeout_seconds", 60))
            try:
                result = await asyncio.wait_for(
                    provider.text_chat(prompt, session_id="whl_stadium_llm"),
                    timeout=timeout,
                )
            except (asyncio.TimeoutError, TimeoutError):
                # 3.11 前 asyncio.TimeoutError 与内置 TimeoutError 不同，两者都要捕获
                if not quiet:
                    logger.warning(
                        "LLM 调用超时（>%gs，可通过配置 llm_timeout_seconds 调大）。",
                        timeout,
                    )
                return None
            text = _extract_llm_text(result)
            if text is None:
                if not quiet:
                    logger.warning(
                        "LLM 返回对象缺少文本字段 completion_text/result_str"
                        "（prompt %d 字符，对象类型 %s），视为空内容。",
                        len(prompt),
                        type(result).__name__,
                    )
                return None
            if not text:
                if not quiet:
                    logger.warning("LLM 返回空内容（prompt %d 字符）。", len(prompt))
                return None
            return text
        except Exception as e:
            if not quiet:
                logger.warning(f"LLM call failed: {e}")
            return None

    # ─── 事件文案 ─────────────────────────────────────────

    async def event_text(self, event_row, team: str, stadium_name: str, context: str = "") -> str:
        template = event_row["template"] or f"{event_row['name']}"
        if not self.enabled(flavor=True):
            return _fill_template(template, team, stadium_name)
        name = str(event_row["name"])
        category = str(event_row["category"]) if "category" in event_row.keys() else ""
        prompt = (
            "你是足球俱乐部的场馆运营播报员，在球迷群里播报一条主场随机事件。\n"
            "用中文写 1~2 句群聊风格的短播报：语气活泼、有画面感，可以带点梗。\n"
            "事实信息（不得改动，也不得另外编造）：\n"
            f"事件：{name}（类别：{category}）\n"
            f"球队：{team}，球场：{stadium_name}\n"
            f"附加背景：{context or '无'}\n"
            "硬性要求：\n"
            "- 总长不超过 80 字，一行输出，不要换行。\n"
            "- 不要出现任何金额、百分比或其他数值——数值由系统另行结算公布。\n"
            "- 只输出播报文案本身，不要解释，不要用引号包裹。"
        )
        text = await self._ask(prompt)
        if not text:
            logger.warning("事件文案 LLM 调用无内容，回退模板（%s·%s）。", team, name)
            return _fill_template(template, team, stadium_name)
        return _fill_template(text[:120], team, stadium_name)

    # ─── 事件广播与结算 ────────────────────────────────────

    async def event_broadcast(self, event, team: str, stadium: str) -> str:
        """选择型事件广播的叙述段（LLM；失败/关闭回退模板）。

        选项与概率由插件确定性拼接，叙述只负责氛围，绝不列选项/概率/金额。
        """
        name = str(event["name"])
        category = str(event["category"]) if "category" in event.keys() else ""
        template = event["template"] or name
        if not self.enabled(flavor=True):
            return _fill_template(template, team, stadium)
        prompt = (
            "你是足球俱乐部的赛事运营文案。一次主场随机事件即将广播给各位教练，"
            "由他们自行选择应对方式，你只负责写广播中的情境叙述段。\n"
            "用中文写 2~4 句：把事件的情境与氛围写生动、有画面感，结尾保持悬念，"
            "把选择留给读者。\n"
            "事实信息（不得改动）：\n"
            f"事件：{name}（{category}）；球队：{team}，球场：{stadium}。\n"
            "硬性要求：\n"
            "- 总长不超过 150 字。\n"
            "- 不要列举应对方式，不要出现选项编号（①②③④）或任何数字列表。\n"
            "- 不要出现概率、百分比或金额数值。\n"
            "- 不要替读者做决定或给出建议。\n"
            "- 只输出叙述正文，不要解释、不要标题。"
        )
        text = await self._ask(prompt)
        if not text:
            logger.warning("事件广播 LLM 调用无内容，回退模板（%s·%s）。", team, name)
            return _fill_template(template, team, stadium)
        return _fill_template(text[:200], team, stadium)

    async def event_result(self, event, team: str, how: str, notes) -> str:
        """结算结果短文案（LLM；失败/关闭返回空串，由调用方回退确定性摘要）。"""
        name = str(event["name"])
        if not self.enabled(flavor=True):
            return ""
        effect_txt = "、".join(notes) if notes else "无变化"
        prompt = (
            "你是足球俱乐部的赛事播报员，播报一次主场随机事件的结算结果。\n"
            "用中文写 1~2 句简短播报，说清这次结果的后果，语气简洁。\n"
            "事实信息（照实播报，不得改动、不得编造其他数值）：\n"
            f"事件：{name}；球队：{team}；结果：{how}；效果：{effect_txt}。\n"
            "硬性要求：\n"
            "- 总长不超过 80 字。\n"
            "- 「效果」中给出的数值可照实引用；除此之外不要出现任何金额、百分比。\n"
            "- 不要提及概率、运气或选项编号（①②③④）。\n"
            "- 只输出播报，不要解释。"
        )
        text = await self._ask(prompt)
        if not text:
            return ""
        return text[:120]

    # ─── 事件设计 ─────────────────────────────────────────

    async def design_events(self, count: int, topic: str = "") -> list[dict]:
        if not self.enabled(flavor=False):
            raise RuntimeError("LLM 事件设计未启用（llm_design_enabled）")
        clamps = {
            "money": float(self._cfg.get("event_money_clamp", 8.0)),
            "fans": float(self._cfg.get("event_fans_clamp", 0.05)),
            "maintenance": float(self._cfg.get("event_maintenance_clamp", 5.0)),
            "satisfaction": float(self._cfg.get("event_satisfaction_clamp", 0.5)),
            "brand_heat": float(self._cfg.get("event_brand_heat_clamp", 0.3)),
            "build_credit": float(self._cfg.get("event_build_credit_clamp", 5.0)),
            "influence": float(self._cfg.get("event_influence_clamp", 10.0)),
        }
        clamps["signals"] = signal_clamp_config(self._cfg)
        clamps["booking_cancel"] = float(self._cfg.get("event_booking_cancel_clamp", 2.0))
        # 结构键的合法取值域：设施/活动/天气键来自配置（缺省不校验）
        clamps["facility_keys"] = set(formula.facility_effects(self._cfg))
        clamps["activity_keys"] = set(formula.activity_names(self._cfg))
        clamps["weather_keys"] = set(formula.weather_probabilities(self._cfg))
        clamps["facility_max_level"] = len(formula.facility_upgrade_costs(self._cfg))
        sat_c, heat_c = clamps["satisfaction"], clamps["brand_heat"]
        bc_c, inf_c = clamps["build_credit"], clamps["influence"]
        sig_c = clamps["signals"]
        fac_names = formula.FACILITY_NAMES
        act_names = formula.activity_names(self._cfg)
        wx_names = formula.weather_probabilities(self._cfg)
        cancel_c = clamps["booking_cancel"]
        prompt = (
            "为足球联赛主场随机事件系统设计新事件。\n"
            f"任务：设计 {count} 个事件，主题：{topic or '不限'}。\n"
            "数值边界（越界会被系统自动钳制）："
            f"金额绝对值 ≤ {clamps['money']}M；死忠比例变化绝对值 ≤ {clamps['fans']}"
            "（小数，0.03 即 3%）；"
            f"维护支出 0 ~ {clamps['maintenance']}M；"
            f"品牌满意度变化绝对值 ≤ {sat_c}（正=合作愉快提升评估，负=损害合作关系），"
            f"品牌热度变化绝对值 ≤ {heat_c}（直接调整品牌行情热度）；"
            "这两项仅当球队当前有冠名品牌时实际生效；"
            f"建设券变化绝对值 ≤ {bc_c}M（影响建设信用余额），"
            f"影响力变化绝对值 ≤ {inf_c}（球队影响力基准值，量纲 0~200）；"
            "信号 signals 可选（dict，信号名→数值）："
            f"fan_mood 粉丝情绪步进 ±{sig_c.get('fan_mood_clamp', 2.0)} 百分点"
            f"（影响死忠演化）、upkeep 维护负担乘数 {sig_c.get('upkeep_low', 0.5)}~"
            f"{sig_c.get('upkeep_high', 2.0)}（乘本队维护费）、"
            f"fee_mod 冠名费乘数 {sig_c.get('fee_mod_low', 0.5)}~"
            f"{sig_c.get('fee_mod_high', 2.0)}（乘本队冠名费，仅冠名队实际生效）；"
            "未知名会被剔除。\n"
            "结构类效果（可选，直接改动球队设施/档期/天气/招商）："
            f"facility 设施升降级（dict {{\"key\":设施键,\"delta\":±1}}，设施键："
            + "、".join(f"{k}={fac_names.get(k, k)}" for k in fac_names) + "）；"
            f"booking_cancel 档期取消（1~{cancel_c:g}，从高到低取消该队本窗已订活动）；"
            "booking_gift 档期赠送（活动类型串，赠一档填本窗最低空档位，活动类型："
            + "、".join(str(k) for k in act_names) + "）；"
            "weather_set 天气设定（该队下一场未赛主场天气，取值："
            + "、".join(str(k) for k in wx_names) + "）；"
            "offer_spawn 品牌上门（1=一个未签约品牌按热度上门递价，挂当前招商轮）。\n"
            "触发条件 conditions（可选，dict，全部条件同时满足才会自然触发）："
            "min_tier/max_tier 球场档位下/上限（整数 1~5）、"
            "min_capacity/max_capacity 容量下/上限（人）、"
            "min_fans/max_fans 死忠球迷数下/上限（人）、"
            "min_balance/max_balance 资金余额下/上限（M）、"
            "facility_min 设施最低等级（dict {{\"key\":最低级}}，设施键同上）、"
            "weather_is 下一场未赛主场天气（取值：" + "、".join(str(k) for k in wx_names) + "）、"
            "last_result 最近一场已完赛主场赛果（win/draw/loss）；"
            "requires_naming 仅当有冠名品牌（true）、"
            "requires_activity 本窗已订某活动（活动类型同上）。\n"
            "soft_conditions（可选）：置 1 时上述条件不满足不再硬性排除该事件，"
            "而是按权重衰减后仍可能触发（适合低概率天灾类，让事件偶发于不满足条件的球队）。\n"
            "输出 JSON 数组，只输出数组本身，不要任何解释或多余文字。每个元素为以下两类之一：\n"
            "一、即发事件：{\"event_type\":\"instant\",\"name\":\"事件名(≤20字)\","
            "\"category\":\"分类(≤10字)\",\"weight\":权重整数1~100,"
            "\"conditions\":{触发条件}(可选),\"soft_conditions\":0或1(可选),"
            "\"effects\":{\"money\":金额M,\"fans_pct\":死忠比例小数,\"maintenance\":维护额外支出M,"
            "\"satisfaction\":品牌满意度变化(可选),\"brand_heat\":品牌热度变化(可选),"
            "\"build_credit\":建设券变化M(可选),\"influence\":影响力变化(可选),"
            "\"signals\":{信号名:数值}(可选),\"facility\":{设施效果}(可选),"
            "\"booking_cancel\":取消档数(可选),\"booking_gift\":活动类型(可选),"
            "\"weather_set\":天气(可选),\"offer_spawn\":1(可选)},"
            "\"template\":\"≤200字播报文案，必须包含{team}和{stadium}占位符\"}\n"
            "   例：{\"event_type\":\"instant\",\"name\":\"草坪闯入者\",\"category\":\"场务\","
            "\"weight\":30,\"effects\":{\"money\":-2.0,\"fans_pct\":0.01,\"maintenance\":1.5},"
            "\"template\":\"一只训练海豚闯入{stadium}，{team}的工作人员手忙脚乱。\"}\n"
            "二、选择事件：{\"event_type\":\"choice\",\"name\":\"事件名(≤20字)\","
            "\"category\":\"分类(≤10字)\",\"weight\":权重整数1~100,"
            "\"conditions\":{触发条件}(可选),\"soft_conditions\":0或1(可选),"
            "\"template\":\"≤200字广播文案，必须包含{team}和{stadium}占位符\","
            "\"options\":[{\"name\":\"操作名\",\"desc\":\"≤40字说明\","
            "\"outcomes\":[{\"w\":概率权重整数,\"effects\":{同上效果}},…]},…]}\n"
            "选择事件需要 2~4 个选项，每个选项至少 2 条概率结果，w 为相对权重（系统自动归一化）；"
            "每个选项的高概率路径与低概率路径应尽量一正一负。\n"
            "示例仅供格式参考，不要照抄内容。"
        )
        text = await self._ask(prompt)
        if not text:
            logger.warning("LLM 事件设计首次调用无内容，重试一次……")
            text = await self._ask(prompt, quiet=True)
        if not text:
            raise RuntimeError(
                "LLM 未返回内容（已重试 1 次，详见日志；若反复如此请检查 AstrBot 是否选用可用模型，"
                "必要时调大 llm_timeout_seconds）"
            )
        raw = _extract_json(text)
        if not isinstance(raw, list):
            raise RuntimeError("LLM 返回格式不是 JSON 数组")
        drafts = []
        for d in raw[:count]:
            if not isinstance(d, dict):
                continue
            try:
                drafts.append(_clamp_event(d, clamps))
            except (ValueError, TypeError, AttributeError):
                continue
        if not drafts:
            raise RuntimeError("LLM 事件均未通过结构校验")
        return drafts

    # ─── 品牌设计 ─────────────────────────────────────────

    async def design_brands(self, count: int, topic: str = "") -> list[dict]:
        if not self.enabled(flavor=False):
            raise RuntimeError("LLM 品牌设计未启用（llm_design_enabled）")
        prompt = (
            "为足球俱乐部球场冠名权市场设计虚拟赞助品牌。\n"
            f"任务：设计 {count} 个品牌，主题：{topic or '不限'}"
            "（可以是群聊梗、虚拟科技公司、本地企业风格）。\n"
            "档位说明：头部=知名大牌；新兴=上升期品牌；口碑=本地亲民品牌。\n"
            "输出 JSON 数组，只输出数组本身，不要任何解释或多余文字。每个元素：\n"
            "{\"brand\": \"品牌名(4-16字)\", \"heat\": 热度小数0.5~1.5（越高品牌越活跃、越容易达成签约）, "
            "\"tier\": \"档位(头部/新兴/口碑)\", \"industry\": \"行业(≤8字，如 科技/饮食)\", "
            "\"pitch\": \"一句卖点(≤30字)\", "
            "\"packages\": [可选，1~3个套餐变体 {\"pkg_name\": \"名(≤6字)\", \"windows\": 窗口数1~12, "
            "\"factor\": 每窗价格系数0.5~2.0}]，不提供则该品牌只有标准价}\n"
            "   例：{\"brand\": \"星际冷却液\", \"heat\": 1.2, \"tier\": \"新兴\", "
            "\"industry\": \"科技\", \"pitch\": \"让主场空调永不宕机\", "
            "\"packages\": [{\"pkg_name\": \"三连冠\", \"windows\": 3, \"factor\": 1.5}]}\n"
            "示例仅供格式参考，不要照抄内容。"
        )
        text = await self._ask(prompt)
        if not text:
            logger.warning("LLM 品牌设计首次调用无内容，重试一次……")
            text = await self._ask(prompt, quiet=True)
        if not text:
            raise RuntimeError(
                "LLM 未返回内容（已重试 1 次，详见日志；若反复如此请检查 AstrBot 是否选用可用模型，"
                "必要时调大 llm_timeout_seconds）"
            )
        raw = _extract_json(text)
        if not isinstance(raw, list):
            raise RuntimeError("LLM 返回格式不是 JSON 数组")
        drafts = []
        for d in raw[:count]:
            if not isinstance(d, dict):
                continue
            try:
                drafts.append(_clamp_brand(d))
            except (ValueError, TypeError, AttributeError):
                continue
        return drafts


def _fill_template(template: str, team: str, stadium: str) -> str:
    return template.replace("{team}", team).replace("{stadium}", stadium)


def _extract_llm_text(result) -> str | None:
    """从 provider 返回对象中尽力提取纯文本。

    新版 AstrBot 的 text_chat 返回 LLMResponse（文本在 completion_text，
    内部走 result_chain.get_plain_text()）；旧版返回 AiMessageResult（result_str）；
    个别 provider 可能直接返回 str。全都不匹配返回 None。
    """
    if isinstance(result, str):
        return result
    if result is None:
        return None
    for attr in ("completion_text", "result_str"):
        try:
            val = getattr(result, attr, None)
        except Exception:
            val = None
        if isinstance(val, str) and val:
            return val
    try:
        chain = getattr(result, "result_chain", None)
        if chain is not None:
            text = chain.get_plain_text()
            return text if isinstance(text, str) else None
    except Exception:
        pass
    return None


def _extract_json(text: str):
    match = _JSON_BLOCK_RE.search(text)
    if match:
        text = match.group(1)
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1] == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 兜底：LLM 在 JSON 前后附带说明文字、或带尾逗号时，取首尾括号切片重试
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start == -1 or end <= start:
            continue
        snippet = _TRAILING_COMMA_RE.sub(r"\1", text[start:end + 1])
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            continue
    return None


def _clamp_effects(effects, clamps: dict) -> dict:
    """对单条效果钳制（逐键委托效果键注册表；clamp 返回 None 即剔除该键）。

    money/fans_pct/maintenance 恒在输出（缺键按 0 收敛），attendance_mod 仅在
    ≠1 时输出，satisfaction/brand_heat 仅在 ≠0 时输出（量纲见 EVENT_KEYS.md）。
    clamps 键名即注册表钳幅维度：money/fans/maintenance/satisfaction/brand_heat。
    """
    out = {}
    for spec in EFFECT_SPECS:
        value = spec.clamp(effects.get(spec.key, spec.default), clamps)
        if value is not None:
            out[spec.key] = value
    return out


def _clamp_conditions(raw, clamps: dict) -> dict:
    """条件键白名单钳制：数值键取整≥0、枚举键域外剔除、结构键逐项校验。"""
    if not isinstance(raw, dict):
        return {}

    def _nonneg_int(v):
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            return None
        return n if n >= 0 else None

    out: dict = {}
    for key in ("min_tier", "max_tier", "min_capacity", "max_capacity",
                "min_fans", "max_fans", "min_balance", "max_balance"):
        if key in raw:
            n = _nonneg_int(raw[key])
            if n is not None:
                out[key] = n
    if raw.get("requires_naming") in (True, 1, "1", "true", "True"):
        out["requires_naming"] = True
    act = raw.get("requires_activity")
    if act is not None:
        act = str(act)
        keys = clamps.get("activity_keys")
        if not keys or act in keys:
            out["requires_activity"] = act
    fmin = raw.get("facility_min")
    if isinstance(fmin, dict) and fmin:
        max_lv = int(clamps.get("facility_max_level", 5))
        keys = clamps.get("facility_keys")
        cleaned = {}
        for k, v in fmin.items():
            n = _nonneg_int(v)
            if n is None or not (1 <= n <= max_lv):
                continue
            if keys and str(k) not in keys:
                continue
            cleaned[str(k)] = n
        if cleaned:
            out["facility_min"] = cleaned
    wx = raw.get("weather_is")
    if wx is not None:
        wx = str(wx)
        keys = clamps.get("weather_keys")
        if not keys or wx in keys:
            out["weather_is"] = wx
    last = raw.get("last_result")
    if last is not None:
        norm = {"win": "win", "w": "win", "draw": "draw", "d": "draw",
                "loss": "loss", "l": "loss"}.get(str(last).strip().lower())
        if norm:
            out["last_result"] = norm
    return out


def _soft_flag(d) -> int:
    return 1 if d.get("soft_conditions") in (1, True, "1", "true", "True") else 0


def _clamp_choice_event(d, clamps: dict) -> dict:
    """校验并钳制选择型事件：2~4 个选项、每选项≥2条概率结果、概率归一、正负路径保障。"""
    name = sanitize_text(str(d.get("name", "")))
    if not name:
        raise ValueError("事件缺少名称")
    raw_options = d.get("options")
    if not isinstance(raw_options, list) or not (2 <= len(raw_options) <= 4):
        raise ValueError(f"选择事件「{name}」需要 2~4 个选项")
    options = []
    for i, opt in enumerate(raw_options, start=1):
        opt_name = sanitize_text(str(opt.get("name", "")))
        if not opt_name:
            raise ValueError(f"事件「{name}」选项缺名称")
        outs = opt.get("outcomes")
        if not isinstance(outs, list) or len(outs) < 2:
            raise ValueError(f"选项「{opt_name}」至少需要 2 条概率结果")
        outcomes = []
        for o in outs:
            w = float(o.get("w", 1))
            if w <= 0:
                w = 1.0
            outcomes.append({"w": round(w, 1),
                             "effects": _clamp_effects(o.get("effects") or {}, clamps)})
        total = sum(x["w"] for x in outcomes)
        for x in outcomes:
            x["w"] = round(x["w"] / total * 100, 1)
        # 保证每项选择带一条负面与一条正面路径（净额 = 资金 − 维护）
        money_clamp = float(clamps.get("money", 8.0))
        nets = [x["effects"]["money"] - x["effects"]["maintenance"] for x in outcomes]
        if min(nets) >= 0:
            outcomes[-1]["effects"]["money"] = round(-min(1.0, max(0.5, money_clamp * 0.1)), 3)
        elif max(nets) < 0:
            outcomes[-1]["effects"]["money"] = round(min(1.0, max(0.5, money_clamp * 0.1)), 3)
        options.append({"no": i, "name": opt_name,
                        "desc": sanitize_text(str(opt.get("desc", "")), 40), "outcomes": outcomes})
    return {"name": name, "event_type": "choice",
            "options": options,
            "category": sanitize_text(str(d.get("category", "自定义")), 10) or "自定义",
            "weight": max(1, min(100, int(d.get("weight", 10)))),
            "conditions": _clamp_conditions(d.get("conditions"), clamps),
            "soft_conditions": _soft_flag(d),
            "template": sanitize_text(str(d.get("template", "")), 200) or f"{name}：{{team}} 的球场发生了趣事。"}


def _clamp_event(d, clamps: dict) -> dict:
    name = sanitize_text(str(d.get("name", "")))
    if not name:
        raise ValueError("事件缺少名称")
    event_type = str(d.get("event_type", "instant")).strip().lower()
    if event_type == "choice":
        return _clamp_choice_event(d, clamps)
    effects = _clamp_effects(d.get("effects") or {}, clamps)
    return {
        "name": name,
        "category": sanitize_text(str(d.get("category", "自定义")), 10) or "自定义",
        "weight": max(1, min(100, int(d.get("weight", 10)))),
        "event_type": "instant",
        "conditions": _clamp_conditions(d.get("conditions"), clamps),
        "soft_conditions": _soft_flag(d),
        "effects": effects,
        "template": sanitize_text(str(d.get("template", "")), 200) or f"{name}：{{team}} 的球场发生了趣事。",
    }


def _clamp_brand(d) -> dict:
    brand = sanitize_text(str(d.get("brand", "")))
    if not brand or len(brand) > 20:
        raise ValueError("品牌名需为 1-20 字")
    heat = max(0.5, min(1.5, float(d.get("heat", 1.0))))
    tier = sanitize_text(str(d.get("tier", "")), 4)
    tier = tier if tier in ("头部", "新兴", "口碑") else "口碑"
    industry = sanitize_text(str(d.get("industry", "")), 8) or "通用"
    pitch = sanitize_text(str(d.get("pitch", "")), 30)
    packages = []
    raw_pkgs = d.get("packages")
    if isinstance(raw_pkgs, list):
        for p in raw_pkgs[:3]:
            if not isinstance(p, dict):
                continue
            try:
                windows = max(1, min(12, int(p.get("windows", 4))))
                factor = max(0.5, min(2.0, float(p.get("factor", 1.0))))
            except (TypeError, ValueError):
                continue
            name = sanitize_text(str(p.get("pkg_name", "")), 6) or f"定制{len(packages) + 1}"
            packages.append({"pkg_name": name, "windows": windows, "factor": round(factor, 3)})
    out = {"brand": brand, "heat": round(heat, 3), "tier": tier,
           "industry": industry, "pitch": pitch}
    if packages:
        out["packages"] = packages
    return out