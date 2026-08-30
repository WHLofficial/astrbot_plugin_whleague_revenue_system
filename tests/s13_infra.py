"""基础设施直测：备份快照/清理（含消失文件容错）+ 群调度门禁与文件钩子。"""

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import TestEnv, install_stubs  # noqa: E402,F401  (导入即装桩)

from astrbot_plugin_whleague_revenue_system.main import StadiumPlugin  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.backup_service import (  # noqa: E402
    BackupService,
)

from astrbot.api.message_components import File  # noqa: E402  (桩)


def _plugin(env, whitelist):
    """绕过 __init__ 组装最小插件（门禁测试只需要这几个字段）。"""
    p = StadiumPlugin.__new__(StadiumPlugin)
    p.config_cache = dict(env.cfg)
    p.config_cache["group_whitelist"] = whitelist
    p.dao = env.dao
    p.db = env.db
    p.stadium_service = env.stadium_service
    p.fixture_service = env.fixture_service
    return p


class _GroupEvent:
    def __init__(self, group="123", sender="10001", admin=False, files=()):
        self._group = group
        self._sender = sender
        self._admin = admin
        self._files = list(files)
        self.sent = []

    def get_group_id(self):
        return self._group

    def get_sender_id(self):
        return self._sender

    def is_admin(self):
        return self._admin

    def get_messages(self):
        return list(self._files)

    async def send(self, msg):
        self.sent.append(msg)


def _file_comp(name, path):
    return File(name=name, file=path, url="")


# ─── 备份服务 ─────────────────────────────────────────────


async def test_backup_run_cleanup_and_errors():
    """快照可打开且含业务表；超出保留数清理最旧；未初始化/库缺失拒绝。"""
    env = await TestEnv({"backup_keep_count": 2}).setup()
    try:
        bs = BackupService(env.db, env.cfg)
        r1 = await bs.run_backup()
        assert Path(r1["path"]).exists() and r1["removed"] == 0, r1
        conn = sqlite3.connect(r1["path"])
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        assert "stadium" in tables, tables

        bdir = Path(env.db.db_path).parent / "backup"
        for i in range(3):
            (bdir / f"revenue_fake_{i}.db").write_text("junk")
        r2 = await bs.run_backup()
        assert r2["removed"] == 3, r2
        remaining = sorted(p.name for p in bdir.glob("revenue_*.db"))
        assert len(remaining) == 2, remaining
    finally:
        await env.teardown()


async def test_backup_requires_initialized_db():
    env = await TestEnv().setup()
    try:
        class _DeadDb:
            conn = None
            db_path = "nowhere.db"

        bs = BackupService(_DeadDb(), env.cfg)
        try:
            await bs.run_backup()
        except RuntimeError as e:
            assert "not initialized" in str(e), e
        else:
            raise AssertionError("未初始化的连接应拒绝备份")

        class _NoFileDb:
            conn = object()
            db_path = "nowhere.db"

        bs2 = BackupService(_NoFileDb(), env.cfg)
        try:
            await bs2.run_backup()
        except RuntimeError as e:
            assert "not found" in str(e), e
        else:
            raise AssertionError("库文件不存在应拒绝备份")
    finally:
        await env.teardown()


async def test_backup_cleanup_tolerates_vanished_file():
    """清理时文件被并发删掉（stat 失败）不应拖垮备份。"""
    env = await TestEnv({"backup_keep_count": 1}).setup()
    try:
        bs = BackupService(env.db, env.cfg)
        bdir = Path(env.db.db_path).parent / "backup"
        bdir.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (bdir / f"revenue_old_{i}.db").write_text("junk")

        real_stat = Path.stat
        counts = {}

        def flaky_stat(self):
            key = str(self)
            counts[key] = counts.get(key, 0) + 1
            # is_file() 消耗第 1 次，排序取 mtime 是第 2 次 → 模拟竞态消失
            if "revenue_old_1" in key and counts[key] >= 2:
                raise FileNotFoundError(key)
            return real_stat(self)

        Path.stat = flaky_stat
        try:
            removed = bs._cleanup_oldest()
        finally:
            Path.stat = real_stat
        # old_1 因 stat 失败被跳过；剩余 [old_0, old_2] 只清到 keep=1
        assert removed == 1, removed
        assert (bdir / "revenue_old_1.db").exists()
        assert not (bdir / "revenue_old_0.db").exists()
        assert (bdir / "revenue_old_2.db").exists()
    finally:
        await env.teardown()


# ─── 调度门禁与群文件钩子 ─────────────────────────────────


async def test_is_group_allowed_truth_table():
    env = await TestEnv().setup()
    try:
        p = _plugin(env, [])
        e = _GroupEvent(group="123")
        assert p._is_group_allowed(e) is True   # 空白名单 → 全放行
        p.config_cache["group_whitelist"] = [456]  # int 条目按字符串比较
        assert p._is_group_allowed(e) is False
        assert p._is_group_allowed(_GroupEvent(group="456")) is True
        assert p._is_group_allowed(_GroupEvent(group="")) is False  # 无群号 → 拒绝
    finally:
        await env.teardown()


async def test_cmd_gates_block_outside_whitelist():
    env = await TestEnv().setup()
    try:
        p = _plugin(env, ["888"])

        async def handler(ev):
            yield "passed"

        out = [r async for r in p._admin_cmd(_GroupEvent(group="123"), handler)]
        assert out == [], out
        out = [r async for r in p._player_cmd(_GroupEvent(group="888"), handler)]
        assert out == ["passed"], out
    finally:
        await env.teardown()


async def test_on_group_file_gates_silent():
    """白名单外 / 非管理员 / 文件名不匹配 → 三路均静默。"""
    env = await TestEnv().setup()
    try:
        p = _plugin(env, ["888"])
        e = _GroupEvent(group="123", admin=True,
                        files=[_file_comp("属性_a.csv", "nowhere.csv")])
        await p.on_group_file(e)
        assert e.sent == [], e.sent

        p2 = _plugin(env, [])
        e2 = _GroupEvent(group="123", sender="10001", admin=False,
                         files=[_file_comp("属性_a.csv", "nowhere.csv")])
        await p2.on_group_file(e2)
        assert e2.sent == [], e2.sent

        e3 = _GroupEvent(group="123", admin=True,
                         files=[_file_comp("无关文件.csv", "nowhere.csv")])
        await p2.on_group_file(e3)
        assert e3.sent == [], e3.sent
    finally:
        await env.teardown()


async def test_on_group_file_attribute_e2e():
    """管理员发属性文件 → 存进 imports 目录 → 自动导入 → 回执含导入汇总。"""
    env = await TestEnv().setup()
    try:
        p = _plugin(env, [])
        src = Path(env.db.db_path).parent / "src_属性.csv"
        src.write_text("队名,影响力,容量,等级\n利物浦,150,12000,0\n",
                       encoding="utf-8-sig")
        e = _GroupEvent(group="123", admin=True,
                        files=[_file_comp("属性_测试1.csv", str(src))])
        await p.on_group_file(e)
        assert len(e.sent) == 1, e.sent
        reply = e.sent[0]
        assert "已导入属性 1 队" in reply, reply
        assert "✅ 利物浦" in reply, reply
        assert (Path(env.db.db_path).parent / "imports" / "属性_测试1.csv").exists()
    finally:
        await env.teardown()
