"""冠名权与品牌池：内置品牌、管理员自定义、LLM 生成草稿（采纳后生效）。

底价/窗口 = (基准 + 容量系数×容量万 + 死忠系数×死忠万) × 热度 × 行业系数；
合同按套餐签订（稳健/进取/对赌），费用条款以 package_json 快照存入合同；
提前解约赔偿剩余 30%；品牌解约改为确定性规则（满意度跌破档位下限，行情演化接入后生效）。
"""

import json

from . import formula


class BrandError(Exception):
    pass


DEFAULT_BRANDS = [
    ("麒麟生物", 1.2, "医疗"),
    ("阿迪达斯", 1.1, "运动"),
    ("亚马逊", 1.3, "科技"),
    ("可口可乐", 1.0, "饮食"),
    ("海底捞", 0.9, "饮食"),
    ("星海通讯", 0.8, "科技"),
    ("CVS Health", 0.7, "医疗"),
]


def build_packages(cfg: dict, base_fee: float) -> list[dict]:
    """三套餐费用条款（稳健/进取/对赌），金额与奖金均为底价的固定系数。

    对赌套餐：每窗保底（底价×bet_factor），窗口上座率或死忠增长率达线时
    发放奖金（保底×bonus_rate）；bet_attend/bet_fans 线随快照存入合同。
    """
    stable_w = int(cfg.get("naming_pkg_stable_windows", 6))
    stable_f = float(cfg.get("naming_pkg_stable_factor", 0.85))
    short_w = int(cfg.get("naming_pkg_short_windows", 2))
    short_f = float(cfg.get("naming_pkg_short_factor", 1.25))
    bet_w = int(cfg.get("naming_pkg_bet_windows", 4))
    bet_f = float(cfg.get("naming_pkg_bet_factor", 0.7))
    bonus_rate = float(cfg.get("naming_pkg_bet_bonus_rate", 0.7))
    bet_attend = float(cfg.get("naming_pkg_bet_attend", 0.8))
    bet_fans = float(cfg.get("naming_pkg_bet_fans", 0.03))
    stable = round(base_fee * stable_f, 3)
    short = round(base_fee * short_f, 3)
    bet = round(base_fee * bet_f, 3)
    bonus = round(bet * bonus_rate, 3)
    return [
        {"pkg_no": 1, "pkg_name": "稳健", "windows": stable_w, "amount": stable,
         "base_fee": round(base_fee, 3), "bonus": 0.0,
         "bet_attend": bet_attend, "bet_fans": bet_fans,
         "desc": f"{stable_w} 个窗口，每窗 {stable:g}M（底价×{stable_f:g}）"},
        {"pkg_no": 2, "pkg_name": "进取", "windows": short_w, "amount": short,
         "base_fee": round(base_fee, 3), "bonus": 0.0,
         "bet_attend": bet_attend, "bet_fans": bet_fans,
         "desc": f"{short_w} 个窗口，每窗 {short:g}M（底价×{short_f:g}）"},
        {"pkg_no": 3, "pkg_name": "对赌", "windows": bet_w, "amount": bet,
         "base_fee": round(base_fee, 3), "bonus": bonus,
         "bet_attend": bet_attend, "bet_fans": bet_fans,
         "desc": (f"{bet_w} 个窗口，每窗保底 {bet:g}M（底价×{bet_f:g}）；"
                  f"窗口上座≥{bet_attend:g} 或死忠增长≥{bet_fans:g} 时奖金 {bonus:g}M")},
    ]


def _snapshot(package: dict) -> dict:
    """入合同的费用条款快照（desc 为展示字段，不入快照）。"""
    return {k: package[k] for k in
            ("pkg_no", "pkg_name", "base_fee", "amount", "windows", "bonus",
             "bet_attend", "bet_fans")}


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
        for brand, heat, industry in DEFAULT_BRANDS:
            await self._dao.add_brand(brand, heat, source="builtin", status="adopted",
                                      industry=industry)

    # ─── 签约/解约 ────────────────────────────────────────

    @staticmethod
    def _package_no(package_no) -> int:
        try:
            no = int(package_no)
        except (TypeError, ValueError):
            raise BrandError("套餐号需为数字 1-3（1=稳健 2=进取 3=对赌）")
        if not (1 <= no <= 3):
            raise BrandError("套餐号需为 1-3（1=稳健 2=进取 3=对赌）")
        return no

    async def _quote(self, capacity: float, fans: float, brand_row) -> tuple[float, list[dict]]:
        """按队况算底价（×行业系数）并生成三套餐。"""
        heat = float(brand_row["heat"])
        industry = (brand_row["industry"] or "通用") if brand_row else "通用"
        base_fee = formula.naming_fee(self._cfg, capacity, fans, heat)
        base_fee = round(base_fee * formula.industry_factor(self._cfg, industry), 3)
        return base_fee, build_packages(self._cfg, base_fee)

    async def sign(self, team_name: str, brand: str, season: int, window_seq: int,
                   package_no: int = 1) -> dict:
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
        _, packages = await self._quote(stadium["capacity"], stadium["fans_diehards"], target)
        package = packages[self._package_no(package_no) - 1]
        await self._dao.add_naming(team_name, brand, target["heat"], package["amount"],
                                   package["windows"], season, window_seq,
                                   package_json=json.dumps(_snapshot(package), ensure_ascii=False))
        return {"team": team_name, "brand": brand, "fee_per_window": package["amount"],
                "windows": package["windows"], "package": package["pkg_name"],
                "package_no": package["pkg_no"]}

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

    async def renew(self, team_name: str, season: int, window_seq: int,
                    package_no: int = 1) -> dict:
        """续约：剩最后 1 窗且品牌满意时，按当期队况重算底价，无缝换签所选套餐。

        新约当窗生效：费用按新套餐收，旧约剩余窗口与新约合并（原地换约）。
        """
        row = await self._dao.get_active_naming(team_name)
        if not row:
            raise BrandError("该队没有生效冠名，无需续约")
        if int(row["windows_remaining"]) != 1:
            raise BrandError(f"合同还剩 {row['windows_remaining']} 个窗口，剩最后 1 窗才可续约")
        if float(row["satisfaction"]) < 1.0:
            raise BrandError(f"品牌满意度 {float(row['satisfaction']):.2f}，低于 1.0 无法续约")
        stadium = await self._dao.get_stadium(team_name)
        if not stadium:
            raise BrandError(f"球队「{team_name}」没有球场")
        pool = await self._dao.list_brands()
        target = next((b for b in pool if b["brand"] == row["brand"]), None)
        _, packages = await self._quote(stadium["capacity"], stadium["fans_diehards"], target)
        package = packages[self._package_no(package_no) - 1]
        await self._dao.renew_naming(team_name, package["amount"], package["windows"],
                                     season, window_seq,
                                     json.dumps(_snapshot(package), ensure_ascii=False))
        return {"team": team_name, "brand": row["brand"], "fee_per_window": package["amount"],
                "windows": package["windows"], "package": package["pkg_name"],
                "package_no": package["pkg_no"]}

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