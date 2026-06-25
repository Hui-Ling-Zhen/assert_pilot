#!/usr/bin/env python3
"""Minimal assertion-generation agent loop scaffold.

This runner is intentionally lightweight: it demonstrates how an agent runtime
can use AssertPilot's tool wrappers, store trajectories, and decide the next
repair action from structured feedback. It does not call an LLM yet; a Hermes
or custom runtime can replace `choose_repair_actions` with a model/tool policy.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from assertpilot_tools import (
    DEFAULT_CLOSURE_BUILD_ROOT,
    DEFAULT_DATASETS_DIR,
    DEFAULT_RUNS_DIR,
    DEFAULT_VERILATOR,
    apply_repair,
    call_llm_json,
    generate_sva,
    generate_testplan,
    parse_feedback,
    repair_sva,
    repair_testbench,
    run_coverage_closure,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a lightweight AssertPilot agent loop.")
    parser.add_argument("--case", required=True, help="Dataset case to optimize.")
    parser.add_argument("--datasets-dir", type=Path, default=DEFAULT_DATASETS_DIR)
    parser.add_argument("--verilator", type=Path, default=DEFAULT_VERILATOR)
    parser.add_argument("--target-score", type=float, default=1.0)
    parser.add_argument("--max-iters", type=int, default=3)
    parser.add_argument(
        "--curriculum-level",
        type=int,
        choices=[1, 2, 3],
        default=3,
        help="Curriculum level to prioritize when choosing built-in repairs.",
    )
    parser.add_argument(
        "--llm-command",
        default=None,
        help="External command used by generate_sva and repair_sva. Falls back to ASSERTPILOT_LLM_COMMAND.",
    )
    parser.add_argument(
        "--policy-command",
        default=None,
        help="External command used to choose repair actions from trajectory and feedback.",
    )
    parser.add_argument(
        "--allow-scaffold",
        action="store_true",
        help="Allow generate_sva TODO scaffold fallback when no LLM command is configured.",
    )
    parser.add_argument(
        "--allow-repair-plan",
        action="store_true",
        help="Allow repair_sva plan fallback when no LLM command is configured.",
    )
    parser.add_argument(
        "--run-root",
        type=Path,
        default=DEFAULT_RUNS_DIR / "assertion_agent",
        help="Directory for trajectory and repair plan artifacts.",
    )
    parser.add_argument(
        "--closure-build-root",
        type=Path,
        default=DEFAULT_CLOSURE_BUILD_ROOT,
        help="Directory for coverage closure artifacts.",
    )
    return parser.parse_args()


def deterministic_repair_actions(parsed_feedback: dict) -> list[str]:
    """Pick coarse repair actions from parsed feedback.

    This is the future policy hook. Today it is deterministic and conservative:
    generate both SVA and testbench repair plans when the feedback contains both
    assertion/mutation and stimulus/boundary gaps.
    """
    action_types = {
        action["action_type"]
        for action in parsed_feedback.get("recommended_actions", [])
    }
    chosen = []
    if any("sva" in action_type or "assertion" in action_type for action_type in action_types):
        chosen.append("repair_sva")
    if any("testbench" in action_type or "trigger" in action_type for action_type in action_types):
        chosen.append("repair_testbench")
    if not chosen and action_types:
        chosen.append("inspect")
    return chosen


def choose_repair_actions(
    parsed_feedback: dict,
    trajectory: dict,
    policy_command: str | None,
) -> list[str]:
    """Choose repair actions with an LLM/agent policy when configured."""
    if not policy_command:
        return deterministic_repair_actions(parsed_feedback)

    response = call_llm_json(
        policy_command,
        "choose_repair_actions",
        {
            "task": "choose_repair_actions",
            "instructions": [
                "Choose the next AssertPilot repair actions.",
                "Allowed actions: repair_sva, repair_testbench, inspect, stop.",
                "Prefer repair_testbench for missing scenarios, inactive triggers, or boundary gaps.",
                "Prefer repair_sva when mutation gaps indicate weak assertions and correct RTL still passes.",
                "Return JSON: {\"actions\": [\"repair_sva\", \"repair_testbench\"], \"rationale\": \"...\"}",
            ],
            "trajectory": trajectory,
            "feedback": parsed_feedback,
        },
    )
    actions = response.get("actions", [])
    if not isinstance(actions, list):
        raise RuntimeError("Policy command must return JSON with an actions list.")
    allowed = {"repair_sva", "repair_testbench", "inspect", "stop"}
    normalized = [str(action) for action in actions if str(action) in allowed]
    return normalized or deterministic_repair_actions(parsed_feedback)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_curriculum(datasets_dir: Path, level: int) -> dict:
    curriculum_path = datasets_dir / "curriculum_levels.json"
    if not curriculum_path.exists():
        return {"name": f"Level {level}", "targets": [], "metrics": []}
    data = json.loads(curriculum_path.read_text(encoding="utf-8"))
    return data.get(f"level_{level}", {"name": f"Level {level}", "targets": [], "metrics": []})


def repair_json_has_patches(path: Path | None) -> bool:
    if not path or not path.exists():
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    repairs = payload.get("repairs")
    return isinstance(repairs, list) and len(repairs) > 0


def basic_testbench_repairs(case: str, parsed_feedback: dict, level_targets: set[str]) -> list[dict]:
    """Create minimal structured testbench repairs for known dataset gaps.

    This is the built-in baseline policy for a runnable agent loop. It is small
    on purpose: external LLM policies can produce richer patches through the
    same repair schema.
    """
    messages = [
        action.get("message", "")
        for action in parsed_feedback.get("recommended_actions", [])
    ]
    repairs = []

    def should_repair(target: str) -> bool:
        return target in level_targets and any(target in message for message in messages)

    if case == "fifo" and should_repair("fifo_read_from_empty"):
        repairs.append(
            {
                "target_file": "sim/tb.cpp",
                "repair_type": "insert_stimulus",
                "anchor": "    top->rst = 0;",
                "code": (
                    "    // Agent repair: exercise read-from-empty boundary before filling.\n"
                    "    top->wr_en = 0;\n"
                    "    top->rd_en = 1;\n"
                    "    tick(top);\n"
                    "    if (top->empty && top->count == 0) {\n"
                    "        std::cout << \"SCENARIO:fifo_read_from_empty\" << std::endl;\n"
                    "    }\n"
                    "    top->rd_en = 0;"
                ),
                "expected_markers": ["fifo_read_from_empty"],
                "rationale": (
                    "The feedback reports missing fifo_read_from_empty and an unkilled "
                    "underflow mutation; this stimulus reaches that boundary condition."
                ),
            }
        )

    if case == "counter" and should_repair("counter_disabled_hold"):
        repairs.append(
            {
                "target_file": "sim/tb.cpp",
                "repair_type": "insert_stimulus",
                "anchor": "    for (int i = 0; i < 20; ++i) {",
                "code": (
                    "    // Agent repair: exercise disabled counter hold before wrap loop.\n"
                    "    unsigned char hold_count = top->count;\n"
                    "    top->en = 0;\n"
                    "    tick(top);\n"
                    "    if (top->count == hold_count && top->wrap == 0) {\n"
                    "        std::cout << \"SCENARIO:counter_disabled_hold\" << std::endl;\n"
                    "    }\n"
                    "    top->en = 1;"
                ),
                "expected_markers": ["counter_disabled_hold"],
                "rationale": (
                    "The feedback reports missing counter_disabled_hold and an unkilled "
                    "disabled-increment mutation; this stimulus verifies hold behavior."
                ),
            }
        )

    if case == "arbiter" and should_repair("arbiter_no_req_idle"):
        repairs.append(
            {
                "target_file": "sim/tb.cpp",
                "repair_type": "insert_stimulus",
                "anchor": "    tick(top);\n\n    delete top;",
                "code": (
                    "    // Agent repair: exercise idle no-request behavior after required scenarios.\n"
                    "    top->req = 0;\n"
                    "    tick(top);\n"
                    "    if (top->grant == 0) {\n"
                    "        std::cout << \"SCENARIO:arbiter_no_req_idle\" << std::endl;\n"
                    "    }"
                ),
                "expected_markers": ["arbiter_no_req_idle"],
                "rationale": (
                    "The feedback reports missing arbiter_no_req_idle and an unkilled "
                    "grant-without-request mutation; this stimulus checks idle grants."
                ),
            }
        )

    if case == "handshake" and should_repair("handshake_data_changes_under_stall"):
        repairs.append(
            {
                "target_file": "sim/tb.cpp",
                "repair_type": "insert_stimulus",
                "anchor": "    tick(top);\n\n    top->ready_i = 1;",
                "code": (
                    "    // Agent repair: perturb input data while output is stalled.\n"
                    "    top->data_i = 0x3C;\n"
                    "    tick(top);\n"
                    "    if (top->valid_o && top->data_o == 0xA5 && !top->ready_o) {\n"
                    "        std::cout << \"SCENARIO:handshake_data_changes_under_stall\" << std::endl;\n"
                    "    }"
                ),
                "expected_markers": ["handshake_data_changes_under_stall"],
                "rationale": (
                    "The feedback reports missing handshake_data_changes_under_stall and an "
                    "unkilled data-change-under-stall mutation; this stimulus checks data stability."
                ),
            }
        )

    return repairs


def prepare_candidate_case(
    active_datasets_dir: Path,
    case: str,
    candidate_root: Path,
) -> Path:
    candidate_case = candidate_root / case
    if candidate_case.exists():
        shutil.rmtree(candidate_case)
    candidate_case.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(active_datasets_dir / case, candidate_case)
    return candidate_case


def candidate_acceptance_decision(
    old_summary: dict,
    candidate_summary: dict,
    curriculum_level: int,
) -> tuple[str, str | None]:
    new_case = candidate_summary["cases"][0]
    old_score = old_summary["score"]
    new_score = candidate_summary["score"]
    old_required = old_summary["required_coverage"]
    new_required = candidate_summary["required_coverage"]

    if not new_case["correct"]["assertion_pass"]:
        return "rejected", "correct RTL failed assertions or compile/run checks"
    if new_case["correct"]["returncode"] != 0:
        return "rejected", "correct RTL returned non-zero status"
    if new_required < old_required:
        return "rejected", "required coverage regressed"
    if new_score <= old_score:
        return "rejected", "candidate score did not improve"
    if curriculum_level == 3:
        boundary_improved = (
            candidate_summary["boundary_case_coverage"]
            > old_summary["boundary_case_coverage"]
        )
        mutation_improved = (
            candidate_summary["mutation_coverage"]
            > old_summary["mutation_coverage"]
        )
        if not (boundary_improved or mutation_improved):
            return "rejected", "level 3 requires boundary or mutation coverage improvement"
    return "accepted", None


def main() -> int:
    args = parse_args()
    args.run_root.mkdir(parents=True, exist_ok=True)
    active_datasets_dir = args.datasets_dir
    curriculum = load_curriculum(args.datasets_dir, args.curriculum_level)
    level_targets = set(curriculum.get("targets", []))

    trajectory = {
        "case": args.case,
        "target_score": args.target_score,
        "initial_datasets_dir": str(args.datasets_dir),
        "curriculum_level": args.curriculum_level,
        "level_name": curriculum.get("name"),
        "level_targets": sorted(level_targets),
        "level_metrics": curriculum.get("metrics", []),
        "iterations": [],
    }

    testplan_path = args.run_root / "testplan.json"
    testplan_result = generate_testplan(
        spec_path=None,
        rtl_dir=active_datasets_dir / args.case / "rtl",
        case=args.case,
        datasets_dir=active_datasets_dir,
        out_path=testplan_path,
    )
    trajectory["testplan"] = testplan_result.output

    sva_path = args.run_root / "generated_sva.json"
    sva_result = generate_sva(
        testplan_path=testplan_path,
        rtl_dir=active_datasets_dir / args.case / "rtl",
        out_path=sva_path,
        llm_command=args.llm_command,
        allow_scaffold=args.allow_scaffold,
    )
    trajectory["generated_sva"] = sva_result.output

    for iteration in range(args.max_iters):
        iter_dir = args.run_root / f"iter_{iteration}"
        closure_result = run_coverage_closure(
            case=args.case,
            datasets_dir=active_datasets_dir,
            verilator=args.verilator,
            build_root=args.closure_build_root,
            iteration=iteration,
            verbose=False,
        )
        summary = closure_result.output["summary"]
        score = summary["score"]
        feedback_json = Path(closure_result.artifacts["feedback_json"])
        parsed_feedback = parse_feedback(feedback_json, None).output
        actions = choose_repair_actions(parsed_feedback, trajectory, args.policy_command)

        iteration_record = {
            "iteration": iteration,
            "curriculum_level": args.curriculum_level,
            "level_targets": sorted(level_targets),
            "level_metrics": curriculum.get("metrics", []),
            "active_datasets_dir": str(active_datasets_dir),
            "score": score,
            "closed": score >= args.target_score and summary["closed"],
            "summary_json": closure_result.artifacts["summary_json"],
            "feedback_json": str(feedback_json),
            "recommended_actions": parsed_feedback["recommended_actions"],
            "chosen_actions": actions,
            "repair_artifacts": {},
            "candidate": {
                "repair_json": [],
                "applied_patch": [],
                "candidate_root": None,
                "candidate_summary": None,
                "old_score": score,
                "new_score": None,
                "decision": "not_attempted",
                "reject_reason": "no structured repair patches were produced",
            },
        }

        if "repair_sva" in actions:
            repair_path = iter_dir / "repair_sva_plan.json"
            repair_result = repair_sva(
                feedback_path=feedback_json,
                out_path=repair_path,
                llm_command=args.llm_command,
                sva_json_path=sva_path,
                summary_json_path=Path(closure_result.artifacts["summary_json"]),
                rtl_dir=active_datasets_dir / args.case / "rtl",
                allow_plan=args.allow_repair_plan,
            )
            iteration_record["repair_artifacts"]["repair_sva_plan"] = repair_result.artifacts.get(
                "repair_sva_plan"
            )

        if "repair_testbench" in actions:
            repair_path = iter_dir / "repair_testbench_plan.json"
            repair_result = repair_testbench(
                feedback_path=feedback_json,
                out_path=repair_path,
                llm_command=args.llm_command,
                summary_json_path=Path(closure_result.artifacts["summary_json"]),
                case=args.case,
                datasets_dir=active_datasets_dir,
                allow_plan=args.allow_repair_plan,
            )
            iteration_record["repair_artifacts"]["repair_testbench_plan"] = (
                repair_result.artifacts.get("repair_testbench_plan")
            )

        basic_repairs = basic_testbench_repairs(args.case, parsed_feedback, level_targets)
        if basic_repairs and not any(
            path and repair_json_has_patches(Path(path))
            for path in iteration_record["repair_artifacts"].values()
        ):
            basic_repair_path = iter_dir / "basic_testbench_repair.json"
            write_json(
                basic_repair_path,
                {
                    "mode": "basic_testbench_anchor",
                    "repairs": basic_repairs,
                },
            )
            iteration_record["repair_artifacts"]["basic_testbench_repair"] = str(
                basic_repair_path
            )

        structured_repairs = [
            Path(path)
            for path in iteration_record["repair_artifacts"].values()
            if path and repair_json_has_patches(Path(path))
        ]

        if structured_repairs:
            candidate_root = iter_dir / "candidate"
            prepare_candidate_case(active_datasets_dir, args.case, candidate_root)
            applied_patch_paths = []
            for repair_index, repair_path in enumerate(structured_repairs):
                applied_path = iter_dir / f"applied_patch_{repair_index}.json"
                apply_result = apply_repair(
                    repair_json_path=repair_path,
                    case=args.case,
                    datasets_dir=candidate_root,
                    out_applied=applied_path,
                    snapshot_dir=iter_dir / "snapshots",
                )
                applied_patch_paths.append(apply_result.artifacts["applied_patch_json"])

            candidate_closure = run_coverage_closure(
                case=args.case,
                datasets_dir=candidate_root,
                verilator=args.verilator,
                build_root=iter_dir / "candidate_closure",
                iteration=0,
                verbose=False,
            )
            candidate_summary = candidate_closure.output["summary"]
            decision, reject_reason = candidate_acceptance_decision(
                summary,
                candidate_summary,
                args.curriculum_level,
            )
            iteration_record["candidate"] = {
                "repair_json": [str(path) for path in structured_repairs],
                "applied_patch": applied_patch_paths,
                "candidate_root": str(candidate_root),
                "candidate_summary": candidate_closure.artifacts["summary_json"],
                "old_score": score,
                "new_score": candidate_summary["score"],
                "decision": decision,
                "reject_reason": reject_reason,
            }
            if decision == "accepted":
                active_datasets_dir = candidate_root

        trajectory["iterations"].append(iteration_record)
        write_json(args.run_root / "trajectory.json", trajectory)

        print(
            f"iter={iteration} score={score:.2%} "
            f"actions={','.join(actions) if actions else 'none'} "
            f"trajectory={args.run_root / 'trajectory.json'}"
        )

        if iteration_record["closed"]:
            return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
