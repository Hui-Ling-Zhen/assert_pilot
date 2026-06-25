#!/usr/bin/env python3
"""Select the next AssertPilot curriculum task from trajectory history.

The scheduler is intentionally deterministic. It consumes dataset metadata and
agent trajectories, then emits a single next_task JSON that can be passed to
`run_assertion_agent.py --task-json`.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASETS_DIR = PROJECT_ROOT / "datasets"
DEFAULT_RUNS_ROOT = PROJECT_ROOT / "runs" / "agent_tools"
DEFAULT_CONFIG = DEFAULT_DATASETS_DIR / "curriculum_scheduler.json"


@dataclass(frozen=True)
class CandidateTask:
    case: str
    stage: str
    curriculum_level: int
    focus_issue: str
    focus_target: str | None
    required_metric_improvement: str
    priority: float
    reason: str
    source_trajectory: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select the next AssertPilot curriculum task.")
    parser.add_argument("--datasets-dir", type=Path, default=DEFAULT_DATASETS_DIR)
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path, default=DEFAULT_RUNS_ROOT / "next_task.json")
    parser.add_argument(
        "--history-root",
        type=Path,
        help="Optional subdirectory under runs-root to prioritize when reading trajectories.",
    )
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_cases(datasets_dir: Path) -> list[str]:
    return sorted(
        path.name
        for path in datasets_dir.iterdir()
        if path.is_dir() and (path / "signals.json").exists()
    )


def trajectory_paths(runs_root: Path, history_root: Path | None) -> list[Path]:
    search_root = history_root or runs_root
    if not search_root.exists():
        return []
    return sorted(search_root.glob("**/trajectory.json"), key=lambda path: path.stat().st_mtime)


def latest_iteration(trajectory: dict[str, Any]) -> dict[str, Any] | None:
    iterations = trajectory.get("iterations", [])
    return iterations[-1] if iterations else None


def load_trajectory_history(runs_root: Path, history_root: Path | None) -> list[dict[str, Any]]:
    history = []
    for path in trajectory_paths(runs_root, history_root):
        try:
            trajectory = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        iteration = latest_iteration(trajectory)
        if not iteration:
            continue
        history.append(
            {
                "path": str(path),
                "case": trajectory.get("case"),
                "trajectory": trajectory,
                "iteration": iteration,
            }
        )
    return history


def issue_stage(issue: dict[str, Any], config: dict[str, Any]) -> str:
    target = str(issue.get("target") or "")
    bug_stage_map = config.get("bug_stage_map", {})
    for token, stage in bug_stage_map.items():
        if token in target:
            return stage
    return config.get("issue_stage_map", {}).get(issue.get("issue_type"), "level_5")


def intent_stage(intent: dict[str, Any], config: dict[str, Any]) -> str:
    for target in intent.get("target_mutations", []):
        stage = issue_stage({"target": target}, config)
        if stage:
            return stage
    intent_type = intent.get("intent_type")
    if intent_type == "repair_false_positive_assertion":
        return "level_1"
    if intent_type == "activate_assertion_trigger_with_plan_and_stimulus":
        return "level_2"
    if intent_type == "add_boundary_testplan_and_stimulus":
        return "level_3"
    if intent_type == "expose_or_kill_mutation":
        return "level_4"
    return "level_5"


def metric_gap(metric: str, iteration: dict[str, Any]) -> float:
    metrics = iteration.get("acceptance_metrics", {})
    if metric == "required_coverage":
        value = metrics.get("new_required_coverage", metrics.get("old_required_coverage", 0.0))
    elif metric == "assertion_activation_rate":
        value = metrics.get(
            "new_assertion_activation_rate",
            metrics.get("old_assertion_activation_rate", 0.0),
        )
    elif metric in {"boundary_case_coverage", "boundary_coverage"}:
        value = metrics.get(
            "new_boundary_case_coverage",
            metrics.get("old_boundary_case_coverage", 0.0),
        )
    elif metric in {"mutation_coverage", "mutation_kill_rate"}:
        value = metrics.get("new_mutation_coverage", metrics.get("old_mutation_coverage", 0.0))
    else:
        value = metrics.get("new_score", metrics.get("old_score", 0.0))
    return max(0.0, 1.0 - float(value or 0.0))


def recent_cases(history: list[dict[str, Any]], window: int) -> list[str]:
    return [item.get("case") for item in history[-window:] if item.get("case")]


def recent_families(history: list[dict[str, Any]], config: dict[str, Any], window: int) -> list[str]:
    families = config.get("case_families", {})
    return [families.get(case, "unknown") for case in recent_cases(history, window)]


def repeated_failure_count(
    history: list[dict[str, Any]],
    case: str,
    focus_target: str | None,
    stage: str,
) -> int:
    count = 0
    for item in reversed(history):
        if item.get("case") != case:
            continue
        iteration = item["iteration"]
        if iteration.get("decision") != "rejected":
            break
        intents = iteration.get("repair_intents", [])
        if not intents:
            continue
        first = intents[0]
        target = (
            first.get("target_scenario")
            or first.get("target_mutation")
            or first.get("target_assertion")
        )
        if target == focus_target and intent_stage(first, {"bug_stage_map": {}}) == stage:
            count += 1
        elif target == focus_target:
            count += 1
    return count


def stage_info(config: dict[str, Any], stage: str) -> dict[str, Any]:
    return config.get("stages", {}).get(stage, config.get("stages", {}).get("level_5", {}))


def make_candidate_from_issue(
    issue: dict[str, Any],
    item: dict[str, Any],
    config: dict[str, Any],
    history: list[dict[str, Any]],
) -> CandidateTask:
    stage = issue_stage(issue, config)
    info = stage_info(config, stage)
    metric = info.get("required_metric_improvement", "score")
    severity = config.get("severity_weights", {}).get(issue.get("severity"), 1.0)
    gap_weight = config.get("metric_gap_weights", {}).get(metric, 1.0)
    gap = metric_gap(metric, item["iteration"])
    case = str(issue.get("case") or item.get("case"))
    target = issue.get("related_scenario") or issue.get("target")
    priority = severity + gap_weight * gap
    priority += diversity_adjustment(case, config, history)
    priority -= retry_penalty(case, target, stage, config, history)
    return CandidateTask(
        case=case,
        stage=stage,
        curriculum_level=int(info.get("curriculum_level", 5)),
        focus_issue=str(issue.get("issue_type")),
        focus_target=str(target) if target else None,
        required_metric_improvement=metric,
        priority=priority,
        reason=str(issue.get("evidence", [issue.get("target")])[0]),
        source_trajectory=item.get("path"),
    )


def make_candidate_from_rejection(
    item: dict[str, Any],
    config: dict[str, Any],
    history: list[dict[str, Any]],
) -> CandidateTask | None:
    iteration = item["iteration"]
    if iteration.get("decision") != "rejected":
        return None
    checks = iteration.get("candidate", {}).get("acceptance_checks", {})
    failed_checks = [name for name, passed in checks.items() if not passed]
    if not failed_checks:
        return None
    first_intent = (iteration.get("repair_intents") or [{}])[0]
    case = str(item.get("case"))
    target = (
        first_intent.get("target_mutation")
        or (first_intent.get("target_mutations") or [None])[0]
        or first_intent.get("target_scenario")
        or first_intent.get("target_assertion")
    )
    if "mutation_coverage_improves" in failed_checks:
        stage = "level_4"
        metric = "mutation_coverage"
        focus_issue = "weak_assertion_or_missing_stimulus"
    elif "assertion_activation_improves" in failed_checks:
        stage = "level_2"
        metric = "assertion_activation_rate"
        focus_issue = "unreachable_or_unstimulated_assertion"
    elif "boundary_case_coverage_improves" in failed_checks:
        stage = "level_3"
        metric = "boundary_case_coverage"
        focus_issue = "missing_boundary_stimulus"
    else:
        stage = "level_5"
        metric = "score"
        focus_issue = "generalization_failure"
    info = stage_info(config, stage)
    priority = 5.0 + config.get("metric_gap_weights", {}).get(metric, 1.0) * metric_gap(metric, iteration)
    priority += diversity_adjustment(case, config, history)
    priority -= retry_penalty(case, target, stage, config, history)
    return CandidateTask(
        case=case,
        stage=stage,
        curriculum_level=int(info.get("curriculum_level", 5)),
        focus_issue=focus_issue,
        focus_target=str(target) if target else None,
        required_metric_improvement=metric,
        priority=priority,
        reason=str(iteration.get("reject_reason") or "previous candidate was rejected"),
        source_trajectory=item.get("path"),
    )


def diversity_adjustment(case: str, config: dict[str, Any], history: list[dict[str, Any]]) -> float:
    diversity = config.get("diversity", {})
    window = int(diversity.get("recent_window", 4))
    recent = recent_cases(history, window)
    families = config.get("case_families", {})
    recent_family_names = recent_families(history, config, window)
    bonus = 0.0
    if case not in recent:
        bonus += float(diversity.get("case_diversity_bonus", 0.0))
    if families.get(case, "unknown") not in recent_family_names:
        bonus += float(diversity.get("family_diversity_bonus", 0.0))
    return bonus


def retry_penalty(
    case: str,
    focus_target: str | None,
    stage: str,
    config: dict[str, Any],
    history: list[dict[str, Any]],
) -> float:
    diversity = config.get("diversity", {})
    recent = recent_cases(history, 1)
    penalty = float(diversity.get("recent_retry_penalty", 0.0)) if recent == [case] else 0.0
    penalty += repeated_failure_count(history, case, focus_target, stage) * float(
        diversity.get("repeated_failure_penalty", 0.0)
    )
    return penalty


def baseline_candidates(
    cases: list[str],
    config: dict[str, Any],
    history: list[dict[str, Any]],
) -> list[CandidateTask]:
    candidates = []
    for case in cases:
        info = stage_info(config, "level_1")
        priority = 1.0 + diversity_adjustment(case, config, history)
        candidates.append(
            CandidateTask(
                case=case,
                stage="level_1",
                curriculum_level=int(info.get("curriculum_level", 1)),
                focus_issue="bootstrap",
                focus_target=None,
                required_metric_improvement=info.get("required_metric_improvement", "required_coverage"),
                priority=priority,
                reason="No trajectory history found for this case; start with syntax/reset curriculum.",
                source_trajectory=None,
            )
        )
    return candidates


def select_next_task(
    datasets_dir: Path,
    runs_root: Path,
    config: dict[str, Any],
    history_root: Path | None,
) -> dict[str, Any]:
    cases = iter_cases(datasets_dir)
    history = load_trajectory_history(runs_root, history_root)
    candidates: list[CandidateTask] = []
    seen_cases = {item.get("case") for item in history}
    candidates.extend(baseline_candidates([case for case in cases if case not in seen_cases], config, history))

    for item in history:
        rejection_candidate = make_candidate_from_rejection(item, config, history)
        if rejection_candidate:
            candidates.append(rejection_candidate)
        diagnosis = item["iteration"].get("diagnosis", {})
        for issue in diagnosis.get("issues", []):
            candidates.append(make_candidate_from_issue(issue, item, config, history))

    if not candidates:
        candidates.extend(baseline_candidates(cases, config, history))

    candidates = sorted(candidates, key=lambda task: (-task.priority, task.case, task.focus_target or ""))
    selected = candidates[0]
    return {
        "next_task": {
            "case": selected.case,
            "curriculum_level": selected.curriculum_level,
            "stage": selected.stage,
            "stage_name": stage_info(config, selected.stage).get("name", selected.stage),
            "focus_issue": selected.focus_issue,
            "focus_target": selected.focus_target,
            "required_metric_improvement": selected.required_metric_improvement,
            "priority": round(selected.priority, 4),
            "reason": selected.reason,
            "source_trajectory": selected.source_trajectory,
        },
        "candidate_count": len(candidates),
        "top_candidates": [
            {
                "case": task.case,
                "stage": task.stage,
                "focus_issue": task.focus_issue,
                "focus_target": task.focus_target,
                "priority": round(task.priority, 4),
                "reason": task.reason,
            }
            for task in candidates[:8]
        ],
    }


def main() -> int:
    args = parse_args()
    config = read_json(args.config)
    output = select_next_task(args.datasets_dir, args.runs_root, config, args.history_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
