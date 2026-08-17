import asyncio
from collections.abc import AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

from .config.defaults import (
    _LIST_KEYS,
    DEFAULT_CONFIG,
    PLUGIN_VERSION,
    parse_group_list,
)
from .db.connection import DatabaseManager
from .db.dao import StadiumDAO
from .db.schema import init_schema
from .utils.rate_limiter import RateLimiter


@register(
    "whleague_revenue_system",
    "WHLofficial",
    "球队主场系统：赛程导入、天气预报、赛果录入、上座/票房统计、球场建设、球迷演化、档期活动、冠名权、随机事件",
    PLUGIN_VERSION,
)
class StadiumPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config
        """AstrBot 托管的插件配置（WebUI 中可见、可修改），见 _conf_schema.json。"""

    async def initialize(self) -> None:
        self.db = DatabaseManager()
        await self.db.init()

        self.dao = StadiumDAO(self.db)

        await init_schema(self.db)

        self.config_cache = await self._load_config_cache()
        self.rate_limiter = RateLimiter()

        from .services.backup_service import BackupService
        from .services.brand_service import BrandService
        from .services.chart_service import ChartService
        from .services.event_engine import EventEngine
        from .services.fans_service import FansService
        from .services.fixture_service import FixtureService
        from .services.llm_writer import LlmWriter
        from .services.negotiation_bridge import NegotiationBridge
        from .services.stadium_service import StadiumService
        from .services.window_service import WindowService

        self.bridge = NegotiationBridge(self.config_cache)
        self.stadium_service = StadiumService(self.db, self.dao, self.config_cache, self.bridge)
        self.fans_service = FansService(self.db, self.dao, self.config_cache)
        self.llm_writer = LlmWriter(self.config_cache, get_provider=self._get_provider)
        self.brand_service = BrandService(self.db, self.dao, self.config_cache, self.llm_writer)
        self.event_engine = EventEngine(self.db, self.dao, self.config_cache, self.llm_writer)
        self.fixture_service = FixtureService(
            self.db, self.dao, self.config_cache, self.stadium_service
        )
        self.window_service = WindowService(
            self.db, self.dao, self.config_cache, self.fans_service, self.brand_service
        )
        self.backup_service = BackupService(self.db, self.config_cache)
        self.chart_service = ChartService(self.db, self.dao, self.config_cache)

        await self.brand_service.init_brand_pool()
        await self.event_engine.init_defaults()

        from .handlers.admin import AdminHandler
        from .handlers.player import PlayerHandler

        self.admin_handler = AdminHandler(self)
        self.player_handler = PlayerHandler(self)

        await self._start_cron_jobs()

        logger.info("Stadium revenue system plugin initialized.")

    def _get_provider(self):
        """返回当前 AstrBot 正在使用的 LLM provider（可能为 None）。"""
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    async def _load_config_cache(self) -> dict:
        if self.config is None:
            cache = dict(DEFAULT_CONFIG)
            for key in _LIST_KEYS:
                cache[key] = parse_group_list(cache[key])
            # 无 WebUI 托管配置时，从 plugin_config 表读回已持久化的变更
            rows = await self.dao.get_all_config()
            for row in rows:
                key = row["key"]
                if key not in DEFAULT_CONFIG or key == "schema_version":
                    continue
                try:
                    from .config.defaults import validate_and_cast

                    cache[key] = validate_and_cast(key, row["value"])
                except ValueError:
                    continue
            return cache
        cache = {}
        for key, default in DEFAULT_CONFIG.items():
            val = self.config.get(key, default)
            if key in _LIST_KEYS:
                cache[key] = parse_group_list(val)
            else:
                cache[key] = val
        return cache

    async def _persist_config(self, key: str, value) -> None:
        """持久化配置变更（优先 AstrBot 托管配置，其次数据库表）。"""
        if self.config is not None:
            self.config[key] = value
            self.config.save_config()
        else:
            await self.dao.set_config(key, str(value))

    # ─── gates ──────────────────────────────────────────────

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        group_id = event.get_group_id()
        if not group_id:
            return False
        whitelist = self.config_cache.get("group_whitelist", [])
        if not whitelist:
            return True
        return str(group_id) in [str(g) for g in whitelist]

    # ─── cron ───────────────────────────────────────────────

    async def _start_cron_jobs(self) -> None:
        try:
            cfg = self.config_cache
            if cfg.get("backup_enabled", True):
                backup_time = cfg.get("backup_time", "04:00")
                hour, minute = self._parse_hhmm(backup_time, 4, 0)
                self._backup_job = await self.context.cron_manager.add_basic_job(
                    name="stadium_backup",
                    cron_expression=f"{minute} {hour} * * *",
                    handler=self._cron_backup,
                    description="Daily stadium system backup",
                )
                logger.info(f"Backup cron job scheduled at {backup_time}.")
        except Exception as e:
            logger.warning(f"Failed to schedule cron jobs: {e}")
            self._backup_job = None

    async def _cron_backup(self) -> None:
        try:
            await self.backup_service.run_backup()
        except Exception as e:
            logger.error(f"Scheduled backup failed: {e}")

    @staticmethod
    def _parse_hhmm(value: str, default_hour: int, default_minute: int) -> tuple[int, int]:
        try:
            hour, minute = value.strip().split(":", 1)
            hour, minute = int(hour), int(minute)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except (AttributeError, ValueError, TypeError):
            pass
        return default_hour, default_minute

    async def _remove_cron_jobs(self) -> None:
        cron_mgr = getattr(self.context, "cron_manager", None)
        job = getattr(self, "_backup_job", None)
        if job:
            job_id = getattr(job, "job_id", None)
            if job_id and cron_mgr and hasattr(cron_mgr, "delete_job"):
                try:
                    await cron_mgr.delete_job(job_id)
                except Exception:
                    pass
        self._backup_job = None

    async def reschedule_cron_jobs(self) -> None:
        await self._remove_cron_jobs()
        await self._start_cron_jobs()

    # ═══════════════════════════════════════════════════════
    # Admin commands
    # ═══════════════════════════════════════════════════════

    async def _admin_cmd(self, event, handler, *args):
        if not self._is_group_allowed(event):
            return
        async for r in handler(event, *args):
            yield r

    @filter.command("主场赛程导入")
    async def cmd_import_fixtures(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.import_fixtures):
            yield r

    @filter.command("主场天气")
    async def cmd_forecast_weather(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.forecast_weather):
            yield r

    @filter.command("主场赛果")
    async def cmd_record_results(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.record_results):
            yield r

    @filter.command("主场轮次统计")
    async def cmd_round_stats(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.round_stats):
            yield r

    @filter.command("主场推进窗口")
    async def cmd_advance_window(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.advance_window):
            yield r

    @filter.command("主场推进赛季")
    async def cmd_advance_season(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.advance_season):
            yield r

    @filter.command("主场属性")
    async def cmd_import_attributes(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.import_attributes):
            yield r

    @filter.command("主场属性导入")
    async def cmd_import_attributes_batch(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.import_attributes_batch):
            yield r

    @filter.command("主场设施")
    async def cmd_import_facility(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.import_facility):
            yield r

    @filter.command("主场发放")
    async def cmd_grant_all(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.grant_all):
            yield r

    @filter.command("主场事件")
    async def cmd_trigger_event(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.trigger_event):
            yield r

    @filter.command("主场事件生成")
    async def cmd_generate_events(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.generate_events):
            yield r

    @filter.command("主场事件列表")
    async def cmd_list_events(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.list_events):
            yield r

    @filter.command("主场事件采纳")
    async def cmd_adopt_event(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.adopt_event):
            yield r

    @filter.command("主场事件丢弃")
    async def cmd_discard_event(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.discard_event):
            yield r

    @filter.command("主场事件写")
    async def cmd_add_custom_event(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.add_custom_event):
            yield r

    @filter.command("主场品牌生成")
    async def cmd_generate_brands(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.generate_brands):
            yield r

    @filter.command("主场品牌列表")
    async def cmd_list_brands(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.list_brands):
            yield r

    @filter.command("主场品牌采纳")
    async def cmd_adopt_brand(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.adopt_brand):
            yield r

    @filter.command("主场品牌丢弃")
    async def cmd_discard_brand(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.discard_brand):
            yield r

    @filter.command("主场结算")
    async def cmd_settle_window(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.settle_window):
            yield r

    @filter.command("主场设置")
    async def cmd_set_config(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.set_config):
            yield r

    @filter.command("主场查看配置")
    async def cmd_view_config(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.view_config):
            yield r

    @filter.command("主场添加管理")
    async def cmd_add_admin(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.add_admin):
            yield r

    @filter.command("主场删除管理")
    async def cmd_remove_admin(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.remove_admin):
            yield r

    # ═══════════════════════════════════════════════════════
    # Player commands
    # ═══════════════════════════════════════════════════════

    async def _player_cmd(self, event, handler, *args):
        if not self._is_group_allowed(event):
            return
        async for r in handler(event, *args):
            yield r

    @filter.command("主场")
    async def cmd_my_stadium(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._player_cmd(event, self.player_handler.my_stadium):
            yield r

    @filter.command("主场信息")
    async def cmd_view_stadium(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._player_cmd(event, self.player_handler.view_stadium):
            yield r

    @filter.command("主场赛季统计")
    async def cmd_season_stats(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._player_cmd(event, self.player_handler.season_stats):
            yield r

    @filter.command("球场命名")
    async def cmd_rename(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._player_cmd(event, self.player_handler.rename):
            yield r

    @filter.command("球场活动")
    async def cmd_book_activity(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._player_cmd(event, self.player_handler.book_activity):
            yield r

    @filter.command("冠名")
    async def cmd_sign_naming(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._player_cmd(event, self.player_handler.sign_naming):
            yield r

    @filter.command("退冠名")
    async def cmd_terminate_naming(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._player_cmd(event, self.player_handler.terminate_naming):
            yield r

    @filter.command("主场财务")
    async def cmd_my_finance(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._player_cmd(event, self.player_handler.my_finance):
            yield r

    @filter.command("主场轮次统计图")
    async def cmd_round_chart(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._player_cmd(event, self.player_handler.round_chart):
            yield r

    @filter.command("主场赛季走势图")
    async def cmd_season_chart(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._player_cmd(event, self.player_handler.season_chart):
            yield r

    # ═══════════════════════════════════════════════════════
    # Teardown
    # ═══════════════════════════════════════════════════════

    async def terminate(self) -> None:
        await self._remove_cron_jobs()
        if hasattr(self, "bridge"):
            await self.bridge.close()
        if hasattr(self, "db"):
            await self.db.close()
        logger.info("Stadium revenue system plugin terminated.")