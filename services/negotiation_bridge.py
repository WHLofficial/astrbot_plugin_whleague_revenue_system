"""只读谈判系统数据库桥（球队/绑定校验）。

谈判库不存在或字段不符时静默降级：is_available() 返回 False，
上层回退为纯管理员白名单模式。
"""

import os

import aiosqlite

from astrbot.api import logger

_DEFAULT_SUBDIR = "astrbot_plugin_whleague_negotiation_system"
_DEFAULT_FILENAME = "negotiation_system.db"


def _default_negotiation_db_path() -> str | None:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        p = os.path.join(get_astrbot_plugin_data_path(), _DEFAULT_SUBDIR, _DEFAULT_FILENAME)
        if os.path.exists(p):
            return p
    except Exception:
        pass
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        p = os.path.join(get_astrbot_data_path(), _DEFAULT_SUBDIR, _DEFAULT_FILENAME)
        if os.path.exists(p):
            return p
    except Exception:
        pass
    return None


class NegotiationBridge:
    """只读访问谈判系统数据库。连接按需打开、只读模式（mode=ro）。"""

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._conn: aiosqlite.Connection | None = None
        self._path: str | None = self._resolve_path()

    def _resolve_path(self) -> str | None:
        configured = str(self._cfg.get("negotiation_db_path", "") or "").strip()
        if configured:
            return configured if os.path.exists(configured) else None
        return _default_negotiation_db_path()

    @property
    def path(self) -> str | None:
        return self._path

    async def connect(self) -> None:
        if self._conn is not None:
            return
        if not self._path:
            return
        try:
            uri = f"file:{self._path}?mode=ro"
            conn = await aiosqlite.connect(uri, uri=True)
            conn.row_factory = aiosqlite.Row
            self._conn = conn
        except Exception as e:
            logger.warning(f"Negotiation bridge connect failed: {e}")
            self._conn = None

    async def close(self) -> None:
        if self._conn:
            try:
                await self._conn.close()
            except Exception:
                pass
            self._conn = None

    async def is_available(self) -> bool:
        if not self._path:
            return False
        if self._conn is None:
            await self.connect()
        return self._conn is not None

    async def _query(self, sql: str, params=()):
        try:
            if self._conn is None:
                await self.connect()
            if self._conn is None:
                return None
            cur = await self._conn.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
            return rows
        except Exception as e:
            logger.warning(f"Negotiation bridge query failed: {e}")
            return None

    async def list_team_names(self) -> list[str]:
        rows = await self._query("SELECT name FROM teams ORDER BY name")
        if rows is None:
            return []
        return [r["name"] for r in rows]

    async def get_binding_by_qq(self, qq: str):
        """返回 {team_id, team_name} 或 None。"""
        rows = await self._query(
            "SELECT b.team_id, t.name AS team_name FROM team_bindings b "
            "JOIN teams t ON t.id=b.team_id WHERE b.qq=? LIMIT 1",
            (str(qq),),
        )
        if not rows:
            return None
        return {"team_id": rows[0]["team_id"], "team_name": rows[0]["team_name"]}