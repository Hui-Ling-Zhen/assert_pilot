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
    diagnose_feedback_tool,
    generate_sva,
    generate_testplan,
    parse_feedback,
    plan_repair_tool,
    repair_policy_tool,
    repair_testplan,
    repair_sva,
    repair_testbench,
    run_coverage_closure,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a lightweight AssertPilot agent loop.")
    parser.add_argument("--case", help="Dataset case to optimize.")
    parser.add_argument(
        "--task-json",
        type=Path,
        help="Scheduler-selected task JSON from curriculum_scheduler.py.",
    )
    parser.add_argument("--datasets-dir", type=Path, default=DEFAULT_DATASETS_DIR)
    parser.add_argument("--verilator", type=Path, default=DEFAULT_VERILATOR)
    parser.add_argument("--target-score", type=float, default=1.0)
    parser.add_argument("--max-iters", type=int, default=3)
    parser.add_argument(
        "--curriculum-level",
        type=int,
        choices=[1, 2, 3, 4, 5],
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
        "--failure-log",
        type=Path,
        help="Optional Verilator failure log to include in diagnosis and repair planning.",
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
    args = parser.parse_args()
    if args.task_json:
        task_payload = json.loads(args.task_json.read_text(encoding="utf-8"))
        task = task_payload.get("next_task", task_payload)
        args.scheduler_task = task
        args.case = args.case or task.get("case")
        args.curriculum_level = int(task.get("curriculum_level", args.curriculum_level))
    else:
        args.scheduler_task = None
    if not args.case:
        parser.error("--case is required unless --task-json provides next_task.case")
    return args


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
    diagnosis: dict,
    repair_plan: dict,
    trajectory: dict,
    policy_command: str | None,
) -> list[str]:
    """Choose repair actions with an LLM/agent policy when configured."""
    if not policy_command:
        planned_actions = []
        for intent in repair_plan.get("repair_intents", []):
            for target in intent.get("repair_order", []):
                if target == "testplan":
                    planned_actions.append("repair_testplan")
                elif target == "tb.cpp":
                    planned_actions.append("repair_testbench")
                elif target == "sva":
                    planned_actions.append("repair_sva")
        if planned_actions:
            return list(dict.fromkeys(planned_actions))
        chosen = deterministic_repair_actions(parsed_feedback)
        if any(
            "testplan" in issue.get("suggested_repair_targets", [])
            for issue in diagnosis.get("issues", [])
        ):
            chosen.insert(0, "repair_testplan")
        return list(dict.fromkeys(chosen))

    response = call_llm_json(
        policy_command,
        "choose_repair_actions",
        {
            "task": "choose_repair_actions",
            "instructions": [
                "Choose the next AssertPilot repair actions.",
                "Allowed actions: repair_testplan, repair_sva, repair_testbench, inspect, stop.",
                "Prefer repair_testplan before code changes when the diagnosis says a scenario is missing from the plan.",
                "Prefer repair_testbench for missing scenarios, inactive triggers, or boundary gaps.",
                "Prefer repair_sva when mutation gaps indicate weak assertions and correct RTL still passes.",
                "Return JSON: {\"actions\": [\"repair_testplan\", \"repair_sva\", \"repair_testbench\"], \"rationale\": \"...\"}",
            ],
            "trajectory": trajectory,
            "feedback": parsed_feedback,
            "diagnosis": diagnosis,
            "repair_plan": repair_plan,
        },
    )
    actions = response.get("actions", [])
    if not isinstance(actions, list):
        raise RuntimeError("Policy command must return JSON with an actions list.")
    allowed = {"repair_testplan", "repair_sva", "repair_testbench", "inspect", "stop"}
    normalized = [str(action) for action in actions if str(action) in allowed]
    if normalized:
        return normalized
    chosen = deterministic_repair_actions(parsed_feedback)
    if any(
        "testplan" in issue.get("suggested_repair_targets", [])
        for issue in diagnosis.get("issues", [])
    ):
        chosen.insert(0, "repair_testplan")
    return list(dict.fromkeys(chosen))


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_json_if_exists(path: str | Path | None) -> dict | None:
    if not path:
        return None
    json_path = Path(path)
    if not json_path.exists():
        return None
    return json.loads(json_path.read_text(encoding="utf-8"))


