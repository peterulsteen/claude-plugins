"""Shared pytest fixtures for benchmark tests."""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure this directory is on sys.path so helpers (and other local modules) can be imported
_THIS_DIR = str(Path(__file__).parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from helpers import add_benchmark_paths  # noqa: E402

add_benchmark_paths()

FIXTURES_DIR = Path(__file__).parent / "fixtures"
THRESHOLDS_PATH = Path(__file__).parent / "thresholds.json"


def _discover_scenarios() -> list[str]:
    """Auto-discover scenario names from fixture directories."""
    if not FIXTURES_DIR.is_dir():
        return []
    return sorted(
        d.name for d in FIXTURES_DIR.iterdir() if d.is_dir()
    )


@pytest.fixture(params=_discover_scenarios())
def scenario_name(request: pytest.FixtureRequest) -> str:
    """Parameterized fixture yielding each benchmark scenario name."""
    return request.param


@pytest.fixture
def fixture_dir(scenario_name: str) -> Path:
    """Return the fixture directory for the current scenario."""
    d = FIXTURES_DIR / scenario_name
    assert d.is_dir(), f"Fixture directory missing: {d}"
    return d


@pytest.fixture
def thresholds(scenario_name: str) -> dict[str, Any]:
    """Load thresholds for the current scenario."""
    with open(THRESHOLDS_PATH, encoding="utf-8") as f:
        all_thresholds = json.load(f)
    scenario_thresholds = all_thresholds["scenarios"].get(scenario_name)
    if scenario_thresholds is None:
        pytest.skip(f"No thresholds configured for scenario: {scenario_name}")
    return scenario_thresholds
