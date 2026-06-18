#include "verilated.h"

#if __has_include("Vrr_arbiter_buggy.h")
#include "Vrr_arbiter_buggy.h"
using Top = Vrr_arbiter_buggy;
#else
#include "Vrr_arbiter.h"
using Top = Vrr_arbiter;
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
    top->req = 0;
    tick(top);
    tick(top);

    top->rst = 0;
    top->req = 1;
    tick(top);

    top->req = 2;
    tick(top);

    // Both requests active. Buggy RTL grants both and violates one-hot.
    top->req = 3;
    tick(top);
    tick(top);

    delete top;
    return 0;
}
