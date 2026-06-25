#!/usr/bin/env python3
"""Agent-callable tool wrappers for AssertPilot.

The functions in this module expose AssertPilot as a small toolset that an
agent runtime can call without depending on the monolithic `src/gen_plan.py`
entry point. Each CLI command prints JSON so it can be consumed by a controller
loop, Hermes-style tool runner, or a future self-evolution evaluator.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
DEFAULT_DATASETS_DIR = PROJECT_ROOT / "datasets"
DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs" / "agent_tools"
DEFAULT_VERILATOR = PROJECT_ROOT.parent / "verilator" / "install" / "bin" / "verilator"
DEFAULT_LLM_COMMAND_ENV = "ASSERTPILOT_LLM_COMMAND"
SVA_GENERATION_SKILL = PROJECT_ROOT / "skills" / "sva_generation_skill.md"
ALLOWED_REPAIR_FILES = {
    "rtl/property_goldmine.sva",
    "sim/tb.cpp",
    "testplan.json",
}
ALLOWED_REPAIR_TYPES = {
    "replace_assertion",
    "append_assertion",
    "insert_stimulus",
    "replace_block",
    "update_testplan_item",
    "append_testplan_item",
}
ISSUE_REPAIR_PRIORITIES = {
    "false_positive_assertion": ["sva"],
    "weak_assertion_or_missing_stimulus": ["tb.cpp", "sva"],
    "unreachable_or_unstimulated_assertion": ["testplan", "tb.cpp"],
    "missing_boundary_stimulus": ["testplan", "tb.cpp"],
}

sys.path.insert(0, str(SCRIPTS_DIR))
from run_coverage_closure import (  # noqa: E402
    DEFAULT_BUILD_ROOT as DEFAULT_CLOSURE_BUILD_ROOT,
    run_case as run_closure_case,
    summarize_iteration,
    targeted_feedback,
    write_iteration_artifacts,
)
from run_dataset_verilator import (  # noqa: E402
    load_case,
    run_lint,
    run_simulation,
    select_variants,
)


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    tool: str
    output: dict[str, Any]
    artifacts: dict[str, str]
    error: str | None = None


def emit(result: ToolResult) -> int:
    print(json.dumps(asdict(result), indent=2))
    return 0 if result.ok else 1


def read_text(path: Path | None) -> str:
    if path is None:
        return ""
    return path.read_text(encoding="utf-8")


def case_dir(datasets_dir: Path, case: str) -> Path:
    path = datasets_dir / case
    if not path.exists():
        raise FileNotFoundError(f"Dataset case does not exist: {path}")
    return path


def load_coverage_config(case_path: Path) -> dict[str, Any]:
    coverage_path = case_path / "coverage_scenarios.json"
    if not coverage_path.exists():
        return {
            "required": [],
            "bonus": [],
            "mutation_targets": [],
            "assertion_triggers": {},
            "boundary_cases": [],
        }
    return json.loads(coverage_path.read_text(encoding="utf-8"))


def resolve_llm_command(llm_command: str | None) -> str | None:
    return llm_command or os.environ.get(DEFAULT_LLM_COMMAND_ENV)


def call_llm_json(
    llm_command: str | None,
    task: str,
    prompt_payload: dict[str, Any],
) -> dict[str, Any]:
    """Call an external LLM/agent command with a structured JSON prompt.

    The command receives the path to the prompt JSON in
    `ASSERTPILOT_LLM_PROMPT_JSON` and the task name in `ASSERTPILOT_LLM_TASK`.
    It must print JSON to stdout. This keeps AssertPilot independent from a
    particular provider while making the tool genuinely LLM-backed when wired
    to Hermes, Cursor, OpenAI, Anthropic, etc.
    """
    command = resolve_llm_command(llm_command)
    if not command:
        raise RuntimeError(
            f"LLM command is required for {task}. Pass --llm-command or set {DEFAULT_LLM_COMMAND_ENV}."
        )

    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".json",
        delete=False,
    ) as prompt_file:
        json.dump(prompt_payload, prompt_file, indent=2)
        prompt_path = Path(prompt_file.name)

    env = os.environ.copy()
    env["ASSERTPILOT_LLM_PROMPT_JSON"] = str(prompt_path)
    env["ASSERTPILOT_LLM_TASK"] = task
    result = subprocess.run(
        command,
        shell=True,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"LLM command failed for {task} with return code {result.returncode}:\n{result.stdout}"
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"LLM command for {task} must print JSON. Raw output:\n{result.stdout}"
        ) from exc


def plan_lookup(testplan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(plan.get("id")): plan for plan in testplan.get("plans", [])}


def assertion_trigger_for_plan(plan_id: str, testplan: dict[str, Any]) -> str:
    plan = plan_lookup(testplan).get(plan_id, {})
    if plan.get("trigger_scenario"):
        return str(plan["trigger_scenario"])
    if plan.get("expected_scenarios"):
        expected = plan["expected_scenarios"]
        if isinstance(expected, list) and expected:
            return str(expected[0])
    triggers = testplan.get("assertion_triggers", {})
    if plan_id in triggers:
        trigger = triggers[plan_id]
        if isinstance(trigger, str):
            return trigger
        if isinstance(trigger, list) and trigger:
            return str(trigger[0])
    return plan_id


def default_activation_condition(plan_id: str, testplan: dict[str, Any]) -> str:
    plan = plan_lookup(testplan).get(plan_id, {})
    if plan.get("activation_condition"):
        return str(plan["activation_condition"])
    if plan.get("scope", {}).get("activation_condition"):
        return str(plan["scope"]["activation_condition"])
    return f"trigger_scenario == {assertion_trigger_for_plan(plan_id, testplan)}"


def infer_obligation_type(scenario: str) -> str:
    name = scenario.lower()
    if "reset" in name:
        return "reset"
    if "underflow" in name or "read_from_empty" in name:
        return "boundary_underflow"
    if "overflow" in name or "write_while_full" in name:
        return "boundary_overflow"
    if "wrap" in name:
        return "boundary_wrap"
    if "hold" in name or "stall" in name:
        return "stability_hold"
    if "both_requests" in name or "one_hot" in name or "grant" in name:
        return "mutual_exclusion_one_hot"
    if "handshake" in name or "valid" in name or "ready" in name:
        return "handshake_protocol"
    return "state_transition"


def infer_activation_condition(scenario: str) -> str:
    name = scenario.lower()
    if "read_from_empty" in name or "underflow" in name:
        return "empty && rd_en"
    if "write_while_full" in name or "overflow" in name:
        return "full && wr_en"
    if "disabled_hold" in name:
        return "!en"
    if "wrap" in name and "counter" in name:
        return "en && count == MAX_COUNT"
    if "no_req_idle" in name:
        return "req == 0"
    if "both_requests" in name:
        return "req == 2'b11"
    if "data_changes_under_stall" in name:
        return "valid_o && !ready_i"
    if "hold_when_stalled" in name or "stall" in name:
        return "valid_o && !ready_i"
    if "reset" in name:
        return "rst"
    return f"SCENARIO:{scenario}"


def infer_expected_behavior(scenario: str) -> str:
    name = scenario.lower()
    if "reset" in name:
        return "state and visible outputs take their reset values"
    if "read_from_empty" in name or "underflow" in name:
        return "empty remains asserted and count does not decrement"
    if "write_while_full" in name or "overflow" in name:
        return "full remains asserted and count does not increment beyond depth"
    if "disabled_hold" in name or "hold" in name or "stall" in name:
        return "state and output data remain stable while held"
    if "both_requests" in name or "grant" in name:
        return "grant is one-hot or zero and any grant corresponds to a request"
    if "wrap" in name:
        return "pointer or counter wraps according to the RTL boundary behavior"
    return "observed DUT state matches the specified scenario behavior"


def infer_forbidden_behavior(scenario: str) -> str:
    name = scenario.lower()
    if "read_from_empty" in name or "underflow" in name:
        return "count decreases or empty deasserts on an empty read"
    if "write_while_full" in name or "overflow" in name:
        return "count increases beyond depth or full deasserts on a full write"
    if "disabled_hold" in name:
        return "count changes while enable is low"
    if "stall" in name or "hold" in name:
        return "valid or data changes while the transfer is stalled"
    if "grant" in name or "both_requests" in name:
        return "grant is not one-hot or grants a requester that was not active"
    if "reset" in name:
        return "state remains non-reset while reset is asserted"
    return "the design violates the scenario's expected behavior"


def infer_timing_model(scenario: str) -> str:
    name = scenario.lower()
    if "reset" in name:
        return "reset_cycle"
    if "arbiter" in name or "handshake" in name or "counter" in name:
        return "registered_next_cycle"
    return "same_or_next_cycle_from_rtl"


def contract_for_scenario(
    scenario: str,
    difficulty: str,
    intent: str,
    valid_signals: list[str],
    assertion_triggers: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": scenario,
        "difficulty": difficulty,
        "intent": intent,
        "obligation_type": infer_obligation_type(scenario),
        "scope": {
            "signals": valid_signals,
            "clock": "clk" if "clk" in valid_signals else None,
            "reset": "rst" if "rst" in valid_signals else None,
        },
        "trigger_scenario": assertion_trigger_for_plan(
            scenario,
            {"plans": [], "assertion_triggers": assertion_triggers},
        ),
        "activation_condition": infer_activation_condition(scenario),
        "expected_behavior": infer_expected_behavior(scenario),
        "forbidden_behavior": infer_forbidden_behavior(scenario),
        "timing_model": infer_timing_model(scenario),
    }


def extract_assertions_from_llm_response(
    response: dict[str, Any],
    testplan: dict[str, Any],
) -> list[dict[str, str]]:
    assertions = response.get("assertions")
    if isinstance(assertions, list):
        normalized = []
        for index, item in enumerate(assertions):
            if isinstance(item, str):
                plan_id = f"plan_{index}"
                normalized.append(
                    {
                        "name": f"assert_agent_{index}",
                        "plan_id": plan_id,
                        "trigger_scenario": assertion_trigger_for_plan(plan_id, testplan),
                        "activation_condition": default_activation_condition(plan_id, testplan),
                        "timing_rationale": "",
                        "sva": item,
                    }
                )
            elif isinstance(item, dict):
                sva = str(item.get("sva", "")).strip()
                if not sva:
                    continue
                plan_id = str(item.get("plan_id", f"plan_{index}"))
                normalized.append(
                    {
                        "name": str(item.get("name", f"assert_agent_{index}")),
                        "plan_id": plan_id,
                        "trigger_scenario": str(
                            item.get("trigger_scenario")
                            or assertion_trigger_for_plan(plan_id, testplan)
                        ),
                        "activation_condition": str(
                            item.get("activation_condition")
                            or default_activation_condition(plan_id, testplan)
                        ),
                        "timing_rationale": str(item.get("timing_rationale", "")),
                        "sva": sva,
                    }
                )
        return normalized

    text = str(response.get("text", ""))
    blocks = re.findall(r"```(?:systemverilog|sv|verilog)?\s*(.*?)```", text, re.DOTALL)
    if not blocks and text.strip():
        blocks = [text]
    return [
        {
            "name": f"assert_agent_{index}",
            "plan_id": (plan_id := f"plan_{index}"),
            "trigger_scenario": assertion_trigger_for_plan(plan_id, testplan),
            "activation_condition": default_activation_condition(plan_id, testplan),
            "timing_rationale": "",
            "sva": block.strip(),
        }
        for index, block in enumerate(blocks)
        if block.strip()
    ]


def extract_needs_stimulus_from_llm_response(
    response: dict[str, Any],
    testplan: dict[str, Any],
) -> list[dict[str, str]]:
    needs_stimulus = response.get("needs_stimulus", [])
    if not isinstance(needs_stimulus, list):
        return []
    normalized = []
    for index, item in enumerate(needs_stimulus):
        if not isinstance(item, dict):
            continue
        plan_id = str(item.get("plan_id", f"plan_{index}"))
        normalized.append(
            {
                "plan_id": plan_id,
                "trigger_scenario": str(
                    item.get("trigger_scenario")
                    or assertion_trigger_for_plan(plan_id, testplan)
                ),
                "reason": str(item.get("reason", "Trigger scenario lacks stimulus.")),
            }
        )
    return normalized


def build_sva_generation_prompt(testplan: dict[str, Any], rtl_dir: Path) -> dict[str, Any]:
    design_path = rtl_dir / "design.v"
    property_path = rtl_dir / "property_goldmine.sva"
    bindings_path = rtl_dir / "bindings.sva"
    skill_text = read_text(SVA_GENERATION_SKILL)[:12000] if SVA_GENERATION_SKILL.exists() else ""
    return {
        "task": "generate_sva",
        "instructions": [
            "Generate real SystemVerilog Assertions for the provided AssertPilot dataset.",
            "Follow the SVA Generation Skill exactly.",
            "Treat each testplan item as an assertion contract, not free-form prose.",
            "Every assertion must correspond to exactly one testplan plan_id.",
            "Every assertion must declare trigger_scenario from the testplan item.",
            "Every assertion must declare activation_condition from the testplan item or a stricter equivalent.",
            "Use only signals from the plan item's scope.signals, valid_signals, or visible reference property module.",
            "Use expected_behavior and forbidden_behavior to define the consequent.",
            "Use timing_model to decide same-cycle, next-cycle, or $past alignment.",
            "If $past is needed, add timing_rationale explaining the alignment.",
            "Reset assertions that check reset behavior must not use disable iff (rst).",
            "Non-reset temporal properties should use disable iff (rst).",
            "Use $past(input) for registered outputs when checking next-cycle behavior.",
            "Avoid vacuous properties: each antecedent must be reachable by trigger_scenario.",
            "If a non-vacuous assertion cannot be written because the trigger has no stimulus, return needs_stimulus instead of inventing an assertion.",
            (
                "Return JSON: {\"assertions\": [{\"name\": ..., \"plan_id\": ..., "
                "\"trigger_scenario\": ..., \"activation_condition\": ..., "
                "\"timing_rationale\": ..., \"sva\": ...}], "
                "\"needs_stimulus\": [{\"plan_id\": ..., \"trigger_scenario\": ..., \"reason\": ...}]}"
            ),
        ],
        "sva_generation_skill": skill_text,
        "testplan": testplan,
        "rtl": {
            "design_v": read_text(design_path)[:12000],
            "property_goldmine_sva": read_text(property_path)[:12000],
            "bindings_sva": read_text(bindings_path)[:6000],
        },
    }


def build_sva_repair_prompt(
    feedback: dict[str, Any],
    sva_json: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    rtl_dir: Path | None,
) -> dict[str, Any]:
    rtl_payload = {}
    if rtl_dir:
        rtl_payload = {
            "design_v": read_text(rtl_dir / "design.v")[:12000],
            "property_goldmine_sva": read_text(rtl_dir / "property_goldmine.sva")[:12000],
            "bindings_sva": read_text(rtl_dir / "bindings.sva")[:6000],
        }
    return {
        "task": "repair_sva",
        "instructions": [
            "Repair or strengthen SVA candidates based on coverage/mutation feedback.",
            "If a failure is caused by missing stimulus rather than weak assertions, explain that in repair_notes.",
            "Do not weaken assertions just to pass buggy RTL. Correct RTL must remain passing.",
            "Return only structured repair patches. Do not return free-form text.",
            (
                "Return JSON: {\"repairs\": [{\"target_file\": \"rtl/property_goldmine.sva\", "
                "\"repair_type\": \"replace_assertion\", \"assertion_name\": \"assert_no_underflow\", "
                "\"new_sva\": \"property ... endproperty\\nassert_no_underflow: assert property(...);\", "
                "\"rationale\": \"...\"}]}"
            ),
        ],
        "feedback": feedback,
        "summary": summary,
        "current_sva": sva_json,
        "rtl": rtl_payload,
    }


def build_testbench_repair_prompt(
    feedback: dict[str, Any],
    summary: dict[str, Any] | None,
    case_path: Path,
) -> dict[str, Any]:
    tb_path = case_path / "sim" / "tb.cpp"
    coverage_path = case_path / "coverage_scenarios.json"
    return {
        "task": "repair_testbench",
        "instructions": [
            "Create structured patches for the Verilator C++ testbench.",
            "Only target sim/tb.cpp.",
            "Use repair_type insert_stimulus for adding stimulus near an anchor comment or line.",
            "Markers must be state-driven: print SCENARIO:<name> only after observing DUT state.",
            "Return only structured repair patches. Do not return free-form text.",
            (
                "Return JSON: {\"repairs\": [{\"target_file\": \"sim/tb.cpp\", "
                "\"repair_type\": \"insert_stimulus\", \"anchor\": \"// Extra write while full\", "
                "\"code\": \"...\", \"expected_markers\": [\"fifo_read_from_empty\"], "
                "\"rationale\": \"...\"}]}"
            ),
        ],
        "feedback": feedback,
        "summary": summary,
        "testbench": read_text(tb_path)[:12000],
        "coverage_scenarios": json.loads(read_text(coverage_path)) if coverage_path.exists() else {},
    }


def build_testplan_repair_prompt(
    diagnosis: dict[str, Any],
    testplan: dict[str, Any],
    coverage: dict[str, Any],
) -> dict[str, Any]:
    return {
        "task": "repair_testplan",
        "instructions": [
            "Revise the case-local AssertPilot testplan using only structured patches.",
            "Only target testplan.json.",
            "Use update_testplan_item when a plan item exists but needs clearer intent or expected scenarios.",
            "Use append_testplan_item when a missing scenario has no plan item.",
            "Do not write free-form prose outside JSON.",
            (
                "Return JSON: {\"repairs\": [{\"target_file\": \"testplan.json\", "
                "\"repair_type\": \"update_testplan_item\", \"plan_id\": \"fifo_read_from_empty\", "
                "\"updates\": {\"intent\": \"...\", \"expected_scenarios\": [\"fifo_read_from_empty\"]}, "
                "\"rationale\": \"...\"}]}"
            ),
        ],
        "diagnosis": diagnosis,
        "current_testplan": testplan,
        "coverage_scenarios": coverage,
    }


def scaffold_assertions(testplan: dict[str, Any]) -> list[dict[str, str]]:
    assertions = []
    for index, plan in enumerate(testplan.get("plans", [])):
        plan_id = plan.get("id", f"plan_{index}")
        trigger_scenario = assertion_trigger_for_plan(str(plan_id), testplan)
        activation_condition = default_activation_condition(str(plan_id), testplan)
        assertions.append(
            {
                "name": f"assert_agent_{plan_id}",
                "plan_id": plan_id,
                "trigger_scenario": trigger_scenario,
                "activation_condition": activation_condition,
                "timing_rationale": str(plan.get("timing_model", "")),
                "status": "scaffold",
                "sva": (
                    f"// TODO(agent): implement assertion for {plan_id}\n"
                    f"// Trigger scenario: {trigger_scenario}\n"
                    f"// Activation condition: {activation_condition}\n"
                    f"// Intent: {plan.get('intent', '')}"
                ),
            }
        )
    return assertions


def tokenize_identifier(text: str | None) -> set[str]:
    if not text:
        return set()
    tokens = {
        token
        for token in re.split(r"[^A-Za-z0-9]+", text.lower())
        if token and token not in {"bug", "assert", "scenario", "target"}
    }
    return tokens


def scenario_ids_from_coverage(coverage: dict[str, Any]) -> list[str]:
    scenario_ids = []
    for key in ("required", "bonus", "boundary_cases"):
        for item in coverage.get(key, []):
            if isinstance(item, str):
                scenario_ids.append(item)
            elif isinstance(item, dict) and item.get("id"):
                scenario_ids.append(str(item["id"]))
    return sorted(set(scenario_ids))


def normalize_trigger_ids(trigger: Any) -> list[str]:
    if isinstance(trigger, str):
        return [trigger]
    if isinstance(trigger, list):
        return [str(item.get("id", item)) if isinstance(item, dict) else str(item) for item in trigger]
    if isinstance(trigger, dict):
        return normalize_trigger_ids(trigger.get("scenarios", []))
    return []


def best_token_match(target: str | None, candidates: list[str]) -> str | None:
    target_tokens = tokenize_identifier(target)
    best_candidate = None
    best_score = 0
    for candidate in candidates:
        score = len(target_tokens & tokenize_identifier(candidate))
        if score > best_score:
            best_candidate = candidate
            best_score = score
    return best_candidate


def related_assertion_for_scenario(
    scenario: str | None,
    assertion_triggers: dict[str, Any],
    fallback_target: str | None = None,
) -> str | None:
    if scenario:
        for assertion_name, trigger in assertion_triggers.items():
            if scenario in normalize_trigger_ids(trigger):
                return assertion_name
    return best_token_match(fallback_target, list(assertion_triggers))


def first_trigger_for_assertion(assertion_name: str | None, assertion_triggers: dict[str, Any]) -> str | None:
    if not assertion_name:
        return None
    triggers = normalize_trigger_ids(assertion_triggers.get(assertion_name))
    return triggers[0] if triggers else None


def extract_assertion_names_from_log(log_text: str) -> list[str]:
    names = []
    for line in log_text.splitlines():
        if not re.search(r"Assertion failed|\$fatal|\$stop|%Error", line, re.IGNORECASE):
            continue
        for pattern in (
            r"Assertion failed.*?['\"]?([A-Za-z_][A-Za-z0-9_$]*)['\"]?",
            r"assert(?:ion)?[_:\s-]+([A-Za-z_][A-Za-z0-9_$]*)",
            r"\b([A-Za-z_][A-Za-z0-9_$]*assert[A-Za-z0-9_$]*)\b",
        ):
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                names.append(match.group(1))
                break
    return sorted(set(names))


def parse_failure_log(log_path: Path | None, log_text: str | None, case: str | None) -> list[dict[str, Any]]:
    """Convert raw Verilator failure text into structured diagnosis issues."""
    text = log_text or (log_path.read_text(encoding="utf-8") if log_path else "")
    if not text.strip():
        return []

    issues = []
    assertion_names = extract_assertion_names_from_log(text)
    failure_lines = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"Assertion failed|\$fatal|\$stop|%Error", line, re.IGNORECASE)
    ]
    if failure_lines and not assertion_names:
        assertion_names = [None]

    for assertion_name in assertion_names:
        issues.append(
            {
                "issue_type": "false_positive_assertion",
                "case": case,
                "target": assertion_name or "correct_rtl_assertion_failure",
                "related_assertion": assertion_name,
                "related_scenario": None,
                "suggested_repair_targets": ISSUE_REPAIR_PRIORITIES["false_positive_assertion"],
                "severity": "critical",
                "evidence": failure_lines[:5],
            }
        )
    return issues


def diagnose_feedback(
    feedback: dict[str, Any],
    summary: dict[str, Any] | None,
    datasets_dir: Path,
    selected_case: str | None,
    failure_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize closure feedback into issue records and repair intents."""
    issues = list(failure_issues or [])
    summary_cases = {
        case_summary.get("case"): case_summary
        for case_summary in (summary or {}).get("cases", [])
    }

    for target_group in feedback.get("targets", []):
        case_name = target_group.get("case") or selected_case
        if not case_name:
            continue
        coverage = load_coverage_config(case_dir(datasets_dir, case_name))
        assertion_triggers = coverage.get("assertion_triggers", {})
        scenarios = scenario_ids_from_coverage(coverage)
        case_summary = summary_cases.get(case_name, {})

        for message in target_group.get("targets", []):
            issue: dict[str, Any] | None = None
            mutation_match = re.search(r"Mutation target '([^']+)' was not killed", message)
            assertion_match = re.search(
                r"Assertion '([^']+)' was not activated; drive one of its trigger scenarios: (.+)\.",
                message,
            )
            boundary_match = re.search(r"Missing boundary case '([^']+)'", message)

            if "Correct RTL failed assertions" in message:
                issue = {
                    "issue_type": "false_positive_assertion",
                    "case": case_name,
                    "target": "correct_rtl_assertion_failure",
                    "related_assertion": None,
                    "related_scenario": None,
                    "suggested_repair_targets": ISSUE_REPAIR_PRIORITIES["false_positive_assertion"],
                    "severity": "critical",
                }
            elif mutation_match:
                target = mutation_match.group(1)
                scenario = best_token_match(target, scenarios)
                related_assertion = related_assertion_for_scenario(
                    scenario,
                    assertion_triggers,
                    target,
                )
                scenario = scenario or first_trigger_for_assertion(related_assertion, assertion_triggers)
                issue = {
                    "issue_type": "weak_assertion_or_missing_stimulus",
                    "case": case_name,
                    "target": target,
                    "related_assertion": related_assertion,
                    "related_scenario": scenario,
                    "suggested_repair_targets": ISSUE_REPAIR_PRIORITIES[
                        "weak_assertion_or_missing_stimulus"
                    ],
                    "severity": "high",
                }
            elif "Mutation set is not fully killed" in message:
                for target in case_summary.get("buggy", {}).get("unkilled_targets", []):
                    scenario = best_token_match(target, scenarios)
                    related_assertion = related_assertion_for_scenario(
                        scenario,
                        assertion_triggers,
                        target,
                    )
                    scenario = scenario or first_trigger_for_assertion(
                        related_assertion,
                        assertion_triggers,
                    )
                    issues.append(
                        {
                            "issue_type": "weak_assertion_or_missing_stimulus",
                            "case": case_name,
                            "target": target,
                            "related_assertion": related_assertion,
                            "related_scenario": scenario,
                            "suggested_repair_targets": ISSUE_REPAIR_PRIORITIES[
                                "weak_assertion_or_missing_stimulus"
                            ],
                            "severity": "high",
                            "evidence": [message],
                        }
                    )
                continue
            elif assertion_match:
                assertion_name = assertion_match.group(1)
                triggers = [item.strip() for item in assertion_match.group(2).split(",") if item.strip()]
                issue = {
                    "issue_type": "unreachable_or_unstimulated_assertion",
                    "case": case_name,
                    "target": assertion_name,
                    "related_assertion": assertion_name,
                    "related_scenario": triggers[0] if triggers else None,
                    "suggested_repair_targets": ISSUE_REPAIR_PRIORITIES[
                        "unreachable_or_unstimulated_assertion"
                    ],
                    "severity": "medium",
                }
            elif boundary_match:
                scenario = boundary_match.group(1)
                issue = {
                    "issue_type": "missing_boundary_stimulus",
                    "case": case_name,
                    "target": scenario,
                    "related_assertion": related_assertion_for_scenario(
                        scenario,
                        assertion_triggers,
                        scenario,
                    ),
                    "related_scenario": scenario,
                    "suggested_repair_targets": ISSUE_REPAIR_PRIORITIES[
                        "missing_boundary_stimulus"
                    ],
                    "severity": "high",
                }

            if issue:
                issue["evidence"] = issue.get("evidence", [message])
                issues.append(issue)

    deduped = []
    seen = set()
    for issue in issues:
        key = (
            issue.get("issue_type"),
            issue.get("case"),
            issue.get("target"),
            issue.get("related_assertion"),
            issue.get("related_scenario"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)

    repair_intents = [
        {
            "intent_type": issue["issue_type"],
            "case": issue.get("case"),
            "target": issue.get("target"),
            "related_assertion": issue.get("related_assertion"),
            "related_scenario": issue.get("related_scenario"),
            "repair_order": issue.get("suggested_repair_targets", []),
            "reason": issue.get("evidence", [""])[0],
        }
        for issue in deduped
    ]

    return {"issues": deduped, "repair_intents": repair_intents}


def validate_repair_schema(repair: dict[str, Any]) -> None:
    target_file = repair.get("target_file")
    repair_type = repair.get("repair_type")
    if target_file not in ALLOWED_REPAIR_FILES:
        raise ValueError(f"Unsupported repair target_file: {target_file!r}")
    if repair_type not in ALLOWED_REPAIR_TYPES:
        raise ValueError(f"Unsupported repair_type: {repair_type!r}")

    if repair_type == "replace_assertion":
        if target_file != "rtl/property_goldmine.sva":
            raise ValueError("replace_assertion may only target rtl/property_goldmine.sva")
        if not repair.get("assertion_name") or not repair.get("new_sva"):
            raise ValueError("replace_assertion requires assertion_name and new_sva")
    elif repair_type == "append_assertion":
        if target_file != "rtl/property_goldmine.sva":
            raise ValueError("append_assertion may only target rtl/property_goldmine.sva")
        if not repair.get("new_sva"):
            raise ValueError("append_assertion requires new_sva")
    elif repair_type == "insert_stimulus":
        if target_file != "sim/tb.cpp":
            raise ValueError("insert_stimulus may only target sim/tb.cpp")
        if not repair.get("anchor") or not repair.get("code"):
            raise ValueError("insert_stimulus requires anchor and code")
    elif repair_type == "replace_block":
        if not repair.get("old") or not repair.get("new"):
            raise ValueError("replace_block requires old and new")
    elif repair_type == "update_testplan_item":
        if target_file != "testplan.json":
            raise ValueError("update_testplan_item may only target testplan.json")
        if not repair.get("plan_id") or not isinstance(repair.get("updates"), dict):
            raise ValueError("update_testplan_item requires plan_id and updates object")
    elif repair_type == "append_testplan_item":
        if target_file != "testplan.json":
            raise ValueError("append_testplan_item may only target testplan.json")
        if not isinstance(repair.get("item"), dict):
            raise ValueError("append_testplan_item requires item object")


def resolve_repair_target(case_path: Path, target_file: str) -> Path:
    if target_file not in ALLOWED_REPAIR_FILES:
        raise ValueError(f"Repair target is not whitelisted: {target_file}")
    resolved_case = case_path.resolve()
    target_path = (case_path / target_file).resolve()
    if not target_path.is_relative_to(resolved_case):
        raise ValueError(f"Repair target escapes case directory: {target_file}")
    return target_path


def snapshot_target(target_path: Path, case_name: str, snapshot_dir: Path) -> Path:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{case_name}__{str(target_path.name)}"
    snapshot_path = snapshot_dir / f"{safe_name}.bak"
    suffix = 1
    while snapshot_path.exists():
        snapshot_path = snapshot_dir / f"{safe_name}.{suffix}.bak"
        suffix += 1
    snapshot_path.write_text(target_path.read_text(encoding="utf-8"), encoding="utf-8")
    return snapshot_path


def apply_replace_assertion(content: str, assertion_name: str, new_sva: str) -> str:
    escaped = re.escape(assertion_name)
    pattern = re.compile(
        rf"property\s+\w+\s*;.*?endproperty\s*{escaped}\s*:\s*assert\s+property\s*\([^;]+?\)\s*;",
        re.DOTALL,
    )
    updated, count = pattern.subn(new_sva.strip(), content, count=1)
    if count != 1:
        raise ValueError(f"Could not find assertion block for {assertion_name}")
    return updated


def apply_append_assertion(content: str, new_sva: str) -> str:
    marker = "endmodule"
    index = content.rfind(marker)
    if index == -1:
        raise ValueError("Could not find endmodule for append_assertion")
    return content[:index].rstrip() + "\n\n" + new_sva.strip() + "\n\n" + content[index:]


def apply_insert_stimulus(content: str, anchor: str, code: str) -> str:
    index = content.find(anchor)
    if index == -1:
        raise ValueError(f"Could not find insert_stimulus anchor: {anchor}")
    line_end = content.find("\n", index)
    if line_end == -1:
        line_end = len(content)
    insertion = "\n" + code.rstrip() + "\n"
    return content[: line_end + 1] + insertion + content[line_end + 1 :]


def apply_replace_block(content: str, old: str, new: str) -> str:
    if old not in content:
        raise ValueError("Could not find old block for replace_block")
    return content.replace(old, new, 1)


def apply_update_testplan_item(content: str, plan_id: str, updates: dict[str, Any]) -> str:
    testplan = json.loads(content)
    plans = testplan.setdefault("plans", [])
    for plan in plans:
        if str(plan.get("id")) == plan_id:
            plan.update(updates)
            return json.dumps(testplan, indent=2) + "\n"
    raise ValueError(f"Could not find testplan item: {plan_id}")


def apply_append_testplan_item(content: str, item: dict[str, Any]) -> str:
    testplan = json.loads(content)
    plans = testplan.setdefault("plans", [])
    item_id = item.get("id")
    if not item_id:
        raise ValueError("append_testplan_item item requires id")
    if any(str(plan.get("id")) == str(item_id) for plan in plans):
        raise ValueError(f"Testplan item already exists: {item_id}")
    plans.append(item)
    return json.dumps(testplan, indent=2) + "\n"


def apply_single_repair(content: str, repair: dict[str, Any]) -> str:
    repair_type = repair["repair_type"]
    if repair_type == "replace_assertion":
        return apply_replace_assertion(
            content,
            str(repair["assertion_name"]),
            str(repair["new_sva"]),
        )
    if repair_type == "append_assertion":
        return apply_append_assertion(content, str(repair["new_sva"]))
    if repair_type == "insert_stimulus":
        return apply_insert_stimulus(
            content,
            str(repair["anchor"]),
            str(repair["code"]),
        )
    if repair_type == "replace_block":
        return apply_replace_block(content, str(repair["old"]), str(repair["new"]))
    if repair_type == "update_testplan_item":
        return apply_update_testplan_item(
            content,
            str(repair["plan_id"]),
            dict(repair["updates"]),
        )
    if repair_type == "append_testplan_item":
        return apply_append_testplan_item(content, dict(repair["item"]))
    raise ValueError(f"Unsupported repair type: {repair_type}")


def generate_testplan(
    spec_path: Path | None,
    rtl_dir: Path | None,
    case: str | None,
    datasets_dir: Path,
    out_path: Path | None,
) -> ToolResult:
    """Create an agent-consumable testplan JSON.

    This wrapper intentionally uses a deterministic dataset-aware fallback. A
    future LLM-backed implementation can replace the plan text while preserving
    the JSON schema.
    """
    case_path = case_dir(datasets_dir, case) if case else None
    coverage = load_coverage_config(case_path) if case_path else {}
    signals = load_case(case_path) if case_path else {}
    spec_text = read_text(spec_path)

    plans = []
    plan_by_id: dict[str, dict[str, Any]] = {}
    valid_signals = signals.get("valid_signals", [])
    assertion_triggers = coverage.get("assertion_triggers", {})

    def add_plan(scenario: str, category: str, intent: str) -> None:
        if scenario in plan_by_id:
            plan = plan_by_id[scenario]
            categories = plan.setdefault("categories", [plan.get("difficulty", category)])
            if category not in categories:
                categories.append(category)
            if category == "boundary":
                plan["difficulty"] = "boundary"
                plan["intent"] = intent
            return
        plan = contract_for_scenario(
            scenario,
            category,
            intent,
            valid_signals,
            assertion_triggers,
        )
        plan["categories"] = [category]
        plan_by_id[scenario] = plan
        plans.append(plan)

    for scenario in coverage.get("required", []):
        add_plan(
            scenario,
            "required",
            f"Exercise required scenario '{scenario}' and bind any marker to observed DUT state.",
        )
    for scenario in coverage.get("bonus", []):
        add_plan(
            scenario,
            "bonus",
            f"Exercise harder scenario '{scenario}' after required behavior is stable.",
        )
    for scenario in coverage.get("boundary_cases", []):
        add_plan(
            scenario,
            "boundary",
            f"Drive edge condition '{scenario}' and check relevant assertion activation.",
        )

    output = {
        "case": case,
        "spec_path": str(spec_path) if spec_path else None,
        "rtl_dir": str(rtl_dir or (case_path / "rtl" if case_path else "")),
        "top_module": signals.get("top_module"),
        "valid_signals": valid_signals,
        "spec_excerpt": spec_text[:1000],
        "plans": plans,
        "assertion_triggers": assertion_triggers,
    }

    artifacts = {}
    if case_path:
        case_testplan_path = case_path / "testplan.json"
        case_testplan_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
        artifacts["case_testplan_json"] = str(case_testplan_path)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
        artifacts["testplan_json"] = str(out_path)

    return ToolResult(True, "generate_testplan", output, artifacts)


def generate_sva(
    testplan_path: Path,
    rtl_dir: Path,
    out_path: Path | None,
    llm_command: str | None,
    allow_scaffold: bool,
) -> ToolResult:
    """Generate SVAs through an external LLM/agent command."""
    testplan = json.loads(testplan_path.read_text(encoding="utf-8"))
    property_path = rtl_dir / "property_goldmine.sva"
    template = property_path.read_text(encoding="utf-8") if property_path.exists() else ""
    mode = "llm"
    try:
        llm_response = call_llm_json(
            llm_command,
            "generate_sva",
            build_sva_generation_prompt(testplan, rtl_dir),
        )
        assertions = [
            {**assertion, "status": assertion.get("status", "llm_generated")}
            for assertion in extract_assertions_from_llm_response(llm_response, testplan)
        ]
        needs_stimulus = extract_needs_stimulus_from_llm_response(llm_response, testplan)
        if not assertions:
            if not needs_stimulus:
                raise RuntimeError("LLM response did not contain assertions or needs_stimulus.")
    except Exception:
        if not allow_scaffold:
            raise
        mode = "scaffold_fallback"
        assertions = scaffold_assertions(testplan)
        needs_stimulus = []

    output = {
        "testplan": str(testplan_path),
        "rtl_dir": str(rtl_dir),
        "template_available": bool(template),
        "mode": mode,
        "assertions": assertions,
        "needs_stimulus": needs_stimulus,
    }

    artifacts = {}
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
        artifacts["sva_json"] = str(out_path)

    return ToolResult(True, "generate_sva", output, artifacts)


def run_verilator(
    case: str,
    datasets_dir: Path,
    verilator: Path,
    build_root: Path,
    mode: str,
    variant: str,
    keep_build: bool,
) -> ToolResult:
    case_path = case_dir(datasets_dir, case)
    case_config = load_case(case_path)
    runs = []
    ok = True
    for selected_variant in select_variants(case_config, variant):
        if mode == "lint":
            result = run_lint(verilator, case_path, selected_variant)
        else:
            result = run_simulation(
                verilator=verilator,
                case_dir=case_path,
                variant=selected_variant,
                build_root=build_root,
                keep_build=keep_build,
            )
        expected_pass = selected_variant.expected == "pass"
        passed = result.returncode == 0 if expected_pass else result.returncode != 0
        ok = ok and passed
        runs.append(
            {
                "variant": selected_variant.name,
                "top_module": selected_variant.top_module,
                "expected": selected_variant.expected,
                "returncode": result.returncode,
                "passed_expectation": passed,
                "log_excerpt": result.stdout[-4000:],
            }
        )

    return ToolResult(ok, "run_verilator", {"case": case, "mode": mode, "runs": runs}, {})


def run_coverage_closure(
    case: str,
    datasets_dir: Path,
    verilator: Path,
    build_root: Path,
    iteration: int,
    verbose: bool,
) -> ToolResult:
    case_path = case_dir(datasets_dir, case)
    case_run = run_closure_case(
        case_dir=case_path,
        verilator=verilator,
        build_root=build_root,
        iteration=iteration,
        verbose=verbose,
    )
    summary = summarize_iteration([case_run])
    feedback_path = write_iteration_artifacts(build_root, iteration, summary)
    output = {
        "case": case,
        "iteration": iteration,
        "summary": summary,
        "feedback": targeted_feedback(summary),
    }
    return ToolResult(
        True,
        "run_coverage_closure",
        output,
        {"feedback_json": str(feedback_path), "summary_json": str(feedback_path.parent / "summary.json")},
    )


def parse_feedback(feedback_path: Path | None, summary_path: Path | None) -> ToolResult:
    if feedback_path:
        feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    elif summary_path:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        feedback = targeted_feedback(summary)
    else:
        raise ValueError("parse_feedback requires --feedback-json or --summary-json")

    actions = []
    for item in feedback.get("targets", []):
        case_name = item.get("case")
        for target in item.get("targets", []):
            if "Mutation target" in target or "Mutation set" in target:
                action_type = "repair_sva_or_strengthen_stimulus"
            elif "Assertion '" in target:
                action_type = "activate_assertion_trigger"
            elif "boundary case" in target.lower():
                action_type = "repair_testbench_boundary"
            elif "bonus scenario" in target:
                action_type = "repair_testbench_bonus"
            else:
                action_type = "inspect"
            actions.append({"case": case_name, "action_type": action_type, "message": target})

    output = {"feedback": feedback, "recommended_actions": actions}
    return ToolResult(True, "parse_feedback", output, {})


def diagnose_feedback_tool(
    feedback_path: Path | None,
    summary_path: Path | None,
    failure_log: Path | None,
    case: str | None,
    datasets_dir: Path,
    out_path: Path | None,
) -> ToolResult:
    if feedback_path:
        feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    elif summary_path:
        summary_from_path = json.loads(summary_path.read_text(encoding="utf-8"))
        feedback = targeted_feedback(summary_from_path)
    else:
        raise ValueError("diagnose-feedback requires --feedback-json or --summary-json")

    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path else None
    failure_issues = parse_failure_log(failure_log, None, case)
    diagnosis = diagnose_feedback(feedback, summary, datasets_dir, case, failure_issues)

    artifacts = {}
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(diagnosis, indent=2) + "\n", encoding="utf-8")
        artifacts["diagnosis_json"] = str(out_path)
    return ToolResult(True, "diagnose_feedback", diagnosis, artifacts)


