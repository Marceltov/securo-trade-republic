"""CodeQL leaves out test code only: the shipped package stays scanned, every test file is left out."""

import subprocess
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _ignored(path: str, patterns: list[str]) -> bool:
    """CodeQL's paths-ignore: a pattern without a glob names a directory (or file) and everything below it."""
    p = PurePosixPath(path)
    for pattern in patterns:
        if any(c in pattern for c in "*?["):
            if p.full_match(pattern):
                return True
        elif path == pattern or path.startswith(pattern.rstrip("/") + "/"):
            return True
    return False


def _tracked() -> list[str]:
    return subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()


def test_the_package_is_scanned_and_the_tests_are_not():
    patterns = yaml.safe_load((ROOT / ".github/codeql/codeql-config.yml").read_text())["paths-ignore"]
    files = [f for f in _tracked() if f.endswith(".py")]
    package = [f for f in files if f.startswith("trade_republic/")]
    tests = [f for f in files if f.startswith("tests/")]
    assert package and tests
    assert [f for f in package if _ignored(f, patterns)] == []
    assert [f for f in tests if not _ignored(f, patterns)] == []
