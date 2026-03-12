#!/usr/bin/env python3
"""Score report generator for E2E benchmark pipeline.

Parses judge reports (judges.json, code-judges.json), computes per-judge
mean scores, compares against a committed baseline, checks absolute
thresholds, and generates a markdown report for PR comments.

Supports 3-tier comparison:
  - Model baseline: OOTB Claude (Opus 4.6) without orchestration
  - ClosedLoop baseline: ClosedLoop orchestration on main branch
  - Current (PR under test): ClosedLoop orchestration with PR changes

Usage:
    python score_report.py \
        --artifacts-dir benchmark-artifacts/ \
        --baseline baseline-scores.json \
        --model-baseline model-baseline-scores.json \
        --thresholds thresholds.json \
        --output-scores /tmp/scores.json \
        --output-markdown /tmp/report.md \
        --output-status /tmp/status.txt

    # README mode: just output Model vs ClosedLoop table
    python score_report.py \
        --readme \
        --baseline baseline-scores.json \
        --model-baseline model-baseline-scores.json \
        --output-markdown /tmp/readme-table.md
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Bootstrap sys.path via shared helper
sys.path.insert(0, str(Path(__file__).parent))
from helpers import add_benchmark_paths  # noqa: E402

add_benchmark_paths()

from validate_judge_report import (  # type: ignore[import-not-found]
    EvaluationReport,
)

REGRESSION_THRESHOLD = 0.05  # 5% regression triggers WARN


@dataclass
class JudgeScore:
    """Score for a single judge."""

    mean_score: float
    final_status: int
    metric_count: int


@dataclass
class ArtifactScores:
    """Aggregate scores for one artifact type (plan or code)."""

    overall_mean: float
    pass_rate: float
    judges: dict[str, JudgeScore] = field(default_factory=dict)


@dataclass
class ScoreDelta:
    """Delta between current and baseline for a single judge."""

    scenario: str
    artifact_type: str
    judge_name: str
    baseline_score: float | None
    current_score: float
    delta_pct: float | None  # None if no baseline


@dataclass
class ThresholdViolation:
    """Record of an absolute threshold violation."""

    scenario: str
    artifact_type: str
    metric: str
    threshold: float
    actual: float


def find_judge_reports(
    artifacts_dir: Path,
) -> dict[str, dict[str, Path | None]]:
    """Find judge report files in downloaded artifact directories.

    Supports two directory layouts:
    1. CI artifacts: artifacts_dir/benchmark-{scenario}/.claude/runs/*/judges.json
    2. Local fixtures: artifacts_dir/{scenario}/judges.json

    Returns: {scenario: {"plan": Path|None, "code": Path|None}}
    """
    results: dict[str, dict[str, Path | None]] = {}

    for subdir in sorted(artifacts_dir.iterdir()):
        if not subdir.is_dir():
            continue

        # Determine scenario name
        scenario = subdir.name
        if scenario.startswith("benchmark-"):
            scenario = scenario[len("benchmark-"):]

        plan_report = None
        code_report = None

        # Layout 1: CI artifacts with .claude/runs/*/
        runs_dir = subdir / ".claude" / "runs"
        if runs_dir.is_dir():
            for run_dir in sorted(runs_dir.iterdir(), reverse=True):
                if not run_dir.is_dir():
                    continue
                if plan_report is None:
                    candidate = run_dir / "judges.json"
                    if candidate.exists():
                        plan_report = candidate
                if code_report is None:
                    candidate = run_dir / "code-judges.json"
                    if candidate.exists():
                        code_report = candidate
                if plan_report and code_report:
                    break

        # Layout 2: Direct fixture layout (scenario/judges.json)
        if plan_report is None:
            candidate = subdir / "judges.json"
            if candidate.exists():
                plan_report = candidate
        if code_report is None:
            candidate = subdir / "code-judges.json"
            if candidate.exists():
                code_report = candidate

        if plan_report or code_report:
            if scenario in results:
                print(
                    f"Warning: duplicate scenario '{scenario}' "
                    f"(from '{subdir.name}') — skipping",
                    file=sys.stderr,
                )
                continue
            results[scenario] = {"plan": plan_report, "code": code_report}

    return results


