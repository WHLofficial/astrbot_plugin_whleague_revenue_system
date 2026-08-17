"""测试公共环境：临时数据库 + 服务装配 + 伪造谈判库。

以插件包全名（astrbot_plugin_whleague_revenue_system.*）绝对导入，
需将 PLUGINS_DIR 加入 sys.path（与谈判系统测试一致）。
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
from astrbot_plugin_whleague_revenue_system.services.brand_service import BrandService  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.event_engine import EventEngine  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.fans_service import FansService  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.fixture_service import FixtureService  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.llm_writer import LlmWriter  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.negotiation_bridge import NegotiationBridge  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.stadium_service import StadiumService  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.window_service import WindowService  # noqa: E402


class FakeProvider:
    """LLM 桩：text_chat 返回预设 JSON（测试事件/品牌设计）。"""

    def __init__(self, response: str | None = None):
        self._response = response
        self.calls = 0

    def set_response(self, response: str | None):
        self._response = response

    async def text_chat(self, prompt: str, session_id: str = ""):
        self.calls += 1
        if self._response is None:
            raise RuntimeError("provider unavailable")
        return type("R", (), {"result_str": self._response})()


TEAMS = ["利物浦", "巴塞罗那", "纽卡斯尔联", "勒沃库森"]


async def make_fake_negotiation_db(tmp: Path, teams: list[str] | None = None) -> str:
    """伪造谈判库（teams + team_bindings 表），返回 db 路径。"""
    import aiosqlite

    db_path = str(tmp / "negotiation_system.db")
    conn = await aiosqlite.connect(db_path)
    await conn.execute("CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT UNIQUE)")
    await conn.execute(
        "CREATE TABLE team_bindings (id INTEGER PRIMARY KEY, team_id INTEGER, qq TEXT UNIQUE)"
    )
    for i, name in enumerate(teams or TEAMS, start=1):
        await conn.execute("INSERT INTO teams (id, name) VALUES (?, ?)", (i, name))
    await conn.execute(
        "INSERT INTO team_bindings (team_id, qq) VALUES (1, '10001'), (2, '10002')"
    )
    await conn.commit()
    await conn.close()
    return db_path


class TestEnv:
    def __init__(self, cfg_override: dict | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_dir = Path(self._tmp.name)
        self.db = DatabaseManager(str(self._tmp_dir / "test.db"))
        self.dao = StadiumDAO(self.db)
        self.cfg = dict(DEFAULT_CONFIG)
        if cfg_override:
            self.cfg.update(cfg_override)
        self.provider = FakeProvider()

    async def setup(self, with_negotiation: bool = True):
        await self.db.init()
        await init_schema(self.db)
        if with_negotiation:
            self.cfg["negotiation_db_path"] = await make_fake_negotiation_db(self._tmp_dir)
        self.bridge = NegotiationBridge(self.cfg)
        self.stadium_service = StadiumService(self.db, self.dao, self.cfg, self.bridge)
        self.fans_service = FansService(self.db, self.dao, self.cfg)
        self.llm_writer = LlmWriter(self.cfg, get_provider=lambda: self.provider)
        self.brand_service = BrandService(self.db, self.dao, self.cfg, self.llm_writer)
        self.event_engine = EventEngine(self.db, self.dao, self.cfg, self.llm_writer)
        self.fixture_service = FixtureService(self.db, self.dao, self.cfg, self.stadium_service)
        self.window_service = WindowService(
            self.db, self.dao, self.cfg, self.fans_service, self.brand_service,
            self.event_engine,
        )
        await self.brand_service.init_brand_pool()
        await self.event_engine.init_defaults()
        return self

    async def teardown(self):
        await self.bridge.close()
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