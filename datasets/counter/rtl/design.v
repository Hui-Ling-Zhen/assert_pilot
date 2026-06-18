module counter (
    input wire clk,
    input wire rst,
    input wire en,
    output reg [3:0] count,
    output reg wrap
);

always @(posedge clk) begin
    if (rst) begin
        count <= 4'd0;
        wrap <= 1'b0;
    end else if (en) begin
        if (count == 4'd15) begin
            count <= 4'd0;
            wrap <= 1'b1;
        end else begin
            count <= count + 4'd1;
            wrap <= 1'b0;
        end
    end else begin
        count <= count;
        wrap <= 1'b0;
    end
end

endmodule
