---
name: "stm32-debug"
description: "Compiles, flashes, debugs, and monitors STM32 firmware. Invoke for build, flash, GDB, SWO logs, or real-time variable/register monitoring."
---

# STM32 Debug Skill

This skill provides a comprehensive suite of tools for STM32 development, including compilation, flashing, debugging, SWO logging, and real-time monitoring of variables and registers.

## Prerequisites
- `arm-none-eabi-gcc`
- `openocd`
- `gdb-multiarch`
- `python3`
- SVD file (e.g., `STM32F746.svd`)

## Commands

### 1. Unified CLI
Prefer the unified entrypoint:
```bash
./scripts/stm32-debug <command> [options]
```

Recommended first step for a project:
```bash
./scripts/stm32-debug start \
  --elf build/firmware.elf \
  --svd STM32F746.svd \
  --source-root Core \
  --build-cmd "cmake --build build/Release" \
  --flash-elf build/firmware.elf
```

### 2. Build And Flash
Build via your project build system:
```bash
make
```

Flash firmware directly:
```bash
./scripts/flash.sh <elf_file>
```

Or use one-step cycle mode:
```bash
./scripts/stm32-debug cycle counter voltage --flash --wait 10
```

### 3. Read Variables, Registers, Fields, Addresses
The `read` command auto-detects the target kind and uses ELF or SVD accordingly.

```bash
# Variable from ELF symbols
./scripts/stm32-debug read counter voltage --type uint

# Register using SVD
./scripts/stm32-debug read 'GPIOA->ODR' --type hex

# Dot notation avoids shell quoting issues
./scripts/stm32-debug read GPIOA.ODR GPIOA.MODER.MODER0 --type uint

# Direct address
./scripts/stm32-debug read 0x20000000 --type hex
```

Generate a GDB memory-read script for offline use:
```bash
./scripts/stm32-debug generate-script counter voltage
```

Read history:
```bash
./scripts/stm32-debug history counter --limit 10
```

Optional variable interpretation config:
```bash
# .stm32-debug.yaml
variables:
  test_step:
    0: "Initializing"
    100: "Complete"
```

### 4. Breakpoints And Stepping
Logical breakpoints and watchpoints are stored in `.stm32-debug/session.json` and reapplied to each GDB invocation.

```bash
# Breakpoint management
./scripts/stm32-debug break main.c:145
./scripts/stm32-debug break HAL_I2C_Mem_Read --condition "voltage_index==2"
./scripts/stm32-debug break --list
./scripts/stm32-debug break --delete 1

# Watchpoints
./scripts/stm32-debug watch voltage_index
./scripts/stm32-debug watch 0x20000028
./scripts/stm32-debug watch --list

# Run control
./scripts/stm32-debug restart
./scripts/stm32-debug continue
./scripts/stm32-debug step 3
./scripts/stm32-debug next 2
./scripts/stm32-debug finish
```

### 5. Inspect Program State
```bash
./scripts/stm32-debug info
./scripts/stm32-debug backtrace
./scripts/stm32-debug backtrace 5
./scripts/stm32-debug locals
./scripts/stm32-debug registers
./scripts/stm32-debug registers r0 r1 pc sp
./scripts/stm32-debug frame 1
./scripts/stm32-debug interactive
```

### 6. Monitor SWO Output
Capture printf logs via SWO pin. If the MCU is configured to a non-default clock frequency (e.g., 216MHz), you must pass the CPU frequency in Hz:
```bash
./scripts/monitor_swo.sh [cpu_frequency_hz]
```
Example (default 16MHz):
```bash
./scripts/monitor_swo.sh
```
Example (216MHz):
```bash
./scripts/monitor_swo.sh 216000000
```
Use a custom OpenOCD board config when needed:
```bash
OPENOCD_CONFIG=board/stm32h7x.cfg ./scripts/monitor_swo.sh 400000000
```
Read the log:
```bash
tail -f swo.log
```

### 7. Inspect Register Definitions
View register or field details (address, mask, fields, description) from SVD:
```bash
./scripts/read_svd.py <svd_file> <peripheral> [register] [field]
```
Example:
```bash
./scripts/read_svd.py STM32F746.svd RCC CR

# Inspect one field directly
./scripts/read_svd.py STM32F746.svd GPIOA MODER MODER0
```

### 8. Session Management
```bash
./scripts/stm32-debug start --elf build/firmware.elf --svd STM32F746.svd
./scripts/stm32-debug status
./scripts/stm32-debug stop
```

### 9. SVD Sources
If your target SVD is missing, common sources are:
- STM32Cube device packages from ST
- ST official MCU download pages
- CMSIS-SVD mirror: `https://github.com/posborne/cmsis-svd/tree/master/data/STMicro`

## Troubleshooting
- **OpenOCD Init Failed**: Ensure no other OpenOCD instance is running (`pkill openocd`).
- **Connection Refused**: Ensure OpenOCD is running before using monitoring scripts.
- **Symbol Not Found**: Ensure the variable is global (not static/local) and the ELF file is up-to-date.
- **GDB Error**: Ensure `gdb-multiarch` is installed to resolve complex types like structs.
- **Shell Redirect Issue**: Quote targets using `->`, for example `'GPIOA->ODR'`, or prefer dot notation such as `GPIOA.ODR`.