def build_obligation_assertion_trace(
    testplan: dict,
    generated_sva: dict,
    diagnosis: dict | None = None,
    repair_plan: dict | None = None,
) -> list[dict]:
    assertions_by_plan: dict[str, list[dict]] = {}
    for assertion in generated_sva.get("assertions", []):
        assertions_by_plan.setdefault(str(assertion.get("plan_id")), []).append(assertion)

    issues_by_scenario: dict[str, list[dict]] = {}
    for issue in (diagnosis or {}).get("issues", []):
        scenario = issue.get("related_scenario") or issue.get("target")
        if scenario:
            issues_by_scenario.setdefault(str(scenario), []).append(
                {
                    "issue_type": issue.get("issue_type"),
                    "target": issue.get("target"),
                    "related_assertion": issue.get("related_assertion"),
                    "severity": issue.get("severity"),
                }
            )

    intents_by_scenario: dict[str, list[dict]] = {}
    for intent in (repair_plan or {}).get("repair_intents", []):
        scenario = (
            intent.get("target_scenario")
            or intent.get("target_mutation")
            or intent.get("target_assertion")
        )
        if scenario:
            intents_by_scenario.setdefault(str(scenario), []).append(
                {
                    "intent_type": intent.get("intent_type"),
                    "repair_order": intent.get("repair_order", []),
                    "required_metric_improvement": intent.get("required_metric_improvement"),
                }
            )

    trace = []
    for plan in testplan.get("plans", []):
        plan_id = str(plan.get("id"))
        plan_assertions = assertions_by_plan.get(plan_id, [])
        trigger = plan.get("trigger_scenario")
        trace.append(
            {
                "plan_id": plan_id,
                "obligation_type": plan.get("obligation_type"),
                "scope_signals": plan.get("scope", {}).get("signals", []),
                "trigger_scenario": trigger,
                "activation_condition": plan.get("activation_condition"),
                "expected_behavior": plan.get("expected_behavior"),
                "forbidden_behavior": plan.get("forbidden_behavior"),
                "timing_model": plan.get("timing_model"),
                "assertions": [
                    {
                        "name": assertion.get("name"),
                        "status": assertion.get("status"),
                        "trigger_scenario": assertion.get("trigger_scenario"),
                        "activation_condition": assertion.get("activation_condition"),
                        "timing_rationale": assertion.get("timing_rationale"),
                    }
                    for assertion in plan_assertions
                ],
                "needs_stimulus": [
                    item
                    for item in generated_sva.get("needs_stimulus", [])
                    if str(item.get("plan_id")) == plan_id
                ],
                "diagnosis_issues": issues_by_scenario.get(str(trigger), []),
                "repair_intents": intents_by_scenario.get(str(trigger), []),
            }
        )
    return trace


def load_curriculum(datasets_dir: Path, level: int) -> dict:
    curriculum_path = datasets_dir / "curriculum_levels.json"
    if not curriculum_path.exists():
        return {"name": f"Level {level}", "targets": [], "metrics": []}
    data = json.loads(curriculum_path.read_text(encoding="utf-8"))
    return data.get(f"level_{level}", {"name": f"Level {level}", "targets": [], "metrics": []})