def compute_scores(report_path: Path) -> ArtifactScores:
    """Parse a judge report and compute per-judge mean scores."""
    with open(report_path) as f:
        data = json.load(f)

    report = EvaluationReport.model_validate(data)

    judges: dict[str, JudgeScore] = {}
    all_means: list[float] = []
    pass_count = 0

    for case in report.stats:
        if not case.metrics:
            continue
        scores = [m.score for m in case.metrics]
        mean = sum(scores) / len(scores)
        judges[case.case_id] = JudgeScore(
            mean_score=round(mean, 4),
            final_status=case.final_status,
            metric_count=len(scores),
        )
        all_means.append(mean)
        if case.final_status == 1:
            pass_count += 1

    total = len([c for c in report.stats if c.metrics])
    return ArtifactScores(
        overall_mean=round(sum(all_means) / len(all_means), 4)
        if all_means
        else 0.0,
        pass_rate=round(pass_count / total, 4) if total > 0 else 0.0,
        judges=judges,
    )


def compare_against_baseline(
    current: dict[str, dict[str, ArtifactScores | None]],
    baseline: dict[str, Any] | None,
) -> list[ScoreDelta]:
    """Compare current scores against baseline, producing per-judge deltas."""
    deltas: list[ScoreDelta] = []

    if baseline is None:
        # No baseline — report current scores with no delta
        for scenario, types in current.items():
            for art_type, scores in types.items():
                if scores is None:
                    continue
                for judge_name, judge_score in scores.judges.items():
                    deltas.append(
                        ScoreDelta(
                            scenario=scenario,
                            artifact_type=art_type,
                            judge_name=judge_name,
                            baseline_score=None,
                            current_score=judge_score.mean_score,
                            delta_pct=None,
                        )
                    )
        return deltas

    baseline_scenarios = baseline.get("scenarios", {})

    for scenario, types in current.items():
        for art_type, scores in types.items():
            if scores is None:
                continue
            baseline_art = (
                baseline_scenarios.get(scenario, {}).get(art_type, {})
            )
            baseline_judges = baseline_art.get("judges", {})

            for judge_name, judge_score in scores.judges.items():
                baseline_judge = baseline_judges.get(judge_name, {})
                baseline_score = baseline_judge.get("mean_score")

                if baseline_score is not None and baseline_score > 0:
                    delta_pct = round(
                        (judge_score.mean_score - baseline_score)
                        / baseline_score,
                        4,
                    )
                else:
                    delta_pct = None

                deltas.append(
                    ScoreDelta(
                        scenario=scenario,
                        artifact_type=art_type,
                        judge_name=judge_name,
                        baseline_score=baseline_score,
                        current_score=judge_score.mean_score,
                        delta_pct=delta_pct,
                    )
                )

    return deltas


def check_thresholds(
    current: dict[str, dict[str, ArtifactScores | None]],
    thresholds: dict[str, Any],
) -> list[ThresholdViolation]:
    """Check absolute threshold compliance from thresholds.json."""
    violations: list[ThresholdViolation] = []
    threshold_scenarios = thresholds.get("scenarios", {})

    for scenario, types in current.items():
        scenario_thresholds = threshold_scenarios.get(scenario, {})

        for art_type, scores in types.items():
            if scores is None:
                continue

            # Determine which quality key to use
            quality_key = (
                "code_quality" if art_type == "code" else "quality"
            )
            quality = scenario_thresholds.get(quality_key, {})
            if not quality:
                continue

            # Max error count
            max_errors = quality.get("max_error_count")
            if max_errors is not None:
                error_count = sum(
                    1
                    for js in scores.judges.values()
                    if js.final_status == 3
                )
                if error_count > max_errors:
                    violations.append(
                        ThresholdViolation(
                            scenario=scenario,
                            artifact_type=art_type,
                            metric="error_count",
                            threshold=float(max_errors),
                            actual=float(error_count),
                        )
                    )

            # Overall pass rate
            min_pass_rate = quality.get("min_pass_rate")
            if min_pass_rate is not None and scores.pass_rate < min_pass_rate:
                violations.append(
                    ThresholdViolation(
                        scenario=scenario,
                        artifact_type=art_type,
                        metric="pass_rate",
                        threshold=min_pass_rate,
                        actual=scores.pass_rate,
                    )
                )

            # Overall mean score
            min_mean = quality.get("min_mean_score")
            if min_mean is not None and scores.overall_mean < min_mean:
                violations.append(
                    ThresholdViolation(
                        scenario=scenario,
                        artifact_type=art_type,
                        metric="mean_score",
                        threshold=min_mean,
                        actual=scores.overall_mean,
                    )
                )

            # Per-judge minimums
            per_judge_mins = quality.get("per_judge_min_score", {})
            for judge_name, min_score in per_judge_mins.items():
                judge_score = scores.judges.get(judge_name)
                if judge_score is None:
                    # Configured judge absent from results
                    violations.append(
                        ThresholdViolation(
                            scenario=scenario,
                            artifact_type=art_type,
                            metric=f"judge:{judge_name}:missing",
                            threshold=min_score,
                            actual=0.0,
                        )
                    )
                elif judge_score.mean_score < min_score:
                    violations.append(
                        ThresholdViolation(
                            scenario=scenario,
                            artifact_type=art_type,
                            metric=f"judge:{judge_name}",
                            threshold=min_score,
                            actual=judge_score.mean_score,
                        )
                    )

    return violations


