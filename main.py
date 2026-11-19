"""WHL 展示插件：赛程/赛果同步、天气、三张图、帮助。

只保留 8 条命令、4 个服务（TourSync/Weather/Backup/Chart）；
赛程/赛果以 tour 为唯一真源，赛季由 club 当前赛季驱动，上座/球场从 club 只读。
"""

from collections.abc import AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star

from .config.defaults import (
    _LIST_KEYS,
    DEFAULT_CONFIG,
    parse_group_list,
)
from .db.connection import DatabaseManager
from .db.dao import StadiumDAO
from .db.schema import init_schema

# v3 保留的 8 条命令（命令处理器在 handlers/，此处只做门禁转发）
_ADMIN_COMMANDS = ("主场赛程同步", "主场天气", "主场天气覆盖", "主场天气推送")
_PLAYER_COMMANDS = ("主场轮次预告图", "主场轮次统计图", "主场赛季走势图", "主场帮助")


class StadiumPlugin(Star):
    """WHL 展示插件：赛程同步、天气、三张图、帮助。

    插件名/作者/版本以 metadata.yaml 为准（AstrBot 4.x 由 Star.__init_subclass__ 自动登记）。
    """

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

        from .services.backup_service import BackupService
        from .services.chart_service import ChartService
        from .services.tour_sync import TourSyncService
        from .services.weather_service import WeatherService

        self.weather_service = WeatherService(self.dao, self.config_cache)
        self.tour_sync_service = TourSyncService(self.dao, self.config_cache)
        self.backup_service = BackupService(self.db, self.config_cache)
        self.chart_service = ChartService(self.db, self.dao, self.config_cache)

        from .handlers.admin import AdminHandler
        from .handlers.player import PlayerHandler

        self.admin_handler = AdminHandler(self)
        self.player_handler = PlayerHandler(self)

        await self._start_cron_jobs()

        logger.info("WHL display plugin initialized (8 commands, 4 services).")

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

    # ─── gates ──────────────────────────────────────────────

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        group_id = event.get_group_id()
        if not group_id:
            return False
        whitelist = self.config_cache.get("group_whitelist", [])
        if not whitelist:
            return True
        return str(group_id) in [str(g) for g in whitelist]

    # ─── cron（每日备份） ───────────────────────────────────

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
    # 命令门禁转发（8 条）
    # ═══════════════════════════════════════════════════════

    async def _admin_cmd(self, event, handler, *args):
        if not self._is_group_allowed(event):
            return
        async for r in handler(event, *args):
            yield r

    async def _player_cmd(self, event, handler, *args):
        if not self._is_group_allowed(event):
            return
        async for r in handler(event, *args):
            yield r

    @filter.command("主场赛程同步")
    async def cmd_sync_tour_times(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.sync_fixture_times):
            yield r

    @filter.command("主场天气")
    async def cmd_forecast_weather(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.forecast_weather):
            yield r

    @filter.command("主场天气覆盖")
    async def cmd_weather_override(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.weather_override):
            yield r

    @filter.command("主场天气推送")
    async def cmd_weather_push(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._admin_cmd(event, self.admin_handler.weather_push):
            yield r

    @filter.command("主场轮次预告图")
    async def cmd_preview_chart(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._player_cmd(event, self.player_handler.preview_chart):
            yield r

    @filter.command("主场轮次统计图")
    async def cmd_round_chart(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._player_cmd(event, self.player_handler.round_chart):
            yield r

    @filter.command("主场赛季走势图")
    async def cmd_season_chart(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._player_cmd(event, self.player_handler.season_chart):
            yield r

    @filter.command("主场帮助")
    async def cmd_help(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        async for r in self._player_cmd(event, self.player_handler.help):
            yield r

    # ═══════════════════════════════════════════════════════
    # Teardown
    # ═══════════════════════════════════════════════════════

    async def terminate(self) -> None:
        await self._remove_cron_jobs()
        if hasattr(self, "db"):
            await self.db.close()
        logger.info("WHL display plugin terminated.")
