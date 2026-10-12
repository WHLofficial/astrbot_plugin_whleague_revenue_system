"""运营平台天气同步推送（HMAC-SHA256 签名，失败不抛、重试 3 次）。

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
_MAX_ATTEMPTS = 3
_TIMEOUT_SECONDS = 10.0


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


class ClubSyncClient:
    """平台天气推送客户端。URL 或密钥为空时 enabled=False（旁路，不发起请求）。"""

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
