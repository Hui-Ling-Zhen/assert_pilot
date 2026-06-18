# Arbiter Specification

The design implements a 2-request round-robin arbiter.

Inputs:
- `clk`: rising-edge clock.
- `rst`: active-high synchronous reset.
- `req[1:0]`: request vector.

Outputs:
- `grant[1:0]`: grant vector.
- `last_grant`: index of the most recently granted requester.

Behavior:
- Reset clears `grant` and `last_grant`.
- If no request is active, no grant may be asserted.
- If exactly one request is active, the corresponding grant must be asserted.
- If both requests are active, exactly one grant must be asserted.
- When both requests are active, the arbiter alternates grants based on `last_grant`.
- A grant must never be asserted for a requester whose request bit is low.
