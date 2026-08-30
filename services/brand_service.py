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


def variant_packages(cfg: dict, base_fee: float, packages_json: str) -> list[dict] | None:
    """品牌自定义套餐变体（LLM 生成，金额=底价×factor，无奖金条款）；无效返回 None。

    packages_json 形如 {"variants": [{"pkg_name","windows","factor"}]}，
    窗口 1-12、系数 0.5-2.0、至多 3 档；解析失败/为空时调用方回退默认三套餐。
    """
    try:
        data = json.loads(packages_json or "{}")
    except (ValueError, TypeError):
        return None
    variants = data.get("variants") if isinstance(data, dict) else None
    if not isinstance(variants, list) or not variants:
        return None
    bet_attend = float(cfg.get("naming_pkg_bet_attend", 0.8))
    bet_fans = float(cfg.get("naming_pkg_bet_fans", 0.03))
    packages = []
    for i, v in enumerate(variants[:3], start=1):
        if not isinstance(v, dict):
            continue
        try:
            windows = max(1, min(12, int(v.get("windows", 4))))
            factor = max(0.5, min(2.0, float(v.get("factor", 1.0))))
        except (TypeError, ValueError):
            continue
        amount = round(base_fee * factor, 3)
        name = str(v.get("pkg_name") or f"定制{i}")[:6]
        packages.append({"pkg_no": len(packages) + 1, "pkg_name": name, "windows": windows,
                         "amount": amount, "base_fee": round(base_fee, 3), "bonus": 0.0,
                         "bet_attend": bet_attend, "bet_fans": bet_fans,
                         "desc": f"{windows} 个窗口，每窗 {amount:g}M（底价×{factor:g}）"})
    return packages or None


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
        """按队况算底价（×行业系数）并生成套餐；品牌自定义变体优先，缺省三套餐。"""
        heat = float(brand_row["heat"])
        industry = (brand_row["industry"] or "通用") if brand_row else "通用"
        base_fee = formula.naming_fee(self._cfg, capacity, fans, heat)
        base_fee = round(base_fee * formula.industry_factor(self._cfg, industry), 3)
        packages = None
        if brand_row:
            packages = variant_packages(self._cfg, base_fee, brand_row["packages_json"] or "{}")
        return base_fee, packages or build_packages(self._cfg, base_fee)

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

    # ─── 行情演化 ─────────────────────────────────────────

    def _satisfy_rules(self) -> dict:
        return formula.parse_json_object(self._cfg.get("market_satisfy_config", "{}"))

    def _heat_rules(self) -> dict:
        return formula.parse_json_object(self._cfg.get("market_heat_config", "{}"))

    def _tier_profile(self, tier: str) -> dict:
        profiles = formula.parse_json_object(self._cfg.get("market_tier_profiles", {}))
        profile = profiles.get(tier or "口碑")
        return profile if isinstance(profile, dict) else {}

    async def _adjust_heat(self, brand_id: int, current: float, delta: float) -> float:
        rules = self._heat_rules()
        heat = min(max(current + delta, float(rules.get("clamp_low", 0.5))),
                   float(rules.get("clamp_high", 1.5)))
        heat = round(heat, 3)
        await self._dao.update_brand_heat(brand_id, heat)
        return heat

    async def evolve_sentiment(self, season: int, window_seq: int,
                               attend_rates: dict[str, float]) -> tuple[list[str], dict]:
        """行情演化（状态类，重算整步跳过）：对每支有冠名的队按三信号步进满意度。

        delta = attend_weight×line_buffer×上座档 + result_weight×line_buffer×战绩档
                + event_weight×事件满意度原始值；负向按档位 penalty_mult 放大，钳 [0,2]。
        上座档按档位 good/bad_attend 定 ±1；战绩档按本窗口该队全部场次（主客场统一
        换算队视角）的胜率对 0.5±line_buffer 定 ±1；事件满意度由即发型 effect_json 与
        选择型 outcome 的 effects 求和（两处都不在此落库，只经此加权入信号）。
        满意度跌破档位下限（profile.satisfy_floor，缺省 market_satisfy_floor）→
        品牌解约（无赔偿）。近 3 场（主客场）全胜/全败只调品牌热度，静默。
        attend_rates: {队名: 本窗平均上座率}，缺省按中性 1.0。
        返回 (解约广播行, {队名: {"sat","delta","terminated"}})。
        """
        rules = self._satisfy_rules()
        attend_w = float(rules.get("attend_weight", 0.5))
        result_w = float(rules.get("result_weight", 0.3))
        event_w = float(rules.get("event_weight", 0.2))
        buffer = float(rules.get("line_buffer", 0.05))
        heat_rules = self._heat_rules()
        lines: list[str] = []
        details: dict[str, dict] = {}
        for s in await self._dao.list_stadiums():
            team = s["team_name"]
            naming = await self._dao.get_active_naming(team)
            if not naming:
                continue
            brand_row = await self._dao.get_brand_by_name(naming["brand"])
            profile = self._tier_profile((brand_row["tier"] if brand_row else "") or "口碑")
            rate = float(attend_rates.get(team, 1.0))
            good = float(profile.get("good_attend", 0.9))
            bad = float(profile.get("bad_attend", 0.6))
            s_attend = 1 if rate >= good else (-1 if rate <= bad else 0)
            wins = played = 0
            for m in await self._dao.get_team_matches_window(team, season, window_seq):
                res = m["result"]
                if res not in ("W", "L", "D"):
                    continue
                if m["home_team"] != team and res in ("W", "L"):
                    res = "L" if res == "W" else "W"
                played += 1
                wins += 1 if res == "W" else 0
            if not played:
                s_result = 0
            else:
                win_rate = wins / played
                s_result = 1 if win_rate >= 0.5 + buffer else (
                    -1 if win_rate <= 0.5 - buffer else 0)
            event_raw = 0.0
            for ev in await self._dao.get_window_events(team, season, window_seq):
                data = formula.parse_json_object(ev["effect_json"])
                event_raw += float(data.get("satisfaction", 0.0) or 0.0)
            for ch in await self._dao.get_window_choice_outcomes(team, season, window_seq):
                data = formula.parse_json_object(ch["outcome"])
                effects = data.get("effects") if isinstance(data.get("effects"), dict) else {}
                event_raw += float(effects.get("satisfaction", 0.0) or 0.0)
            delta = (attend_w * buffer * s_attend + result_w * buffer * s_result
                     + event_w * event_raw)
            if delta < 0:
                delta *= float(profile.get("penalty_mult", 1.0))
            sat = min(2.0, max(0.0, round(float(naming["satisfaction"]) + delta, 3)))
            await self._dao.update_naming_satisfaction(team, sat)
            if brand_row:
                recent = await self._dao.get_team_results_recent(team, 3)
                if len(recent) == 3:
                    marks = {r["result_team"] for r in recent}
                    if marks == {"W"}:
                        await self._adjust_heat(int(brand_row["id"]), float(brand_row["heat"]),
                                                float(heat_rules.get("win_streak", 0.03)))
                    elif marks == {"L"}:
                        await self._adjust_heat(int(brand_row["id"]), float(brand_row["heat"]),
                                                float(heat_rules.get("slump", -0.02)))
            floor = float(profile.get("satisfy_floor",
                                      self._cfg.get("market_satisfy_floor", 0.6)))
            terminated = False
            if sat < floor:
                await self._dao.terminate_naming(team)
                terminated = True
                lines.append(f"💔 品牌「{naming['brand']}」对 {team} 的满意度降至 {sat:.2f}"
                             f"（低于 {floor:.2f}），已解除冠名（无赔偿）")
            details[team] = {"sat": sat, "delta": round(delta, 4), "terminated": terminated}
        return lines, details

    async def champion_bonuses(self, season: int) -> dict | None:
        """赛季冠军加成：按已有比分场次算积分（胜3平1负0）→ 净胜球 → 队名定冠军。

        冠军队有生效冠名时品牌热度 +champion、合同满意度 +0.10；
        无比分场次跳过，无任何比分返回 None。
        """
        matches = await self._dao.get_season_result_matches(season)
        table: dict[str, dict] = {}
        for m in matches:
            goals = formula.score_goals(m["score"])
            if goals is None:
                continue
            home_goals, away_goals = goals
            res = m["result"]  # 主队视角 W/L/D
            pairs = ((m["home_team"], home_goals, away_goals, res),
                     (m["away_team"], away_goals, home_goals,
                      "W" if res == "L" else ("L" if res == "W" else "D")))
            for team, gf, ga, r in pairs:
                row = table.setdefault(team, {"team": team, "pts": 0, "gd": 0})
                row["pts"] += 3 if r == "W" else (1 if r == "D" else 0)
                row["gd"] += gf - ga
        if not table:
            return None
        champ = sorted(table.values(), key=lambda r: (-r["pts"], -r["gd"], r["team"]))[0]
        naming = await self._dao.get_active_naming(champ["team"])
        info = {**champ, "brand": naming["brand"] if naming else None}
        if naming:
            brand = await self._dao.get_brand_by_name(naming["brand"])
            if brand:
                await self._adjust_heat(int(brand["id"]), float(brand["heat"]),
                                        float(self._heat_rules().get("champion", 0.10)))
            sat = min(2.0, round(float(naming["satisfaction"]) + 0.10, 3))
            await self._dao.update_naming_satisfaction(champ["team"], sat)
            info["satisfaction"] = sat
        return info

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
            packages = d.get("packages") if isinstance(d.get("packages"), list) else None
            await self._dao.add_brand(
                d["brand"], d["heat"], source="llm", status="pending",
                industry=d.get("industry") or "通用",
                tier=d.get("tier") or "",
                pitch=d.get("pitch") or "",
                packages_json=(json.dumps({"variants": packages}, ensure_ascii=False)
                               if packages else "{}"),
            )
            added.append({"brand": d["brand"], "heat": d["heat"],
                          "tier": d.get("tier") or "口碑",
                          "industry": d.get("industry") or "通用",
                          "pitch": d.get("pitch") or ""})
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