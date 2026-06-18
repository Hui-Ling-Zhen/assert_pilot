#include "verilated.h"

#if __has_include("Vsimple_fifo_buggy.h")
#include "Vsimple_fifo_buggy.h"
using Top = Vsimple_fifo_buggy;
#else
#include "Vsimple_fifo.h"
using Top = Vsimple_fifo;
#endif

static vluint64_t main_time = 0;
double sc_time_stamp() { return main_time; }

static void tick(Top* top) {
    top->clk = 0;
    top->eval();
    main_time++;
    top->clk = 1;
    top->eval();
    main_time++;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    Top* top = new Top;

    top->rst = 1;
    top->wr_en = 0;
    top->rd_en = 0;
    top->wdata = 0;
    tick(top);
    tick(top);

    top->rst = 0;
    for (int i = 0; i < 4; ++i) {
        top->wr_en = 1;
        top->rd_en = 0;
        top->wdata = static_cast<unsigned char>(0x10 + i);
        tick(top);
    }

    // Extra write while full. Correct RTL holds count; buggy RTL overflows count.
    top->wr_en = 1;
    top->rd_en = 0;
    top->wdata = 0x55;
    tick(top);
    tick(top);

    delete top;
    return 0;
}
