"""数值解析与配置校验：NaN/inf 不得绕过范围检查（审查 🔴 R1 回归）。"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import install_stubs  # noqa: E402,F401  (导入即装桩)

from astrbot_plugin_whleague_revenue_system.config.defaults import validate_and_cast  # noqa: E402
from astrbot_plugin_whleague_revenue_system.services.file_import_service import (  # noqa: E402
    build_attribute_rows,
)
from astrbot_plugin_whleague_revenue_system.utils.security import parse_float  # noqa: E402


async def test_parse_float_rejects_nonfinite():
    """NaN 与任何区间比较恒 False，必须在解析层直接拒绝。"""
    for bad in ("nan", "NaN", "inf", "-inf", "Infinity", "1e999"):
        try:
            parse_float(bad, min_val=0.0, max_val=100.0)
        except ValueError:
            pass
        else:
            raise AssertionError(f"parse_float 应拒绝 {bad!r}")
        try:
            parse_float(bad)  # 无范围参数时同样拒绝
        except ValueError:
            pass
        else:
            raise AssertionError(f"parse_float 无范围也应拒绝 {bad!r}")
    assert parse_float(" 12.5 ") == 12.5
    assert parse_float("-3", min_val=-5.0) == -3.0


async def test_validate_and_cast_rejects_nonfinite_float():
    for bad in ("nan", "inf", "-inf", "1e999"):
        try:
            validate_and_cast("start_funds", bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"validate_and_cast 应拒绝 {bad!r}")
    assert validate_and_cast("start_funds", "80") == 80.0


async def test_build_attribute_rows_rejects_nonfinite_cells():
    rows = [
        ["队A", "nan", "12000", "3"],     # 影响力 NaN → 错误行
        ["队B", "99.5", "inf", "2"],      # 容量 inf → int() OverflowError → 错误行
        ["队C", "95", "12000", "1"],
    ]
    records, errors = build_attribute_rows(rows)
    assert len(errors) == 2, errors
    assert records == [("队C", 95.0, 12000, 1)], records


async def test_sanitize_text_max_length_param():
    from astrbot_plugin_whleague_revenue_system.utils.security import sanitize_text
    assert sanitize_text("a\x00b\nc") == "abc"
    assert sanitize_text("x" * 100, 10) == "x" * 10
    assert sanitize_text("") == ""
    assert sanitize_text("  ok  ") == "ok"


async def test_parse_and_format_helpers():
    from astrbot_plugin_whleague_revenue_system.utils.security import (
        parse_int, parse_qq, parse_qq_arg, parse_choice_no,
        format_m, format_int,
    )
    # parse_int：非法输入与越界
    for bad in ("abc", ""):
        try:
            parse_int(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"parse_int 应拒绝 {bad!r}")
    assert parse_int("5", 1, 20) == 5
    for bad in ("0", "21"):
        try:
            parse_int(bad, 1, 20)
        except ValueError:
            pass
        else:
            raise AssertionError(f"parse_int 应拒绝越界值 {bad!r}")
    # parse_qq：允许空格与 @ 前缀，拒绝非数字
    assert parse_qq(" 12345 ") == "12345"
    assert parse_qq("@67890") == "67890"
    for bad in ("abc", "12 34"):
        try:
            parse_qq(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"parse_qq 应拒绝 {bad!r}")
    # parse_qq_arg：空/CQ 码/圆括号/@前缀/无法解析
    assert parse_qq_arg("") is None
    assert parse_qq_arg("[CQ:at,qq=10001]") == "10001"
    assert parse_qq_arg("(10002)") == "10002"
    assert parse_qq_arg("@10003") == "10003"
    assert parse_qq_arg("小明") is None
    # parse_choice_no：带圈数字与普通整数
    assert parse_choice_no("①") == 1
    assert parse_choice_no("④") == 4
    assert parse_choice_no("3") == 3
    # format_m 去尾零 / format_int 千分位
    assert format_m(12.50) == "12.5"
    assert format_m(12.00) == "12"
    assert format_m(0.05) == "0.05"
    assert format_int(1234567) == "1,234,567"
