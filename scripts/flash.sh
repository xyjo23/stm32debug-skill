#!/bin/bash
if [ -z "$1" ]; then
    echo "Usage: $0 <elf_file>"
    exit 1
fi
ELF_FILE=$1
OPENOCD_CONFIG=${OPENOCD_CONFIG:-board/stm32f7discovery.cfg}
openocd -f "$OPENOCD_CONFIG" -c "program $ELF_FILE verify reset exit"
