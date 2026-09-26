#!/usr/bin/env python3
"""Run the test suite without installing the package.

chunkforge uses a ``src/`` layout, which normally means installing it first.
This script puts ``src`` on the path instead, so the suite runs from a fresh
checkout on a bare interpreter with nothing but the standard library:

    python run_tests.py            # everything
    python run_tests.py -v         # verbose
    python run_tests.py gear       # only tests/test_gear.py
    python run_tests.py chunker    # only tests/test_chunker.py

The same discovery works under pytest if you have it, but pytest is not
required and nothing here depends on it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
TESTS = ROOT / "tests"


def build_suite(patterns: list[str]) -> unittest.TestSuite:
    """Discover tests, optionally restricted to files matching a substring."""
    sys.path.insert(0, str(SRC))
    sys.path.insert(0, str(ROOT))

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    names = sorted(p.stem for p in TESTS.glob("test_*.py"))
    if patterns:
        names = [n for n in names if any(p in n for p in patterns)]
        if not names:
            raise SystemExit(
                f"no test module matches {patterns}; have {[n for n in sorted(p.stem for p in TESTS.glob('test_*.py'))]}"
            )

    for stem in names:
        suite.addTests(loader.loadTestsFromName(f"tests.{stem}"))

    return suite


def main(argv: list[str]) -> int:
    verbose = False
    patterns: list[str] = []
    for arg in argv[1:]:
        if arg in ("-v", "--verbose"):
            verbose = True
        elif arg in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            patterns.append(arg)

    suite = build_suite(patterns)
    runner = unittest.TextTestRunner(verbosity=2 if verbose else 1, buffer=False)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
