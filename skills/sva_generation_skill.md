# SVA Generation Skill

This skill guides assertion generation from a natural-language spec, RTL, and AssertPilot testplan. Use it whenever generating or repairing SVA candidates.

## 1. Obligation Extraction

Extract verification obligations before writing SVA. Prefer small obligations that can be tied to a testplan item and a trigger scenario.

### Reset Obligation

- Identify all state, valid, flag, count, pointer, grant, and output registers that must take a known value under reset.
- Reset assertions should check reset behavior directly.
- Do not hide reset obligations behind `disable iff (rst)`.

### State Transition Obligation

- Identify state updates caused by enables, handshakes, request/grant decisions, read/write events, or counter transitions.
- Determine whether outputs are combinational or registered.
- For registered outputs, align the assertion with `$past(input)` or `$past(condition)` as needed.

### Handshake / Protocol Obligation

- Identify valid/ready acceptance, stall, hold, and backpressure rules.
- During stall, output data and valid state should usually remain stable.
- During acceptance, data should move from input-side state to output-side state with the RTL's intended latency.

### Boundary / Overflow / Underflow Obligation

- Identify full, empty, wrap, max-count, zero-count, overflow, underflow, and pointer wrap conditions.
- Use explicit edge conditions such as `empty && rd_en`, `full && wr_en`, `count == DEPTH`, or `count == 0`.
- Avoid broad implications that never activate or only restate a flag without driving the boundary.

### Mutual Exclusion / One-Hot Obligation

- Identify grants, enables, selects, valid outputs, or state encodings that must be one-hot, one-hot-or-zero, or mutually exclusive.
- For registered grants or selects, compare outputs against `$past(request)` when the RTL updates outputs on the clock edge.

## 2. SVA Writing Rules

- Reset properties that verify reset behavior should not use `disable iff (rst)`, because that masks the reset cycle.
- Non-reset temporal properties should use `disable iff (rst)`.
- Registered outputs should use `$past(input)` or `$past(condition)` to align with clocked behavior.
- Hold and stability properties should use `$stable(signal)` for data, flags, grants, or valid signals that must not change.
- Counter and FIFO boundary properties should use explicit edge conditions and expected state updates.
- Prefer narrow, meaningful antecedents over broad conditions that pass vacuously.
- Do not weaken an assertion simply to make a buggy RTL pass.
- Use only signals available in the RTL, valid signal list, or reference property module.

## 3. Vacuity Avoidance

- Every assertion antecedent must be coverable by a `trigger_scenario`.
- Every generated assertion must bind to exactly one `trigger_scenario`.
- If the trigger scenario has no stimulus, request a testplan/testbench repair before generating a speculative assertion.
- Generate and return an `activation_condition` for each assertion.
- The `activation_condition` should be a concise Boolean expression that explains when the assertion antecedent is expected to fire.
- If a non-vacuous assertion cannot be written from the current testplan and RTL context, return a repair note or `needs_stimulus` instead of inventing an untriggered property.

## 4. Required Output Schema

Each testplan item is an assertion contract. Use these fields as hard constraints:

```json
{
  "id": "fifo_read_from_empty",
  "obligation_type": "boundary_underflow",
  "scope": {
    "signals": ["empty", "rd_en", "count"],
    "clock": "clk",
    "reset": "rst"
  },
  "trigger_scenario": "fifo_read_from_empty",
  "activation_condition": "empty && rd_en",
  "expected_behavior": "count remains zero and empty stays high",
  "forbidden_behavior": "count decreases or empty deasserts",
  "timing_model": "registered_next_cycle"
}
```

Do not use signals outside `scope.signals` unless they are visible in the reference property module and necessary for the contract. If `timing_model` requires next-cycle behavior, use `$past` or `|=>` and explain the alignment in `timing_rationale`.

`generate-sva` must return JSON with assertion metadata:

```json
{
  "assertions": [
    {
      "name": "assert_no_underflow",
      "plan_id": "fifo_read_from_empty",
      "trigger_scenario": "fifo_read_from_empty",
      "activation_condition": "empty && rd_en",
      "timing_rationale": "FIFO flags are registered, so the consequent checks the next cycle after the empty read.",
      "sva": "property no_underflow; @(posedge clk) disable iff (rst) (empty && rd_en) |=> empty && count == 0; endproperty\nassert_no_underflow: assert property(no_underflow);"
    }
  ],
  "needs_stimulus": [
    {
      "plan_id": "fifo_read_from_empty",
      "trigger_scenario": "fifo_read_from_empty",
      "reason": "No stimulus currently reaches empty && rd_en."
    }
  ]
}
```

## 5. Quick Pattern Hints

- Reset clear:
  - Trigger: reset scenario.
  - Avoid `disable iff (rst)`.
  - Example shape: `rst |=> state == RESET_VALUE`.
- Registered response:
  - Trigger: request/accept scenario.
  - Use `$past(req)` or `$past(valid_i && ready_o)`.
- Stall hold:
  - Trigger: stalled valid data scenario.
  - Use `$stable(data_o)` and `$stable(valid_o)` under stall.
- FIFO underflow:
  - Trigger: read while empty.
  - Antecedent: `empty && rd_en`.
  - Expected: empty remains asserted and count does not decrement.
- Arbiter grant safety:
  - Trigger: request/no-request/both-request scenario.
  - Use one-hot or grant-implies-request checks, with `$past(req)` for registered grants.
