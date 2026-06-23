#!/usr/bin/env python3
"""Feedback-driven coverage closure runner for AssertPilot datasets.

This runner implements a practical first version of the self-improvement loop:

1. Run Verilator simulations for correct RTL and each mutation RTL.
2. Parse assertion pass/fail and hand-written scenario coverage markers.
3. Compute proxy score from separate verification-quality signals:
   - correct RTL assertion pass
   - mutation kill rate
   - stimulus scenario coverage
   - assertion activation rate approximated by trigger scenarios
   - boundary case coverage
4. If proxy coverage is below 100%, write targeted feedback for the next
   stimulus/testbench update iteration.

The script does not call an LLM by default. Use `--generator-command` to plug in
an external generator that reads the feedback JSON and updates `sim/tb.cpp`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
DEFAULT_DATASETS_DIR = PROJECT_ROOT / "datasets"
DEFAULT_BUILD_ROOT = PROJECT_ROOT / "runs" / "coverage_closure"
DEFAULT_VERILATOR = (
    PROJECT_ROOT.parent / "verilator" / "install" / "bin" / "verilator"
)
SCORE_WEIGHTS = {
    "correct_pass": 0.25,
    "mutation_kill_rate": 0.25,
    "scenario_coverage": 0.25,
    "assertion_activation_rate": 0.15,
    "boundary_case_coverage": 0.10,
}

sys.path.insert(0, str(SCRIPTS_DIR))
from run_dataset_verilator import (  # noqa: E402
    Variant,
    load_case,
    run_simulation,
    select_variants,
)


@dataclass(frozen=True)
class CaseRun:
    case_name: str
    correct: dict
    buggy: dict
    scenario: dict
    assertion: dict
    boundary: dict
    proxy: dict


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a feedback-driven coverage closure loop over AssertPilot datasets."
    )
    parser.add_argument(
        "--datasets-dir",
        type=Path,
        default=DEFAULT_DATASETS_DIR,
        help="Path to AssertPilot datasets directory.",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        help="Dataset case to run. May be repeated. Defaults to all cases.",
    )
    parser.add_argument(
        "--max-iters",
        type=int,
        default=3,
        help="Maximum closure iterations.",
    )
    parser.add_argument(
        "--verilator",
        type=Path,
        default=DEFAULT_VERILATOR,
        help="Path to the Verilator executable.",
    )
    parser.add_argument(
        "--build-root",
        type=Path,
        default=DEFAULT_BUILD_ROOT,
        help="Directory for closure reports and Verilator build artifacts.",
    )
    parser.add_argument(
        "--generator-command",
        default=None,
        help=(
            "Optional command to run when gaps remain. The command receives "
            "ASSERTPILOT_FEEDBACK_JSON, ASSERTPILOT_DATASETS_DIR, and "
            "ASSERTPILOT_ITERATION in the environment."
        ),
    )
    parser.add_argument(
        "--stop-on-compile-error",
        action="store_true",
        help="Stop immediately if a Verilator compile/run command errors unexpectedly.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print simulation logs for each case.",
    )
    return parser.parse_args()


def iter_cases(datasets_dir: Path, selected_cases: list[str] | None) -> Iterable[Path]:
    if selected_cases:
        for case_name in selected_cases:
            case_dir = datasets_dir / case_name
            if not case_dir.exists():
                raise FileNotFoundError(f"Dataset case does not exist: {case_dir}")
            yield case_dir
        return

    for case_dir in sorted(datasets_dir.iterdir()):
        if case_dir.is_dir() and (case_dir / "signals.json").exists():
            yield case_dir


def _scenario_ids(items: list[str | dict]) -> list[str]:
    ids = []
    for item in items:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict) and "id" in item:
            ids.append(str(item["id"]))
        else:
            raise ValueError(f"Unsupported scenario entry: {item!r}")
    return ids


def load_scenarios(case_dir: Path) -> dict:
    scenario_path = case_dir / "coverage_scenarios.json"
    if not scenario_path.exists():
        return {
            "required": [],
            "bonus": [],
            "mutation_targets": [],
            "assertion_triggers": {},
            "boundary_cases": [],
        }
    data = json.loads(scenario_path.read_text(encoding="utf-8"))
    if "scenarios" in data:
        return {
            "required": _scenario_ids(data.get("scenarios", [])),
            "bonus": [],
            "mutation_targets": [],
            "assertion_triggers": {},
            "boundary_cases": [],
        }
    return {
        "required": _scenario_ids(data.get("required", [])),
        "bonus": _scenario_ids(data.get("bonus", [])),
        "mutation_targets": _scenario_ids(data.get("mutation_targets", [])),
        "assertion_triggers": data.get("assertion_triggers", {}),
        "boundary_cases": _scenario_ids(data.get("boundary_cases", [])),
    }


def coverage_rate(observed: set[str], expected: list[str]) -> float:
    return (len(observed & set(expected)) / len(expected)) if expected else 1.0


def trigger_ids(trigger: str | list[str] | dict) -> list[str]:
    if isinstance(trigger, str):
        return [trigger]
    if isinstance(trigger, list):
        return _scenario_ids(trigger)
    if isinstance(trigger, dict):
        return _scenario_ids(trigger.get("scenarios", []))
    raise ValueError(f"Unsupported assertion trigger entry: {trigger!r}")


def assertion_activation(assertion_triggers: dict, observed: set[str]) -> dict:
    activated = []
    missing = {}
    for assertion_name, trigger in assertion_triggers.items():
        triggers = trigger_ids(trigger)
        observed_triggers = sorted(observed & set(triggers))
        if observed_triggers:
            activated.append(assertion_name)
        else:
            missing[assertion_name] = triggers

    total = len(assertion_triggers)
    rate = (len(activated) / total) if total else 1.0
    return {
        "expected": assertion_triggers,
        "activated": sorted(activated),
        "missing": missing,
        "activation_rate": rate,
    }


def mutation_variants(
    case_dir: Path,
    case_config: dict,
    mutation_targets: list[str],
) -> list[Variant]:
    mutations_dir = case_dir / "rtl" / "mutations"
    if mutation_targets:
        variants = []
        for target in mutation_targets:
            mutation_rtl = mutations_dir / f"{target}.v"
            if not mutation_rtl.exists():
                raise FileNotFoundError(
                    f"Mutation target '{target}' is listed but missing: {mutation_rtl}"
                )
            variants.append(
                Variant(
                    name=target,
                    top_module=case_config["buggy_top_module"],
                    expected="fail",
                    rtl_file=str(mutation_rtl),
                )
            )
        return variants

    if mutations_dir.exists():
        return [
            Variant(
                name=mutation_rtl.stem,
                top_module=case_config["buggy_top_module"],
                expected="fail",
                rtl_file=str(mutation_rtl),
            )
            for mutation_rtl in sorted(mutations_dir.glob("*.v"))
        ]

    return [
        Variant(
            name="design_buggy",
            top_module=case_config["buggy_top_module"],
            expected="fail",
        )
    ]


def observed_scenarios(log_text: str) -> set[str]:
    observed = set()
    for line in log_text.splitlines():
        line = line.strip()
        if line.startswith("SCENARIO:"):
            observed.add(line.split(":", 1)[1].strip())
    return observed


def run_case(
    case_dir: Path,
    verilator: Path,
    build_root: Path,
    iteration: int,
    verbose: bool,
) -> CaseRun:
    case_config = load_case(case_dir)
    correct_variant = select_variants(case_config, "correct")[0]

    correct_result = run_simulation(
        verilator=verilator,
        case_dir=case_dir,
        variant=correct_variant,
        build_root=build_root / f"iter_{iteration}",
        keep_build=False,
    )

    scenarios = load_scenarios(case_dir)
    required_ids = scenarios["required"]
    bonus_ids = scenarios["bonus"]
    mutation_targets = scenarios["mutation_targets"]
    assertion_triggers = scenarios["assertion_triggers"]
    boundary_cases = scenarios["boundary_cases"]
    scenario_ids = set(required_ids + bonus_ids)
    observed = observed_scenarios(correct_result.stdout)

    assertion_pass = correct_result.returncode == 0
    mutation_run_variants = mutation_variants(case_dir, case_config, mutation_targets)
    if not mutation_targets:
        mutation_targets = [variant.name for variant in mutation_run_variants]
    mutation_runs = []
    killed_mutation_targets = set()
    for mutation_variant in mutation_run_variants:
        mutation_result = run_simulation(
            verilator=verilator,
            case_dir=case_dir,
            variant=mutation_variant,
            build_root=build_root / f"iter_{iteration}",
            keep_build=False,
        )
        killed = mutation_result.returncode != 0
        if killed:
            killed_mutation_targets.add(mutation_variant.name)
        mutation_runs.append(
            {
                "name": mutation_variant.name,
                "top_module": mutation_variant.top_module,
                "rtl_file": mutation_variant.rtl_file,
                "returncode": mutation_result.returncode,
                "killed": killed,
            }
        )

        if verbose:
            print(f"\n--- {case_dir.name}:{mutation_variant.name} log ---")
            print(mutation_result.stdout)

    if verbose:
        print(f"\n--- {case_dir.name}:correct log ---")
        print(correct_result.stdout)

    required_rate = coverage_rate(observed, required_ids)
    bonus_rate = coverage_rate(observed, bonus_ids)
    mutation_rate = coverage_rate(killed_mutation_targets, mutation_targets)
    scenario_rate = coverage_rate(observed, sorted(scenario_ids))
    assertion_activation_detail = assertion_activation(assertion_triggers, observed)
    assertion_activation_rate = assertion_activation_detail["activation_rate"]
    boundary_case_rate = coverage_rate(observed, boundary_cases)

    coverage_score = (
        SCORE_WEIGHTS["correct_pass"] * (1.0 if assertion_pass else 0.0)
        + SCORE_WEIGHTS["mutation_kill_rate"] * mutation_rate
        + SCORE_WEIGHTS["scenario_coverage"] * scenario_rate
        + SCORE_WEIGHTS["assertion_activation_rate"] * assertion_activation_rate
        + SCORE_WEIGHTS["boundary_case_coverage"] * boundary_case_rate
    )

    missing_required = sorted(set(required_ids) - observed)
    missing_bonus = sorted(set(bonus_ids) - observed)
    missing_boundary_cases = sorted(set(boundary_cases) - observed)
    unkilled_mutation_targets = sorted(set(mutation_targets) - killed_mutation_targets)

    return CaseRun(
        case_name=case_dir.name,
        correct={
            "top_module": correct_variant.top_module,
            "returncode": correct_result.returncode,
            "assertion_pass": assertion_pass,
        },
        buggy={
            "top_module": case_config["buggy_top_module"],
            "mutation_killed": mutation_rate == 1.0,
            "total_mutations": len(mutation_targets),
            "killed_mutations": len(killed_mutation_targets),
            "killed_targets": sorted(killed_mutation_targets),
            "unkilled_targets": unkilled_mutation_targets,
            "mutations": mutation_runs,
        },
        scenario={
            "required": {
                "expected": required_ids,
                "observed": sorted(observed & set(required_ids)),
                "missing": missing_required,
                "coverage": required_rate,
            },
            "bonus": {
                "expected": bonus_ids,
                "observed": sorted(observed & set(bonus_ids)),
                "missing": missing_bonus,
                "coverage": bonus_rate,
            },
            "observed": sorted(observed & scenario_ids),
            "missing": missing_required + missing_bonus,
            "scenario_coverage": scenario_rate,
        },
        assertion={
            "activation": assertion_activation_detail,
        },
        boundary={
            "expected": boundary_cases,
            "observed": sorted(observed & set(boundary_cases)),
            "missing": missing_boundary_cases,
            "coverage": boundary_case_rate,
        },
        proxy={
            "assertion_pass": 1.0 if assertion_pass else 0.0,
            "required_coverage": required_rate,
            "bonus_coverage": bonus_rate,
            "mutation_coverage": mutation_rate,
            "mutation_kill_rate": mutation_rate,
            "scenario_coverage": scenario_rate,
            "assertion_activation_rate": assertion_activation_rate,
            "boundary_case_coverage": boundary_case_rate,
            "score_weights": SCORE_WEIGHTS,
            "score": coverage_score,
            "coverage": coverage_score,
            "proxy_coverage": coverage_score,
        },
    )


def summarize_iteration(case_runs: list[CaseRun]) -> dict:
    if not case_runs:
        return {
            "score": 0.0,
            "score_weights": SCORE_WEIGHTS,
            "coverage": 0.0,
            "proxy_coverage": 0.0,
            "assertion_pass_rate": 0.0,
            "required_coverage": 0.0,
            "bonus_coverage": 0.0,
            "mutation_kill_rate": 0.0,
            "mutation_coverage": 0.0,
            "scenario_coverage": 0.0,
            "assertion_activation_rate": 0.0,
            "boundary_case_coverage": 0.0,
            "closed": False,
            "cases": [],
        }

    avg_proxy = sum(run.proxy["proxy_coverage"] for run in case_runs) / len(case_runs)
    avg_assert = sum(run.proxy["assertion_pass"] for run in case_runs) / len(case_runs)
    avg_mutation = sum(run.proxy["mutation_kill_rate"] for run in case_runs) / len(case_runs)
    avg_scenario = sum(run.proxy["scenario_coverage"] for run in case_runs) / len(case_runs)
    avg_required = sum(run.proxy["required_coverage"] for run in case_runs) / len(case_runs)
    avg_bonus = sum(run.proxy["bonus_coverage"] for run in case_runs) / len(case_runs)
    avg_activation = (
        sum(run.proxy["assertion_activation_rate"] for run in case_runs) / len(case_runs)
    )
    avg_boundary = (
        sum(run.proxy["boundary_case_coverage"] for run in case_runs) / len(case_runs)
    )
    closed = avg_proxy == 1.0 and all(run.correct["assertion_pass"] for run in case_runs)

    return {
        "score": avg_proxy,
        "score_weights": SCORE_WEIGHTS,
        "coverage": avg_proxy,
        "proxy_coverage": avg_proxy,
        "assertion_pass_rate": avg_assert,
        "required_coverage": avg_required,
        "bonus_coverage": avg_bonus,
        "mutation_kill_rate": avg_mutation,
        "mutation_coverage": avg_mutation,
        "scenario_coverage": avg_scenario,
        "assertion_activation_rate": avg_activation,
        "boundary_case_coverage": avg_boundary,
        "closed": closed,
        "cases": [
            {
                "case": run.case_name,
                "correct": run.correct,
                "buggy": run.buggy,
                "scenario": run.scenario,
                "assertion": run.assertion,
                "boundary": run.boundary,
                "proxy": run.proxy,
            }
            for run in case_runs
        ],
    }


def targeted_feedback(summary: dict) -> dict:
    targets = []
    for case in summary["cases"]:
        case_targets = []
        if not case["correct"]["assertion_pass"]:
            case_targets.append(
                "Correct RTL failed assertions; inspect assertion strength or testbench reset/stimulus timing."
            )
        if not case["buggy"]["mutation_killed"]:
            case_targets.append(
                f"Mutation set is not fully killed ({case['buggy']['killed_mutations']}/{case['buggy']['total_mutations']}); add stimulus that reaches the remaining bugs or strengthen assertions."
            )
        for target in case["buggy"].get("unkilled_targets", []):
            case_targets.append(
                f"Mutation target '{target}' was not killed; add stimulus/assertions that expose this injected bug."
            )
        for missing in case["scenario"]["required"]["missing"]:
            case_targets.append(
                f"Missing required scenario '{missing}'; drive the behavior and print SCENARIO:{missing} only after the DUT state confirms it."
            )
        for missing in case["scenario"]["bonus"]["missing"]:
            case_targets.append(
                f"Missing bonus scenario '{missing}'; add a harder stimulus phase and guard the marker with DUT state checks."
            )
        for assertion_name, triggers in case["assertion"]["activation"]["missing"].items():
            case_targets.append(
                f"Assertion '{assertion_name}' was not activated; drive one of its trigger scenarios: {', '.join(triggers)}."
            )
        for missing in case["boundary"]["missing"]:
            case_targets.append(
                f"Missing boundary case '{missing}'; add stimulus that reaches this edge condition and guard its marker with DUT state checks."
            )
        if case_targets:
            targets.append({"case": case["case"], "targets": case_targets})

    return {"targets": targets}


def write_iteration_artifacts(build_root: Path, iteration: int, summary: dict) -> Path:
    iter_dir = build_root / f"iter_{iteration}"
    iter_dir.mkdir(parents=True, exist_ok=True)
    summary_path = iter_dir / "summary.json"
    feedback_path = iter_dir / "targeted_feedback.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    feedback_path.write_text(
        json.dumps(targeted_feedback(summary), indent=2),
        encoding="utf-8",
    )
    return feedback_path


def maybe_run_generator(command: str | None, feedback_path: Path, args: argparse.Namespace, iteration: int) -> None:
    if not command:
        return
    env = os.environ.copy()
    env["ASSERTPILOT_FEEDBACK_JSON"] = str(feedback_path)
    env["ASSERTPILOT_DATASETS_DIR"] = str(args.datasets_dir)
    env["ASSERTPILOT_ITERATION"] = str(iteration)
    result = subprocess.run(
        command,
        shell=True,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    print(result.stdout)
    if result.returncode != 0:
        raise RuntimeError(f"generator command failed with return code {result.returncode}")


def main() -> int:
    args = parse_args()
    if not args.verilator.exists():
        print(f"ERROR: Verilator executable not found: {args.verilator}", file=sys.stderr)
        return 2

    args.build_root.mkdir(parents=True, exist_ok=True)

    for iteration in range(args.max_iters):
        print(f"\n=== Coverage closure iteration {iteration} ===")
        case_runs = []
        for case_dir in iter_cases(args.datasets_dir, args.cases):
            case_run = run_case(
                case_dir=case_dir,
                verilator=args.verilator,
                build_root=args.build_root,
                iteration=iteration,
                verbose=args.verbose,
            )
            case_runs.append(case_run)
            print(
                f"{case_run.case_name}: score={case_run.proxy['score']:.2%} "
                f"assertion_pass={case_run.correct['assertion_pass']} "
                f"mutations={case_run.buggy['killed_mutations']}/{case_run.buggy['total_mutations']} "
                f"scenario={case_run.proxy['scenario_coverage']:.2%} "
                f"activation={case_run.proxy['assertion_activation_rate']:.2%} "
                f"boundary={case_run.proxy['boundary_case_coverage']:.2%}"
            )

            if args.stop_on_compile_error and case_run.correct["returncode"] not in [0]:
                print(f"Stopping due to correct RTL failure in {case_run.case_name}.")
                return 1

        summary = summarize_iteration(case_runs)
        feedback_path = write_iteration_artifacts(args.build_root, iteration, summary)
        print(
            f"iteration {iteration}: score={summary['score']:.2%} "
            f"assertion_pass_rate={summary['assertion_pass_rate']:.2%} "
            f"mutation_coverage={summary['mutation_coverage']:.2%} "
            f"scenario_coverage={summary['scenario_coverage']:.2%} "
            f"assertion_activation={summary['assertion_activation_rate']:.2%} "
            f"boundary_coverage={summary['boundary_case_coverage']:.2%}"
        )
        print(f"wrote feedback: {feedback_path}")

        if summary["closed"]:
            print("coverage closure reached 100% weighted score.")
            return 0

        maybe_run_generator(args.generator_command, feedback_path, args, iteration)

    print("coverage closure stopped before reaching 100% weighted score.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
