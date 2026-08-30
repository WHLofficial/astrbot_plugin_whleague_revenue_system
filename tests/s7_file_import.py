"""文件导入：CSV/XLSX 解析（表头/编码/报错）、三种类型端到端、imports 目录 by-name 命令、群文件钩子自动导入、文件名判型。"""

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
        self.results = []

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
            # 顶级9/次级9 是各自赛事首个轮次文本 → 各赛事内第 1 轮
            comp_top, rn_top = await env.fixture_service.resolve_round_arg("顶级9")
            comp_sub, rn_sub = await env.fixture_service.resolve_round_arg("次级9")
            assert (comp_top, rn_top) == ("顶级联赛", 1)
            assert (comp_sub, rn_sub) == ("次级联赛", 1)
            top = await env.dao.get_round_matches(1, 1, rn_top, comp_top)
            sub = await env.dao.get_round_matches(1, 1, rn_sub, comp_sub)
            assert len(top) == 1 and top[0]["competition"] == "顶级联赛"
            assert len(sub) == 1 and sub[0]["competition"] == "次级联赛"
        finally:
            p.unlink(missing_ok=True)
    finally:
        await env.teardown()


async def test_build_fixture_lines_text_round_passthrough():
    """轮次列为纯文字（无数字）不再拒绝，原样透传由服务层登记改写。"""
    rows = [["轮次", "主队", "客队"],
            ["顶级", "利物浦", "巴塞罗那"],
            ["次级", "纽卡斯尔联", "勒沃库森"]]
    lines, errors = fis.build_fixture_lines(rows)
    assert errors == [], errors
    assert lines == ["顶级 利物浦 巴塞罗那", "次级 纽卡斯尔联 勒沃库森"], lines


async def test_fixture_file_named_round_no_digit():
    """文件导入纯文字轮次：同名同一轮、命令侧恒同号、不同名递增。"""
    env = await TestEnv().setup()
    try:
        p = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_fix_named.csv"))
        p.write_text("轮次,主队,客队\n顶级,利物浦,巴塞罗那\n顶级,纽卡斯尔联,勒沃库森\n",
                     encoding="utf-8")
        try:
            result = await env.fixture_service.import_fixtures_file(str(p))
            assert result["imported"] == 2, result
            assert result["file_errors"] == [], result
            matches = await env.dao.get_window_matches(1, 1)
            assert {m["round_no"] for m in matches} == {1}
            assert {m["competition"] for m in matches} == {"顶级联赛"}
            assert await env.fixture_service.resolve_round_arg("顶级") == ("顶级联赛", 1)
            # 未登记的命名轮次（命令侧只读）应报错而非自动建号
            try:
                await env.fixture_service.resolve_round_arg("次级")
                assert False, "未登记的命名轮次应报错"
            except ValueError as e:
                assert "尚未导入" in str(e)
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


async def test_result_lines_score_derived():
    # 赛果列留空、由比分列推导；赛果列直接写比分亦推导
    rows = [["主队", "比分"], ["利物浦", "2-1"], ["纽卡斯尔联", "0-0PK2-4"]]
    lines, errors = fis.build_result_lines(rows)
    assert errors == [], errors
    assert lines == ["利物浦 W 2-1", "纽卡斯尔联 L 0-0PK2-4"], lines
    rows2 = [["主队", "赛果"], ["巴塞罗那", "1-1"]]
    lines2, errors2 = fis.build_result_lines(rows2)
    assert errors2 == [], errors2
    assert lines2 == ["巴塞罗那 D 1-1"], lines2
    # 非比分也不能静默通过
    rows3 = [["主队", "赛果"], ["利物浦", "大胜"]]
    _, errors3 = fis.build_result_lines(rows3)
    assert len(errors3) == 1 and "赛果需为" in errors3[0], errors3


async def test_result_lines_cancelled():
    """文件赛果列写「取消」/「C」→ 归一化为 C。"""
    rows = [["主队", "赛果"], ["利物浦", "取消"], ["巴塞罗那", "C"], ["纽卡斯尔联", "cancel"]]
    lines, errors = fis.build_result_lines(rows)
    assert errors == [], errors
    assert lines == ["利物浦 C", "巴塞罗那 C", "纽卡斯尔联 C"], lines