def determine_status(
    deltas: list[ScoreDelta],
    violations: list[ThresholdViolation],
) -> str:
    """Determine overall status: PASS, WARN, or FAIL."""
    if violations:
        return "FAIL"

    # Check for significant regressions (>5%)
    for d in deltas:
        if d.delta_pct is not None and d.delta_pct < -REGRESSION_THRESHOLD:
            return "WARN"

    return "PASS"


def _lookup_baseline_judge(
    baseline: dict[str, Any] | None,
    scenario: str,
    art_type: str,
    judge_name: str,
) -> float | None:
    """Look up a judge's mean_score from a baseline JSON structure."""
    if baseline is None:
        return None
    return (
        baseline.get("scenarios", {})
        .get(scenario, {})
        .get(art_type, {})
        .get("judges", {})
        .get(judge_name, {})
        .get("mean_score")
    )


def _lookup_baseline_overall(
    baseline: dict[str, Any] | None,
    scenario: str,
    art_type: str,
) -> float | None:
    """Look up overall_mean from a baseline JSON structure."""
    if baseline is None:
        return None
    return (
        baseline.get("scenarios", {})
        .get(scenario, {})
        .get(art_type, {})
        .get("overall_mean")
    )


def _fmt_pct(value: float | None) -> str:
    """Format a score as a percentage string, or N/A."""
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%"


def _fmt_delta(current: float | None, baseline: float | None) -> str:
    """Compute and format a delta as +X.X% or -X.X%, or N/A."""
    if current is None or baseline is None or baseline == 0:
        return "N/A"
    delta = (current - baseline) / baseline
    return f"{delta:+.1%}"


