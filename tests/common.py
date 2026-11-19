"""测试公共环境：临时数据库 + 4 服务装配（TourSync/Weather/Backup/Chart）。

以插件包全名（astrbot_plugin_whleague_revenue_system.*）绝对导入，
需将 PLUGINS_DIR 加入 sys.path。
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_DIR = os.path.dirname(PLUGIN_ROOT)
if PLUGINS_DIR not in sys.path:
    sys.path.insert(0, PLUGINS_DIR)

from .stubs import install_stubs  # noqa: E402

install_stubs()

from astrbot_plugin_whleague_revenue_system.config.defaults import DEFAULT_CONFIG  # noqa: E402
from astrbot_plugin_whleague_revenue_system.db.connection import DatabaseManager  # noqa: E402
from astrbot_plugin_whleague_revenue_system.db.dao import StadiumDAO  # noqa: E402
from astrbot_plugin_whleague_revenue_system.db.schema import init_schema  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.backup_service import BackupService  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.chart_service import ChartService  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.tour_sync import TourSyncService  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.weather_service import WeatherService  # noqa: E402


class TestEnv:
    def __init__(self, cfg_override: dict | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_dir = Path(self._tmp.name)
        self.db = DatabaseManager(str(self._tmp_dir / "test.db"))
        self.dao = StadiumDAO(self.db)
        self.cfg = dict(DEFAULT_CONFIG)
        if cfg_override:
            self.cfg.update(cfg_override)

    async def setup(self):
        try:
            await self.db.init()
            await init_schema(self.db)
            self.weather_service = WeatherService(self.dao, self.cfg)
            self.tour_sync_service = TourSyncService(self.dao, self.cfg)
            self.backup_service = BackupService(self.db, self.cfg)
            self.chart_service = ChartService(self.db, self.dao, self.cfg)
        except BaseException:
            # 装配失败也要释放 aiosqlite 连接线程（非 daemon，泄漏会让进程退出挂死）
            await self.db.close()
            self._tmp.cleanup()
            raise
        return self

    async def teardown(self):
        await self.db.close()
        self._tmp.cleanup()


def run_module(name: str) -> int:
    """运行单个测试模块并返回失败数，方便 run_all 汇总。"""
    count = 0
    mod = __import__(name, fromlist=["*"])
    for attr in dir(mod):
        if attr.startswith("test_"):
            fn = getattr(mod, attr)
            if callable(fn):
                try:
                    asyncio.run(fn())
                    print(f"  PASS {name}.{attr}")
                except AssertionError as e:
                    count += 1
                    print(f"  FAIL {name}.{attr}: {e}")
                except Exception as e:
                    count += 1
                    print(f"  ERROR {name}.{attr}: {type(e).__name__}: {e}")
    if count:
        print(f"  -> {name}: {count} failed")
    return count