def prior_intent_attempts(trajectory: dict[str, Any], intent_type: str, target: str | None) -> int:
    attempts = 0
    for iteration in trajectory.get("iterations", []):
        for intent in iteration.get("repair_intents", []):
            if intent.get("intent_type") == intent_type and (
                intent.get("target_scenario") == target
                or intent.get("target_mutation") == target
                or intent.get("target_assertion") == target
                or intent.get("target") == target
            ):
                attempts += 1
    return attempts


def issue_reason(issue: dict[str, Any]) -> str:
    evidence = issue.get("evidence", [])
    if isinstance(evidence, list) and evidence:
        return str(evidence[0])
    return str(issue.get("target") or issue.get("issue_type") or "")


def plan_repair_intents(
    diagnosis: dict[str, Any],
    trajectory: dict[str, Any],
) -> list[dict[str, Any]]:
    issues = diagnosis.get("issues", [])
    mutation_by_scenario: dict[str, list[dict[str, Any]]] = {}
    inactive_by_scenario: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        scenario = issue.get("related_scenario")
        if not scenario:
            continue
        if issue.get("issue_type") == "weak_assertion_or_missing_stimulus":
            mutation_by_scenario.setdefault(scenario, []).append(issue)
        elif issue.get("issue_type") == "unreachable_or_unstimulated_assertion":
            inactive_by_scenario.setdefault(scenario, []).append(issue)

    intents = []
    covered_issue_keys = set()

    def mark(issue: dict[str, Any]) -> None:
        covered_issue_keys.add(
            (
                issue.get("issue_type"),
                issue.get("case"),
                issue.get("target"),
                issue.get("related_scenario"),
            )
        )

    def is_marked(issue: dict[str, Any]) -> bool:
        return (
            issue.get("issue_type"),
            issue.get("case"),
            issue.get("target"),
            issue.get("related_scenario"),
        ) in covered_issue_keys

    for issue in issues:
        if issue.get("issue_type") != "missing_boundary_stimulus":
            continue
        scenario = issue.get("related_scenario") or issue.get("target")
        related_mutations = mutation_by_scenario.get(str(scenario), [])
        related_inactive = inactive_by_scenario.get(str(scenario), [])
        reason_parts = [issue_reason(issue)]
        reason_parts.extend(issue_reason(item) for item in related_mutations[:2])
        reason_parts.extend(issue_reason(item) for item in related_inactive[:1])
        intent = {
            "intent_type": "add_boundary_testplan_and_stimulus",
            "case": issue.get("case"),
            "target_scenario": scenario,
            "target_mutations": [item.get("target") for item in related_mutations],
            "target_assertions": sorted(
                {
                    item.get("related_assertion")
                    for item in [issue, *related_mutations, *related_inactive]
                    if item.get("related_assertion")
                }
            ),
            "repair_order": ["testplan", "tb.cpp", "sva"] if related_mutations else ["testplan", "tb.cpp"],
            "reason": " ".join(part for part in reason_parts if part),
            "prior_attempts": prior_intent_attempts(
                trajectory,
                "add_boundary_testplan_and_stimulus",
                str(scenario),
            ),
        }
        intents.append(intent)
        mark(issue)
        for item in related_mutations + related_inactive:
            mark(item)

    for issue in issues:
        if is_marked(issue):
            continue
        issue_type = issue.get("issue_type")
        if issue_type == "false_positive_assertion":
            target = issue.get("related_assertion") or issue.get("target")
            intents.append(
                {
                    "intent_type": "repair_false_positive_assertion",
                    "case": issue.get("case"),
                    "target_assertion": target,
                    "repair_order": ["sva"],
                    "reason": issue_reason(issue),
                    "prior_attempts": prior_intent_attempts(
                        trajectory,
                        "repair_false_positive_assertion",
                        str(target),
                    ),
                }
            )
        elif issue_type == "weak_assertion_or_missing_stimulus":
            target = issue.get("target")
            intents.append(
                {
                    "intent_type": "expose_or_kill_mutation",
                    "case": issue.get("case"),
                    "target_mutation": target,
                    "target_scenario": issue.get("related_scenario"),
                    "target_assertion": issue.get("related_assertion"),
                    "repair_order": ["tb.cpp", "sva"],
                    "reason": issue_reason(issue),
                    "prior_attempts": prior_intent_attempts(
                        trajectory,
                        "expose_or_kill_mutation",
                        str(target),
                    ),
                }
            )
        elif issue_type == "unreachable_or_unstimulated_assertion":
            scenario = issue.get("related_scenario")
            intents.append(
                {
                    "intent_type": "activate_assertion_trigger_with_plan_and_stimulus",
                    "case": issue.get("case"),
                    "target_scenario": scenario,
                    "target_assertion": issue.get("related_assertion") or issue.get("target"),
                    "repair_order": ["testplan", "tb.cpp"],
                    "reason": issue_reason(issue),
                    "prior_attempts": prior_intent_attempts(
                        trajectory,
                        "activate_assertion_trigger_with_plan_and_stimulus",
                        str(scenario),
                    ),
                }
            )

    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    severity_by_target = {
        (
            issue.get("case"),
            issue.get("related_scenario") or issue.get("target"),
        ): severity_rank.get(str(issue.get("severity")), 9)
        for issue in issues
    }
    return sorted(
        intents,
        key=lambda intent: (
            severity_by_target.get(
                (
                    intent.get("case"),
                    intent.get("target_scenario")
                    or intent.get("target_mutation")
                    or intent.get("target_assertion"),
                ),
                9,
            ),
            0 if intent.get("target_mutations") else 1,
            0 if "sva" in intent.get("repair_order", []) else 1,
            intent.get("prior_attempts", 0),
            intent.get("case") or "",
            intent.get("target_scenario")
            or intent.get("target_mutation")
            or intent.get("target_assertion")
            or "",
        ),
    )


