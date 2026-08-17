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
        try:
            provider = self._get_provider()
            if provider is None:
                return None
            timeout = float(self._cfg.get("llm_timeout_seconds", 15))
            result = await asyncio.wait_for(
                provider.text_chat(prompt, session_id="whl_stadium_llm"),
                timeout=timeout,
            )
            text = getattr(result, "result_str", None)
            if not text:
                return None
            return str(text).strip()
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
            "为足球联赛主场随机事件系统设计若干个新事件。"
            f"主题：{topic or '不限'}。输出 JSON 数组，每个元素结构：\n"
            '{"name": "事件名", "category": "分类", "weight": 权重整数1-100, '
            '"effects": {"money": 金额M（-5到5之间的小数，可用于模板显示，实际结算会钳制）, '
            '"fans_pct": 死忠百分比小数如0.03或-0.02, "maintenance": 维护额外支出M}, '
            '"template": "一条带{team}和{stadium}占位符的播报模板文案"}\n'
            f"金额绝对值不要超过 {money_clamp}M，死忠百分比绝对值不要超过 {fans_clamp}，"
            f"维护绝对值不要超过 {maintenance_clamp}M。只输出 JSON 数组。"
        )
        text = await self._ask(prompt)
        if not text:
            raise RuntimeError("LLM 未返回内容")
        raw = _extract_json(text)
        if not isinstance(raw, list):
            raise RuntimeError("LLM 返回格式不是 JSON 数组")
        return [_clamp_event(d, money_clamp, fans_clamp, maintenance_clamp) for d in raw[:count]]

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
            raise RuntimeError("LLM 未返回内容")
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


def _clamp_event(d, money_clamp: float, fans_clamp: float, maintenance_clamp: float) -> dict:
    name = str(d.get("name", "")).strip()
    if not name:
        raise ValueError("事件缺少名称")
    effects = d.get("effects") or {}
    money = float(effects.get("money", 0.0))
    fans_pct = float(effects.get("fans_pct", 0.0))
    maintenance = float(effects.get("maintenance", 0.0))
    money = max(-money_clamp, min(money_clamp, money))
    fans_pct = max(-fans_clamp, min(fans_clamp, fans_pct))
    # 维护只允许支出（0 ~ 上限），不允许负维护退款
    maintenance = max(0.0, min(maintenance_clamp, maintenance))
    weight = max(1, min(100, int(d.get("weight", 10))))
    template = str(d.get("template", "")).strip() or f"{name}：{team} 的球场发生了趣事。"
    return {
        "name": name,
        "category": str(d.get("category", "自定义")).strip()[:10] or "自定义",
        "weight": weight,
        "effects": {"money": round(money, 3), "fans_pct": round(fans_pct, 4),
                    "maintenance": round(maintenance, 3)},
        "template": template[:200],
    }


def _clamp_brand(d) -> dict:
    brand = str(d.get("brand", "")).strip()
    if not brand or len(brand) > 20:
        raise ValueError("品牌名需为 1-20 字")
    heat = max(0.5, min(1.5, float(d.get("heat", 1.0))))
    return {"brand": brand, "heat": round(heat, 3)}