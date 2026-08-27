"""球场：属性导入（纯管理员）、越界校验、自动记账、建设券、管理员改名。

属性变更时自动按 S9 定价记支出；建设券 = 扩建/升级支出的 25% 返还，
仅可抵扣后续建设支出（均为配置项）。
"""

from astrbot.api import logger

from . import formula
from .file_import_service import (
    FileImportError,
    check_import_file,
    is_import_ext,
    list_import_files as _list_import_files,
    parse_attribute_file,
)


class StadiumError(Exception):
    pass


_FACILITY_KEYS = (
    formula.FACILITY_COMMERCIAL,
    formula.FACILITY_BROADCAST,
    formula.FACILITY_PITCH,
    formula.FACILITY_YOUTH,
    formula.FACILITY_MEDICAL,
)

_FACILITY_NAMES = {
    formula.FACILITY_COMMERCIAL: "商业区",
    formula.FACILITY_BROADCAST: "灯光转播",
    formula.FACILITY_PITCH: "草皮",
    formula.FACILITY_YOUTH: "青训中心",
    formula.FACILITY_MEDICAL: "医疗中心",
}


class StadiumService:
    def __init__(self, db, dao, cfg, bridge=None):
        self._db = db
        self._dao = dao
        self._cfg = cfg
        self._bridge = bridge

    # ─── 基础 ─────────────────────────────────────────────

    async def list_known_teams(self) -> set[str]:
        """已知球队 = 谈判库球队 ∪ 已建球场球队。"""
        names = set()
        if self._bridge is not None and await self._bridge.is_available():
            try:
                names.update(await self._bridge.list_team_names())
            except Exception:
                pass
        for s in await self._dao.list_stadiums():
            names.add(s["team_name"])
        return names

    async def ensure_stadium(self, team_name: str):
        """确保球场行存在（缺失时按默认属性初始化并发放启动资金）。"""
        row = await self._dao.get_stadium(team_name)
        if row:
            await self._dao.ensure_balance(team_name, float(self._cfg.get("start_funds", 50.0)))
            return row
        influence = float(self._cfg.get("default_influence", 90.0))
        fans = formula.diehard_target(self._cfg, influence)
        await self._dao.create_stadium(
            team_name, capacity=12000, tier=0, influence=influence, fans=fans
        )
        await self._dao.ensure_balance(team_name, float(self._cfg.get("start_funds", 50.0)))
        await self._dao.add_transaction(
            team_name, 0, 0, "init", float(self._cfg.get("start_funds", 50.0)),
            note="初始球场与启动资金",
        )
        return await self._dao.get_stadium(team_name)

    async def grant_all(self) -> dict:
        """主场发放：为谈判库全部球队初始化球场。"""
        names = await self.list_known_teams()
        if not names:
            raise StadiumError("没有可发放的球队（谈判库不可用且无已建球场）")
        created = 0
        for name in sorted(names):
            await self.ensure_stadium(name)
            created += 1
        return {"created": created}

    def _validate_tier(self, tier: int) -> None:
        max_tier = int(self._cfg.get("max_open_tier", 1))
        if tier > max_tier:
            raise StadiumError(f"等级 {tier} 未开放（当前上限 {max_tier}）")
        table = parse_tier_table(self._cfg)
        if str(tier) not in table:
            raise StadiumError(f"未知等级 {tier}")

    def _validate_capacity_for_tier(self, tier: int, capacity: int) -> None:
        hi = int(formula.tier_config(self._cfg, tier)["max_seats"])
        if capacity > hi:
            raise StadiumError(f"容量 {capacity:,} 超过等级 {tier} 的座位上限 {hi:,}")

    # ─── 属性导入 ─────────────────────────────────────────

    async def import_attributes(self, team_name: str, influence: float | None = None,
                                capacity: int | None = None, tier: int | None = None) -> dict:
        """导入更新球队属性；属性变更自动记账（扩建 0.1M/100座、升级按等级表）。"""
        state = await self._dao.get_league_state()
        season = state["season_number"] if state else 1
        window_seq = state["window_seq"] if state else 1
        stadium = await self.ensure_stadium(team_name)

        notes = []
        if tier is not None:
            self._validate_tier(tier)
            old_tier = stadium["tier"]
            if tier > old_tier:
                cost = sum(
                    formula.tier_upgrade_cost(self._cfg, t)
                    for t in range(old_tier + 1, tier + 1)
                )
                if old_tier == 0 and old_tier + 1 <= tier:
                    # 0 级没有升级费（初始即 0 级），仅累加 1..tier 的升级费
                    cost = sum(
                        formula.tier_upgrade_cost(self._cfg, t)
                        for t in range(max(1, old_tier + 1), tier + 1)
                    )
                if cost > 0:
                    await self._record_build_cost(team_name, season, window_seq, cost, f"升到{formula.tier_config(self._cfg, tier)['name']}")
                    notes.append(f"升级 {old_tier}→{tier} 花费 {cost:.1f}M")
            await self._dao.update_stadium_attrs(team_name, tier=tier)
            stadium = await self._dao.get_stadium(team_name)

        if capacity is not None:
            self._validate_capacity_for_tier(stadium["tier"], capacity)
            old_cap = stadium["capacity"]
            if capacity > old_cap:
                cost = formula.expansion_cost(self._cfg, old_cap, capacity)
                await self._record_build_cost(team_name, season, window_seq, cost, f"扩建 {old_cap:,}→{capacity:,}")
                notes.append(f"扩建 {old_cap:,}→{capacity:,} 花费 {cost:.1f}M")
            await self._dao.update_stadium_attrs(team_name, capacity=capacity)
            stadium = await self._dao.get_stadium(team_name)

        if influence is not None:
            if influence < 1 or influence > 1000:
                raise StadiumError("影响力需在 1~1000 之间")
            await self._dao.update_influence(team_name, influence)
            await self._dao.add_influence_history(team_name, season, window_seq, influence)
            notes.append(f"影响力 → {influence:g}")

        return {"team": team_name, "notes": notes, "stadium": await self._dao.get_stadium(team_name)}

    async def import_attributes_file(self, path: str) -> dict:
        """从 xlsx/csv 文件批量导入属性（逐队复用 import_attributes，独立报错）。"""
        parsed = await parse_attribute_file(self._cfg, path)
        if not parsed["records"]:
            detail = f"（{parsed['errors'][0]}）" if parsed["errors"] else ""
            raise StadiumError(f"文件中没有可导入的属性行{detail}")
        results = []
        imported = 0
        for team, influence, capacity, tier in parsed["records"]:
            try:
                r = await self.import_attributes(team, influence, capacity, tier)
                results.append({"team": team, "ok": True, "notes": r["notes"]})
                imported += 1
            except (StadiumError, ValueError) as e:
                results.append({"team": team, "ok": False, "error": str(e)})
        return {"imported": imported, "results": results, "errors": parsed["errors"]}

    # ─── 本地 imports 目录（文件名直读 / 钩子共用入口） ──────

    def resolve_import_file(self, name: str) -> str | None:
        """命令参数自动判定：返回 imports 目录里的真实路径，None 表示按文本处理。

        参数以 .csv/.xlsx 结尾（明确的文件意图）但目录里没有 → 抛 FileImportError；
        普通文本参数 → None（走文本导入）。
        """
        s = str(name or "").strip()
        if not s:
            return None
        try:
            return check_import_file(self._db.db_path, s)
        except FileImportError:
            if is_import_ext(s):
                raise
            return None

    def list_import_files(self) -> list[str]:
        """imports 目录内可导入文件名（命令无参时的提示用）。"""
        return [p.name for p in _list_import_files(self._db.db_path)]

    async def import_attributes_by_name(self, name: str) -> dict:
        """按文件名从 imports 目录批量导入属性。"""
        path = check_import_file(self._db.db_path, name)
        return await self.import_attributes_file(path)

    async def _record_build_cost(self, team_name: str, season: int, window_seq: int,
                                 cost: float, note: str) -> None:
        """记录建设支出：建设券抵扣 + 支出入账 + 25% 返还建设券（可配）。"""
        if cost <= 0:
            return
        await self._dao.ensure_balance(team_name, float(self._cfg.get("start_funds", 50.0)))
        used = await self._dao.deduct_build_credit(team_name, cost)
        await self._dao.apply_balance(team_name, -cost)
        paid_note = note if not used else f"{note}（建设券抵扣 {used:.1f}M）"
        await self._dao.add_transaction(team_name, season, window_seq, "expand" if "扩建" in note else "upgrade", -cost, paid_note)
        if self._cfg.get("build_credit_enabled", True):
            credit = cost * float(self._cfg.get("build_credit_ratio", 0.25))
            await self._dao.apply_balance(team_name, 0.0, build_credit_amount=credit)
            await self._dao.add_transaction(team_name, season, window_seq, "credit", credit, "建设券返还")

    # ─── 子设施 ───────────────────────────────────────────

    async def import_facility(self, team_name: str, facility_key: str, level: int) -> dict:
        if facility_key not in _FACILITY_KEYS:
            raise StadiumError(f"未知设施: {facility_key}（可用: {', '.join(_FACILITY_NAMES.values())}）")
        if not (0 <= level <= 5):
            raise StadiumError("设施等级需在 0~5 之间")
        state = await self._dao.get_league_state()
        season = state["season_number"] if state else 1
        window_seq = state["window_seq"] if state else 1
        await self.ensure_stadium(team_name)
        old = await self._dao.get_facility_level(team_name, facility_key)
        if level > old:
            cost = formula.facility_cost_to_level(self._cfg, level) - formula.facility_cost_to_level(self._cfg, old)
            await self._record_build_cost(team_name, season, window_seq, cost,
                                          f"{_FACILITY_NAMES[facility_key]} {old}→{level}")
        await self._dao.upsert_facility(team_name, facility_key, level)
        return {"facility": _FACILITY_NAMES[facility_key], "level": level}

    # ─── 改名（管理员） ───────────────────────────────────

    async def admin_rename(self, team_name: str, new_name: str) -> dict:
        """管理员代改球场名：免费、不记流水；无球场的队伍报错（不自动建场）。"""
        new_name = new_name.strip()
        if not new_name:
            raise StadiumError("名称不能为空")
        if "\n" in new_name or "\r" in new_name:
            raise StadiumError("名称不能包含换行")
        if len(new_name) > 30:
            raise StadiumError("名称最长 30 字")
        stadium = await self._dao.get_stadium(team_name)
        if not stadium:
            raise StadiumError(f"{team_name} 还没有球场，无法改名")
        if new_name == stadium["name"]:
            raise StadiumError(f"名称未变化（当前已是「{stadium['name']}」）")
        await self._dao.update_stadium_name(team_name, new_name)
        return {"old": stadium["name"], "new": new_name}


def parse_tier_table(cfg: dict) -> dict:
    from ..config.defaults import parse_json_object

    return parse_json_object(cfg.get("tier_table", {}))