# AssertPilot Datasets

This directory contains small smoke-test designs for AssertPilot. Each case is intentionally compact and includes:

- Natural-language specification for KG and test-plan generation.
- Correct RTL (`design.v`), a compatibility buggy RTL (`design_buggy.v`), and a mutation set under `rtl/mutations/`.
- Reference signal list (`signals.json`).
- Hand-written scenario coverage points (`coverage_scenarios.json`).
- Reference SVA interface/properties (`property_goldmine.sva`) and `bindings.sva`.
- A JasperGold TCL template (`FPV_<case>.tcl`) for the formal backend.
- A Verilator C++ testbench (`sim/tb.cpp`) for the simulation-driven backend.

The correct RTL is expected to pass the reference assertions. The compatibility buggy RTL is expected to fail at least one reference assertion under the provided Verilator testbench. The coverage closure runner uses the mutation set to compute `mutation_kill_rate = killed_mutations / total_mutations`.

To use the Verilator backend, point `design_dir` to a case's `rtl` directory, set `verilator_testbench_path` to the case's `sim/tb.cpp`, and choose the correct top module:

```python
verification_backend = "verilator"
verilator_testbench_path = "<case>/sim/tb.cpp"
verilator_top_module = "counter"          # correct design
verilator_top_module = "counter_buggy"    # buggy design
```

For generated SVAs, AssertPilot writes new property files using the module interface in `property_goldmine.sva`. The provided `bindings.sva` binds the property module to both correct and buggy top modules.

## Smoke Test Script

Run the bundled Verilator smoke tests from the project root:

```bash
cd /path/to/AssertPilot
./scripts/run_dataset_verilator.py --mode lint
./scripts/run_dataset_verilator.py --mode simulate
```

Run the feedback-driven coverage closure loop:

```bash
./scripts/run_coverage_closure.py --max-iters 3
```

The closure runner uses weighted proxy coverage until a full coverage database is wired in:

- `correct_pass`: correct RTL assertion pass/fail.
- `mutation_kill_rate`: killed mutation files divided by total mutation files.
- `scenario_coverage`: required and bonus scenario markers observed in `sim/tb.cpp`.
- `assertion_activation_rate`: assertion-to-trigger mappings in `coverage_scenarios.json`, approximated by observed markers.
- `boundary_case_coverage`: harder edge-condition markers listed in `coverage_scenarios.json`.

The default score is:

```text
0.25 * correct_pass
+ 0.25 * mutation_kill_rate
+ 0.25 * scenario_coverage
+ 0.15 * assertion_activation_rate
+ 0.10 * boundary_case_coverage
```

Expected simulation behavior:

- `correct` variants pass.
- `buggy` variants fail one or more reference assertions.
- mutation variants should fail when the current stimulus and assertions expose the injected bug.

Run a single case:

```bash
./scripts/run_dataset_verilator.py --mode simulate --case counter
```

Run only one RTL variant:

```bash
./scripts/run_dataset_verilator.py --mode simulate --case counter --variant correct
./scripts/run_dataset_verilator.py --mode simulate --case counter --variant buggy
```
