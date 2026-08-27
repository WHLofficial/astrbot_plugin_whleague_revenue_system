"""全部测试入口：python tests/run_all.py"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import run_module  # noqa: E402

MODULES = [
    "tests.s0_schema",
    "tests.s1_formula",
    "tests.s2_fixture_flow",
    "tests.s3_stadium_attr",
    "tests.s4_window",
    "tests.s5_events_brands",
    "tests.s6_handlers",
    "tests.s7_file_import",
    "tests.s8_charts",
    "tests.s9_listeners",
]


def main() -> int:
    total = 0
    for name in MODULES:
        total += run_module(name)
    print(f"\n{'=' * 40}\nTotal failed: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())