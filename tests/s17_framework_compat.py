"""AstrBot 4.x 框架适配回归：main 可导入、命令注册数、版本与元数据一致性。

不依赖 PyYAML：metadata.yaml 按行解析，保持零依赖测试。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _metadata_fields() -> dict:
    fields: dict[str, str] = {}
    with open(os.path.join(REPO_ROOT, "metadata.yaml"), encoding="utf-8") as f:
        for line in f:
            m = re.match(r'\s*(version|astrbot_version):\s*"?([^"#]+?)"?\s*(?:#.*)?$', line)
            if m:
                fields[m.group(1)] = m.group(2).strip()
    return fields


async def test_main_imports_clean():
    """install_stubs 后 import main 成功（v3 收敛后无失效 import）。"""
    import tests.common  # noqa: F401

    from astrbot_plugin_whleague_revenue_system import main  # noqa: F401


async def test_main_registers_exactly_8_commands():
    """main.py 恰好注册 8 条 @filter.command（v3 命令面收敛）。"""
    with open(os.path.join(REPO_ROOT, "main.py"), encoding="utf-8") as f:
        src = f.read()
    commands = re.findall(r'@filter\.command\("([^"]+)"', src)
    assert len(commands) == 8, commands
    assert set(commands) == {
        "主场赛程同步", "主场天气", "主场天气覆盖", "主场天气推送",
        "主场轮次预告图", "主场轮次统计图", "主场赛季走势图", "主场帮助",
    }, commands


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
