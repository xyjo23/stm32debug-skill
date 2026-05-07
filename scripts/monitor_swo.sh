#!/bin/bash
CPU_FREQ=${1:-16000000}
OPENOCD_CONFIG=${OPENOCD_CONFIG:-board/stm32f7discovery.cfg}
openocd -f "$OPENOCD_CONFIG" -c "init" -c "tpiu config internal swo.log uart off $CPU_FREQ" -c "itm port 0 on"