def generate_markdown(
    current: dict[str, dict[str, ArtifactScores | None]],
    deltas: list[ScoreDelta],
    violations: list[ThresholdViolation],
    status: str,
    model_baseline: dict[str, Any] | None = None,
    cl_baseline: dict[str, Any] | None = None,
    commit_sha: str = "",
    pr_number: str = "",
) -> str:
    """Generate markdown PR comment with 3-tier comparison tables.

    Columns: Claude (Opus 4.6) | ClosedLoop (main) | Current | Delta (vs main) | Status
    """
    lines: list[str] = []

    title = "## E2E Benchmark Report"
    if pr_number:
        title += f" -- PR #{pr_number}"
    lines.append(title)
    lines.append("")

    status_icon = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}[status]
    meta = f"**Status: {status_icon}**"
    if commit_sha:
        meta += f" | Commit: `{commit_sha[:7]}`"
    lines.append(meta)
    lines.append("")

    has_model = model_baseline is not None
    has_cl = cl_baseline is not None

    # Group deltas by artifact type
    for art_type in ["plan", "code"]:
        art_deltas = [d for d in deltas if d.artifact_type == art_type]
        if not art_deltas:
            continue

        label = "Plan Quality" if art_type == "plan" else "Code Quality"
        lines.append(f"### {label}")
        lines.append("")

        # Build header based on available tiers
        header = "| Scenario | Judge "
        sep = "|----------|-------"
        if has_model:
            header += "| Claude (Opus 4.6) "
            sep += "|-------------------"
        if has_cl:
            header += "| ClosedLoop (main) "
            sep += "|-------------------"
        header += "| Current | Delta | Status |"
        sep += "|---------|-------|--------|"

        lines.append(header)
        lines.append(sep)

        # Group by scenario
        scenarios = sorted(set(d.scenario for d in art_deltas))
        for scenario in scenarios:
            s_deltas = sorted(
                [d for d in art_deltas if d.scenario == scenario],
                key=lambda d: d.judge_name,
            )
            for d in s_deltas:
                model_score = _lookup_baseline_judge(
                    model_baseline, scenario, art_type, d.judge_name
                )
                cl_score = _lookup_baseline_judge(
                    cl_baseline, scenario, art_type, d.judge_name
                )

                # Delta is vs ClosedLoop baseline (main)
                delta_str = _fmt_delta(d.current_score, cl_score)

                # Row status
                row_status = "OK"
                if cl_score is not None and cl_score > 0:
                    delta_val = (d.current_score - cl_score) / cl_score
                    if delta_val < -REGRESSION_THRESHOLD:
                        row_status = "WARN"
                for v in violations:
                    if (
                        v.scenario == scenario
                        and v.artifact_type == art_type
                        and v.metric == f"judge:{d.judge_name}"
                    ):
                        row_status = "FAIL"
                        break

                row = f"| {scenario} | {d.judge_name} "
                if has_model:
                    row += f"| {_fmt_pct(model_score)} "
                if has_cl:
                    row += f"| {_fmt_pct(cl_score)} "
                row += (
                    f"| {_fmt_pct(d.current_score)} "
                    f"| {delta_str} | {row_status} |"
                )
                lines.append(row)

            # Overall row
            scores = current.get(scenario, {}).get(art_type)
            if scores:
                model_overall = _lookup_baseline_overall(
                    model_baseline, scenario, art_type
                )
                cl_overall = _lookup_baseline_overall(
                    cl_baseline, scenario, art_type
                )
                overall_delta = _fmt_delta(scores.overall_mean, cl_overall)

                row = f"| {scenario} | **Overall** "
                if has_model:
                    row += f"| **{_fmt_pct(model_overall)}** "
                if has_cl:
                    row += f"| **{_fmt_pct(cl_overall)}** "
                row += (
                    f"| **{_fmt_pct(scores.overall_mean)}** "
                    f"| {overall_delta} | |"
                )
                lines.append(row)

        lines.append("")

    # Summary section
    lines.append("### Summary")
    lines.append("")

    if pr_number:
        lines.append(f"Given PR #{pr_number} changes:")
    else:
        lines.append("Results:")

    for art_type in ["plan", "code"]:
        art_deltas = [d for d in deltas if d.artifact_type == art_type]
        if not art_deltas:
            continue
        label = "Plan" if art_type == "plan" else "Code"

        # Compute delta vs ClosedLoop baseline
        if cl_baseline is not None:
            cl_scenarios = cl_baseline.get("scenarios", {})
            for scenario in sorted(set(d.scenario for d in art_deltas)):
                scores = current.get(scenario, {}).get(art_type)
                cl_overall = (
                    cl_scenarios.get(scenario, {})
                    .get(art_type, {})
                    .get("overall_mean")
                )
                if scores and cl_overall and cl_overall > 0:
                    delta = (scores.overall_mean - cl_overall) / cl_overall
                    lines.append(
                        f"- **{label} quality ({scenario}):** "
                        f"{delta:+.1%} vs main"
                    )
                else:
                    lines.append(
                        f"- **{label} quality ({scenario}):** "
                        f"no baseline for comparison"
                    )
        else:
            deltas_with_pct = [
                d for d in art_deltas if d.delta_pct is not None
            ]
            if deltas_with_pct:
                avg_delta = sum(
                    d.delta_pct for d in deltas_with_pct
                    if d.delta_pct is not None
                ) / len(deltas_with_pct)
                lines.append(
                    f"- **{label} quality:** {avg_delta:+.1%} overall"
                )
            else:
                lines.append(
                    f"- **{label} quality:** no baseline for comparison"
                )

    if violations:
        lines.append(
            f"- **Threshold violations:** {len(violations)}"
        )
        for v in violations:
            if v.metric == "error_count" or v.metric.endswith(":missing"):
                lines.append(
                    f"  - {v.scenario}/{v.artifact_type}: {v.metric} "
                    f"= {v.actual:.0f} (max: {v.threshold:.0f})"
                )
            else:
                lines.append(
                    f"  - {v.scenario}/{v.artifact_type}: {v.metric} "
                    f"= {v.actual * 100:.1f}% (min: {v.threshold * 100:.1f}%)"
                )
    else:
        lines.append("- **Threshold violations:** None")

    regression_count = sum(
        1
        for d in deltas
        if d.delta_pct is not None and d.delta_pct < -REGRESSION_THRESHOLD
    )
    if regression_count:
        lines.append(
            f"- **Regressions (>{REGRESSION_THRESHOLD:.0%}):** "
            f"{regression_count} judge(s)"
        )

    lines.append("")

    return "\n".join(lines)


