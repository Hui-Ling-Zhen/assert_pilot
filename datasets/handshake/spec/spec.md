# Handshake Stage Specification

The design implements a one-entry valid/ready pipeline stage for 8-bit data.

Inputs:
- `clk`: rising-edge clock.
- `rst`: active-high synchronous reset.
- `valid_i`: input data is valid.
- `ready_i`: downstream is ready to accept output data.
- `data_i[7:0]`: input payload.

Outputs:
- `ready_o`: this stage can accept input data.
- `valid_o`: output data is valid.
- `data_o[7:0]`: output payload.

Behavior:
- Reset clears `valid_o` and `data_o`.
- The stage may accept new input when it is empty or when downstream is ready.
- If input is accepted while `valid_i` is high, the next cycle must present `valid_o=1` and `data_o` equal to the accepted data.
- If `valid_o` is high and `ready_i` is low, the stage must hold both `valid_o` and `data_o` until downstream becomes ready.
- `ready_o` is high when the stage is empty or downstream is ready.
