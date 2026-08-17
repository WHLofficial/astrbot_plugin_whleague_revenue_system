"""数据库备份服务：VACUUM INTO 一致性快照 + 保留数量管理。"""

import time
from pathlib import Path

from astrbot.api import logger


class BackupService:
    def __init__(self, db, cfg):
        self._db = db
        self._cfg = cfg

    @property
    def backup_dir(self) -> Path:
        d = Path(self._db.db_path).parent / "backup"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def run_backup(self) -> dict:
        """执行一次备份并清理旧备份，返回结果信息。"""
        if not self._db.conn:
            raise RuntimeError("Database not initialized")
        if not Path(self._db.db_path).exists():
            raise RuntimeError("Database file not found")

        ts = time.strftime("%Y%m%d_%H%M%S")
        target = self.backup_dir / f"revenue_{ts}_{time.time_ns() % 1000000:06d}.db"
        async with self._db.lock:
            await self._db.conn.execute(
                f"VACUUM INTO '{target.as_posix().replace(chr(39), chr(39) * 2)}'"
            )
        removed = self._cleanup_oldest()
        logger.info(f"Backup created: {target.name}")
        return {"path": str(target), "removed": removed}

    def _cleanup_oldest(self) -> int:
        keep = max(1, int(self._cfg.get("backup_keep_count", 10)))
        files = sorted(
            (p for p in self.backup_dir.glob("revenue_*.db") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        removed = 0
        while len(files) > keep:
            old = files.pop(0)
            try:
                old.unlink()
                removed += 1
            except OSError as e:
                logger.warning(f"Failed to remove old backup {old}: {e}")
        return removed