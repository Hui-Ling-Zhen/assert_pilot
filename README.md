# AssertPilot

AssertPilot generates and checks hardware verification assets from natural-language specifications and RTL.

The core flow is:

```text
spec + RTL
  -> knowledge graph / retrieval context
  -> natural-language test plan
  -> SystemVerilog Assertions (SVAs)
  -> verification backend
```

Generated SVAs are intentionally plan-guided: the natural-language test plan acts as the intermediate representation between the spec/RTL context and concrete assertion code.

## Key Features

- Builds structured context from specifications and RTL to guide LLM generation.
- Generates natural-language test plans for selected architectural signals.
- Generates candidate SystemVerilog Assertions from the test plans.
- Supports two verification backends:
  - `jasper`: Cadence JasperGold formal verification.
  - `verilator`: open-source simulation-driven assertion checking.
- Includes small runnable datasets for backend smoke testing.

## Repository Layout

```text
AssertPilot/
  README.md
  src/
    config.py              # Main configuration
    main.py                # Entry point
    gen_plan.py            # Test-plan, SVA, and backend orchestration
    verilator_backend.py   # Verilator simulation backend
    ...
  datasets/
    counter/
    fifo/
    handshake/
    arbiter/
  scripts/
    run_dataset_verilator.py
    run_coverage_closure.py
```

## Verification Backends

| Backend | Tool | Purpose | Result Meaning |
|---------|------|---------|----------------|
| `jasper` | JasperGold | Formal proof and coverage for generated SVAs | `proven`, `covered`, counterexample, or error |
| `verilator` | Verilator | Simulation-time assertion checking with a user-provided C++ testbench | pass/fail for the executed tests |
| `both` | JasperGold + Verilator | Run both backend flows | formal result plus simulation result |

Verilator is not a drop-in replacement for JasperGold. A Verilator pass only means no assertion failed in the exercised simulation traces.

## Quick Smoke Test

The bundled datasets can be checked without running the full LLM pipeline.

Run Verilator lint for all dataset cases:

```bash
cd /Users/huilingzhen/Desktop/0002-personal-projects/verification/SVA-checker/AssertPilot
./scripts/run_dataset_verilator.py --mode lint
```

Run full simulation smoke tests:

```bash
./scripts/run_dataset_verilator.py --mode simulate
```

Run the feedback-driven coverage closure loop:

```bash
./scripts/run_coverage_closure.py --max-iters 3
```

The closure runner currently separates stimulus coverage from assertion-quality signals and computes a weighted score:

- `0.25 * correct_pass`
- `0.25 * mutation_kill_rate`
- `0.25 * scenario_coverage`
- `0.15 * assertion_activation_rate`
- `0.10 * boundary_case_coverage`

`scenario_coverage` comes from required and bonus `SCENARIO:<name>` markers. `assertion_activation_rate` is approximated by `coverage_scenarios.json` mappings from assertion names to trigger scenarios. `boundary_case_coverage` tracks harder edge-condition markers separately from general scenario progress. Each file in `rtl/mutations/` is compiled and run independently, then scored as `killed_mutations / total_mutations`.

If proxy coverage is below 100%, the runner writes targeted feedback to:

```text
runs/coverage_closure/iter_<N>/targeted_feedback.json
```

## Agent Tool Interface

AssertPilot can also be used as an agent-callable toolset. The wrapper script emits JSON for every command:

```bash
./scripts/assertpilot_tools.py generate-testplan --case fifo --out runs/agent_tools/fifo_testplan.json
./scripts/assertpilot_tools.py generate-sva \
  --testplan-json runs/agent_tools/fifo_testplan.json \
  --rtl-dir datasets/fifo/rtl \
  --llm-command "python /path/to/llm_adapter.py" \
  --out runs/agent_tools/fifo_sva.json
./scripts/assertpilot_tools.py run-verilator --case fifo --variant both
./scripts/assertpilot_tools.py run-coverage-closure --case fifo --iteration 0
./scripts/assertpilot_tools.py parse-feedback --feedback-json runs/coverage_closure/iter_0/targeted_feedback.json
./scripts/assertpilot_tools.py repair-sva \
  --feedback-json runs/coverage_closure/iter_0/targeted_feedback.json \
  --sva-json runs/agent_tools/fifo_sva.json \
  --rtl-dir datasets/fifo/rtl \
  --llm-command "python /path/to/llm_adapter.py" \
  --out runs/agent_tools/repair_sva.json
./scripts/assertpilot_tools.py repair-testbench --feedback-json runs/coverage_closure/iter_0/targeted_feedback.json --out runs/agent_tools/repair_tb.json
```

`generate-sva` and `repair-sva` are LLM-backed by default. The external command receives:

```text
ASSERTPILOT_LLM_PROMPT_JSON
ASSERTPILOT_LLM_TASK
```

and must print JSON to stdout. For `generate-sva`, return:

```json
{"assertions": [{"name": "assert_example", "plan_id": "fifo_reset_empty", "sva": "..."}]}
```

For `repair-sva`, return:

