# STM32 Debug Skill for AI Agents

这是一个专为 AI Agent（如基于大语言模型的编程助手）设计的 STM32 调试技能包（Skill）。它封装了常用的 STM32 固件编译、烧录、调试、寄存器查看以及变量实时监控功能，旨在让 AI 助手能够无缝、高效地协助开发者进行 STM32 嵌入式开发。

## ✨ 核心特性

- **一键烧录**: 封装 OpenOCD 烧录流程，支持快速部署。
- **统一调试 CLI**: 新增 `stm32-debug` 统一入口，覆盖读取、断点、单步、观察点、历史、一键调试循环和会话管理。
- **实时监控 (无停机)**: 利用 GDB 和 OpenOCD TCL 接口，支持在程序运行期间实时读取全局变量、结构体成员、寄存器、寄存器位域以及直接内存地址。
- **寄存器解析**: 内置 `read_svd.py` 脚本，可直接解析 `.svd` 文件，根据外设、寄存器和位域名称查看地址、掩码和定义。
- **自动 OpenOCD 管理**: `stm32_monitor.py` 在需要时会自动拉起 OpenOCD，减少手工启动和残留进程问题。
- **SWO 日志捕获**: 自动配置并捕获通过 SWO (Serial Wire Output) 输出的 `printf` 调试信息。
- **断点与单步调试**: 支持断点、条件断点、观察点、继续执行、单步、调用栈、局部变量和寄存器查看。
- **开箱即用**: 提供预打包好的 `stm32-debug-skill.tar.gz`，解压即用。

## 📦 包含内容

解压 `stm32-debug-skill.tar.gz` 后，包含以下内容：

```text
stm32-debug-skill/
├── SKILL.md                  # AI Agent 读取的技能说明和使用指南
├── STM32F746.svd             # (示例) 芯片 SVD 文件，用于解析寄存器
└── scripts/
    ├── stm32-debug           # 统一命令入口
    ├── stm32_debug.py        # 统一 CLI 实现
    ├── flash.sh              # 固件烧录脚本
    ├── monitor_swo.sh        # SWO 日志监听脚本
    ├── read_svd.py           # SVD 解析与寄存器读取脚本
    └── stm32_monitor.py      # GDB 符号解析与内存实时监控脚本
```

## 🛠️ 环境依赖

在使用此 Skill 前，主机环境需要安装以下工具：
- `arm-none-eabi-gcc` (用于编译)
- `openocd` (用于烧录和底层通信)
- `gdb-multiarch` (用于解析 ELF 符号和结构体类型)
- `python3` (运行监控脚本)

## 🚀 使用示例 (Agent 指令)

当将此 Skill 挂载到 AI Agent 后，你可以直接使用自然语言下达指令，Agent 会自动调用对应的脚本。

### 1. 初始化一次调试会话
```bash
# Agent 将执行：
./scripts/stm32-debug start \
  --elf build/firmware.elf \
  --svd STM32F746.svd \
  --source-root Core \
  --build-cmd "cmake --build build/Release" \
  --flash-elf build/firmware.elf
```

### 2. 读取变量、寄存器、位域、地址
```bash
# Agent 将执行：
./scripts/stm32-debug read counter "my_sensor.value" --type uint
./scripts/stm32-debug read 'GPIOA->ODR' GPIOA.MODER.MODER0 --type uint

# 或读取直接地址：
./scripts/stm32-debug read 0x20000000 --type hex
```

注意：带 `->` 的目标在 shell 中要加引号，或者改写成 `GPIOA.ODR` 这样的点号形式。

### 3. 设置断点并单步调试
```bash
# Agent 将执行：
./scripts/stm32-debug break main.c:145
./scripts/stm32-debug break HAL_I2C_Mem_Read --condition "voltage_index==2"
./scripts/stm32-debug restart
./scripts/stm32-debug continue
./scripts/stm32-debug step 3
./scripts/stm32-debug locals
./scripts/stm32-debug backtrace
./scripts/stm32-debug registers
```

### 4. 观察点和历史
```bash
# Agent 将执行：
./scripts/stm32-debug watch voltage_index
./scripts/stm32-debug history counter --limit 10
./scripts/stm32-debug generate-script counter voltage
```

### 5. 一键调试循环
```bash
# Agent 将执行：
./scripts/stm32-debug cycle counter voltage --flash --wait 10
```

### 6. 监听 printf 日志 (SWO)
支持自定义 CPU 主频（如果芯片被配置为了非默认的 16MHz，比如 216MHz，需显式指定以保证波特率正确）：
```bash
# Agent 将执行 (默认 16MHz)：
./scripts/monitor_swo.sh

# 如果 CPU 配置为 216MHz，则 Agent 将执行：
./scripts/monitor_swo.sh 216000000

# 并在后台查看 swo.log
```

如果不是 `stm32f7discovery`，也可以通过环境变量切换 OpenOCD 配置：
```bash
OPENOCD_CONFIG=board/stm32h7x.cfg ./scripts/monitor_swo.sh 400000000
```

### 7. SVD 获取方式
当当前 Skill 未附带目标芯片的 SVD 时，推荐从以下来源获取：
- STM32Cube 设备包自带的 CMSIS 目录
- ST 官网对应系列的 STM32Cube 下载页
- [CMSIS-SVD GitHub 镜像](https://github.com/posborne/cmsis-svd/tree/master/data/STMicro)

### 8. 变量解释配置
可以在项目根目录放置 `.stm32-debug.yaml` 或 `.stm32-debug.json`，给关键状态变量增加可读解释：

```yaml
variables:
  test_step:
    0: "Initializing"
    100: "Complete"
    99: "Failed"
```

之后 `stm32-debug read test_step` 和 `stm32-debug history test_step` 会附带解释文本。

## 📝 如何集成到你的 Agent

1. 下载 `stm32-debug-skill.tar.gz`。
2. 解压到你的项目或 Agent 的 skills 目录下（例如 `.trae/skills/stm32-debug/`）。
3. 确保 `scripts/` 目录下的所有 `.sh` 和 `.py` 文件具有可执行权限 (`chmod +x scripts/*`)。
4. Agent 读取 `SKILL.md` 后即可习得这些调试能力。