def executable_curriculum_level(level: int) -> int:
    """Map scheduler-only stages onto currently runnable repair templates."""
    return level if level in {1, 2, 3} else 3


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
    diagnosis: dict,
    repair_plan: dict,
) -> tuple[str, str | None]:
    new_case = candidate_summary["cases"][0]
    old_required = old_summary["required_coverage"]
    new_required = candidate_summary["required_coverage"]

    if not new_case["correct"]["assertion_pass"]:
        return "rejected", "correct RTL failed assertions or compile/run checks"
    if new_case["correct"]["returncode"] != 0:
        return "rejected", "correct RTL returned non-zero status"
    if new_required < old_required:
        return "rejected", "required coverage regressed"

    required_checks = metric_checks_for_repair(diagnosis, repair_plan, old_summary)
    failed_checks = [
        check_name
        for check_name, passed in required_checks.items()
        if not metric_check_passed(check_name, old_summary, candidate_summary)
    ]
    if failed_checks:
        return "rejected", "metric-specific repair checks failed: " + ", ".join(failed_checks)
    return "accepted", None


def metric_checks_for_repair(
    diagnosis: dict,
    repair_plan: dict,
    old_summary: dict,
) -> dict[str, bool]:
    checks: dict[str, bool] = {}

    for issue in diagnosis.get("issues", []):
        issue_type = issue.get("issue_type")
        if issue_type == "false_positive_assertion":
            checks["correct_rtl_recovers"] = True
        elif issue_type == "weak_assertion_or_missing_stimulus":
            checks["mutation_coverage_improves"] = True
        elif issue_type == "unreachable_or_unstimulated_assertion":
            checks["assertion_activation_improves"] = True
        elif issue_type == "missing_boundary_stimulus":
            checks["boundary_case_coverage_improves"] = True

    for intent in repair_plan.get("repair_intents", []):
        intent_type = intent.get("intent_type")
        if intent_type == "repair_false_positive_assertion":
            checks["correct_rtl_recovers"] = True
        elif intent_type == "expose_or_kill_mutation" or intent.get("target_mutations"):
            checks["mutation_coverage_improves"] = True
        elif intent_type == "activate_assertion_trigger_with_plan_and_stimulus":
            checks["assertion_activation_improves"] = True
        elif intent_type == "add_boundary_testplan_and_stimulus":
            checks["boundary_case_coverage_improves"] = True

    if not checks and old_summary["score"] < 1.0:
        checks["score_improves"] = True
    return checks


def metric_check_passed(check_name: str, old_summary: dict, candidate_summary: dict) -> bool:
    if check_name == "correct_rtl_recovers":
        old_pass = all(case["correct"]["assertion_pass"] for case in old_summary["cases"])
        new_pass = all(case["correct"]["assertion_pass"] for case in candidate_summary["cases"])
        return new_pass and (not old_pass or new_pass)
    if check_name == "mutation_coverage_improves":
        return candidate_summary["mutation_coverage"] > old_summary["mutation_coverage"]
    if check_name == "assertion_activation_improves":
        return (
            candidate_summary["assertion_activation_rate"]
            > old_summary["assertion_activation_rate"]
        )
    if check_name == "boundary_case_coverage_improves":
        return candidate_summary["boundary_case_coverage"] > old_summary["boundary_case_coverage"]
    if check_name == "score_improves":
        return candidate_summary["score"] > old_summary["score"]
    raise ValueError(f"Unknown acceptance check: {check_name}")


def acceptance_metrics(old_summary: dict, candidate_summary: dict | None) -> dict:
    metrics = {
        "old_score": old_summary["score"],
        "new_score": None,
        "score_delta": None,
        "old_mutation_coverage": old_summary["mutation_coverage"],
        "new_mutation_coverage": None,
        "mutation_delta": None,
        "old_boundary_case_coverage": old_summary["boundary_case_coverage"],
        "new_boundary_case_coverage": None,
        "boundary_delta": None,
        "old_assertion_activation_rate": old_summary["assertion_activation_rate"],
        "new_assertion_activation_rate": None,
        "assertion_activation_delta": None,
        "old_required_coverage": old_summary["required_coverage"],
        "new_required_coverage": None,
        "required_delta": None,
    }
    if candidate_summary is None:
        return metrics

    metrics.update(
        {
            "new_score": candidate_summary["score"],
            "score_delta": candidate_summary["score"] - old_summary["score"],
            "new_mutation_coverage": candidate_summary["mutation_coverage"],
            "mutation_delta": (
                candidate_summary["mutation_coverage"] - old_summary["mutation_coverage"]
            ),
            "new_boundary_case_coverage": candidate_summary["boundary_case_coverage"],
            "boundary_delta": (
                candidate_summary["boundary_case_coverage"]
                - old_summary["boundary_case_coverage"]
            ),
            "new_assertion_activation_rate": candidate_summary["assertion_activation_rate"],
            "assertion_activation_delta": (
                candidate_summary["assertion_activation_rate"]
                - old_summary["assertion_activation_rate"]
            ),
            "new_required_coverage": candidate_summary["required_coverage"],
            "required_delta": (
                candidate_summary["required_coverage"] - old_summary["required_coverage"]
            ),
        }
    )
    return metrics


