set RTL_PATH [file dirname [info script]]

analyze -v2k ${RTL_PATH}/design.v
analyze -sva ${RTL_PATH}/bindings.sva ${RTL_PATH}/property_goldmine.sva
elaborate -top simple_fifo

clock clk
reset rst

prove -all
report