async def test_results_file_score_derived_e2e():
    """文件录入赛果：赛果列留空用比分推导、赛果列写比分也推导。"""
    env = await TestEnv().setup()
    try:
        await env.fixture_service.import_fixtures(
            "1 利物浦 巴塞罗那\n1 纽卡斯尔联 勒沃库森"
        )
        p = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_res_score.csv"))
        p.write_text("主队,赛果,比分\n利物浦,,2-1\n纽卡斯尔联,0-0PK2-4,\n", encoding="utf-8")
        try:
            result = await env.fixture_service.record_results_file(1, str(p))
            assert result["count"] == 2, result
            assert result["file_errors"] == [], result
            by = {m["home_team"]: m for m in await env.dao.get_round_matches(1, 1, 1)}
            assert by["利物浦"]["result"] == "W" and by["利物浦"]["score"] == "2-1"
            assert by["纽卡斯尔联"]["result"] == "L" and by["纽卡斯尔联"]["score"] == "0-0PK2-4"
        finally:
            p.unlink(missing_ok=True)
    finally:
        await env.teardown()


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


async def test_fixture_file_schedule_and_result_score():
    env = await TestEnv().setup()
    try:
        p = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_fix_sched.csv"))
        p.write_text("轮次,主队,客队,周,天,时间\n1,利物浦,巴塞罗那,W12,D6,15:00\n",
                     encoding="utf-8")
        try:
            result = await env.fixture_service.import_fixtures_file(str(p))
            assert result["imported"] == 1, result
            assert result["file_errors"] == [], result
            m = (await env.dao.get_round_matches(1, 1, 1))[0]
            assert m["week_no"] == 12 and m["day_no"] == 6 and m["match_time"] == "15:00"
        finally:
            p.unlink(missing_ok=True)

        await env.fixture_service.set_weather(1, "利物浦", "晴")
        p2 = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_res_score.csv"))
        p2.write_text("主队,赛果,比分\n利物浦,胜,2-1\n", encoding="utf-8")
        try:
            r2 = await env.fixture_service.record_results_file(1, str(p2))
            assert r2["count"] == 1 and r2["results"][0]["score"] == "2-1", r2
        finally:
            p2.unlink(missing_ok=True)
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


# ─── 文件名判型 / imports 目录 ───────────────────────────

async def test_parse_import_name():
    assert fis.parse_import_name("赛程_第一周.csv") == ("fixture", None)
    assert fis.parse_import_name("schedule_w1.csv") == ("fixture", None)
    assert fis.parse_import_name("赛果_顶级1.csv") == ("result", "顶级1")
    assert fis.parse_import_name("result_9.xlsx") == ("result", "9")
    assert fis.parse_import_name("赛果_1 平局.csv") == ("result", "1 平局")
    assert fis.parse_import_name("属性_w1.csv") == ("attribute", None)
    assert fis.parse_import_name("attr_名单.csv") == ("attribute", None)
    assert fis.parse_import_name("随手记.csv") is None
    assert fis.parse_import_name("") is None


async def test_check_and_list_import_files():
    env = await TestEnv().setup()
    import time as _t

    def _touch(p, ts):
        p.write_text("x", encoding="utf-8")
        os.utime(p, (ts, ts))

    try:
        d = fis.imports_dir(env.db.db_path)
        assert d.is_dir()
        _touch(d / "a.xlsx", _t.time() + 10)
        _touch(d / "b.csv", _t.time())
        _touch(d / "notme.txt", _t.time() + 20)
        # list 只列 .csv/.xlsx，按 mtime 倒序（后写的排前面）
        names = [p.name for p in fis.list_import_files(env.db.db_path)]
        assert set(names) == {"a.xlsx", "b.csv"}, names
        assert names[0] == "a.xlsx", names
        # check 命中
        assert fis.check_import_file(env.db.db_path, "b.csv") == str(d / "b.csv")
        assert fis.is_import_ext("a.CSV") and fis.is_import_ext("b.xlsx")
        assert not fis.is_import_ext("a.txt") and not fis.is_import_ext("")
        # 缺失 → 报错并列出目录现有文件
        try:
            fis.check_import_file(env.db.db_path, "缺失.csv")
            assert False, "缺失文件应报错"
        except FileImportError as e:
            assert "imports 目录里没有" in str(e) and "b.csv" in str(e), e
        # 扩展名白名单
        try:
            fis.check_import_file(env.db.db_path, "notme.txt")
            assert False, "非白名单扩展名应报错"
        except FileImportError as e:
            assert "仅支持" in str(e), e
        # 目录穿越 / 非法名
        for bad in ("../x.csv", "..\\x.csv", "C:\\x.csv", "a/b.csv", ".", "..", ""):
            try:
                fis.check_import_file(env.db.db_path, bad)
                assert False, f"「{bad}」应被拒绝"
            except FileImportError:
                pass
    finally:
        await env.teardown()


