"""Stage markers, and the grouped failure summary.

Markers come from the file's directory rather than a decorator, because a
hand-applied marker drifts the first time someone renames a file.

The summary exists because a participant staring at 60 unsorted failures learns
nothing; grouped by stage with the assignment underneath, the next thing to do
is at the bottom of the run instead of buried in it.

The numbers below are the SIX HOMEWORK STAGES from the README, not the build
spec's construction order. A participant never sees the latter, so if these ever
drift someone reading "Stage 2 -- async and backpressure" will be told stage 3
is failing. test_layout.py pins the mapping.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Which stage each participant-facing file belongs to. These are the ten red
# files plus test_breaker.py, which ships green as the worked
# example. A file here that is not on disk, or on disk and not here, is a bug
# in the repo rather than in a participant's work -- test_layout.py checks both.
STAGE_OF_FILE: dict[str, int] = {
    # Stage 1 -- structured output: text in, a validated TriageResult out.
    "test_extract.py": 1,
    # Stage 2 -- async and backpressure.
    "test_concurrency.py": 2,
    # Stage 3 -- streaming.
    "test_stream.py": 3,
    # Stage 4 -- testing a non-deterministic dependency.
    "test_properties.py": 4,
    "test_golden.py": 4,
    # Stage 5 -- surviving a bad provider.
    "test_retry.py": 5,
    "test_idempotency.py": 5,
    "test_timeout.py": 5,
    "test_budget.py": 5,
    # Stage 6 -- degrade and report. Ships GREEN: breaker.py is given complete,
    # and the work is wiring it in and writing REPORT.md, which no test asserts.
    # Listed here so the file is mapped and test_layout.py stays happy.
    "test_breaker.py": 6,
    # Stage 7 -- a second provider through the same interface.
    "test_openai_port.py": 7,
}

STAGE_GOALS: dict[int, str] = {
    1: "Implement extract() in app/providers/anthropic_p.py -- one call, a validated result",
    2: "Implement ConcurrencyLimiter and RateLimiter in app/reliability/limits.py",
    3: "Implement stream() in app/providers/anthropic_p.py -- never a partial object",
    4: "Write tests/stages/test_properties.py, then commit a threshold in samples/golden.yaml -- four properties stay red until stage 5",
    5: "Implement reliability/{retry,idempotency,limits}.py -- retry, result cache, spend ceiling",
    6: "Check in /stats that an open breaker stops upstream calls, and write REPORT.md (no test asserts this one)",
    7: "Implement app/providers/openai_p.py -- same Protocol, no change above the adapter",
}

STAGE_TITLES: dict[int, str] = {
    1: "Structured output",
    2: "Async and backpressure",
    3: "Streaming",
    4: "Testing a non-deterministic dependency",
    5: "Surviving a bad provider",
    6: "Degrade and report",
    7: "A second provider",
}


def pytest_collection_modifyitems(config, items):
    """Mark every test from its location. No hand-maintained lists of tests."""
    for item in items:
        path = Path(str(item.fspath))
        parent = path.parent.name
        if parent == "stages":
            stage = STAGE_OF_FILE.get(path.name)
            if stage is not None:
                item.add_marker(pytest.mark.stage(stage))
        elif parent == "support":
            item.add_marker(pytest.mark.support)


def _stage_of(item) -> int | None:
    marker = item.get_closest_marker("stage")
    if marker and marker.args:
        return marker.args[0]
    return None


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Group failures by stage so the next thing to do is obvious."""
    failed = terminalreporter.stats.get("failed", []) + terminalreporter.stats.get("error", [])
    if not failed:
        return

    by_stage: dict[int | None, list] = {}
    homework: list = []
    for report in failed:
        # nodeid looks like tests/stages/test_retry.py::test_name
        filename = Path(report.nodeid.split("::")[0]).name
        parent = Path(report.nodeid.split("::")[0]).parent.name
        if parent == "homework":
            homework.append(report)
            continue
        stage = STAGE_OF_FILE.get(filename) if parent == "stages" else None
        by_stage.setdefault(stage, []).append(report)

    writer = terminalreporter
    writer.write_sep("=", "failures by stage", bold=True)

    for stage in sorted((s for s in by_stage if s is not None)):
        reports = by_stage[stage]
        title = STAGE_TITLES.get(stage, "")
        writer.write_line("")
        writer.write_line(
            f"  stage {stage} ({title}): {len(reports)} failing", bold=True, red=True
        )
        goal = STAGE_GOALS.get(stage)
        if goal:
            writer.write_line(f"    -> {goal}")
        files: dict[str, int] = {}
        for report in reports:
            files[Path(report.nodeid.split("::")[0]).name] = (
                files.get(Path(report.nodeid.split("::")[0]).name, 0) + 1
            )
        for name, count in sorted(files.items()):
            writer.write_line(f"       {name}: {count}")

    if homework:
        writer.write_line("")
        writer.write_line(f"  homework: {len(homework)} failing", bold=True, red=True)
        writer.write_line("    -> HOMEWORK.md describes each task. Red here is the assignment.")
        files = {}
        for report in homework:
            name = Path(report.nodeid.split("::")[0]).name
            files[name] = files.get(name, 0) + 1
        for name, count in sorted(files.items()):
            writer.write_line(f"       {name}: {count}")

    if None in by_stage:
        reports = by_stage[None]
        writer.write_line("")
        writer.write_line(
            f"  support: {len(reports)} failing", bold=True, yellow=True
        )
        writer.write_line(
            "    -> NOT part of the assignment. These check the repo itself "
            "(HTTP surface, /stats accuracy,"
        )
        writer.write_line(
            "       the no-silencing rule). A red one here usually means a "
            "genuine regression, not homework."
        )
        files = {}
        for report in reports:
            name = Path(report.nodeid.split("::")[0]).name
            files[name] = files.get(name, 0) + 1
        for name, count in sorted(files.items()):
            writer.write_line(f"       {name}: {count}")

    writer.write_line("")
