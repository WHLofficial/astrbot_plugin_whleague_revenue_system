"""冠名权市场引擎：品牌按队况估值发报价，球队签约成交；四模式、TTL、档位校准与热度反馈。

时间线：推进时先收口上轮（未签约报价作废+全冷落品牌降热，全部静默）→ 校准档位 →
开轮发报价 → 播报。签约即成交（命令响应即播报），合同当窗生效、下轮结算收首笔冠名费。
报价 TTL：expire_at 懒检查（签约/报价板读取路径）+ 推进硬收口。
先到先得模式：品牌按平均队况挂公开一口价（稳健套餐），任意队先签先得，名额满即关。
"""

import json
import math
from datetime import datetime, timedelta

from . import formula
from .brand_service import _snapshot, build_packages, variant_packages


class MarketError(Exception):
    pass


class MarketService:
    def __init__(self, db, dao, cfg, brand_service):
        self._db = db
        self._dao = dao
        self._cfg = cfg
        self._brand_service = brand_service

    # ─── 模式与档位 ───────────────────────────────────────

    def mode(self) -> str:
        return str(self._cfg.get("market_bid_mode", "明标"))

    def _tier_quota(self, tier: str) -> int | None:
        """档位名额：头部每品牌 1 队、新兴限量、口碑不限（None）。"""
        if tier == "头部":
            return 1
        if tier == "新兴":
            return max(1, int(self._cfg.get("market_emerging_slots", 2)))
        return None

    async def recalibrate_tiers(self) -> int:
        """排名制档位校准：未锁档品牌按热度划档（头部配额=ceil(队数/配额除数)）。

        头部取热度前 K 名，其余热度≥0.9 入新兴、剩下口碑；锁档品牌跳过。返回调整数。
        """
        teams = await self._dao.list_stadiums()
        team_count = max(1, len(teams))
        top_k = max(1, math.ceil(team_count / max(1, int(self._cfg.get("market_top_seat_ratio", 8)))))
        brands = [b for b in await self._dao.list_brands("adopted") if not int(b["tier_locked"] or 0)]
        brands.sort(key=lambda b: float(b["heat"]), reverse=True)
        changed = 0
        for i, b in enumerate(brands):
            if i < top_k:
                tier = "头部"
            elif float(b["heat"]) >= 0.9:
                tier = "新兴"
            else:
                tier = "口碑"
            if b["tier"] != tier:
                await self._dao.update_brand_tier(int(b["id"]), tier)
                changed += 1
        return changed

    async def set_tier(self, brand_id: int, tier: str, industry: str | None = None) -> None:
        """管理定档：写入档位并锁档（退出自动排名），可选改行业（行业名钳 8 字，与 LLM 生成一致）。"""
        if tier not in ("头部", "新兴", "口碑"):
            raise MarketError("档位需为 头部/新兴/口碑")
        brands = {int(b["id"]): b for b in await self._dao.list_brands()}
        if brand_id not in brands:
            raise MarketError("品牌不存在（/主场品牌列表 查看 ID）")
        if industry:
            industry = industry.strip()[:8] or None
        await self._dao.update_brand_tier(brand_id, tier, industry=industry, lock=True)

    # ─── 热度反馈 ─────────────────────────────────────────

    def _heat_rules(self) -> dict:
        return formula.parse_json_object(self._cfg.get("market_heat_config", {}))

    async def _adjust_heat(self, brand_id: int, delta: float) -> None:
        rules = self._heat_rules()
        low = float(rules.get("clamp_low", 0.5))
        high = float(rules.get("clamp_high", 1.5))
        row = await self._dao.get_brand(brand_id)
        if not row:
            return
        heat = min(high, max(low, float(row["heat"]) + delta))
        await self._dao.update_brand_heat(brand_id, heat)

    # ─── 推进例行 ─────────────────────────────────────────

    async def run_window(self, season: int, window_seq: int, opened_by: str = "auto") -> list[str]:
        """/主场推进 后的市场例行：①上轮收口 ②档位校准 ③开轮发报价。返回播报行。"""
        mode = self.mode()
        if mode == "常态":
            return []
        await self._close_open_rounds()
        await self.recalibrate_tiers()
        return await self._open_round(season, window_seq, mode, opened_by)

    async def _close_open_rounds(self) -> None:
        """推进硬收口：未签约报价作废、轮置 settled；本轮报价全未成交的品牌降热（静默）。"""
        ignored = float(self._heat_rules().get("ignored", -0.03))
        for rnd in await self._dao.list_open_rounds():
            offers = await self._dao.list_round_offers(int(rnd["id"]))
            await self._dao.settle_market_round(int(rnd["id"]))
            by_brand: dict[int, list] = {}
            for o in offers:
                by_brand.setdefault(int(o["brand_id"]), []).append(o)
            for brand_id, brand_offers in by_brand.items():
                if all(o["status"] != "accepted" for o in brand_offers):
                    await self._adjust_heat(brand_id, ignored)

    async def _open_round(self, season: int, window_seq: int, mode: str,
                          opened_by: str) -> list[str]:
        round_id = await self._dao.open_market_round(season, window_seq, mode, opened_by)
        rnd = await self._dao.get_market_round(season, window_seq)
        if not rnd or rnd["status"] != "open":
            return []  # 同窗轮已收口（异常场景）不再发报价
        expire_at = self._expire_at()
        # 名额未满的品牌才出手（头部 1 队/新兴限量；口碑不限）
        sellers = []
        for b in await self._dao.list_brands("adopted"):
            quota = self._tier_quota(b["tier"] or "口碑")
            if quota is not None:
                used = await self._dao.count_active_namings_by_brand(b["brand"])
                if used >= quota:
                    continue
            sellers.append(b)
        if mode == "先到先得":
            return await self._fcfs_offers(round_id, sellers, expire_at)
        return await self._targeted_offers(round_id, sellers, expire_at, mode)

    def _expire_at(self) -> str | None:
        ttl = int(self._cfg.get("market_offer_ttl_hours", 72))
        if ttl <= 0:
            return None
        return (datetime.now() + timedelta(hours=ttl)).strftime("%Y-%m-%d %H:%M:%S")

    async def _valuations(self, brand_row) -> list[tuple[str, float]]:
        """品牌对所有可签队（无球场跳过、已有生效冠名排除）的估值，降序。

        估值 = naming_fee(容量, 死忠) × 热度 × 行业系数——同品牌给大队报得高。
        """
        heat = float(brand_row["heat"])
        factor = formula.industry_factor(self._cfg, brand_row["industry"] or "通用")
        out = []
        for s in await self._dao.list_stadiums():
            if await self._dao.get_active_naming(s["team_name"]):
                continue
            fee = formula.naming_fee(self._cfg, float(s["capacity"]),
                                     float(s["fans_diehards"]), heat)
            out.append((s["team_name"], round(fee * factor, 3)))
        out.sort(key=lambda x: x[1], reverse=True)
        return out

    def _brand_package(self, brand_row, tier: str, base_fee: float) -> dict:
        """品牌选套餐：自定义变体优先；缺省头部大牌只做短约（进取），新兴/口碑走稳健。"""
        packages = (variant_packages(self._cfg, base_fee, brand_row["packages_json"] or "{}")
                    if brand_row else None) or build_packages(self._cfg, base_fee)
        idx = 1 if tier == "头部" else 0
        return packages[min(idx, len(packages) - 1)]

    async def _targeted_offers(self, round_id: int, sellers: list, expire_at: str | None,
                               mode: str) -> list[str]:
        per_brand = max(1, int(self._cfg.get("market_offers_per_brand", 3)))
        offered_teams: set[str] = set()
        rows: list = []  # (brand_row, team, package)
        for brand in sellers:
            tier = brand["tier"] or "口碑"
            vals = await self._valuations(brand)
            if not vals:
                continue
            if tier == "头部":
                picks = vals[:min(2, per_brand)]  # 盯估值最高 1-2 队
            elif tier == "新兴":
                mid = len(vals) // 2  # 盯中游
                picks = vals[max(0, mid - 1):max(0, mid - 1) + per_brand]
            else:
                # 口碑广撒：优先零报价球队兜底，再按估值补位
                zero = [v for v in vals if v[0] not in offered_teams]
                rest = [v for v in vals if v[0] in offered_teams]
                picks = (zero + rest)[:per_brand]
            for team, fee in picks:
                offered_teams.add(team)
                rows.append((brand, team, self._brand_package(brand, tier, fee)))
        for brand, team, package in rows:
            await self._dao.add_market_offer(
                round_id, int(brand["id"]), team, int(package["pkg_no"]),
                float(package["amount"]), int(package["windows"]),
                json.dumps(_snapshot(package), ensure_ascii=False), expire_at)
        lines = []
        if mode == "明标":
            lines.append(f"📣 招商期开启：本轮 {len(rows)} 份报价送达")
            for brand, team, package in rows:
                lines.append(f"· {brand['brand']} → {team}：{package['amount']:g}M/窗 × "
                             f"{package['windows']} 窗（{package['pkg_name']}）")
            lines.append("各队发送 /主场招商 查看报价")
        else:  # 暗标：明细仅收价队可见，无公开明细
            teams = len({team for _, team, _ in rows})
            lines.append(f"📣 招商期已开启：本轮 {teams} 支球队收到报价，明细仅收价队可见")
        return lines

    async def _fcfs_offers(self, round_id: int, sellers: list,
                           expire_at: str | None) -> list[str]:
        """先到先得：按平均队况估值挂公开一口价（稳健套餐），任意队先签先得。"""
        teams = await self._dao.list_stadiums()
        if not teams:
            return []
        avg_cap = sum(float(s["capacity"]) for s in teams) / len(teams)
        avg_fans = sum(float(s["fans_diehards"]) for s in teams) / len(teams)
        lines = []
        for brand in sellers:
            fee = formula.naming_fee(self._cfg, avg_cap, avg_fans, float(brand["heat"]))
            fee = round(fee * formula.industry_factor(self._cfg, brand["industry"] or "通用"), 3)
            packages = (variant_packages(self._cfg, fee, brand["packages_json"] or "{}")
                        or build_packages(self._cfg, fee))
            package = packages[0]  # 稳健结构（自定义变体取第一档）
            await self._dao.add_market_offer(
                round_id, int(brand["id"]), "", int(package["pkg_no"]),
                float(package["amount"]), int(package["windows"]),
                json.dumps(_snapshot(package), ensure_ascii=False), expire_at)
            lines.append(f"🏷 {brand['brand']} 公开挂价：{package['amount']:g}M/窗 × "
                         f"{package['windows']} 窗（先签先得）")
        if lines:
            lines.append("发送 /主场招商 查看挂价，/主场签约 <报价号> 先签先得")
        return lines

    # ─── 签约与报价板 ─────────────────────────────────────

    async def accept(self, team_name: str, offer_id, season: int, window_seq: int) -> dict:
        """/主场签约：报价认领→合同生效→品牌热度+成交值。返回广播 dict。"""
        mode = self.mode()
        if mode == "常态":
            raise MarketError("当前为常态模式，请直接 /主场冠名 <品牌>")
        await self._dao.expire_stale_offers()  # TTL 懒检查
        offer = await self._dao.get_market_offer(int(offer_id))
        if not offer:
            raise MarketError("报价不存在（/主场招商 查看最新报价号）")
        if offer["round_status"] != "open":
            raise MarketError("该报价所属招商期已结束")
        if offer["status"] == "accepted":
            raise MarketError("该报价已被接受")
        if offer["status"] != "pending":
            raise MarketError("该报价已失效")
        if offer["expire_at"] and offer["expire_at"] <= self._dao.now_str():
            raise MarketError("该报价已超过时效，无法签约")
        if offer["team_name"] and offer["team_name"] != team_name:
            raise MarketError("该报价是发给其他球队的定向报价")
        if await self._dao.get_active_naming(team_name):
            raise MarketError("本队已有生效冠名合同，先退约才能签新约")
        tier = offer["tier"] or "口碑"
        quota = self._tier_quota(tier)
        if quota is not None:
            used = await self._dao.count_active_namings_by_brand(offer["brand"])
            if used >= quota:
                raise MarketError(f"「{offer['brand']}」{tier}档名额已满（{quota} 队）")
        result = await self._dao.accept_market_offer(int(offer_id), team_name, season, window_seq,
                                                     brand_quota=quota)
        if result is None:
            raise MarketError("签约失败：报价刚被占用或已失效，请刷新报价后重试")
        await self._adjust_heat(int(offer["brand_id"]),
                                float(self._heat_rules().get("deal", 0.02)))
        pkg = formula.parse_json_object(offer["package_json"])
        return {"team": team_name, "brand": offer["brand"],
                "fee_per_window": float(offer["amount"]), "windows": int(offer["windows"]),
                "package": str(pkg.get("pkg_name", "稳健")),
                "bonus": float(pkg.get("bonus", 0.0) or 0.0)}

    async def board(self, team_name: str, season: int, window_seq: int) -> list[str]:
        """/主场招商 数据：本队收到的报价 +（明标）全联赛板 /（先到先得）公开挂价。"""
        mode = self.mode()
        if mode == "常态":
            return ["当前为常态模式：/主场冠名 <品牌> 直接签约"]
        await self._dao.expire_stale_offers()
        now = self._dao.now_str()
        lines = []
        mine = await self._dao.list_pending_offers_for_team(team_name)
        if mine:
            lines.append(f"📩 {team_name} 收到的报价：")
            for o in mine:
                expired = o["status"] != "pending" or (
                    o["expire_at"] is not None and o["expire_at"] <= now)
                tag = "（已过期）" if expired else ""
                pkg = formula.parse_json_object(o["package_json"])
                bonus = float(pkg.get("bonus", 0.0) or 0.0)
                ev = ""
                if bonus > 0:
                    prob = float(self._cfg.get("naming_bet_expected_prob", 0.5))
                    ev = f"｜对赌奖金 {bonus:g}M（期望 +{round(bonus * prob, 3):g}M）"
                lines.append(f"#{o['id']} {o['brand']}（{o['tier']}）{pkg.get('pkg_name', '')}："
                             f"{o['amount']:g}M/窗 × {o['windows']} 窗{ev}{tag}")
                if o["pitch"]:
                    lines.append(f"　「{o['pitch']}」")
            lines.append("签约：/主场签约 <报价号>")
        else:
            lines.append("本队暂无报价（品牌按队况估值发报价，留意下轮招商）")
        if mode == "明标":
            rnd = await self._dao.get_market_round(season, window_seq)
            if rnd:
                offers = await self._dao.list_round_offers(int(rnd["id"]))
                if offers:
                    lines.append("📋 全联赛报价板：")
                    for o in offers:
                        if not o["team_name"]:
                            continue
                        mark = "✅" if o["status"] == "accepted" else "⏳"
                        lines.append(f"{mark} {o['brand']} → {o['team_name']}："
                                     f"{o['amount']:g}M/窗 × {o['windows']} 窗")
        elif mode == "先到先得":
            fcfs = await self._dao.list_fcfs_offers()
            if fcfs:
                lines.append("🏷 公开挂价（先签先得）：")
                for o in fcfs:
                    lines.append(f"#{o['id']} {o['brand']}（{o['tier']}）："
                                 f"{o['amount']:g}M/窗 × {o['windows']} 窗")
        return lines
