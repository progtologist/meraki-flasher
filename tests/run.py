"""Test entry point for CI: the same suite, with a stall watchdog.

``python -m tests.run`` runs everything ``unittest discover`` would, but arms
``faulthandler.dump_traceback_later`` first: if the suite ever stops making
progress, every thread's stack is printed, so a hang in CI says *where*.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import unittest

WATCHDOG_SECONDS = float(os.environ.get("TEST_WATCHDOG_SECONDS", "120"))


def main() -> int:
    faulthandler.enable()
    faulthandler.dump_traceback_later(WATCHDOG_SECONDS, repeat=True, exit=False)
    suite = unittest.TestLoader().discover("tests", top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    faulthandler.cancel_dump_traceback_later()
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
