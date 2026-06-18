# AssertPilot Datasets

This directory contains small smoke-test designs for AssertPilot. Each case is intentionally compact and includes:

- Natural-language specification for KG and test-plan generation.
- Correct RTL (`design.v`) and intentionally buggy RTL (`design_buggy.v`).
- Reference signal list (`signals.json`).
- Reference SVA interface/properties (`property_goldmine.sva`) and `bindings.sva`.
- A JasperGold TCL template (`FPV_<case>.tcl`) for the formal backend.
- A Verilator C++ testbench (`sim/tb.cpp`) for the simulation-driven backend.

The correct RTL is expected to pass the reference assertions. The buggy RTL is expected to fail at least one reference assertion under the provided Verilator testbench.

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

Expected simulation behavior:

- `correct` variants pass.
- `buggy` variants fail one or more reference assertions.

Run a single case:

```bash
./scripts/run_dataset_verilator.py --mode simulate --case counter
```

Run only one RTL variant:

```bash
./scripts/run_dataset_verilator.py --mode simulate --case counter --variant correct
./scripts/run_dataset_verilator.py --mode simulate --case counter --variant buggy
```
