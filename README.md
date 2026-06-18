# AssertPilot

AssertPilot is a research prototype for **generating high-quality SystemVerilog Assertions (SVAs)** from natural-language specifications and RTL. It constructs a joint knowledge graph that bridges the semantic gap between specification and implementation, then leverages LLMs to produce a focused test plan and candidate SVAs.

AssertPilot extends the original AssertionForge flow with a dual-backend verification model:

- **Formal backend**: uses Cadence JasperGold to prove and cover generated SVAs.
- **Simulation backend**: uses Verilator to check generated SVAs during simulation-driven validation.

## Project Overview

AssertPilot enhances hardware verification assertion generation with structured representation of specifications and RTL. The project follows a three-stage workflow:

1. **Knowledge Graph Construction (Indexing)**
2. **Test Plan and SVA Generation**
3. **Verification Backend Execution**

The generated SVAs depend on the natural-language test plans produced in Stage 2. The test plans act as an intermediate representation between specification/RTL context and concrete SVA code.

## Verification Backends

AssertPilot supports two complementary verification backends:

| Backend | Tool | Purpose | Result Meaning |
|---------|------|---------|----------------|
| Formal | JasperGold | Prove generated SVAs over the design state space | `proven`, `covered`, or counterexample |
| Simulation-driven | Verilator | Check generated SVAs over user-provided simulation traces/stimuli | assertion pass/fail for the exercised tests |

The JasperGold backend preserves the original formal-verification flow from AssertionForge. The Verilator backend provides an open-source simulation-driven flow for designs where a testbench or stimulus source is available. A Verilator pass means no assertion failed in the executed simulation; it is not equivalent to a formal proof.

## Setup and Usage

Before running any command, always activate the virtual environment:

```bash
cd /<path>/<to>/src && conda activate fv
```

## Runnable Scripts

AssertPilot includes runnable scripts under `scripts/` for local smoke testing.

### Dataset Verilator Smoke Tests

Use `scripts/run_dataset_verilator.py` to run the bundled `datasets/` examples with the Verilator simulation backend. The script supports both front-end lint checks and full simulation checks. Correct RTL variants are expected to pass; buggy RTL variants are expected to fail at least one assertion.

Run Verilator lint for all dataset cases:

```bash
cd /path/to/AssertPilot
./scripts/run_dataset_verilator.py --mode lint
```

Run full simulation smoke tests for all dataset cases:

```bash
cd /path/to/AssertPilot
./scripts/run_dataset_verilator.py --mode simulate
```

Run one case or one variant:

```bash
./scripts/run_dataset_verilator.py --mode simulate --case counter
./scripts/run_dataset_verilator.py --mode simulate --case fifo --variant correct
./scripts/run_dataset_verilator.py --mode simulate --case fifo --variant buggy
```

Useful options:

```bash
./scripts/run_dataset_verilator.py --help
./scripts/run_dataset_verilator.py --mode simulate --verbose
./scripts/run_dataset_verilator.py --mode simulate --build-root /tmp/assertpilot-verilator
```

By default, the script uses the local Verilator installed at:

```text
../verilator/install/bin/verilator
```

## Working with a New Design

For a new design, you'll need to set specific parameters in config.py for the KG, generation, and verification stages. Here's what you need to modify:

### Common Parameters for New Designs

- `design_name`: A unique identifier for your design (e.g., 'my_new_design')
- Create appropriate paths for your design's files:
  - Specification document (PDF)
  - RTL code directory (containing .v files)
  - Output directory for the Knowledge Graph

## Stage 1: Knowledge Graph Construction (Indexing)

