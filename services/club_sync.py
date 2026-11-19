"""运营平台同步客户端：天气推送（HMAC-SHA256 签名）+ club 当前赛季拉取。

签名与平台 `src/lib/hmac.ts` 逐字节对齐：规范串 `POST|path|ts|body`，
密钥同串，HMAC-SHA256 输出十六进制小写；`x-timestamp` 为 Unix 秒。
签名与发送必须使用同一字节串（`encode_body` 紧凑 JSON，非 ASCII 不转义），
重新序列化会改变键序/空白导致验签失败。
"""

import hashlib
import hmac
import json
import time

from astrbot.api import logger

SYNC_PATH = "/api/internal/weather-sync"
SEASON_PATH = "/api/seasons/current"
FIXTURES_PATH = "/api/fixtures"
_MAX_ATTEMPTS = 3
_TIMEOUT_SECONDS = 10.0
_SEASON_TTL_SECONDS = 600.0
_FIXTURES_TTL_SECONDS = 600.0

# club 赛事映射缓存（plugin_config kv）：club 当前赛季成功时落库，
# club 不可用回落 league_state 时读回——设计 §3.1「失败时用缓存」的落地
# （league_state 表只存赛季号，赛事映射单独存 kv）
TOURNAMENTS_KV_KEY = "club_tournaments"

# club competitionType → 本地 competition 文本；tournament_id 一律以 club 返回的 tournamentId 为准
COMPETITION_TYPE_MAP = {
    "league_premier": "顶级联赛",
    "league_second": "次级联赛",
    "champions_cup": "冠军杯",
}


class CurrentSeasonError(Exception):
    """club 与本地 league_state 均无法给出当前赛季。"""


def canonical_string(method: str, path: str, ts: str, body: str) -> str:
    """签名规范串：METHOD|path|ts|body（method 大写、path 不带查询串）。"""
    return f"{method.upper()}|{path}|{ts}|{body}"


def sign(secret: str, method: str, path: str, ts: str, body: str) -> str:
    """HMAC-SHA256 十六进制小写签名。"""
    message = canonical_string(method, path, ts, body).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def encode_body(payload: dict) -> str:
    """紧凑 JSON 请求体：分隔符固定、非 ASCII 原样，供签名与发送共用同一字节串。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_fixtures_payload(payload) -> list:
    """club /api/fixtures 响应 → [{"matchId", "attendance", "stadium_name", "capacity"}]。

    club 实际形状（src/worker/routes/fixtures.ts）：顶层 `{tournamentId, round,
    matches:[...]}`，逐项 `matchId/homeClub/awayTeamName/stageName/finished/
    stadium:{name,capacity,tier,venueName}|null/weather/attendance`。
    stadium 为对象：展示名取 venueName（生效冠名落点名，内部已回落球场名），
    其次 name；兼容旧的顶层 fixtures/items 键与扁平 stadiumName/capacity 字段。
    无法解析返回 []（调用方按缺失展示「—」，不阻塞出图）。
    """
    if isinstance(payload, dict):
        items = payload.get("matches")
        if items is None:
            items = payload.get("fixtures")
        if items is None:
            items = payload.get("items")
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    if not isinstance(items, list):
        return []

    def _int_or_none(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    out: list = []
    for it in items:
        if not isinstance(it, dict) or it.get("matchId") is None:
            continue
        mid = _int_or_none(it["matchId"])
        if mid is None:
            continue
        stadium_name = it.get("stadiumName")
        capacity = it.get("capacity")
        stadium = it.get("stadium")
        if isinstance(stadium, dict):
            stadium_name = stadium.get("venueName") or stadium.get("name") or stadium_name
            capacity = stadium.get("capacity", capacity)
        elif isinstance(stadium, str) and stadium:
            stadium_name = stadium
        out.append({
            "matchId": mid,
            "attendance": _int_or_none(it.get("attendance")) if it.get("attendance") is not None else None,
            "stadium_name": stadium_name,
            "capacity": _int_or_none(capacity) if capacity is not None else None,
        })
    return out


def parse_current_season(payload) -> dict | None:
    """club /api/seasons/current 响应 → 归一结构；无法得出赛季返回 None。

    season 字段兼容单对象与候选数组：取 preparing/running 且优先 running；
    响应中的 window 是全局最新，一律不当赛季窗口用（直接忽略）；
    tournaments 只保留 competitionType 可映射且带 tournamentId 的条目，
    tournament_id 以 club 返回的 tournamentId 为准（不硬编码 1/2/3）。
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("season")
    candidates = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    picked: tuple[int, str] | None = None
    for cand in candidates:
        status = str(cand.get("status") or "").strip().lower()
        if status not in ("preparing", "running"):
            continue
        try:
            num = int(cand.get("season"))
        except (TypeError, ValueError):
            continue
        if picked is None or (status == "running" and picked[1] != "running"):
            picked = (num, status)
    if picked is None:
        return None
    season, status = picked

    tournaments: list[dict] = []
    seen_comps: set[str] = set()
    for t in payload.get("tournaments") or []:
        if not isinstance(t, dict):
            continue
        comp = COMPETITION_TYPE_MAP.get(str(t.get("competitionType") or "").strip().lower())
        tid = t.get("tournamentId")
        if comp is None or tid is None or comp in seen_comps:
            continue
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            continue
        seen_comps.add(comp)
        tournaments.append({"tournament_id": tid, "competition": comp})
    return {
        "season": season,
        "season_label": f"S{season}",
        "status": status,
        "tournaments": tournaments,
    }


