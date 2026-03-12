"""Helper functions for benchmark tests."""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

CHARS_PER_TOKEN = 4

# Shared path to run-judges scripts (used by conftest.py and score_report.py)
SCRIPTS_DIR = (
    Path(__file__).parent.parent.parent.parent
    / "skills"
    / "run-judges"
    / "scripts"
)


def add_benchmark_paths() -> None:
    """Add benchmark-related directories to sys.path."""
    scripts_str = str(SCRIPTS_DIR)
    if scripts_str not in sys.path:
        sys.path.insert(0, scripts_str)
    benchmarks_parent = str(Path(__file__).parent.parent)
    if benchmarks_parent not in sys.path:
        sys.path.insert(0, benchmarks_parent)
    benchmarks_dir = str(Path(__file__).parent)
    if benchmarks_dir not in sys.path:
        sys.path.insert(0, benchmarks_dir)


def heuristic_token_count(text: str) -> int:
    """Estimate token count: ~4 characters per token."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def _require_file(path: Path) -> None:
    """Skip the test if a fixture file is not present."""
    if not path.exists():
        pytest.skip(f"Fixture not present: {path.name}")


def load_json_fixture(fixture_dir: Path, filename: str) -> dict[str, Any]:
    """Load and parse a JSON fixture file. Skips test if missing."""
    path = fixture_dir / filename
    _require_file(path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_text_fixture(fixture_dir: Path, filename: str) -> str:
    """Load a text fixture file. Skips test if missing."""
    path = fixture_dir / filename
    _require_file(path)
    return path.read_text(encoding="utf-8")


def load_jsonl_fixture(
    fixture_dir: Path, filename: str
) -> list[dict[str, Any]]:
    """Load a JSONL fixture file as a list of dicts. Skips test if missing."""
    path = fixture_dir / filename
    _require_file(path)
    events: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events
