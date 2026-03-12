"""Performance benchmark tests for the judge evaluation system.

Uses pre-computed static fixtures (PRDs, plans, judge reports, perf logs)
to establish quality and performance baselines. No LLM calls, no network,
no subprocesses -- purely deterministic tests against static fixture data.

Run:
    pytest plugins/judges/tools/python/benchmarks/ -v
    pytest plugins/judges/tools/python/benchmarks/ -v --benchmark-only
    pytest plugins/judges/tools/python/benchmarks/ -v --benchmark-min-rounds=10
"""

import json
from pathlib import Path
from typing import Any

import pytest

from validate_judge_report import (  # type: ignore[import-not-found]
    JUDGE_REGISTRY,
    EvaluationReport,
    validate_report,
)
from perf_summary import (  # type: ignore[import-not-found]
    load_events,
    summarize_agents,
    summarize_iterations,
    summarize_pipeline,
)

from helpers import (
    heuristic_token_count,
    load_json_fixture,
    load_jsonl_fixture,
    load_text_fixture,
)

pytestmark = pytest.mark.benchmark


class TestFixtureIntegrity:
    """Verify that all fixture files are well-formed and structurally valid."""

    def test_prd_is_nonempty_markdown(self, fixture_dir: Path) -> None:
        """PRD fixture must be non-empty markdown text."""
        prd = load_text_fixture(fixture_dir, "prd.md")
        assert len(prd.strip()) > 0, "PRD fixture is empty"
        assert "#" in prd, "PRD should contain markdown headings"

    def test_plan_conforms_to_schema(self, fixture_dir: Path) -> None:
        """Plan fixture must have all required plan-schema.json fields."""
        plan = load_json_fixture(fixture_dir, "plan.json")
        required_fields = [
            "content",
            "acceptanceCriteria",
            "pendingTasks",
            "completedTasks",
            "openQuestions",
            "answeredQuestions",
            "gaps",
        ]
        for field in required_fields:
            assert field in plan, f"plan.json missing required field: {field}"
        assert isinstance(plan["content"], str)
        assert len(plan["content"]) > 0, "plan.json content is empty"
        for task in plan["pendingTasks"]:
            assert "id" in task
            assert task["id"].startswith("T-"), f"Bad task ID: {task['id']}"

    def test_judges_report_validates(self, fixture_dir: Path) -> None:
        """Judge report fixture must pass validate_report() for plan category."""
        judges_path = fixture_dir / "judges.json"
        if not judges_path.exists():
            pytest.skip("judges.json fixture not present")
        valid, message = validate_report(judges_path, category="plan")
        assert valid is True, f"Judge report validation failed: {message}"

    def test_judges_report_has_all_plan_judges(
        self, fixture_dir: Path
    ) -> None:
        """Judge report must contain all plan judges."""
        judges = load_json_fixture(fixture_dir, "judges.json")
        report = EvaluationReport.model_validate(judges)
        found_ids = {case.case_id for case in report.stats}
        expected_ids = JUDGE_REGISTRY["plan"]
        missing = expected_ids - found_ids
        assert not missing, f"Missing judges in fixture: {sorted(missing)}"

    def test_perf_jsonl_loads_successfully(self, fixture_dir: Path) -> None:
        """Perf JSONL fixture must parse into valid events."""
        events = load_jsonl_fixture(fixture_dir, "perf.jsonl")
        assert len(events) > 0, "perf.jsonl is empty"
        for e in events:
            assert "event" in e, f"Perf event missing 'event' field: {e}"

    def test_perf_events_have_required_fields(
        self, fixture_dir: Path
    ) -> None:
        """Perf events must have type-appropriate fields."""
        events = load_jsonl_fixture(fixture_dir, "perf.jsonl")
        for e in events:
            event_type = e["event"]
            if event_type == "iteration":
                assert "duration_s" in e
                assert "iteration" in e
            elif event_type == "pipeline_step":
                assert "step_name" in e
                assert "duration_s" in e
            elif event_type == "agent":
                assert "agent_name" in e
                assert "duration_s" in e


