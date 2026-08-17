"""文件导入：CSV/XLSX 解析（表头/编码/报错）、三种类型端到端、附件命令路径、临时文件清理。"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 先导入 tests.common 以安装 astrbot 桩，再导入 astrbot 组件
from tests.common import TestEnv  # noqa: E402
from astrbot.api.message_components import File  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services import file_import_service as fis  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.file_import_service import (  # noqa: E402
    FileImportError,
)


def make_xlsx(path, rows):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    wb.save(path)
    wb.close()


class _FakeEvent:
    def __init__(self, message: str, sender: str = "10001", is_admin: bool = False,
                 messages=None):
        self.message = message
        self.sender = sender
        self.admin = is_admin
        self.messages = messages or []

    def get_message_str(self):
        return self.message

    def get_sender_id(self):
        return self.sender

    def get_sender_name(self):
        return "tester"

    def get_group_id(self):
        return "99999"

    def get_messages(self):
        return self.messages

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        self.results.append(text)
        return text


_FakeEvent.results = []


def _make_admin_handler(env):
    from astrbot_plugin_whleague_revenue_system.handlers.admin import AdminHandler

    return AdminHandler(type("P", (), {
        "dao": env.dao,
        "config_cache": env.cfg,
        "fixture_service": env.fixture_service,
        "stadium_service": env.stadium_service,
        "event_engine": env.event_engine,
        "brand_service": env.brand_service,
        "window_service": env.window_service,
        "bridge": env.bridge,
        "_persist_config": lambda k, v: None,
    })())


# ─── 解析层 ───────────────────────────────────────────────

async def test_fixture_csv_utf8_with_header():
    rows = [["轮次", "主队", "客队"], ["1", "利物浦", "巴塞罗那"], ["2", "纽卡斯尔联", "勒沃库森"]]
    text = "\n".join(",".join(r) for r in rows)
    p = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_fix_utf8.csv"))
    p.write_bytes(text.encode("utf-8"))
    try:
        out = fis.read_rows(p)
        lines, errors = fis.build_fixture_lines(out)
        assert errors == [], errors
        assert lines == ["1 利物浦 巴塞罗那", "2 纽卡斯尔联 勒沃库森"], lines
    finally:
        p.unlink(missing_ok=True)


async def test_fixture_csv_gbk_no_header():
    p = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_fix_gbk.csv"))
    p.write_bytes("1,利物浦,巴塞罗那\n2,纽卡斯尔联,勒沃库森\n".encode("gbk"))
    try:
        out = fis.read_rows(p)
        lines, errors = fis.build_fixture_lines(out)
        assert errors == [], errors
        assert lines == ["1 利物浦 巴塞罗那", "2 纽卡斯尔联 勒沃库森"], lines
        assert fis._read_csv(p)[0] == ["1", "利物浦", "巴塞罗那"]
    finally:
        p.unlink(missing_ok=True)


async def test_fixture_csv_tsv():
    p = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_fix_tsv.csv"))
    p.write_bytes("轮次\t主队\t客队\n1\t利物浦\t巴塞罗那\n".encode("utf-8-sig"))
    try:
        out = fis.read_rows(p)
        lines, errors = fis.build_fixture_lines(out)
        assert errors == [], errors
        assert lines == ["1 利物浦 巴塞罗那"], lines
    finally:
        p.unlink(missing_ok=True)


async def test_fixture_file_competition_token():
    env = await TestEnv().setup()
    try:
        p = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_fix_comp.csv"))
        p.write_text("轮次,主队,客队\n顶级9,利物浦,巴塞罗那\n次级9,纽卡斯尔联,勒沃库森\n",
                     encoding="utf-8")
        try:
            result = await env.fixture_service.import_fixtures_file(str(p))
            assert result["imported"] == 2, result
            assert result["file_errors"] == [], result
            top = await env.dao.get_round_matches(1, 1, 9, "顶级联赛")
            sub = await env.dao.get_round_matches(1, 1, 9, "次级联赛")
            assert len(top) == 1 and top[0]["competition"] == "顶级联赛"
            assert len(sub) == 1 and sub[0]["competition"] == "次级联赛"
        finally:
            p.unlink(missing_ok=True)
    finally:
        await env.teardown()


async def test_xlsx_header_columns():
    base = os.path.dirname(os.path.abspath(__file__))

    p = Path(os.path.join(base, "tmp_fix.xlsx"))
    make_xlsx(p, [["轮次", "客队", "主队"], [1, "巴塞罗那", "利物浦"]])
    try:
        rows = fis.read_rows(p)
        lines, errors = fis.build_fixture_lines(rows)
        assert errors == [], errors
        assert lines == ["1 利物浦 巴塞罗那"], lines
    finally:
        p.unlink(missing_ok=True)

    p2 = Path(os.path.join(base, "tmp_res.xlsx"))
    make_xlsx(p2, [["主队", "赛果"], ["利物浦", "胜"], ["纽卡斯尔联", "平"]])
    try:
        rows = fis.read_rows(p2)
        lines, errors = fis.build_result_lines(rows)
        assert errors == [], errors
        assert lines == ["利物浦 W", "纽卡斯尔联 D"], lines
    finally:
        p2.unlink(missing_ok=True)


async def test_result_lines_errors_and_skip():
    rows = [["利物浦", "胜"], ["巴塞罗那", "平"], ["纽卡斯尔联", "赢"], ["", ""]]
    lines, errors = fis.build_result_lines(rows)
    assert lines == ["利物浦 W", "巴塞罗那 D"], lines
    assert len(errors) == 1 and "赛果需为" in errors[0], errors


async def test_attribute_rows_errors_and_skip():
    rows = [["队名", "影响力", "容量", "等级"], ["利物浦", 150, 12000, 0], ["巴塞罗那", "abc", 20000, 1]]
    records, errors = fis.build_attribute_rows(rows)
    assert len(records) == 1 and records[0][0] == "利物浦", records
    assert records[0][1] == 150 and records[0][2] == 12000 and records[0][3] == 0
    assert len(errors) == 1 and "格式错误" in errors[0], errors


async def test_unsupported_and_oversize():
    p = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_bad.txt"))
    p.write_text("hello")
    try:
        try:
            fis.read_rows(p)
            assert False, "应拒绝非 csv/xlsx"
        except FileImportError as e:
            assert "仅支持" in str(e)
    finally:
        p.unlink(missing_ok=True)

    p2 = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_tiny.csv"))
    p2.write_text("1,利物浦,巴塞罗那")
    try:
        try:
            fis.read_rows(p2, max_size_mb=0.000001)
            assert False, "应拒绝超限文件"
        except FileImportError as e:
            assert "大小上限" in str(e)
    finally:
        p2.unlink(missing_ok=True)


async def test_cleanup_file():
    p = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_cleanup.csv"))
    p.write_text("x")
    fis.cleanup_file(str(p))
    assert not p.exists()
    fis.cleanup_file(str(p))  # 不存在时静默
    fis.cleanup_file("")


# ─── 端到端（服务层） ─────────────────────────────────────

async def test_fixture_file_e2e():
    env = await TestEnv().setup()
    try:
        p = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_e2e_fix.xlsx"))
        make_xlsx(p, [["轮次", "主队", "客队"],
                      [1, "利物浦", "巴塞罗那"],
                      [1, "纽卡斯尔联", "勒沃库森"],
                      [2, "利物浦", "纽卡斯尔联"]])
        try:
            result = await env.fixture_service.import_fixtures_file(str(p))
            assert result["imported"] == 3, result
            assert result["file_errors"] == [], result
            matches = await env.dao.get_round_matches(1, 1, 2)
            assert len(matches) == 1 and matches[0]["home_team"] == "利物浦"
            # 未知球队行：跳过并报错
            p2 = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_e2e_fix2.csv"))
            p2.write_text("轮次,主队,客队\n1,利物浦,不存在队\n", encoding="utf-8")
            try:
                result2 = await env.fixture_service.import_fixtures_file(str(p2))
                assert result2["imported"] == 0 and result2["skipped"] == 1, result2
                assert any("未知球队" in e for e in result2["errors"])
            finally:
                p2.unlink(missing_ok=True)
        finally:
            p.unlink(missing_ok=True)
    finally:
        await env.teardown()


async def test_results_file_e2e():
    env = await TestEnv().setup()
    try:
        p = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_e2e_res.xlsx"))
        make_xlsx(p, [["主队", "赛果"], ["利物浦", "胜"], ["纽卡斯尔联", "负"]])
        try:
            await env.fixture_service.import_fixtures(
                "1 利物浦 巴塞罗那\n1 纽卡斯尔联 勒沃库森"
            )
            for home in ("利物浦", "纽卡斯尔联"):
                await env.fixture_service.set_weather(1, home, "晴")
            result = await env.fixture_service.record_results_file(1, str(p))
            assert result["count"] == 2, result
            for detail in result["results"]:
                assert detail["attendance"] > 0 and detail["ticket"] > 0, detail
            matches = await env.dao.get_round_matches(1, 1, 1)
            for m in matches:
                assert m["attendance"] is not None
            txs = await env.dao.list_transactions("利物浦", season=1, window_seq=1)
            assert any(t["kind"] == "ticket" for t in txs), txs
        finally:
            p.unlink(missing_ok=True)
    finally:
        await env.teardown()


async def test_attributes_file_e2e():
    env = await TestEnv().setup()
    try:
        p = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_e2e_attr.csv"))
        p.write_bytes(
            "队名,影响力,容量,等级\n利物浦,150,12000,0\n巴塞罗那,120,25000,1\n"
            "纽卡斯尔联,130,30000,2\n".encode("utf-8-sig")
        )
        try:
            result = await env.stadium_service.import_attributes_file(str(p))
            assert result["imported"] == 2, result
            by_team = {r["team"]: r for r in result["results"]}
            assert by_team["利物浦"]["ok"]
            assert not by_team["纽卡斯尔联"]["ok"], "max_open_tier=1 应拒绝 2 级"
            assert "未开放" in by_team["纽卡斯尔联"]["error"]
            stadium = await env.dao.get_stadium("利物浦")
            assert abs(stadium["influence"] - 150.0) < 1e-6
            stadium_b = await env.dao.get_stadium("巴塞罗那")
            assert stadium_b["tier"] == 1 and stadium_b["capacity"] == 25000
            assert result["errors"] == [], result
        finally:
            p.unlink(missing_ok=True)
    finally:
        await env.teardown()


# ─── 命令层（附件路径） ──────────────────────────────────

async def test_handler_attachment_and_cleanup():
    env = await TestEnv().setup()
    try:
        handler = _make_admin_handler(env)
        p = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_attach.csv"))
        p.write_text("轮次,主队,客队\n1,利物浦,巴塞罗那\n2,利物浦,纽卡斯尔联\n", encoding="utf-8")

        _FakeEvent.results = []
        event = _FakeEvent("/主场赛程导入", sender="10001", is_admin=True,
                           messages=[File(name="fix.csv", file=str(p))])
        results = [r async for r in handler.import_fixtures(event)]
        assert results and "已导入" in results[0], results
        assert "2 场" in results[0], results
        assert not p.exists(), "临时附件应被清理"
        matches = await env.dao.get_round_matches(1, 1, 1)
        assert len(matches) == 1

        # 文本回退路径仍可用
        _FakeEvent.results = []
        event2 = _FakeEvent("/主场赛程导入\n1 巴塞罗那 勒沃库森", sender="10001", is_admin=True)
        results2 = [r async for r in handler.import_fixtures(event2)]
        assert results2 and "已导入" in results2[0], results2
        matches2 = await env.dao.get_round_matches(1, 1, 1)
        assert len(matches2) == 2
    finally:
        await env.teardown()


async def test_handler_attachment_results_and_attributes():
    env = await TestEnv().setup()
    try:
        handler = _make_admin_handler(env)

        res_path = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_attach_res.csv"))
        res_path.write_text("主队,赛果\n利物浦,胜\n", encoding="utf-8")
        await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那")
        await env.fixture_service.set_weather(1, "利物浦", "晴")
        try:
            _FakeEvent.results = []
            event = _FakeEvent("/主场赛果 1", sender="10001", is_admin=True,
                               messages=[File(name="res.csv", file=str(res_path))])
            results = [r async for r in handler.record_results(event)]
            assert results and "已录入" in results[0], results
            assert not res_path.exists()
        finally:
            res_path.unlink(missing_ok=True)

        attr_path = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_attach_attr.csv"))
        attr_path.write_text("队名,影响力,容量,等级\n利物浦,160,12000,0\n", encoding="utf-8")
        try:
            _FakeEvent.results = []
            event2 = _FakeEvent("/主场属性导入", sender="10001", is_admin=True,
                                messages=[File(name="attr.csv", file=str(attr_path))])
            results2 = [r async for r in handler.import_attributes_batch(event2)]
            assert results2 and "文件导入属性" in results2[0], results2
            assert not attr_path.exists()
        finally:
            attr_path.unlink(missing_ok=True)
    finally:
        await env.teardown()


async def test_no_attachment_usage_hint():
    env = await TestEnv().setup()
    try:
        handler = _make_admin_handler(env)
        _FakeEvent.results = []
        event = _FakeEvent("/主场赛程导入", sender="10001", is_admin=True, messages=[])
        results = [r async for r in handler.import_fixtures(event)]
        assert results and "用法" in results[0], results
    finally:
        await env.teardown()