def generate_readme_table(
    model_baseline: dict[str, Any],
    cl_baseline: dict[str, Any],
) -> str:
    """Generate a compact Model vs ClosedLoop table for the README.

    Shows the improvement ClosedLoop provides over OOTB Claude.
    """
    lines: list[str] = []
    lines.append("### Benchmark Quality Scores")
    lines.append("")

    model_name = model_baseline.get("model", "claude-opus-4-6")
    # Derive judge count from baseline data
    judge_count = 0
    for scenario_data in model_baseline.get("scenarios", {}).values():
        for art_data in scenario_data.values():
            if isinstance(art_data, dict) and "judges" in art_data:
                judge_count = max(judge_count, len(art_data["judges"]))
    judge_label = f"{judge_count} judge agents" if judge_count else "judge agents"
    lines.append(
        f"> Evaluated by {judge_label} against static fixture PRDs. "
        f"Model baseline: {model_name} (no orchestration)."
    )
    lines.append("")

    model_scenarios = model_baseline.get("scenarios", {})
    cl_scenarios = cl_baseline.get("scenarios", {})

    all_scenarios = sorted(
        set(list(model_scenarios.keys()) + list(cl_scenarios.keys()))
    )

    for art_type in ["plan", "code"]:
        # Check if any scenario has this artifact type
        has_data = False
        for scenario in all_scenarios:
            if (
                model_scenarios.get(scenario, {}).get(art_type)
                or cl_scenarios.get(scenario, {}).get(art_type)
            ):
                has_data = True
                break
        if not has_data:
            continue

        label = "Plan Quality" if art_type == "plan" else "Code Quality"
        lines.append(f"**{label}**")
        lines.append("")
        lines.append(
            "| Scenario | Judge | Claude (Opus 4.6) "
            "| ClosedLoop | Improvement |"
        )
        lines.append(
            "|----------|-------|-------------------"
            "|------------|-------------|"
        )

        for scenario in all_scenarios:
            model_art = model_scenarios.get(scenario, {}).get(art_type, {})
            cl_art = cl_scenarios.get(scenario, {}).get(art_type, {})
            if not model_art and not cl_art:
                continue

            model_judges = model_art.get("judges", {})
            cl_judges = cl_art.get("judges", {})

            all_judges = sorted(
                set(list(model_judges.keys()) + list(cl_judges.keys()))
            )

            for judge_name in all_judges:
                model_score = model_judges.get(judge_name, {}).get(
                    "mean_score"
                )
                cl_score = cl_judges.get(judge_name, {}).get("mean_score")
                improvement = _fmt_delta(cl_score, model_score)

                lines.append(
                    f"| {scenario} | {judge_name} "
                    f"| {_fmt_pct(model_score)} "
                    f"| {_fmt_pct(cl_score)} "
                    f"| {improvement} |"
                )

            # Overall row
            model_overall = model_art.get("overall_mean")
            cl_overall = cl_art.get("overall_mean")
            overall_improvement = _fmt_delta(cl_overall, model_overall)
            model_pass = model_art.get("pass_rate")
            cl_pass = cl_art.get("pass_rate")

            lines.append(
                f"| {scenario} | **Overall** "
                f"| **{_fmt_pct(model_overall)}** "
                f"| **{_fmt_pct(cl_overall)}** "
                f"| **{overall_improvement}** |"
            )
            lines.append(
                f"| {scenario} | **Pass Rate** "
                f"| **{_fmt_pct(model_pass)}** "
                f"| **{_fmt_pct(cl_pass)}** "
                f"| |"
            )

        lines.append("")

    return "\n".join(lines)