class TestRuntimePerformance:
    """Benchmark validation and summarization code paths.

    Uses pytest-benchmark for statistical rigor (multiple rounds,
    mean/stddev/min/max). Run with --benchmark-min-rounds=10 for
    stable averages.
    """

    def test_judge_report_validation_speed(
        self, fixture_dir: Path, benchmark: Any
    ) -> None:
        """Benchmark validate_report() execution time."""
        judges_path = fixture_dir / "judges.json"
        if not judges_path.exists():
            pytest.skip("judges.json fixture not present")

        result = benchmark(validate_report, judges_path, "plan")
        assert result[0] is True  # valid

    def test_perf_load_and_summarize_speed(
        self, fixture_dir: Path, benchmark: Any
    ) -> None:
        """Benchmark load_events + summarize_* execution time."""
        perf_path = fixture_dir / "perf.jsonl"
        if not perf_path.exists():
            pytest.skip("perf.jsonl fixture not present")

        def run_summarization() -> dict[str, int]:
            events = load_events(perf_path)
            iters = summarize_iterations(events)
            pipeline = summarize_pipeline(events)
            agents = summarize_agents(events)
            return {
                "iterations": len(iters),
                "pipeline_steps": len(pipeline),
                "agents": len(agents),
            }

        result = benchmark(run_summarization)
        assert result["iterations"] >= 0

    def test_full_analysis_speed(
        self, fixture_dir: Path, benchmark: Any
    ) -> None:
        """Benchmark full fixture analysis pipeline."""
        judges_path = fixture_dir / "judges.json"
        perf_path = fixture_dir / "perf.jsonl"
        if not judges_path.exists() or not perf_path.exists():
            pytest.skip("judges.json or perf.jsonl fixture not present")

        def run_full_analysis() -> None:
            validate_report(judges_path, category="plan")
            load_json_fixture(fixture_dir, "plan.json")
            events = load_events(perf_path)
            summarize_iterations(events)
            summarize_pipeline(events)
            summarize_agents(events)
            prd = load_text_fixture(fixture_dir, "prd.md")
            heuristic_token_count(prd)

        benchmark(run_full_analysis)


class TestTokenUsage:
    """Assert that artifact sizes (estimated tokens) are within bounds."""

    def test_prd_token_count_in_range(
        self, fixture_dir: Path, thresholds: dict[str, Any]
    ) -> None:
        """PRD token count must be within min/max bounds."""
        prd = load_text_fixture(fixture_dir, "prd.md")
        tokens = heuristic_token_count(prd)
        t = thresholds["tokens"]

        assert tokens >= t["prd_min"], (
            f"PRD too small: {tokens} tokens < {t['prd_min']} minimum"
        )
        assert tokens <= t["prd_max"], (
            f"PRD too large: {tokens} tokens > {t['prd_max']} maximum"
        )

    def test_plan_content_token_count_in_range(
        self, fixture_dir: Path, thresholds: dict[str, Any]
    ) -> None:
        """Plan content (markdown) token count within bounds."""
        plan = load_json_fixture(fixture_dir, "plan.json")
        content_tokens = heuristic_token_count(plan["content"])
        t = thresholds["tokens"]

        assert content_tokens >= t["plan_content_min"], (
            f"Plan content too small: {content_tokens} tokens"
        )
        assert content_tokens <= t["plan_content_max"], (
            f"Plan content too large: {content_tokens} tokens"
        )

    def test_plan_json_total_token_count_in_range(
        self, fixture_dir: Path, thresholds: dict[str, Any]
    ) -> None:
        """Full plan.json serialized size within bounds."""
        plan = load_json_fixture(fixture_dir, "plan.json")
        json_str = json.dumps(plan)
        total_tokens = heuristic_token_count(json_str)
        t = thresholds["tokens"]

        assert total_tokens >= t["plan_json_min"], (
            f"Plan JSON too small: {total_tokens} tokens"
        )
        assert total_tokens <= t["plan_json_max"], (
            f"Plan JSON too large: {total_tokens} tokens"
        )