class ClubSyncClient:
    """平台同步客户端：天气推送 + 当前赛季。

    天气推送：URL 或密钥为空时 enabled=False（旁路，不发起请求）。
    当前赛季：仅需 club_sync_url（公开接口无签名），无需密钥。
    """

    # 当前赛季缓存为类级共享（client 可能在每次命令中新建实例），10 分钟 TTL
    _season_cache: dict | None = None
    _season_cache_at: float = 0.0
    # 上座/球场只读缓存：(tournament_id, round_no) -> (monotonic, list)
    _fixtures_cache: dict = {}

    def __init__(self, cfg: dict):
        self._url = str(cfg.get("club_sync_url", "") or "").strip()
        self._secret = str(cfg.get("club_sync_secret", "") or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self._url and self._secret)

    def endpoint(self) -> str:
        """推送地址：配置为平台根地址时补 `/api/internal/weather-sync`，已含该路径则原样使用。"""
        base = self._url.rstrip("/")
        if base.endswith(SYNC_PATH):
            return base
        return base + SYNC_PATH

    def root_url(self) -> str:
        """平台根地址：配置若误填了推送完整路径，剥掉 `/api/internal/weather-sync` 后缀。"""
        base = self._url.rstrip("/")
        if base.endswith(SYNC_PATH):
            base = base[: -len(SYNC_PATH)]
        return base.rstrip("/")

    @classmethod
    def reset_season_cache(cls) -> None:
        """清空当前赛季缓存（测试/手动刷新用）。"""
        cls._season_cache = None
        cls._season_cache_at = 0.0

    @classmethod
    def reset_fixtures_cache(cls) -> None:
        """清空上座/球场只读缓存（测试/手动刷新用）。"""
        cls._fixtures_cache = {}

    async def fetch_fixtures(self, tournament_id: int, round_no: int) -> list:
        """club 赛程上座/球场只读接口：GET /api/fixtures?tournament_id=&round=。

        (tournament_id, round_no) 内存缓存 10 分钟（类级共享）；只读不回写本地库；
        请求失败/解析失败记 warning 返回 []（空结果同样进入缓存，避免反复打 club）。
        返回列表项：{"matchId", "attendance", "stadium_name", "capacity"}。
        """
        key = (int(tournament_id), int(round_no))
        now = time.monotonic()
        cached = ClubSyncClient._fixtures_cache.get(key)
        if cached is not None and now - cached[0] < _FIXTURES_TTL_SECONDS:
            return cached[1]
        data = await self._fetch_fixtures_remote(*key)
        ClubSyncClient._fixtures_cache[key] = (now, data)
        return data

    async def _fetch_fixtures_remote(self, tournament_id: int, round_no: int) -> list:
        try:
            import aiohttp
        except ImportError:
            logger.warning("缺少 aiohttp 依赖，无法拉取 club 赛程上座")
            return []
        url = (
            f"{self.root_url()}{FIXTURES_PATH}"
            f"?tournament_id={int(tournament_id)}&round={int(round_no)}"
        )
        try:
            timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        logger.warning(f"club 赛程上座拉取失败：HTTP {resp.status} {text[:160]}")
                        return []
            try:
                payload = json.loads(text)
            except ValueError:
                logger.warning("club 赛程上座返回的不是 JSON")
                return []
            return parse_fixtures_payload(payload)
        except Exception as e:
            logger.warning(f"club 赛程上座拉取异常: {type(e).__name__}: {e}")
            return []

    async def fetch_current_season(self, dao=None) -> dict:
        """club 当前赛季 → {season, season_label, status, tournaments, source}。

        GET {club_sync_url 根}/api/seasons/current，公开无签名；成功结果内存
        缓存 10 分钟（类级共享，跨实例生效）；请求失败/不可用时回落本地
        league_state：赛季号取 league_state 行，赛事映射读上次成功时落库的
        plugin_config kv（club_tournaments，source=league_state，不写内存缓存）；
        club 与本地缓存皆无时抛 CurrentSeasonError('无法确定当前赛季')。
        成功获取（source=club）时写 league_state(season_number, 'S{n}')，并把
        赛事映射写 kv（空列表不落库，避免瞬时异常清掉最后一份可用映射）。
        """
        now = time.monotonic()
        if (
            ClubSyncClient._season_cache is not None
            and now - ClubSyncClient._season_cache_at < _SEASON_TTL_SECONDS
        ):
            return ClubSyncClient._season_cache

        parsed = await self._fetch_season_remote() if self._url else None
        if parsed is not None:
            parsed["source"] = "club"
            ClubSyncClient._season_cache = parsed
            ClubSyncClient._season_cache_at = now
            if dao is not None:
                await dao.set_league_state(parsed["season"], parsed["season_label"])
                if parsed["tournaments"]:
                    try:
                        await dao.set_config(
                            TOURNAMENTS_KV_KEY,
                            json.dumps(parsed["tournaments"], ensure_ascii=False,
                                       separators=(",", ":")),
                        )
                    except Exception as e:  # kv 写失败不阻断赛季获取（回落时无缓存可用而已）
                        logger.warning(f"club_tournaments kv 写入失败: {type(e).__name__}: {e}")
            return parsed

        state = await dao.get_league_state() if dao is not None else None
        if state is not None and state["season_number"] is not None:
            season = int(state["season_number"])
            return {
                "season": season,
                "season_label": str(state["season_label"] or f"S{season}"),
                "status": None,
                "tournaments": await self._cached_tournaments(dao),
                "source": "league_state",
            }
        raise CurrentSeasonError(
            "无法确定当前赛季：club 当前赛季接口不可用，且本地 league_state 为空"
        )

    @staticmethod
    async def _cached_tournaments(dao) -> list:
        """读 kv 里缓存的赛事映射；无 dao/无 kv/解析失败返回 []。"""
        if dao is None:
            return []
        try:
            raw = await dao.get_config(TOURNAMENTS_KV_KEY)
            data = json.loads(raw) if raw else None
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        out: list = []
        for t in data:
            if not isinstance(t, dict):
                continue
            comp = t.get("competition")
            tid = t.get("tournament_id")
            if not comp or tid is None:
                continue
            try:
                out.append({"tournament_id": int(tid), "competition": str(comp)})
            except (TypeError, ValueError):
                continue
        return out

    async def _fetch_season_remote(self) -> dict | None:
        """拉取并解析 club 当前赛季；任何失败都记 warning 返回 None（由上层回落）。"""
        try:
            import aiohttp
        except ImportError:
            logger.warning("缺少 aiohttp 依赖，无法拉取 club 当前赛季（回落本地 league_state）")
            return None
        url = self.root_url() + SEASON_PATH
        try:
            timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        logger.warning(f"club 当前赛季拉取失败：HTTP {resp.status} {text[:160]}")
                        return None
            try:
                payload = json.loads(text)
            except ValueError:
                logger.warning("club 当前赛季返回的不是 JSON")
                return None
            parsed = parse_current_season(payload)
            if parsed is None:
                logger.warning("club 当前赛季无 preparing/running 状态的赛季")
            return parsed
        except Exception as e:
            logger.warning(f"club 当前赛季拉取异常: {type(e).__name__}: {e}")
            return None

    async def push_round(self, season: int, competition: str, round_no: int,
                         items: list) -> dict:
        """推送一轮天气，返回 {"status": "bypass"|"ok"|"error", ...}（不抛异常）。

        status=ok 时附平台响应体（含逐场 verdicts）；4xx 视为确定性拒绝不重试，
        网络异常与 5xx 重试至多 3 次，时间戳每次重试重新取。
        """
        if not self.enabled:
            return {"status": "bypass"}
        try:
            import aiohttp
        except ImportError:
            return {"status": "error", "error": "缺少 aiohttp 依赖，无法推送（pip install aiohttp）"}

        payload = {
            "season": int(season),
            "competition": competition,
            "roundNo": int(round_no),
            "items": items,
        }
        body = encode_body(payload)
        endpoint = self.endpoint()
        last_error = "推送失败"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            ts = str(int(time.time()))
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "x-timestamp": ts,
                "x-sign": sign(self._secret, "POST", SYNC_PATH, ts, body),
            }
            try:
                timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        endpoint, data=body.encode("utf-8"), headers=headers
                    ) as resp:
                        text = await resp.text()
                        if resp.status == 200:
                            try:
                                data = json.loads(text)
                            except ValueError:
                                data = {}
                            return {
                                "status": "ok",
                                "competition": competition,
                                "round_no": round_no,
                                "response": data,
                            }
                        last_error = f"HTTP {resp.status}: {text[:200]}"
                        if resp.status < 500:
                            # 4xx（签名/绑定/匹配）为确定性拒绝，重试不会变好
                            break
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
            logger.warning(
                f"天气推送失败（第 {attempt}/{_MAX_ATTEMPTS} 次，{competition} 第 {round_no} 轮）: {last_error}"
            )
        return {"status": "error", "error": last_error}