1. Edit `/<path>/<to>/src/config.py`:
   - Set `task = 'build_KG'`
   - Set `design_name` to your new design name
   - Set paths for your design:
     ```python
     input_file_path = "/path/to/your/specification.pdf"
     ```
   - Keep GraphRAG paths as standard (usually don't need to change):
     ```python
     env_source_path = "/<path>/<to>/rag_apb/.env"
     settings_source_path = "/<path>/<to>/rag_apb/settings.yaml"
     entity_extraction_prompt_source_path = "/<path>/<to>/rag_apb/prompts/entity_extraction.txt"
     graphrag_local_dir = "/<path>/<to>/graphrag"
     ```

2. Run the indexing:
   ```bash
   python main.py
   ```

3. **Note the KG output path from the console** - you'll need it for Stage 2. It will be something like:
   ```
   /<path>/<to>/data/your_design_name/spec/graph_rag_your_design_name/output/[timestamp]/artifacts/clustered_graph.0.graphml
   ```

## Stage 2: Test Plan and SVA Generation

1. Edit `/<path>/<to>/src/config.py`:
   - Set `task = 'gen_plan'`
   - Set `subtask = 'actual_gen'`
   - Configure design parameters:
     ```python
     design_name = "your_design_name"  # Same as in Stage 1
     file_path = "/path/to/your/specification.pdf"  # Same as input_file_path from Stage 1
     design_dir = "/path/to/your/rtl/directory"  # Directory containing your design's .v files
     KG_path = "/path/from/stage1/output/clustered_graph.0.graphml"  # Path noted from Stage 1
     ```
   - Set architectural signals:
     ```python
     gen_plan_sva_using_valid_signals = True
     valid_signals = ['signal1', 'signal2']  # Replace with your design's actual signal names
     ```
   - For new designs, disable SVA generation:
     ```python
     generate_SVAs = False  # Important for designs without TCL files
     ```
   - LLM configuration (usually keep as is):
     ```python
     llm_model = "gpt-4o"
     use_KG = True
     prompt_builder = "dynamic"
     ```
   - Select the verification backend:
     ```python
     verification_backend = "jasper"      # "jasper", "verilator", or "both"
     ```

2. Run the test plan generation:
   ```bash
   python main.py
   ```

## Stage 3: Verification Backend Execution

When `generate_SVAs = True`, AssertPilot writes generated assertions to SVA files and can dispatch them to one or both verification backends.

### JasperGold Formal Backend

Use this backend when the goal is formal assertion verification.

```python
verification_backend = "jasper"
```

The JasperGold flow generates TCL scripts, runs formal proof, and reports whether each generated assertion is proven, covered, or has a counterexample.

### Verilator Simulation Backend

Use this backend when the goal is open-source simulation-driven assertion checking.

```python
verification_backend = "verilator"
verilator_bin = "verilator"
verilator_top_module = "your_top_module"
verilator_testbench_path = "/path/to/your/testbench.cpp"
verilator_extra_args = ["--assert", "--trace"]
```

The Verilator flow compiles RTL, generated SVAs, bindings or wrappers, and the user-provided testbench. It reports compile status, simulation status, and assertion failures observed during simulation.

To run both backends:

```python
verification_backend = "both"
```

## Parameter Details for New Designs

### Required Parameters

| Parameter | Description | Example Value |
|-----------|-------------|---------------|
| `design_name` | Unique name for your design | `"my_custom_asic"` |
| `input_file_path` / `file_path` | Path to specification PDF | `"/home/user/specs/my_design_spec.pdf"` |
| `design_dir` | Directory containing RTL (.v) files | `"/home/user/rtl/my_design/"` |
| `KG_path` | Path to KG from Stage 1 | Output path from Stage 1 |
| `valid_signals` | List of architectural signals | `['clk', 'reset', 'data_valid']` |

### Optional Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `DEBUG` | Enable faster processing (fewer signals) | `False` |
| `max_num_signals_process` | Limit number of signals to process | `float('inf')` |
| `max_prompts_per_signal` | Number of prompts per signal | `3` |
| `generate_SVAs` | Whether to generate SVA code | `False` |
| `verification_backend` | Verification backend to run | `"jasper"` |
| `verilator_testbench_path` | Verilator testbench path for simulation-driven checking | `None` |

## Important Notes

- The KG construction process may take some time depending on the size of the specification.
- Always check the console output for any errors or warnings during the process.
- For new designs, keep `generate_SVAs = False` since TCL files might not be provided.
- Always specify `valid_signals` with the actual architectural signals from your design.
- Architectural signals are typically input/output ports and architectural-level registers mentioned in the specification.
- JasperGold results are formal verification results. Verilator results are simulation-driven results and depend on the provided testbench and stimuli.
- Verilator is an open-source backend for assertion checking during simulation, but it is not a drop-in replacement for JasperGold formal proof.

## Example Workflow for a New Design

```bash
# Activate environment
cd /<path>/<to>/src && conda activate fv

# Edit config.py for build_KG task with your design information
# Then run:
python main.py

# When KG construction is complete, note the output path
# Edit config.py for gen_plan task with the correct KG_path
# Then run:
python main.py
```

This is the recommended workflow for reliable operation of AssertPilot with new designs.


## Knowledge Graph Example

![OpenMSP430 Knowledge Graph](src/KG_vis.png)

*Visualization of the KGs from **OpenMSP430** using Gephi. Node colors represent different entity types—modules, signals, etc.  Left: KG generated with the vanilla GraphRAG prompt.  Middle: KG produced by our domain‑customized prompt.  Right: two zoomed‑in views highlighting key entities and their labels.*



## Citation

AssertPilot builds on the AssertionForge research prototype. If you build on the original AssertionForge methodology, please cite the LAD 2025 paper:

```
@inproceedings{bai2025assertionforge,
  title={AssertionForge: Enhancing Formal Verification Assertion Generation with Structured Representation of Specifications and RTL},
  author={Bai, Yunsheng and Bany Hamad, Ghaith and Suhaib, Syed and Ren, Haoxing},
  booktitle={Proceedings of the IEEE International Conference on LLM-Aided Design (LAD)},
  address={Stanford, CA},
  year={2025}
}
```

*Accepted at LAD 2025, Stanford (June 26‑27, 2025).*

📄 **Paper:** [arXiv:2503.19174](https://arxiv.org/abs/2503.19174)


##  Acknowledgements

We deeply thank Vigyan Singhal for his technical guidance and support. We also acknowledge Cadence Design Systems for implementing the formal assertion-to-assertion equivalence checking in Jasper and for their many helpful discussions that contributed to the success of this project.

