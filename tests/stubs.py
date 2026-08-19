"""astrbot.api 桩：测试环境无 AstrBot 运行时，替换 logger/event/star 依赖。

安全说明：本模块仅存在于 tests/ 目录，不随插件运行加载。
"""

import sys
import types


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _Star:
    def __init__(self, context=None):
        self.context = context


def install_stubs():
    if "astrbot" in sys.modules:
        return
    astrbot_pkg = types.ModuleType("astrbot")
    astrbot_pkg.__path__ = []
    api_pkg = types.ModuleType("astrbot.api")
    api_pkg.logger = _Logger()

    event_pkg = types.ModuleType("astrbot.api.event")
    event_pkg.MessageEventResult = types.SimpleNamespace
    event_pkg.AstrMessageEvent = object

    class _MessageChain:
        """桩：MessageChain().message(text) 返回 text（群文件钩子回复用）。"""

        @staticmethod
        def message(text):
            return text

    event_pkg.MessageChain = _MessageChain

    filter_mod = types.ModuleType("astrbot.api.event.filter")
    filter_mod.regex = lambda *a, **k: (lambda fn: fn)
    filter_mod.command = lambda *a, **k: (lambda fn: fn)
    filter_mod.event_message_type = lambda *a, **k: (lambda fn: fn)
    filter_mod.EventMessageType = types.SimpleNamespace(GROUP_MESSAGE="group_message")
    event_pkg.filter = filter_mod
    sys.modules["astrbot.api.event.filter"] = filter_mod

    star_pkg = types.ModuleType("astrbot.api.star")
    star_pkg.Context = object
    star_pkg.Star = _Star
    star_pkg.register = lambda *a, **k: (lambda cls: cls)
    sys.modules["astrbot.api.star"] = star_pkg

    mc_pkg = types.ModuleType("astrbot.api.message_components")

    class _File:
        """文件消息段桩：File(name, file=本地路径, url=...) + async get_file()。"""

        def __init__(self, name: str = "", file: str = "", url: str = ""):
            self.name = name
            self.file_ = file
            self.url = url

        async def get_file(self, allow_return_url: bool = False) -> str:
            return self.file_ or ""

    mc_pkg.File = _File
    mc_pkg.Plain = object
    sys.modules["astrbot.api.message_components"] = mc_pkg

    sys.modules["astrbot"] = astrbot_pkg
    sys.modules["astrbot.api"] = api_pkg
    sys.modules["astrbot.api.event"] = event_pkg