"""Discover and run the whole hub test suite; emit a one-line summary envelope.

    python -m tests.run          # run all, human-readable
    python -m tests.run --json   # machine envelope on the last line

Exit code 0 iff every test passed — so this doubles as a CI gate.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def main() -> int:
    as_json = "--json" in sys.argv[1:]
    verbosity = 1 if as_json else 2

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(TESTS_DIR), pattern="test_*.py",
                            top_level_dir=str(TESTS_DIR.parent))
    runner = unittest.TextTestRunner(verbosity=verbosity, buffer=True)
    result = runner.run(suite)

    ok = result.wasSuccessful()
    summary = {
        "ok": ok,
        "action": "tests.run",
        "data": {
            "tests_run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
        },
        "error": None if ok else
        f"{len(result.failures)} failure(s), {len(result.errors)} error(s)",
    }
    if as_json:
        print(json.dumps(summary, ensure_ascii=True))
    else:
        d = summary["data"]
        print(f"\n{'PASS' if ok else 'FAIL'} -- {d['tests_run']} run, "
              f"{d['failures']} failed, {d['errors']} errored, "
              f"{d['skipped']} skipped")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