class TestArtifactQuality:
    """Assert that judge scores from fixtures meet quality thresholds."""

    def test_no_judge_errors(
        self, fixture_dir: Path, thresholds: dict[str, Any]
    ) -> None:
        """No judge should have final_status=3 (error)."""
        judges = load_json_fixture(fixture_dir, "judges.json")
        report = EvaluationReport.model_validate(judges)
        max_errors = thresholds["quality"]["max_error_count"]

        error_judges = [
            c.case_id for c in report.stats if c.final_status == 3
        ]
        assert len(error_judges) <= max_errors, (
            f"Too many judge errors: {error_judges}"
        )

    def test_overall_pass_rate(
        self, fixture_dir: Path, thresholds: dict[str, Any]
    ) -> None:
        """Overall judge pass rate (status=1) meets minimum."""
        judges = load_json_fixture(fixture_dir, "judges.json")
        report = EvaluationReport.model_validate(judges)
        min_rate = thresholds["quality"]["min_pass_rate"]

        pass_count = sum(1 for c in report.stats if c.final_status == 1)
        total = len(report.stats)
        pass_rate = pass_count / total if total > 0 else 0.0

        assert pass_rate >= min_rate, (
            f"Pass rate {pass_rate:.2f} < minimum {min_rate:.2f}. "
            f"Failing judges: "
            f"{[c.case_id for c in report.stats if c.final_status != 1]}"
        )

    def test_overall_mean_score(
        self, fixture_dir: Path, thresholds: dict[str, Any]
    ) -> None:
        """Mean score across all judges meets minimum."""
        judges = load_json_fixture(fixture_dir, "judges.json")
        report = EvaluationReport.model_validate(judges)
        min_mean = thresholds["quality"]["min_mean_score"]

        all_scores: list[float] = []
        for case in report.stats:
            for metric in case.metrics:
                all_scores.append(metric.score)

        mean_score = (
            sum(all_scores) / len(all_scores) if all_scores else 0.0
        )
        assert mean_score >= min_mean, (
            f"Mean score {mean_score:.3f} < minimum {min_mean}"
        )

    def test_per_judge_minimum_scores(
        self, fixture_dir: Path, thresholds: dict[str, Any]
    ) -> None:
        """Each judge's mean metric score meets its per-judge minimum."""
        judges = load_json_fixture(fixture_dir, "judges.json")
        report = EvaluationReport.model_validate(judges)
        per_judge_mins = thresholds["quality"].get("per_judge_min_score", {})

        failures: list[str] = []
        for case in report.stats:
            min_score = per_judge_mins.get(case.case_id, 0.0)
            if not case.metrics:
                continue
            mean = sum(m.score for m in case.metrics) / len(case.metrics)
            if mean < min_score:
                failures.append(
                    f"{case.case_id}: mean={mean:.3f} < min={min_score}"
                )

        assert not failures, (
            "Judges below minimum scores:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )


class TestPerfEventAnalysis:
    """Assert perf.jsonl event summaries match expected patterns."""

    def test_iteration_count(
        self, fixture_dir: Path, thresholds: dict[str, Any]
    ) -> None:
        """Iteration count meets minimum."""
        events = load_jsonl_fixture(fixture_dir, "perf.jsonl")
        min_count = thresholds["perf_events"]["min_iteration_count"]

        iters = summarize_iterations(events)
        has_summary = iters and iters[-1].get("iteration") == "summary"
        iter_count = max(0, len(iters) - 1) if has_summary else len(iters)
        assert iter_count >= min_count, (
            f"Only {iter_count} iterations, need >= {min_count}"
        )

    def test_total_duration_within_bounds(
        self, fixture_dir: Path, thresholds: dict[str, Any]
    ) -> None:
        """Total run duration from perf events within maximum."""
        events = load_jsonl_fixture(fixture_dir, "perf.jsonl")
        max_dur = thresholds["perf_events"]["max_total_duration_s"]

        iters = summarize_iterations(events)
        if iters and iters[-1].get("iteration") == "summary":
            total = float(iters[-1].get("total_s", 0))
        else:
            total = sum(
                float(e.get("duration_s", 0))
                for e in events
                if e.get("event") == "iteration"
            )

        assert total <= max_dur, (
            f"Total duration {total:.1f}s > max {max_dur}s"
        )

    def test_agent_diversity(
        self, fixture_dir: Path, thresholds: dict[str, Any]
    ) -> None:
        """Enough distinct agents appeared in the run."""
        events = load_jsonl_fixture(fixture_dir, "perf.jsonl")
        min_agents = thresholds["perf_events"]["min_agent_count"]

        agents = summarize_agents(events)
        assert len(agents) >= min_agents, (
            f"Only {len(agents)} agents, need >= {min_agents}. "
            f"Found: {[a.get('agent_name') for a in agents]}"
        )

    def test_pipeline_steps_present(self, fixture_dir: Path) -> None:
        """At least one pipeline step event exists."""
        events = load_jsonl_fixture(fixture_dir, "perf.jsonl")
        pipeline = summarize_pipeline(events)
        assert len(pipeline) > 0, "No pipeline step events found"