async def test_save_uploaded():
    env = await TestEnv().setup()
    try:
        src = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_src.csv"))
        src.write_text("轮次,主队,客队\n1,利物浦,巴塞罗那\n", encoding="utf-8")
        try:
            target = fis.save_uploaded(env.db.db_path, str(src), "赛程_第一周.csv")
            assert target.exists() and target.name == "赛程_第一周.csv"
            # 恶意文件名净化（只保留文件名基准）
            target2 = fis.save_uploaded(env.db.db_path, str(src), "..\\evil.csv")
            assert target2.name == "evil.csv"
            # 非法扩展名拒绝
            try:
                fis.save_uploaded(env.db.db_path, str(src), "x.txt")
                assert False, "非法扩展名应拒绝"
            except FileImportError:
                pass
            # 超上限拒绝
            try:
                fis.save_uploaded(env.db.db_path, str(src), "big.csv", max_size_mb=0.0000001)
                assert False, "超限文件应拒绝"
            except FileImportError as e:
                assert "大小上限" in str(e), e
        finally:
            src.unlink(missing_ok=True)
    finally:
        await env.teardown()


async def test_import_by_name_e2e():
    env = await TestEnv().setup()
    try:
        d = fis.imports_dir(env.db.db_path)
        (d / "赛程_w1.csv").write_text(
            "轮次,主队,客队\n1,利物浦,巴塞罗那\n1,纽卡斯尔联,勒沃库森\n", encoding="utf-8")
        result = await env.fixture_service.import_fixtures_by_name("赛程_w1.csv")
        assert result["imported"] == 2, result
        try:
            await env.fixture_service.import_fixtures_by_name("缺失.csv")
            assert False, "缺失文件应报错"
        except FileImportError as e:
            assert "赛程_w1.csv" in str(e), e

        (d / "属性_w1.csv").write_text(
            "队名,影响力,容量,等级\n利物浦,150,12000,0\n", encoding="utf-8-sig")
        a = await env.stadium_service.import_attributes_by_name("属性_w1.csv")
        assert a["imported"] == 1, a
        st = await env.dao.get_stadium("利物浦")
        assert abs(st["influence"] - 150.0) < 1e-6
    finally:
        await env.teardown()


async def test_record_results_by_name_e2e():
    env = await TestEnv().setup()
    try:
        await env.fixture_service.import_fixtures("顶级1 利物浦 巴塞罗那\n顶级1 纽卡斯尔联 勒沃库森")
        d = fis.imports_dir(env.db.db_path)
        (d / "赛果_顶级1.csv").write_text("主队,赛果\n利物浦,胜\n纽卡斯尔联,负\n", encoding="utf-8")
        result = await env.fixture_service.record_results_by_name(1, "赛果_顶级1.csv", "顶级联赛")
        assert result["count"] == 2, result
        comp, rn = await env.fixture_service.resolve_round_arg("顶级1")
        assert (comp, rn) == ("顶级联赛", 1)
    finally:
        await env.teardown()


# ─── 群文件钩子（发文件即导入） ─────────────────────────

class _GroupFileEvent:
    """群消息事件桩：携带 File 文件段 + 异步 send + 权限。"""

    def __init__(self, group_id, qq, is_admin, files):
        self._group_id = group_id
        self._qq = qq
        self._admin = is_admin
        self._files = files
        self.sent = []

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._qq

    def get_messages(self):
        return list(self._files)

    def is_admin(self):
        return self._admin

    async def send(self, result):
        self.sent.append(result)


def _make_plugin(env):
    """构造 StadiumPlugin 实例（__new__ 绕过 initialize，手工注入服务）。"""
    from astrbot_plugin_whleague_revenue_system import main as plugin_module

    p = plugin_module.StadiumPlugin.__new__(plugin_module.StadiumPlugin)
    p.config_cache = env.cfg
    p.dao = env.dao
    p.db = env.db
    p.fixture_service = env.fixture_service
    p.stadium_service = env.stadium_service
    return p


