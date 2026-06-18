module rr_arbiter_buggy (
    input wire clk,
    input wire rst,
    input wire [1:0] req,
    output reg [1:0] grant,
    output reg last_grant
);

always @(posedge clk) begin
    if (rst) begin
        grant <= 2'b00;
        last_grant <= 1'b0;
    end else begin
        case (req)
            2'b00: begin
                grant <= 2'b00;
            end
            2'b01: begin
                grant <= 2'b01;
                last_grant <= 1'b0;
            end
            2'b10: begin
                grant <= 2'b10;
                last_grant <= 1'b1;
            end
            2'b11: begin
                grant <= 2'b11;  // BUG: grants must be mutually exclusive.
                last_grant <= ~last_grant;
            end
        endcase
    end
end

endmodule
