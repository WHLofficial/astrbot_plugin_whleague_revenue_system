"""LLM 文案与设计：运行时事件文案、事件/品牌草稿生成。

职责边界：LLM 只碰文案和创意草稿，数值效果永远由事件/品牌结构 + 配置钳制决定。
所有调用失败/超时/无 provider 均回退（文案→模板，设计→报错给管理员）。
"""

import asyncio
import json
import re

from astrbot.api import logger

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class LlmWriter:
    def __init__(self, cfg: dict, get_provider=None):
        self._cfg = cfg
        self._get_provider = get_provider or (lambda: None)

    def enabled(self, flavor: bool = True) -> bool:
        if flavor:
            return bool(self._cfg.get("llm_flavor_enabled", True))
        return bool(self._cfg.get("llm_design_enabled", True))

    async def _ask(self, prompt: str) -> str | None:
        """发起 LLM 调用；失败/空内容返回 None 并分类告警（便于归因）。"""
        try:
            provider = self._get_provider()
            if provider is None:
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
            except TimeoutError:
                logger.warning(
                    "LLM 调用超时（>%gs，可通过配置 llm_timeout_seconds 调大）。",
                    timeout,
                )
                return None
            text = getattr(result, "result_str", None)
            if text is None:
                logger.warning(
                    "LLM 返回对象缺少 result_str 字段（prompt %d 字符），视为空内容。",
                    len(prompt),
                )
                return None
            text = str(text).strip()
            if not text:
                logger.warning("LLM 返回空内容（prompt %d 字符）。", len(prompt))
                return None
            return text
        except Exception as e:
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
            "你是足球俱乐部的场馆运营播报员。用中文写 1-2 句简短群聊风格的播报文案，"
            "语气活泼、有画面感，可以带一点梗，但不要编造具体数字之外的金额描述。\n"
            f"事件：{name}（类别：{category}）\n"
            f"球队：{team}，球场：{stadium_name}\n"
            f"附加背景：{context or '无'}\n"
            "只需要输出播报文案本身，不要解释。"
        )
        text = await self._ask(prompt)
        if not text:
            logger.warning("事件文案 LLM 调用无内容，回退模板（%s·%s）。", team, name)
            return _fill_template(template, team, stadium_name)
        return _fill_template(text[:120], team, stadium_name)

    # ─── 事件设计 ─────────────────────────────────────────

    async def design_events(self, count: int, topic: str = "") -> list[dict]:
        if not self.enabled(flavor=False):
            raise RuntimeError("LLM 事件设计未启用（llm_design_enabled）")
        money_clamp = float(self._cfg.get("event_money_clamp", 8.0))
        fans_clamp = float(self._cfg.get("event_fans_clamp", 0.05))
        maintenance_clamp = float(self._cfg.get("event_maintenance_clamp", 5.0))
        prompt = (
            "为足球联赛主场随机事件系统设计新事件。"
            f"主题：{topic or '不限'}。输出 JSON 数组，每个元素二选一：\n"
            "一、即发事件：{\"event_type\":\"instant\",\"name\":\"事件名\",\"category\":\"分类\","
            "\"weight\":权重整数1-100,\"effects\":{\"money\":金额M,\"fans_pct\":死忠比例小数如0.03,"
            "\"maintenance\":维护额外支出M},\"template\":\"带{team}和{stadium}占位符的播报文案\"}\n"
            "二、选择事件：{\"event_type\":\"choice\",\"name\":\"事件名\",\"category\":\"分类\","
            "\"weight\":权重整数1-100,\"template\":\"…\",\"options\":[{\"name\":\"操作1\","
            "\"desc\":\"简短说明\",\"outcomes\":[{\"w\":概率权重整数,\"effects\":{同上效果}},…]},…]}\n"
            "选择事件需要 2~4 个选项，每个选项 2 条以上概率结果，概率权重为相对大小（会自动归一化）；"
            "每个选项的高低概率路径应尽量一正一负。\n"
            f"金额绝对值不要超过 {money_clamp}M，死忠百分比绝对值不要超过 {fans_clamp}，"
            f"维护不超过 {maintenance_clamp}M。只输出 JSON 数组本身，不要任何解释或多余文字。"
        )
        text = await self._ask(prompt)
        if not text:
            logger.warning("LLM 事件设计首次调用无内容，重试一次……")
            text = await self._ask(prompt)
        if not text:
            raise RuntimeError("LLM 未返回内容（已重试 1 次，详见日志）")
        raw = _extract_json(text)
        if not isinstance(raw, list):
            raise RuntimeError("LLM 返回格式不是 JSON 数组")
        drafts = []
        for d in raw[:count]:
            try:
                drafts.append(_clamp_event(d, money_clamp, fans_clamp, maintenance_clamp))
            except (ValueError, TypeError):
                continue
        if not drafts:
            raise RuntimeError("LLM 事件均未通过结构校验")
        return drafts

    # ─── 品牌设计 ─────────────────────────────────────────

    async def design_brands(self, count: int, topic: str = "") -> list[dict]:
        if not self.enabled(flavor=False):
            raise RuntimeError("LLM 品牌设计未启用（llm_design_enabled）")
        prompt = (
            "为足球俱乐部球场冠名权设计若干个虚拟赞助品牌。"
            f"主题：{topic or '不限'}（可以是群聊梗、虚拟科技公司、本地企业风格）。"
            "输出 JSON 数组，每个元素：{\"brand\": \"品牌名(4-16字)\", \"heat\": 热度小数0.5-1.5}"
            "只输出 JSON 数组。"
        )
        text = await self._ask(prompt)
        if not text:
            logger.warning("LLM 品牌设计首次调用无内容，重试一次……")
            text = await self._ask(prompt)
        if not text:
            raise RuntimeError("LLM 未返回内容（已重试 1 次，详见日志）")
        raw = _extract_json(text)
        if not isinstance(raw, list):
            raise RuntimeError("LLM 返回格式不是 JSON 数组")
        return [_clamp_brand(d) for d in raw[:count]]