```json
{
  "repairs": [
    {
      "target_file": "rtl/property_goldmine.sva",
      "repair_type": "replace_assertion",
      "assertion_name": "assert_no_underflow",
      "new_sva": "property no_underflow; ... endproperty\nassert_no_underflow: assert property(no_underflow);",
      "rationale": "..."
    }
  ]
}
```

For `repair-testbench`, return:

```json
{
  "repairs": [
    {
      "target_file": "sim/tb.cpp",
      "repair_type": "insert_stimulus",
      "anchor": "// Extra write while full",
      "code": "...",
      "expected_markers": ["fifo_read_from_empty"]
    }
  ]
}
```

Apply a structured repair patch with:

```bash
./scripts/assertpilot_tools.py apply-repair \
  --repair-json runs/agent_tools/repair_sva.json \
  --case fifo \
  --out-applied runs/agent_tools/applied_patch.json
```

`apply-repair` only writes whitelisted files inside the selected dataset case: `rtl/property_goldmine.sva` and `sim/tb.cpp`. It rejects path escapes and unsupported repair types. Supported repair types are `replace_assertion`, `append_assertion`, `insert_stimulus`, and `replace_block`; a snapshot is saved before any target file is written.

For local smoke tests without an LLM, pass `--allow-scaffold` to `generate-sva` or `--allow-plan` to `repair-sva`; these modes are explicit fallbacks and are not treated as real generation.

The lightweight loop scaffold stores a trajectory while repeatedly running closure and producing repair plans:

```bash
./scripts/run_assertion_agent.py \
  --case fifo \
  --target-score 1.0 \
  --max-iters 3 \
  --llm-command "python /path/to/llm_adapter.py" \
  --policy-command "python /path/to/policy_adapter.py"
```

`--policy-command` is optional. When present, it chooses actions from `repair_sva`, `repair_testbench`, `inspect`, and `stop` using the current trajectory and parsed feedback. Without it, the loop falls back to a deterministic policy.

Structured repairs are applied to a candidate copy before they are evaluated:

```text
runs/agent_tools/assertion_agent/iter_<N>/candidate/<case>/
```

The loop runs coverage closure on the candidate dataset root and accepts the candidate only if:

- correct RTL still passes
- the candidate score is greater than the previous score
- the correct RTL returns zero status
- required coverage does not regress

Rejected candidates stay isolated under the iteration directory and the original dataset is not modified. Each trajectory iteration records `repair_json`, `applied_patch`, `candidate_summary`, `old_score`, `new_score`, `decision`, and `reject_reason` so the result can be reused for later self-evolution.

Without an external LLM repair adapter, the loop includes a minimal built-in testbench-anchor baseline for known dataset gaps. For example, when `fifo_read_from_empty` is missing, it generates a structured `insert_stimulus` patch anchored at `top->rst = 0;`, applies it to a candidate copy, and reruns closure. This keeps the loop runnable while preserving the same repair schema expected from an LLM.

## Agent Loop Demo

The current built-in demo focuses on curriculum Level 3 boundary repair as the first autonomous action. The agent reads targeted closure feedback, emits a structured `insert_stimulus` repair for `sim/tb.cpp`, applies it only to a candidate copy, reruns closure, and accepts the candidate if the score improves without breaking required coverage or correct-RTL assertions. For Level 3, the candidate must also improve either `boundary_case_coverage` or `mutation_coverage`.

Run the baseline:

```bash
./scripts/run_coverage_closure.py \
  --max-iters 1 \
  --build-root runs/agent_tools/experiments/baseline_closure
```

Run one Level 3 agent iteration for each case:

```bash
./scripts/run_assertion_agent.py --case fifo --curriculum-level 3 --max-iters 1 --allow-scaffold --allow-repair-plan
./scripts/run_assertion_agent.py --case counter --curriculum-level 3 --max-iters 1 --allow-scaffold --allow-repair-plan
./scripts/run_assertion_agent.py --case arbiter --curriculum-level 3 --max-iters 1 --allow-scaffold --allow-repair-plan
./scripts/run_assertion_agent.py --case handshake --curriculum-level 3 --max-iters 1 --allow-scaffold --allow-repair-plan
```

Observed baseline over all four datasets:

| Metric | Baseline Closure |
| --- | ---: |
| Weighted score | 67.77% |
| Required coverage | 100.00% |
| Bonus coverage | 0.00% |
| Mutation coverage | 75.00% |
| Scenario coverage | 45.09% |
| Assertion activation rate | 74.17% |
| Boundary case coverage | 16.25% |

Observed Level 3 single-iteration improvements after the testbench-anchor agent repair:

| Case | Level 3 Target | Score Before | Score After | Delta | Required Coverage | Boundary Coverage | Mutation Coverage | Decision |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `fifo` | `fifo_read_from_empty` | 67.13% | 81.50% | +14.38% | 100.00% -> 100.00% | 20.00% -> 40.00% | 75.00% -> 100.00% | accepted |
| `counter` | `counter_disabled_hold` | 68.75% | 86.67% | +17.92% | 100.00% -> 100.00% | 25.00% -> 50.00% | 75.00% -> 100.00% | accepted |
| `arbiter` | `arbiter_no_req_idle` | 69.50% | 78.38% | +8.87% | 100.00% -> 100.00% | 20.00% -> 40.00% | 75.00% -> 75.00% | accepted |
| `handshake` | `handshake_data_changes_under_stall` | 65.71% | 78.87% | +13.15% | 100.00% -> 100.00% | 0.00% -> 33.33% | 75.00% -> 100.00% | accepted |

The most recent experiment artifacts live under:

```text
runs/agent_tools/curriculum_readme/<case>/trajectory.json
```

This demonstrates the practical value of the loop compared with plain closure reporting: without the agent, feedback only reports missing Level 3 boundary scenarios and related mutation/assertion gaps; with the agent loop, the system creates candidate testbench patches, reruns verification, accepts only improvements that preserve required coverage, and records the decision in `trajectory.json`.

Current built-in repair coverage includes local Level 3 templates for `fifo_read_from_empty`, `counter_disabled_hold`, `arbiter_no_req_idle`, and `handshake_data_changes_under_stall`. Richer repairs can still be delegated to an external LLM repair adapter through the same structured patch schema.

This is the intended integration point for a Hermes-style or custom agent runtime:

```text
while score < target:
  inspect feedback
  choose repair action
  edit artifact
  rerun verification
  store trajectory
```

An external stimulus/testbench generator can be plugged in with:

```bash
./scripts/run_coverage_closure.py \
  --max-iters 3 \
  --generator-command "python /path/to/update_tb.py"
```

The generator receives these environment variables:

```text
ASSERTPILOT_FEEDBACK_JSON
ASSERTPILOT_DATASETS_DIR
ASSERTPILOT_ITERATION
```

Expected behavior:

- `correct` RTL variants pass.
- `buggy` RTL variants fail one or more reference assertions.

Run one case or variant:

```bash
./scripts/run_dataset_verilator.py --mode simulate --case counter
./scripts/run_dataset_verilator.py --mode simulate --case fifo --variant correct
./scripts/run_dataset_verilator.py --mode simulate --case fifo --variant buggy
```

The script uses the local Verilator installation by default:

```text
../verilator/install/bin/verilator
```

## Bundled Datasets

Each dataset case contains a compact verification package:

```text
case/
  spec/spec.md              # Natural-language specification
  signals.json              # Reference signal list and top-module names
  rtl/design.v              # Correct RTL
  rtl/design_buggy.v        # Compatibility buggy RTL for smoke tests
  rtl/mutations/*.v         # Mutation set for closure quality scoring
  rtl/property_goldmine.sva # Reference assertion module / interface template
  rtl/bindings.sva          # Bind assertions to correct and buggy RTL
  rtl/FPV_<case>.tcl        # JasperGold TCL template
  sim/tb.cpp                # Verilator C++ testbench
  coverage_scenarios.json   # Hand-written scenario coverage points
```

Current cases:

- `counter`
- `fifo`
- `handshake`
- `arbiter`

## Running the Main Pipeline

The main entry point is `src/main.py`; most settings live in `src/config.py`.

For test-plan and SVA generation:

```python
task = "gen_plan"
subtask = "actual_gen"

file_path = "/path/to/spec.pdf"
design_dir = "/path/to/rtl"
KG_path = "/path/to/clustered_graph.0.graphml"

gen_plan_sva_using_valid_signals = True
valid_signals = ["clk", "rst", "count"]

generate_SVAs = True
```

Choose a backend:

```python
verification_backend = "jasper"      # formal backend
verification_backend = "verilator"   # simulation backend
verification_backend = "both"        # run both
```

For the Verilator backend, provide a top module and C++ testbench:

```python
verilator_bin = "/Users/huilingzhen/Desktop/0002-personal-projects/verification/SVA-checker/verilator/install/bin/verilator"
verilator_top_module = "counter"
verilator_testbench_path = "/path/to/tb.cpp"
verilator_build_dir = "verilator_build"
verilator_timeout_sec = 300
verilator_extra_args = ["--assert", "--trace"]
```

Run:

```bash
cd /Users/huilingzhen/Desktop/0002-personal-projects/verification/SVA-checker/AssertPilot/src
python main.py
```

## Building a New Dataset Case

For a small smoke-test case, provide:

- A natural-language spec.
- Correct and buggy RTL.
- A `signals.json` file listing key signals and top modules.
- A reference `property_goldmine.sva` with the property module interface.
- A `bindings.sva` file.
- A JasperGold TCL template if formal checking is needed.
- A Verilator C++ testbench for simulation-driven checking.

Use the existing `datasets/counter`, `datasets/fifo`, `datasets/handshake`, and `datasets/arbiter` cases as templates.

## Notes

- JasperGold results are formal verification results.
- Verilator results are simulation-driven and depend on the provided testbench and stimuli.
- `runs/` and `logs/` are local output directories and are ignored by git.

