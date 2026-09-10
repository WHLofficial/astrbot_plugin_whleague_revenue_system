"""AstrBot 4.x 框架适配回归：provider 取用（同步/异步）+ 版本与元数据一致性。

不依赖 PyYAML：metadata.yaml 按行解析，保持零依赖测试。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _metadata_fields() -> dict:
    fields: dict[str, str] = {}
    with open(os.path.join(REPO_ROOT, "metadata.yaml"), encoding="utf-8") as f:
        for line in f:
            m = re.match(r'\s*(version|astrbot_version):\s*"?([^"#]+?)"?\s*(?:#.*)?$', line)
            if m:
                fields[m.group(1)] = m.group(2).strip()
    return fields


class _AsyncCtx:
    """仅暴露 4.2x 的异步取 provider 接口。"""

    def __init__(self, provider):
        self._provider = provider

    async def get_using_provider_async(self):
        return self._provider


class _SyncCtx:
    """旧版 4.x：只有同步取 provider 接口。"""

    def __init__(self, provider):
        self._provider = provider

    def get_using_provider(self):
        return self._provider


class _AsyncGetterProvider:
    async def text_chat(self, prompt: str, session_id: str = ""):
        return type("R", (), {"completion_text": "异步取用的文案"})()


async def test_get_provider_prefers_async():
    """_get_provider 走异步接口（4.2x 推荐），并正确解包 awaitable。"""
    from astrbot_plugin_whleague_revenue_system.main import StadiumPlugin

    sentinel = object()
    p = StadiumPlugin.__new__(StadiumPlugin)
    p.context = _AsyncCtx(sentinel)
    assert await p._get_provider() is sentinel


async def test_get_provider_sync_fallback():
    """旧版 4.x 无异步接口时回退同步实现。"""
    from astrbot_plugin_whleague_revenue_system.main import StadiumPlugin

    sentinel = object()
    p = StadiumPlugin.__new__(StadiumPlugin)
    p.context = _SyncCtx(sentinel)
    assert await p._get_provider() is sentinel


async def test_get_provider_swallows_errors():
    """接口抛异常时返回 None（LLM 回退路径不中断结算）。"""
    from astrbot_plugin_whleague_revenue_system.main import StadiumPlugin

    class _Boom:
        async def get_using_provider_async(self):
            raise RuntimeError("no provider")

    p = StadiumPlugin.__new__(StadiumPlugin)
    p.context = _Boom()
    assert await p._get_provider() is None


async def test_llm_writer_accepts_async_getter():
    """LlmWriter 同时接受同步/异步 getter（框架切异步后无需改调用方）。"""
    env = await TestEnv().setup()
    try:
        provider = _AsyncGetterProvider()
        env.llm_writer._get_provider = lambda: provider  # 同步可调用返回 awaitable
        text = await env.llm_writer._ask("任意 prompt")
        assert text == "异步取用的文案", text

        async def _aget():
            return provider

        env.llm_writer._get_provider = _aget  # 异步可调用
        text = await env.llm_writer._ask("任意 prompt")
        assert text == "异步取用的文案", text
    finally:
        await env.teardown()


async def test_plugin_version_matches_metadata():
    """config.PLUGIN_VERSION 必须与 metadata.yaml 的 version 一致（防漂移）。"""
    from astrbot_plugin_whleague_revenue_system.config.defaults import PLUGIN_VERSION

    meta = _metadata_fields()
    assert "version" in meta, "metadata.yaml 缺少 version"
    assert PLUGIN_VERSION == meta["version"].lstrip("v"), (
        f"版本漂移：PLUGIN_VERSION={PLUGIN_VERSION!r} vs metadata={meta['version']!r}"
    )


async def test_metadata_astrbot_version_bounds():
    """astrbot_version 应为带 <5 上限的 PEP 440 区间（4.28.0 已验证）。"""
    meta = _metadata_fields()
    spec = meta.get("astrbot_version", "")
    assert spec.startswith(">=4"), spec
    assert "<5" in spec, f"应为 5.x 设上限避免误装：{spec!r}"


async def test_no_deprecated_register_decorator():
    """移除已废弃的 @register_star，改由 Star.__init_subclass__ 自动登记。"""
    with open(os.path.join(REPO_ROOT, "main.py"), encoding="utf-8") as f:
        src = f.read()
    assert "@register(" not in src, "main.py 仍使用已废弃的 @register 装饰器"
    assert ", register" not in src, "main.py 仍从 astrbot.api.star 导入 register"
