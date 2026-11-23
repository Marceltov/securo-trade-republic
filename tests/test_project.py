"""The project files that must agree with each other."""

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_manifest_and_project_have_one_version():
    manifest = json.loads((ROOT / "securo-plugin.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert manifest["version"] == project["version"]


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_runtime_lock_matches_uv_lock():
    """requirements.txt is what Securo's runner installs: the export of uv.lock, hash-pinned."""
    exported = subprocess.run(
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-header", "--format", "requirements-txt"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert (ROOT / "requirements.txt").read_text() == exported


def _texts(node):
    if isinstance(node, dict):
        if "en" in node and all(isinstance(v, str) for v in node.values()):
            yield node
        else:
            for value in node.values():
                yield from _texts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _texts(value)


def test_every_manifest_text_is_in_english_and_german():
    texts = list(_texts(json.loads((ROOT / "securo-plugin.json").read_text())))
    assert len(texts) >= 6
    assert all(set(t) == {"en", "de"} and all(t.values()) for t in texts)
