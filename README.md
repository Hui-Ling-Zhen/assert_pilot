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

The closure runner currently uses tiered proxy metrics:

- Required scenario coverage: default testbenches should hit the required bins.
- Bonus scenario coverage: harder bins stay open until new stimulus is generated.
- Mutation kill rate: each file in `rtl/mutations/` is compiled and run independently, then scored as `killed_mutations / total_mutations`.

If proxy coverage is below 100%, the runner writes targeted feedback to:

```text
runs/coverage_closure/iter_<N>/targeted_feedback.json
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

