module simple_fifo_buggy (
    input wire clk,
    input wire rst,
    input wire wr_en,
    input wire rd_en,
    input wire [7:0] wdata,
    output reg [7:0] rdata,
    output wire full,
    output wire empty,
    output reg [2:0] count
);

reg [7:0] mem [0:3];
reg [1:0] wr_ptr;
reg [1:0] rd_ptr;

assign full = (count == 3'd4);
assign empty = (count == 3'd0);

wire do_write = wr_en;  // BUG: accepts writes even when full.
wire do_read = rd_en && !empty;

always @(posedge clk) begin
    if (rst) begin
        wr_ptr <= 2'd0;
        rd_ptr <= 2'd0;
        rdata <= 8'd0;
        count <= 3'd0;
    end else begin
        if (do_write) begin
            mem[wr_ptr] <= wdata;
            wr_ptr <= wr_ptr + 2'd1;
        end

        if (do_read) begin
            rdata <= mem[rd_ptr];
            rd_ptr <= rd_ptr + 2'd1;
        end

        case ({do_write, do_read})
            2'b10: count <= count + 3'd1;
            2'b01: count <= count - 3'd1;
            default: count <= count;
        endcase
    end
end

endmodule
