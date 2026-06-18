# FIFO Specification

The design implements a synchronous FIFO with depth 4 and 8-bit data.

Inputs:
- `clk`: rising-edge clock.
- `rst`: active-high synchronous reset.
- `wr_en`: write request.
- `rd_en`: read request.
- `wdata[7:0]`: data to write.

Outputs:
- `rdata[7:0]`: data read from the FIFO.
- `full`: asserted when the FIFO contains 4 entries.
- `empty`: asserted when the FIFO contains 0 entries.
- `count[2:0]`: number of valid entries.

Behavior:
- Reset clears the FIFO, sets `count` to `0`, asserts `empty`, and deasserts `full`.
- A write is accepted when `wr_en` is high and `full` is low.
- A read is accepted when `rd_en` is high and `empty` is low.
- Writing to a full FIFO without a simultaneous read must not increase `count`.
- Reading from an empty FIFO without a simultaneous write must not decrease `count`.
- `full` is equivalent to `count == 4`; `empty` is equivalent to `count == 0`.