def plan_repair_tool(
    feedback_path: Path,
    summary_path: Path,
    trajectory_path: Path | None,
    datasets_dir: Path,
    out_path: Path | None,
    case: str | None = None,
    failure_log: Path | None = None,
) -> ToolResult:
    feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    trajectory = (
        json.loads(trajectory_path.read_text(encoding="utf-8"))
        if trajectory_path and trajectory_path.exists()
        else {}
    )
    failure_issues = parse_failure_log(failure_log, None, case)
    diagnosis = diagnose_feedback(feedback, summary, datasets_dir, case, failure_issues)
    repair_intents = plan_repair_intents(diagnosis, trajectory)
    output = {
        "diagnosis": diagnosis,
        "repair_intents": repair_intents,
    }
    artifacts = {}
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
        artifacts["repair_intent_json"] = str(out_path)
    return ToolResult(True, "plan_repair", output, artifacts)


def parse_failure_log_tool(
    log_path: Path,
    case: str | None,
    out_path: Path | None,
) -> ToolResult:
    output = {"issues": parse_failure_log(log_path, None, case)}
    artifacts = {}
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
        artifacts["failure_diagnosis_json"] = str(out_path)
    return ToolResult(True, "parse_failure_log", output, artifacts)


def repair_testplan(
    diagnosis_path: Path | None,
    out_path: Path | None,
    llm_command: str | None,
    case: str,
    datasets_dir: Path,
    allow_plan: bool,
) -> ToolResult:
    if not diagnosis_path:
        raise ValueError("repair-testplan requires --diagnosis-json or --repair-intent-json")
    diagnosis_payload = json.loads(diagnosis_path.read_text(encoding="utf-8"))
    diagnosis = diagnosis_payload.get("diagnosis", diagnosis_payload)
    case_path = case_dir(datasets_dir, case)
    testplan_path = case_path / "testplan.json"
    if not testplan_path.exists():
        generate_testplan(None, case_path / "rtl", case, datasets_dir, testplan_path)
    testplan = json.loads(testplan_path.read_text(encoding="utf-8"))
    coverage = load_coverage_config(case_path)

    mode = "llm"
    try:
        llm_response = call_llm_json(
            llm_command,
            "repair_testplan",
            build_testplan_repair_prompt(diagnosis, testplan, coverage),
        )
        repairs = llm_response.get("repairs")
        if not isinstance(repairs, list) or not repairs:
            raise RuntimeError("LLM response did not contain non-empty repairs.")
        for repair in repairs:
            if not isinstance(repair, dict):
                raise RuntimeError("Each repair must be a JSON object.")
            validate_repair_schema(repair)
        output = {"mode": mode, "repairs": repairs}
    except Exception:
        if not allow_plan:
            raise
        mode = "plan_fallback"
        proposals = []
        existing_plan_ids = {str(plan.get("id")) for plan in testplan.get("plans", [])}
        for issue in diagnosis.get("issues", []):
            if issue.get("case") != case:
                continue
            if "testplan" not in issue.get("suggested_repair_targets", []):
                continue
            scenario = issue.get("related_scenario") or issue.get("target")
            repair_type = "update_testplan_item" if scenario in existing_plan_ids else "append_testplan_item"
            proposals.append(
                {
                    "case": case,
                    "repair_type": repair_type,
                    "target_file": "testplan.json",
                    "plan_id": scenario if repair_type == "update_testplan_item" else None,
                    "message": issue.get("evidence", [""])[0],
                    "instruction": (
                        "Revise the testplan intent and expected_scenarios so the next "
                        "SVA/testbench repair is grounded in this scenario."
                    ),
                }
            )
        output = {"mode": mode, "proposals": proposals}

    artifacts = {}
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
        artifacts["repair_testplan_plan"] = str(out_path)
    return ToolResult(True, "repair_testplan", output, artifacts)


