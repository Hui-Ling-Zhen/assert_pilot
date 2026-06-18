#include "verilated.h"

#if __has_include("Vcounter_buggy.h")
#include "Vcounter_buggy.h"
using Top = Vcounter_buggy;
#else
#include "Vcounter.h"
using Top = Vcounter;
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
    top->en = 0;
    tick(top);
    tick(top);

    top->rst = 0;
    top->en = 1;
    for (int i = 0; i < 20; ++i) {
        tick(top);
    }

    delete top;
    return 0;
}
