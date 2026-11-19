"""全部测试入口：python tests/run_all.py"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import run_module  # noqa: E402

MODULES = [
    "tests.s0_schema",
    "tests.s1_formula",
    "tests.s6_handlers",
    "tests.s8_charts",
    "tests.s13_infra",
    "tests.s17_framework_compat",
    "tests.s18_club_sync",
    "tests.s19_tour_sync",
]


def main() -> int:
    total = 0
    for name in MODULES:
        total += run_module(name)
    print(f"\n{'=' * 40}\nTotal failed: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())