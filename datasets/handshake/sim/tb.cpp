#include "verilated.h"
#include <iostream>

#if __has_include("Vhandshake_stage_buggy.h")
#include "Vhandshake_stage_buggy.h"
using Top = Vhandshake_stage_buggy;
#else
#include "Vhandshake_stage.h"
using Top = Vhandshake_stage;
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
    top->valid_i = 0;
    top->ready_i = 1;
    top->data_i = 0;
    tick(top);
    if (!top->valid_o && top->data_o == 0) {
        std::cout << "SCENARIO:handshake_reset" << std::endl;
    }

    top->rst = 0;
    top->valid_i = 1;
    top->ready_i = 0;
    top->data_i = 0xA5;
    tick(top);
    if (top->valid_o && top->data_o == 0xA5) {
        std::cout << "SCENARIO:handshake_accept_input" << std::endl;
    }

    // Hold downstream not-ready long enough for the hold assertion to fire on buggy RTL.
    top->valid_i = 0;
    top->ready_i = 0;
    tick(top);
    if (top->valid_o && top->data_o == 0xA5 && !top->ready_o) {
        std::cout << "SCENARIO:handshake_hold_when_stalled" << std::endl;
    }
    tick(top);

    top->ready_i = 1;
    tick(top);

    delete top;
    return 0;
}