async def test_group_file_hook_auto_import_fixture():
    env = await TestEnv().setup()
    try:
        p = _make_plugin(env)
        d = fis.imports_dir(env.db.db_path)
        src = d / "src_赛程_w1.csv"
        src.write_text("轮次,主队,客队\n1,利物浦,巴塞罗那\n2,利物浦,纽卡斯尔联\n", encoding="utf-8")
        event = _GroupFileEvent("99999", "10001", True,
                                [File(name="赛程_w1.csv", file=str(src))])
        await p.on_group_file(event)
        assert event.sent and "已导入" in event.sent[0] and "2 场" in event.sent[0], event.sent
        assert (d / "赛程_w1.csv").exists(), "文件应落盘 imports 目录"
        matches = await env.dao.get_round_matches(1, 1, 1)
        assert len(matches) == 1
    finally:
        await env.teardown()


async def test_group_file_hook_result_round_from_name():
    """赛果轮次取自文件名前缀后剩余：赛果_顶级1.csv → 轮次「顶级1」。"""
    env = await TestEnv().setup()
    try:
        p = _make_plugin(env)
        await env.fixture_service.import_fixtures("顶级1 利物浦 巴塞罗那")
        await env.fixture_service.set_weather(1, "利物浦", "晴")
        d = fis.imports_dir(env.db.db_path)
        src = d / "src_赛果.csv"
        src.write_text("主队,赛果\n利物浦,胜\n", encoding="utf-8")
        event = _GroupFileEvent("99999", "10001", True,
                                [File(name="赛果_顶级1.csv", file=str(src))])
        await p.on_group_file(event)
        assert event.sent and "已录入" in event.sent[0] and "胜" in event.sent[0], event.sent
        comp, rn = await env.fixture_service.resolve_round_arg("顶级1")
        m = (await env.dao.get_round_matches(1, 1, rn, comp))[0]
        assert m["result"] == "W"
    finally:
        await env.teardown()


async def test_group_file_hook_result_unregistered_round():
    """赛果轮次文本未导入赛程 → 友好报错、不静默吞掉。"""
    env = await TestEnv().setup()
    try:
        p = _make_plugin(env)
        d = fis.imports_dir(env.db.db_path)
        (d / "赛程_w1.csv").write_text("轮次,主队,客队\n1,利物浦,巴塞罗那\n", encoding="utf-8")
        src = d / "src_赛果.csv"
        src.write_text("主队,赛果\n利物浦,胜\n", encoding="utf-8")
        event = _GroupFileEvent("99999", "10001", True,
                                [File(name="赛果_顶级9.csv", file=str(src))])
        await p.on_group_file(event)
        assert event.sent and "尚未导入" in event.sent[0], event.sent
    finally:
        await env.teardown()


async def test_group_file_hook_attribute():
    env = await TestEnv().setup()
    try:
        p = _make_plugin(env)
        d = fis.imports_dir(env.db.db_path)
        src = d / "src_属性.csv"
        src.write_text("队名,影响力,容量,等级\n利物浦,150,12000,0\n", encoding="utf-8-sig")
        event = _GroupFileEvent("99999", "10001", True,
                                [File(name="属性_w1.csv", file=str(src))])
        await p.on_group_file(event)
        assert event.sent and "已导入属性" in event.sent[0], event.sent
        st = await env.dao.get_stadium("利物浦")
        assert abs(st["influence"] - 150.0) < 1e-6
    finally:
        await env.teardown()


async def test_group_file_hook_skips_unmatched_and_nonadmin():
    env = await TestEnv().setup()
    try:
        p = _make_plugin(env)
        d = fis.imports_dir(env.db.db_path)
        src = d / "src_随手.csv"
        src.write_text("随便,内容\n", encoding="utf-8")
        # 文件名不匹配前缀 → 静默跳过、不落盘、不回复
        ev1 = _GroupFileEvent("99999", "10001", True, [File(name="随手.csv", file=str(src))])
        await p.on_group_file(ev1)
        assert not ev1.sent and not (d / "随手.csv").exists()
        # 非法文件名（../）→ 不落盘
        ev2 = _GroupFileEvent("99999", "10001", True, [File(name="../evil.csv", file=str(src))])
        await p.on_group_file(ev2)
        assert not ev2.sent
        # 非管理员 → 不处理
        ev3 = _GroupFileEvent("99999", "10002", False,
                              [File(name="赛程_w1.csv", file=str(src))])
        await p.on_group_file(ev3)
        assert not ev3.sent and not (d / "赛程_w1.csv").exists()
    finally:
        await env.teardown()