def _fill_template(template: str, team: str, stadium: str) -> str:
    return template.replace("{team}", team).replace("{stadium}", stadium)


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
        return None


def _clamp_effects(effects, money_clamp: float, fans_clamp: float, maintenance_clamp: float) -> dict:
    """对单条效果钳制：金额/money、死忠/fans_pct、维护/maintenance、上座/attendance_mod。"""
    money = max(-money_clamp, min(money_clamp, float(effects.get("money", 0.0))))
    fans_pct = max(-fans_clamp, min(fans_clamp, float(effects.get("fans_pct", 0.0))))
    # 维护只允许支出（0 ~ 上限），不允许负维护退款
    maintenance = max(0.0, min(maintenance_clamp, float(effects.get("maintenance", 0.0))))
    out = {"money": round(money, 3), "fans_pct": round(fans_pct, 4),
           "maintenance": round(maintenance, 3)}
    attendance = float(effects.get("attendance_mod", 1.0))
    if attendance != 1.0:
        out["attendance_mod"] = round(max(0.5, min(2.0, attendance)), 3)
    return out


def _clamp_choice_event(d, money_clamp: float, fans_clamp: float, maintenance_clamp: float) -> dict:
    """校验并钳制选择型事件：2~4 个选项、每选项≥2条概率结果、概率归一、正负路径保障。"""
    name = str(d.get("name", "")).strip()
    if not name:
        raise ValueError("事件缺少名称")
    raw_options = d.get("options")
    if not isinstance(raw_options, list) or not (2 <= len(raw_options) <= 4):
        raise ValueError(f"选择事件「{name}」需要 2~4 个选项")
    options = []
    for i, opt in enumerate(raw_options, start=1):
        opt_name = str(opt.get("name", "")).strip()
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
                             "effects": _clamp_effects(o.get("effects") or {},
                                                       money_clamp, fans_clamp, maintenance_clamp)})
        total = sum(x["w"] for x in outcomes)
        for x in outcomes:
            x["w"] = round(x["w"] / total * 100, 1)
        # 保证每项选择带一条负面与一条正面路径（净额 = 资金 − 维护）
        nets = [x["effects"]["money"] - x["effects"]["maintenance"] for x in outcomes]
        if min(nets) >= 0:
            outcomes[-1]["effects"]["money"] = round(-min(1.0, max(0.5, money_clamp * 0.1)), 3)
        elif max(nets) < 0:
            outcomes[-1]["effects"]["money"] = round(min(1.0, max(0.5, money_clamp * 0.1)), 3)
        options.append({"no": i, "name": opt_name,
                        "desc": str(opt.get("desc", "")).strip()[:40], "outcomes": outcomes})
    return {"name": name, "event_type": "choice",
            "options": options,
            "category": str(d.get("category", "自定义")).strip()[:10] or "自定义",
            "weight": max(1, min(100, int(d.get("weight", 10)))),
            "template": (str(d.get("template", "")).strip() or f"{name}：{{team}} 的球场发生了趣事。")[:200]}


def _clamp_event(d, money_clamp: float, fans_clamp: float, maintenance_clamp: float) -> dict:
    name = str(d.get("name", "")).strip()
    if not name:
        raise ValueError("事件缺少名称")
    event_type = str(d.get("event_type", "instant")).strip().lower()
    if event_type == "choice":
        return _clamp_choice_event(d, money_clamp, fans_clamp, maintenance_clamp)
    effects = _clamp_effects(d.get("effects") or {}, money_clamp, fans_clamp, maintenance_clamp)
    return {
        "name": name,
        "category": str(d.get("category", "自定义")).strip()[:10] or "自定义",
        "weight": max(1, min(100, int(d.get("weight", 10)))),
        "event_type": "instant",
        "effects": effects,
        "template": (str(d.get("template", "")).strip() or f"{name}：{{team}} 的球场发生了趣事。")[:200],
    }


def _clamp_brand(d) -> dict:
    brand = str(d.get("brand", "")).strip()
    if not brand or len(brand) > 20:
        raise ValueError("品牌名需为 1-20 字")
    heat = max(0.5, min(1.5, float(d.get("heat", 1.0))))
    return {"brand": brand, "heat": round(heat, 3)}