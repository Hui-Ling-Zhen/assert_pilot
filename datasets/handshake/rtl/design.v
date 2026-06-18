module handshake_stage (
    input wire clk,
    input wire rst,
    input wire valid_i,
    input wire ready_i,
    input wire [7:0] data_i,
    output wire ready_o,
    output reg valid_o,
    output reg [7:0] data_o
);

assign ready_o = !valid_o || ready_i;

always @(posedge clk) begin
    if (rst) begin
        valid_o <= 1'b0;
        data_o <= 8'd0;
    end else if (ready_o) begin
        valid_o <= valid_i;
        if (valid_i) begin
            data_o <= data_i;
        end
    end else begin
        valid_o <= valid_o;
        data_o <= data_o;
    end
end

endmodule
