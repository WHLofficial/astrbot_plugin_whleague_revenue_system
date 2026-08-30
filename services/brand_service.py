"""冠名权与品牌池：内置品牌、管理员自定义、LLM 生成草稿（采纳后生效）。

报价/窗口 = (基准 + 容量系数×容量万 + 死忠系数×死忠万) × 热度；
合同默认 4 窗口；提前解约赔偿剩余 30%；死忠单窗口跌 ≥30% 品牌有概率主动解约。
"""

from . import formula


class BrandError(Exception):
    pass


DEFAULT_BRANDS = [
    ("麒麟生物", 1.2),
    ("阿迪达斯", 1.1),
    ("亚马逊", 1.3),
    ("可口可乐", 1.0),
    ("海底捞", 0.9),
    ("星海通讯", 0.8),
    ("CVS Health", 0.7),
]


class BrandService:
    def __init__(self, db, dao, cfg, llm=None):
        self._db = db
        self._dao = dao
        self._cfg = cfg
        self._llm = llm

    async def init_brand_pool(self) -> None:
        existing = await self._dao.list_brands("adopted")
        if existing:
            return
        for brand, heat in DEFAULT_BRANDS:
            await self._dao.add_brand(brand, heat, source="builtin", status="adopted")

    # ─── 签约/解约 ────────────────────────────────────────

    async def sign(self, team_name: str, brand: str, season: int, window_seq: int) -> dict:
        brand = brand.strip()
        if not brand:
            raise BrandError("品牌名不能为空")
        stadium = await self._dao.get_stadium(team_name)
        if not stadium:
            raise BrandError(f"球队「{team_name}」没有球场")
        row = await self._dao.get_active_naming(team_name)
        if row:
            raise BrandError(f"已有生效冠名（{row['brand']}），请先退冠名")
        pool = await self._dao.list_brands("adopted")
        target = next((b for b in pool if b["brand"] == brand), None)
        if target is None:
            raise BrandError(f"品牌「{brand}」不在品牌池（可用 /主场品牌列表 查看）")
        fee = formula.naming_fee(self._cfg, stadium["capacity"], stadium["fans_diehards"], target["heat"])
        windows = int(self._cfg.get("naming_windows", 4))
        await self._dao.add_naming(team_name, brand, target["heat"], fee, windows, season, window_seq)
        return {"team": team_name, "brand": brand, "fee_per_window": fee, "windows": windows}

    async def terminate(self, team_name: str, season: int, window_seq: int,
                        initiated_by: str = "team") -> dict:
        row = await self._dao.get_active_naming(team_name)
        if not row:
            raise BrandError("该队没有生效冠名")
        fee_penalty = 0.0
        if initiated_by == "team":
            remaining = max(0, row["windows_remaining"] - 1)
            penalty_ratio = float(self._cfg.get("naming_terminate_penalty", 0.3))
            fee_penalty = round(remaining * row["fee_per_window"] * penalty_ratio, 3)
            if fee_penalty > 0:
                await self._dao.ensure_balance(team_name, float(self._cfg.get("start_funds", 50.0)))
                await self._dao.record_entries(
                    team_name, season, window_seq,
                    [("naming", -fee_penalty, f"提前解约赔款（{row['brand']}）", None)],
                )
        await self._dao.terminate_naming(team_name)
        return {"team": team_name, "brand": row["brand"], "penalty": fee_penalty}

    async def maybe_brand_terminate(self, team_name: str, season: int, window_seq: int,
                                    fans_before: float, fans_after: float) -> dict | None:
        """死忠单窗口跌幅达阈值时品牌有概率主动解约（无赔偿）。"""
        row = await self._dao.get_active_naming(team_name)
        if not row:
            return None
        if fans_before <= 0:
            return None
        drop = (fans_before - fans_after) / fans_before
        if drop < float(self._cfg.get("naming_fans_drop_threshold", 0.3)):
            return None
        import random

        if random.random() >= float(self._cfg.get("naming_terminate_probability", 0.3)):
            return None
        await self._dao.terminate_naming(team_name)
        tx_id = await self._dao.add_transaction(
            team_name, season, window_seq, "naming", 0.0,
            note=f"{row['brand']} 因球迷流失提前解约",
        )
        return {"team": team_name, "brand": row["brand"], "tx_id": tx_id}

    # ─── 品牌池 ───────────────────────────────────────────

    async def add_custom(self, brand: str, heat: float) -> None:
        brand = brand.strip()
        if not brand or len(brand) > 20:
            raise BrandError("品牌名需为 1-20 字")
        if not (0.5 <= heat <= 1.5):
            raise BrandError("热度需在 0.5~1.5 之间")
        await self._dao.add_brand(brand, heat, source="custom", status="adopted")

    async def generate_drafts(self, count: int, topic: str = "") -> list[dict]:
        if self._llm is None:
            raise BrandError("LLM 不可用")
        drafts = await self._llm.design_brands(count, topic)
        added = []
        for d in drafts:
            await self._dao.add_brand(d["brand"], d["heat"], source="llm", status="pending")
            added.append({"brand": d["brand"], "heat": d["heat"]})
        return added

    async def adopt(self, brand_id: int) -> None:
        rows = await self._dao.list_brands("pending")
        if not any(r["id"] == brand_id for r in rows):
            raise BrandError("待采纳品牌不存在")
        await self._dao.adopt_brand(brand_id)

    async def discard(self, brand_id: int) -> None:
        rows = await self._dao.list_brands("pending")
        if not any(r["id"] == brand_id for r in rows):
            raise BrandError("待处理品牌不存在")
        await self._dao.discard_brand(brand_id)