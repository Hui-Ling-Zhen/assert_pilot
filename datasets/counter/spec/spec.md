# Counter Specification

The design implements a 4-bit synchronous up-counter.

Inputs:
- `clk`: rising-edge clock.
- `rst`: active-high synchronous reset.
- `en`: count enable.

Outputs:
- `count[3:0]`: current counter value.
- `wrap`: asserted for one cycle when the counter is enabled and wraps from `15` to `0`.

Behavior:
- When `rst` is high on a rising clock edge, `count` must become `0` and `wrap` must become `0`.
- When `rst` is low and `en` is low, `count` must hold its previous value and `wrap` must be `0`.
- When `rst` is low, `en` is high, and `count` is not `15`, `count` must increment by one and `wrap` must be `0`.
- When `rst` is low, `en` is high, and `count` is `15`, the next value of `count` must be `0` and `wrap` must be `1`.