def generate_scores_json(
    current: dict[str, dict[str, ArtifactScores | None]],
    commit_sha: str = "",
) -> dict[str, Any]:
    """Generate the current-scores.json structure for baseline storage."""
    scenarios: dict[str, Any] = {}

    for scenario, types in current.items():
        scenario_data: dict[str, Any] = {}
        for art_type, scores in types.items():
            if scores is None:
                continue
            scenario_data[art_type] = {
                "overall_mean": scores.overall_mean,
                "pass_rate": scores.pass_rate,
                "judges": {
                    name: {
                        "mean_score": js.mean_score,
                        "final_status": js.final_status,
                        "metric_count": js.metric_count,
                    }
                    for name, js in sorted(scores.judges.items())
                },
            }
        scenarios[scenario] = scenario_data

    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": commit_sha,
        "scenarios": scenarios,
    }


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Score report generator for E2E benchmarks"
    )
    parser.add_argument(
        "--artifacts-dir",
        help="Directory containing benchmark artifacts",
    )
    parser.add_argument(
        "--baseline",
        help="Path to baseline-scores.json (ClosedLoop on main)",
    )
    parser.add_argument(
        "--model-baseline",
        help="Path to model-baseline-scores.json (OOTB Claude)",
    )
    parser.add_argument(
        "--thresholds",
        help="Path to thresholds.json (optional)",
    )
    parser.add_argument(
        "--output-scores",
        help="Path to write current-scores.json",
    )
    parser.add_argument(
        "--output-markdown",
        help="Path to write markdown report",
    )
    parser.add_argument(
        "--output-status",
        help="Path to write status (PASS/WARN/FAIL)",
    )
    parser.add_argument(
        "--commit-sha",
        default="",
        help="Commit SHA for report metadata",
    )
    parser.add_argument(
        "--pr-number",
        default="",
        help="PR number for report title",
    )
    parser.add_argument(
        "--readme",
        action="store_true",
        help="Generate README table (Model vs ClosedLoop only, no artifacts needed)",
    )
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="Aggregate a single judges.json into a baseline-scores JSON",
    )
    parser.add_argument(
        "--judges-json",
        help="Path to judges.json (for --aggregate mode)",
    )
    parser.add_argument(
        "--model",
        help="Model identifier for baseline metadata (for --aggregate mode)",
    )
    parser.add_argument(
        "--description",
        help="Human-readable description (for --aggregate mode)",
    )
    parser.add_argument(
        "--scenario",
        help="Scenario name (for --aggregate mode)",
    )
    parser.add_argument(
        "--output",
        help="Output baseline-scores JSON path (for --aggregate mode)",
    )
    parser.add_argument(
        "--artifact-type",
        default="plan",
        help="Artifact type: plan or code (for --aggregate mode, default: plan)",
    )

    args = parser.parse_args()

    # Load model baseline
    model_baseline = None
    if args.model_baseline:
        model_path = Path(args.model_baseline)
        if model_path.exists():
            with open(model_path) as f:
                model_baseline = json.load(f)

    # Load ClosedLoop baseline
    cl_baseline = None
    if args.baseline:
        baseline_path = Path(args.baseline)
        if baseline_path.exists():
            with open(baseline_path) as f:
                cl_baseline = json.load(f)

    # Aggregate mode: single judges.json → baseline-scores JSON
    if args.aggregate:
        for required in ("judges_json", "model", "description", "scenario", "output"):
            if not getattr(args, required):
                print(
                    f"Error: --aggregate requires --{required.replace('_', '-')}",
                    file=sys.stderr,
                )
                return 1

        judges_path = Path(args.judges_json)
        if not judges_path.exists():
            print(f"Error: {judges_path} not found", file=sys.stderr)
            return 1

        scores = compute_scores(judges_path)
        baseline = {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
            "description": args.description,
            "scenarios": {
                args.scenario: {
                    args.artifact_type: {
                        "overall_mean": scores.overall_mean,
                        "pass_rate": scores.pass_rate,
                        "judges": {
                            name: {
                                "mean_score": js.mean_score,
                                "final_status": js.final_status,
                                "metric_count": js.metric_count,
                            }
                            for name, js in sorted(scores.judges.items())
                        },
                    }
                },
            },
        }

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(baseline, indent=2) + "\n")

        print(f"Baseline written to {output_path}")
        print(f"  Model: {args.model}")
        print(f"  Scenario: {args.scenario}")
        print(f"  Overall mean: {scores.overall_mean:.1%}")
        pass_count = sum(
            1 for j in scores.judges.values() if j.final_status == 1
        )
        print(
            f"  Pass rate: {scores.pass_rate:.1%} "
            f"({pass_count}/{len(scores.judges)})"
        )
        return 0

    # README mode: just generate Model vs ClosedLoop table
    if args.readme:
        if model_baseline is None or cl_baseline is None:
            print(
                "Error: --readme requires both --model-baseline and --baseline",
                file=sys.stderr,
            )
            return 1
        table = generate_readme_table(model_baseline, cl_baseline)
        if args.output_markdown:
            Path(args.output_markdown).write_text(table + "\n")
        else:
            print(table)
        return 0

    # Normal mode: requires artifacts
    if not args.artifacts_dir:
        print(
            "Error: --artifacts-dir is required (unless --readme)",
            file=sys.stderr,
        )
        return 1

    artifacts_dir = Path(args.artifacts_dir)
    if not artifacts_dir.is_dir():
        print(
            f"Error: artifacts directory not found: {artifacts_dir}",
            file=sys.stderr,
        )
        return 1

    # Find judge reports
    reports = find_judge_reports(artifacts_dir)
    if not reports:
        print("Error: no judge reports found", file=sys.stderr)
        return 1

    # Compute scores
    current: dict[str, dict[str, ArtifactScores | None]] = {}
    for scenario, paths in reports.items():
        current[scenario] = {}
        for art_type in ["plan", "code"]:
            report_path = paths.get(art_type)
            if report_path and report_path.exists():
                try:
                    current[scenario][art_type] = compute_scores(report_path)
                except Exception as e:
                    print(
                        f"Warning: failed to compute scores for "
                        f"{scenario}/{art_type}: {e}",
                        file=sys.stderr,
                    )
                    current[scenario][art_type] = None
            else:
                current[scenario][art_type] = None

    # Load thresholds
    thresholds: dict[str, Any] = {}
    if args.thresholds:
        thresholds_path = Path(args.thresholds)
        if thresholds_path.exists():
            with open(thresholds_path) as f:
                thresholds = json.load(f)

    # Fail if all scores are None (every compute_scores() failed or no reports found)
    all_none = all(
        scores is None
        for scenario_scores in current.values()
        for scores in scenario_scores.values()
    )
    if all_none and current:
        print(
            "Error: all benchmark score computations failed — no valid results",
            file=sys.stderr,
        )
        if args.output_status:
            Path(args.output_status).write_text("FAIL")
        return 1

    # Compare against ClosedLoop baseline
    deltas = compare_against_baseline(current, cl_baseline)
    violations = check_thresholds(current, thresholds)
    status = determine_status(deltas, violations)

    # Generate outputs
    if args.output_scores:
        scores_json = generate_scores_json(current, args.commit_sha)
        Path(args.output_scores).write_text(
            json.dumps(scores_json, indent=2) + "\n"
        )

    if args.output_markdown:
        markdown = generate_markdown(
            current,
            deltas,
            violations,
            status,
            model_baseline=model_baseline,
            cl_baseline=cl_baseline,
            commit_sha=args.commit_sha,
            pr_number=args.pr_number,
        )
        Path(args.output_markdown).write_text(markdown)

    if args.output_status:
        Path(args.output_status).write_text(status)

    # Print summary to stdout
    print(f"Status: {status}")
    for scenario in sorted(current):
        for art_type in ["plan", "code"]:
            scores = current[scenario].get(art_type)
            if scores:
                print(
                    f"  {scenario}/{art_type}: "
                    f"mean={scores.overall_mean * 100:.1f}% "
                    f"pass_rate={scores.pass_rate:.0%} "
                    f"({len(scores.judges)} judges)"
                )

    if violations:
        print(f"\nThreshold violations: {len(violations)}")
        for v in violations:
            if v.metric == "error_count" or v.metric.endswith(":missing"):
                print(f"  {v.scenario}/{v.artifact_type}: {v.metric} "
                      f"= {v.actual:.0f} (max: {v.threshold:.0f})")
            else:
                print(f"  {v.scenario}/{v.artifact_type}: {v.metric} "
                      f"= {v.actual * 100:.1f}% < {v.threshold * 100:.1f}%")

    return 1 if status == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