def repair_sva(
    feedback_path: Path,
    out_path: Path | None,
    llm_command: str | None,
    sva_json_path: Path | None,
    summary_json_path: Path | None,
    rtl_dir: Path | None,
    allow_plan: bool,
) -> ToolResult:
    parsed = parse_feedback(feedback_path, None).output
    sva_json = json.loads(sva_json_path.read_text(encoding="utf-8")) if sva_json_path else None
    summary_json = (
        json.loads(summary_json_path.read_text(encoding="utf-8")) if summary_json_path else None
    )
    mode = "llm"
    try:
        llm_response = call_llm_json(
            llm_command,
            "repair_sva",
            build_sva_repair_prompt(parsed["feedback"], sva_json, summary_json, rtl_dir),
        )
        repairs = llm_response.get("repairs")
        if not isinstance(repairs, list) or not repairs:
            raise RuntimeError("LLM response did not contain non-empty repairs.")
        for repair in repairs:
            if not isinstance(repair, dict):
                raise RuntimeError("Each repair must be a JSON object.")
            validate_repair_schema(repair)
        output = {"mode": mode, "repairs": repairs}
    except Exception:
        if not allow_plan:
            raise
        mode = "plan_fallback"
        proposals = []
        for action in parsed["recommended_actions"]:
            if action["action_type"] in {"repair_sva_or_strengthen_stimulus", "activate_assertion_trigger"}:
                proposals.append(
                    {
                        "case": action["case"],
                        "repair_type": "sva",
                        "message": action["message"],
                        "instruction": (
                            "Inspect the related assertion trigger and mutation log. "
                            "Strengthen the property only if correct RTL still passes; otherwise revise stimulus."
                        ),
                    }
                )
        output = {"mode": mode, "proposals": proposals}

    artifacts = {}
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
        artifacts["repair_sva_plan"] = str(out_path)

    return ToolResult(True, "repair_sva", output, artifacts)