def main() -> int:
    args = parse_args()
    args.run_root.mkdir(parents=True, exist_ok=True)
    active_datasets_dir = args.datasets_dir
    effective_level = executable_curriculum_level(args.curriculum_level)
    curriculum = load_curriculum(args.datasets_dir, effective_level)
    level_targets = set(curriculum.get("targets", []))
    if args.scheduler_task and args.scheduler_task.get("focus_target"):
        level_targets.add(str(args.scheduler_task["focus_target"]))

    trajectory = {
        "case": args.case,
        "target_score": args.target_score,
        "initial_datasets_dir": str(args.datasets_dir),
        "curriculum_level": args.curriculum_level,
        "effective_curriculum_level": effective_level,
        "level_name": curriculum.get("name"),
        "level_targets": sorted(level_targets),
        "level_metrics": curriculum.get("metrics", []),
        "scheduler_task": args.scheduler_task,
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
    trajectory["obligation_assertion_triggers"] = build_obligation_assertion_trace(
        trajectory["testplan"],
        trajectory["generated_sva"],
    )

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
        diagnosis_path = iter_dir / "diagnosis.json"
        diagnosis_result = diagnose_feedback_tool(
            feedback_path=feedback_json,
            summary_path=Path(closure_result.artifacts["summary_json"]),
            failure_log=args.failure_log,
            case=args.case,
            datasets_dir=active_datasets_dir,
            out_path=diagnosis_path,
        )
        diagnosis = diagnosis_result.output
        repair_intent_path = iter_dir / "repair_intent.json"
        repair_plan_result = plan_repair_tool(
            feedback_path=feedback_json,
            summary_path=Path(closure_result.artifacts["summary_json"]),
            trajectory_path=args.run_root / "trajectory.json",
            datasets_dir=active_datasets_dir,
            out_path=repair_intent_path,
            case=args.case,
            failure_log=args.failure_log,
        )
        repair_plan = repair_plan_result.output
        repair_policy_path = iter_dir / "repair_policy.json"
        repair_policy_result = repair_policy_tool(
            diagnosis_path=diagnosis_path,
            repair_intent_path=repair_intent_path,
            policy_path=Path(__file__).resolve().parents[1] / "policies" / "repair_policy.json",
            out_path=repair_policy_path,
            summary_path=Path(closure_result.artifacts["summary_json"]),
            case=args.case,
            datasets_dir=active_datasets_dir,
        )
        repair_policy = repair_policy_result.output
        actions = choose_repair_actions(
            parsed_feedback,
            diagnosis,
            repair_plan,
            trajectory,
            args.policy_command,
        )

        iteration_record = {
            "iteration": iteration,
            "curriculum_level": args.curriculum_level,
            "effective_curriculum_level": effective_level,
            "scheduler_task": args.scheduler_task,
            "level_targets": sorted(level_targets),
            "level_metrics": curriculum.get("metrics", []),
            "active_datasets_dir": str(active_datasets_dir),
            "score": score,
            "closed": score >= args.target_score and summary["closed"],
            "summary_json": closure_result.artifacts["summary_json"],
            "feedback_json": str(feedback_json),
            "failure_log": str(args.failure_log) if args.failure_log else None,
            "diagnosis_json": str(diagnosis_path),
            "diagnosis": diagnosis,
            "repair_intent_json": str(repair_intent_path),
            "repair_intent": repair_plan,
            "repair_intents": repair_plan["repair_intents"],
            "repair_policy_json": str(repair_policy_path),
            "repair_policy": repair_policy,
            "policy_decisions": repair_policy["policy_decisions"],
            "obligation_assertion_triggers": build_obligation_assertion_trace(
                trajectory["testplan"],
                trajectory["generated_sva"],
                diagnosis,
                repair_plan,
            ),
            "generated_testplan_patch": None,
            "generated_sva_patch": None,
            "generated_tb_patch": None,
            "acceptance_metrics": acceptance_metrics(summary, None),
            "decision": "not_attempted",
            "repair_stages": {
                "closure": closure_result.artifacts["summary_json"],
                "feedback": str(feedback_json),
                "diagnosis": str(diagnosis_path),
                "repair_intent": str(repair_intent_path),
                "candidate_repairs": {},
                "candidate_verification": None,
                "acceptance": None,
            },
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
                "acceptance_metrics": acceptance_metrics(summary, None),
            },
        }

        if "repair_testplan" in actions:
            repair_path = iter_dir / "repair_testplan_plan.json"
            repair_result = repair_testplan(
                diagnosis_path=repair_intent_path,
                out_path=repair_path,
                llm_command=args.llm_command,
                case=args.case,
                datasets_dir=active_datasets_dir,
                allow_plan=args.allow_repair_plan,
            )
            iteration_record["repair_artifacts"]["repair_testplan_plan"] = (
                repair_result.artifacts.get("repair_testplan_plan")
            )
            iteration_record["generated_testplan_patch"] = repair_result.output
            iteration_record["repair_stages"]["candidate_repairs"]["revised_testplan"] = (
                repair_result.artifacts.get("repair_testplan_plan")
            )

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
            iteration_record["generated_sva_patch"] = repair_result.output
            iteration_record["repair_stages"]["candidate_repairs"]["revised_sva"] = (
                repair_result.artifacts.get("repair_sva_plan")
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
            iteration_record["generated_tb_patch"] = repair_result.output
            iteration_record["repair_stages"]["candidate_repairs"]["revised_tb"] = (
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
            iteration_record["generated_tb_patch"] = read_json_if_exists(basic_repair_path)
            iteration_record["repair_stages"]["candidate_repairs"]["revised_tb"] = str(
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
                diagnosis,
                repair_plan,
            )
            acceptance_checks = metric_checks_for_repair(diagnosis, repair_plan, summary)
            metrics = acceptance_metrics(summary, candidate_summary)
            iteration_record["candidate"] = {
                "repair_json": [str(path) for path in structured_repairs],
                "applied_patch": applied_patch_paths,
                "applied_patch_payloads": [
                    read_json_if_exists(path) for path in applied_patch_paths
                ],
                "candidate_root": str(candidate_root),
                "candidate_summary": candidate_closure.artifacts["summary_json"],
                "old_score": score,
                "new_score": candidate_summary["score"],
                "decision": decision,
                "reject_reason": reject_reason,
                "acceptance_metrics": metrics,
                "acceptance_checks": {
                    check_name: metric_check_passed(check_name, summary, candidate_summary)
                    for check_name in acceptance_checks
                },
            }
            iteration_record["acceptance_metrics"] = metrics
            iteration_record["decision"] = decision
            iteration_record["reject_reason"] = reject_reason
            iteration_record["repair_stages"]["candidate_verification"] = (
                candidate_closure.artifacts["summary_json"]
            )
            iteration_record["repair_stages"]["acceptance"] = {
                "decision": decision,
                "reject_reason": reject_reason,
                "metrics": metrics,
                "checks": iteration_record["candidate"]["acceptance_checks"],
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