# ─── 命令层（by-name / 无参列表／文本回退） ──────────────

async def test_handler_import_fixtures_by_name():
    env = await TestEnv().setup()
    try:
        handler = _make_admin_handler(env)
        d = fis.imports_dir(env.db.db_path)
        (d / "赛程_w1.csv").write_text(
            "轮次,主队,客队\n1,利物浦,巴塞罗那\n2,利物浦,纽卡斯尔联\n", encoding="utf-8")
        event = _FakeEvent("/主场赛程导入 赛程_w1.csv", sender="10001", is_admin=True, messages=[])
        results = [r async for r in handler.import_fixtures(event)]
        assert results and "已导入" in results[0] and "2 场" in results[0], results
        assert (d / "赛程_w1.csv").exists(), "命令导入不删除 imports 文件"
        matches = await env.dao.get_round_matches(1, 1, 1)
        assert len(matches) == 1
    finally:
        await env.teardown()


async def test_handler_import_by_name_missing():
    env = await TestEnv().setup()
    try:
        handler = _make_admin_handler(env)
        event = _FakeEvent("/主场赛程导入 缺失.csv", sender="10001", is_admin=True, messages=[])
        results = [r async for r in handler.import_fixtures(event)]
        assert results and "imports 目录里没有" in results[0], results
    finally:
        await env.teardown()


async def test_handler_results_and_attributes_by_name():
    env = await TestEnv().setup()
    try:
        handler = _make_admin_handler(env)
        await env.fixture_service.import_fixtures("1 利物浦 巴塞罗那")
        await env.fixture_service.set_weather(1, "利物浦", "晴")
        d = fis.imports_dir(env.db.db_path)
        (d / "赛果_1.csv").write_text("主队,赛果\n利物浦,胜\n", encoding="utf-8")
        event = _FakeEvent("/主场赛果 1 赛果_1.csv", sender="10001", is_admin=True, messages=[])
        results = [r async for r in handler.record_results(event)]
        assert results and "已录入" in results[0], results

        (d / "属性_w1.csv").write_text("队名,影响力,容量,等级\n利物浦,160,12000,0\n", encoding="utf-8-sig")
        event2 = _FakeEvent("/主场属性导入 属性_w1.csv", sender="10001", is_admin=True, messages=[])
        results2 = [r async for r in handler.import_attributes_batch(event2)]
        assert results2 and "文件导入属性" in results2[0], results2
    finally:
        await env.teardown()


async def test_handler_no_arg_lists_imports_dir():
    env = await TestEnv().setup()
    try:
        handler = _make_admin_handler(env)
        d = fis.imports_dir(env.db.db_path)
        (d / "赛程_w1.csv").write_text("轮次,主队,客队\n1,利物浦,巴塞罗那\n", encoding="utf-8")
        for msg in ("/主场赛程导入", "/主场属性导入"):
            event = _FakeEvent(msg, sender="10001", is_admin=True, messages=[])
            if "赛程" in msg:
                results = [r async for r in handler.import_fixtures(event)]
            else:
                results = [r async for r in handler.import_attributes_batch(event)]
            assert results and "用法" in results[0] and "赛程_w1.csv" in results[0], results
    finally:
        await env.teardown()


async def test_handler_text_paste_still_works():
    """移除附件入口后，纯文本粘贴导入不受影响。"""
    env = await TestEnv().setup()
    try:
        handler = _make_admin_handler(env)
        event = _FakeEvent("/主场赛程导入\n1 利物浦 巴塞罗那", sender="10001", is_admin=True, messages=[])
        results = [r async for r in handler.import_fixtures(event)]
        assert results and "已导入" in results[0], results
        matches = await env.dao.get_round_matches(1, 1, 1)
        assert len(matches) == 1
    finally:
        await env.teardown()