def repair_testbench(
    feedback_path: Path,
    out_path: Path | None,
    llm_command: str | None,
    summary_json_path: Path | None,
    case: str | None,
    datasets_dir: Path,
    allow_plan: bool,
) -> ToolResult:
    parsed = parse_feedback(feedback_path, None).output
    case_name = case or next(
        (
            action["case"]
            for action in parsed["recommended_actions"]
            if action.get("case")
        ),
        None,
    )
    if not case_name:
        raise ValueError("repair-testbench requires --case when feedback has no case.")
    case_path = case_dir(datasets_dir, case_name)
    summary_json = (
        json.loads(summary_json_path.read_text(encoding="utf-8")) if summary_json_path else None
    )

    mode = "llm"
    try:
        llm_response = call_llm_json(
            llm_command,
            "repair_testbench",
            build_testbench_repair_prompt(parsed["feedback"], summary_json, case_path),
        )
        repairs = llm_response.get("repairs")
        if not isinstance(repairs, list) or not repairs:
            raise RuntimeError("LLM response did not contain non-empty repairs.")
        for repair in repairs:
            if not isinstance(repair, dict):
                raise RuntimeError("Each repair must be a JSON object.")
            validate_repair_schema(repair)
        output = {"mode": mode, "repairs": repairs}
    except Exception:
        if not allow_plan:
            raise
        mode = "plan_fallback"
        proposals = []
        for action in parsed["recommended_actions"]:
            if action["action_type"].startswith("repair_testbench") or action["action_type"] == "activate_assertion_trigger":
                proposals.append(
                    {
                        "case": action["case"],
                        "repair_type": "testbench",
                        "message": action["message"],
                        "instruction": (
                            "Add a stimulus phase for the missing scenario and print SCENARIO:<name> "
                            "only after observing the corresponding DUT state."
                        ),
                    }
                )
        output = {"mode": mode, "proposals": proposals}

    artifacts = {}
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
        artifacts["repair_testbench_plan"] = str(out_path)

    return ToolResult(True, "repair_testbench", output, artifacts)


def apply_repair(
    repair_json_path: Path,
    case: str,
    datasets_dir: Path,
    out_applied: Path | None,
    snapshot_dir: Path,
) -> ToolResult:
    repair_payload = json.loads(repair_json_path.read_text(encoding="utf-8"))
    repairs = repair_payload.get("repairs")
    if not isinstance(repairs, list) or not repairs:
        raise ValueError("repair JSON must contain a non-empty repairs list")

    case_path = case_dir(datasets_dir, case)
    applied = []
    snapshots: dict[str, str] = {}
    target_contents: dict[Path, str] = {}

    for index, repair in enumerate(repairs):
        if not isinstance(repair, dict):
            raise ValueError(f"Repair at index {index} must be an object")
        validate_repair_schema(repair)
        target_path = resolve_repair_target(case_path, str(repair["target_file"]))
        if target_path not in target_contents:
            target_contents[target_path] = target_path.read_text(encoding="utf-8")
        target_contents[target_path] = apply_single_repair(target_contents[target_path], repair)
        applied.append(
            {
                "index": index,
                "target_file": repair["target_file"],
                "target_path": str(target_path),
                "repair_type": repair["repair_type"],
                "rationale": repair.get("rationale"),
                "expected_markers": repair.get("expected_markers", []),
            }
        )

    for target_path, updated_content in target_contents.items():
        snapshots[str(target_path)] = str(snapshot_target(target_path, case, snapshot_dir))
        target_path.write_text(updated_content, encoding="utf-8")

    output = {
        "case": case,
        "repair_json": str(repair_json_path),
        "applied": applied,
        "snapshots": snapshots,
    }
    artifacts = {"snapshot_dir": str(snapshot_dir)}
    if out_applied:
        out_applied.parent.mkdir(parents=True, exist_ok=True)
        out_applied.write_text(json.dumps(output, indent=2), encoding="utf-8")
        artifacts["applied_patch_json"] = str(out_applied)

    return ToolResult(True, "apply_repair", output, artifacts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AssertPilot agent-callable tools.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen_tp = subparsers.add_parser("generate-testplan")
    gen_tp.add_argument("--spec", type=Path)
    gen_tp.add_argument("--rtl-dir", type=Path)
    gen_tp.add_argument("--case")
    gen_tp.add_argument("--datasets-dir", type=Path, default=DEFAULT_DATASETS_DIR)
    gen_tp.add_argument("--out", type=Path)

    gen_sva = subparsers.add_parser("generate-sva")
    gen_sva.add_argument("--testplan-json", type=Path, required=True)
    gen_sva.add_argument("--rtl-dir", type=Path, required=True)
    gen_sva.add_argument("--out", type=Path)
    gen_sva.add_argument("--llm-command", default=None)
    gen_sva.add_argument(
        "--allow-scaffold",
        action="store_true",
        help="Allow TODO scaffold output when no LLM command is configured.",
    )

    verilator = subparsers.add_parser("run-verilator")
    verilator.add_argument("--case", required=True)
    verilator.add_argument("--datasets-dir", type=Path, default=DEFAULT_DATASETS_DIR)
    verilator.add_argument("--verilator", type=Path, default=DEFAULT_VERILATOR)
    verilator.add_argument("--build-root", type=Path, default=DEFAULT_RUNS_DIR / "verilator")
    verilator.add_argument("--mode", choices=["lint", "simulate"], default="simulate")
    verilator.add_argument("--variant", choices=["correct", "buggy", "both"], default="both")
    verilator.add_argument("--keep-build", action="store_true")

    closure = subparsers.add_parser("run-coverage-closure")
    closure.add_argument("--case", required=True)
    closure.add_argument("--datasets-dir", type=Path, default=DEFAULT_DATASETS_DIR)
    closure.add_argument("--verilator", type=Path, default=DEFAULT_VERILATOR)
    closure.add_argument("--build-root", type=Path, default=DEFAULT_CLOSURE_BUILD_ROOT)
    closure.add_argument("--iteration", type=int, default=0)
    closure.add_argument("--verbose", action="store_true")

    parse = subparsers.add_parser("parse-feedback")
    parse.add_argument("--feedback-json", type=Path)
    parse.add_argument("--summary-json", type=Path)

    parse_log = subparsers.add_parser("parse-failure-log")
    parse_log.add_argument("--log", type=Path, required=True)
    parse_log.add_argument("--case")
    parse_log.add_argument("--out", type=Path)

    diagnose = subparsers.add_parser("diagnose-feedback")
    diagnose.add_argument("--feedback-json", type=Path)
    diagnose.add_argument("--summary-json", type=Path)
    diagnose.add_argument("--failure-log", type=Path)
    diagnose.add_argument("--case")
    diagnose.add_argument("--datasets-dir", type=Path, default=DEFAULT_DATASETS_DIR)
    diagnose.add_argument("--out", type=Path)

    plan_repair = subparsers.add_parser("plan-repair")
    plan_repair.add_argument("--feedback-json", type=Path, required=True)
    plan_repair.add_argument("--summary-json", type=Path, required=True)
    plan_repair.add_argument("--trajectory-json", type=Path)
    plan_repair.add_argument("--failure-log", type=Path)
    plan_repair.add_argument("--datasets-dir", type=Path, default=DEFAULT_DATASETS_DIR)
    plan_repair.add_argument("--case")
    plan_repair.add_argument("--out", type=Path)

    repair_tp_parser = subparsers.add_parser("repair-testplan")
    repair_tp_parser.add_argument("--diagnosis-json", type=Path)
    repair_tp_parser.add_argument("--repair-intent-json", type=Path, dest="diagnosis_json")
    repair_tp_parser.add_argument("--out", type=Path)
    repair_tp_parser.add_argument("--llm-command", default=None)
    repair_tp_parser.add_argument("--case", required=True)
    repair_tp_parser.add_argument("--datasets-dir", type=Path, default=DEFAULT_DATASETS_DIR)
    repair_tp_parser.add_argument(
        "--allow-plan",
        action="store_true",
        help="Allow repair-plan fallback when no LLM command is configured.",
    )

    repair_sva_parser = subparsers.add_parser("repair-sva")
    repair_sva_parser.add_argument("--feedback-json", type=Path, required=True)
    repair_sva_parser.add_argument("--out", type=Path)
    repair_sva_parser.add_argument("--llm-command", default=None)
    repair_sva_parser.add_argument("--sva-json", type=Path)
    repair_sva_parser.add_argument("--summary-json", type=Path)
    repair_sva_parser.add_argument("--rtl-dir", type=Path)
    repair_sva_parser.add_argument(
        "--allow-plan",
        action="store_true",
        help="Allow repair-plan fallback when no LLM command is configured.",
    )

    repair_tb_parser = subparsers.add_parser("repair-testbench")
    repair_tb_parser.add_argument("--feedback-json", type=Path, required=True)
    repair_tb_parser.add_argument("--out", type=Path)
    repair_tb_parser.add_argument("--llm-command", default=None)
    repair_tb_parser.add_argument("--summary-json", type=Path)
    repair_tb_parser.add_argument("--case")
    repair_tb_parser.add_argument("--datasets-dir", type=Path, default=DEFAULT_DATASETS_DIR)
    repair_tb_parser.add_argument(
        "--allow-plan",
        action="store_true",
        help="Allow repair-plan fallback when no LLM command is configured.",
    )

    apply_parser = subparsers.add_parser("apply-repair")
    apply_parser.add_argument("--repair-json", type=Path, required=True)
    apply_parser.add_argument("--case", required=True)
    apply_parser.add_argument("--datasets-dir", type=Path, default=DEFAULT_DATASETS_DIR)
    apply_parser.add_argument("--out-applied", type=Path)
    apply_parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=DEFAULT_RUNS_DIR / "snapshots",
        help="Directory for pre-apply file snapshots.",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "generate-testplan":
            result = generate_testplan(args.spec, args.rtl_dir, args.case, args.datasets_dir, args.out)
        elif args.command == "generate-sva":
            result = generate_sva(
                args.testplan_json,
                args.rtl_dir,
                args.out,
                args.llm_command,
                args.allow_scaffold,
            )
        elif args.command == "run-verilator":
            result = run_verilator(
                args.case,
                args.datasets_dir,
                args.verilator,
                args.build_root,
                args.mode,
                args.variant,
                args.keep_build,
            )
        elif args.command == "run-coverage-closure":
            result = run_coverage_closure(
                args.case,
                args.datasets_dir,
                args.verilator,
                args.build_root,
                args.iteration,
                args.verbose,
            )
        elif args.command == "parse-feedback":
            result = parse_feedback(args.feedback_json, args.summary_json)
        elif args.command == "parse-failure-log":
            result = parse_failure_log_tool(args.log, args.case, args.out)
        elif args.command == "diagnose-feedback":
            result = diagnose_feedback_tool(
                args.feedback_json,
                args.summary_json,
                args.failure_log,
                args.case,
                args.datasets_dir,
                args.out,
            )
        elif args.command == "plan-repair":
            result = plan_repair_tool(
                args.feedback_json,
                args.summary_json,
                args.trajectory_json,
                args.datasets_dir,
                args.out,
                args.case,
                args.failure_log,
            )
        elif args.command == "repair-testplan":
            result = repair_testplan(
                args.diagnosis_json,
                args.out,
                args.llm_command,
                args.case,
                args.datasets_dir,
                args.allow_plan,
            )
        elif args.command == "repair-sva":
            result = repair_sva(
                args.feedback_json,
                args.out,
                args.llm_command,
                args.sva_json,
                args.summary_json,
                args.rtl_dir,
                args.allow_plan,
            )
        elif args.command == "repair-testbench":
            result = repair_testbench(
                args.feedback_json,
                args.out,
                args.llm_command,
                args.summary_json,
                args.case,
                args.datasets_dir,
                args.allow_plan,
            )
        elif args.command == "apply-repair":
            result = apply_repair(
                args.repair_json,
                args.case,
                args.datasets_dir,
                args.out_applied,
                args.snapshot_dir,
            )
        else:
            parser.error(f"Unsupported command: {args.command}")
            return 2
        return emit(result)
    except Exception as exc:
        return emit(ToolResult(False, args.command, {}, {}, str(exc)))


if __name__ == "__main__":
    raise SystemExit(main())
