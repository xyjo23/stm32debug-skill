#!/usr/bin/env python3
import argparse
import fnmatch
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from read_svd import get_peripheral_info
from stm32_monitor import (
    DEFAULT_OPENOCD_CONFIG,
    OpenOCDManager,
    format_value,
    get_address_from_gdb,
    get_field_info,
    get_register_address,
    get_type_size_from_gdb,
    openocd_read_memory,
    parse_values,
    select_width,
)


STATE_DIRNAME = ".stm32-debug"
STATE_FILENAME = "session.json"
HISTORY_FILENAME = "history.jsonl"
DEFAULT_GDB_PORT = 3333
DEFAULT_TCL_PORT = 6666
DEFAULT_TELNET_PORT = 4444
DEFAULT_WAIT_SECONDS = 3.0


class Stm32DebugError(RuntimeError):
    pass


def workspace_root():
    return Path.cwd()


def state_dir():
    path = workspace_root() / STATE_DIRNAME
    path.mkdir(exist_ok=True)
    return path


def state_file():
    return state_dir() / STATE_FILENAME


def history_file():
    return state_dir() / HISTORY_FILENAME


def default_state():
    return {
        "openocd": {
            "pid": None,
            "config": DEFAULT_OPENOCD_CONFIG,
            "host": "127.0.0.1",
            "gdb_port": DEFAULT_GDB_PORT,
            "tcl_port": DEFAULT_TCL_PORT,
            "telnet_port": DEFAULT_TELNET_PORT,
            "log_file": None,
            "managed": False,
        },
        "context": {
            "elf": None,
            "svd": None,
            "build_cmd": None,
            "flash_elf": None,
            "source_root": None,
        },
        "breakpoints": [],
        "watchpoints": [],
        "next_breakpoint_id": 1,
        "next_watchpoint_id": 1,
    }


def load_state():
    path = state_file()
    if not path.exists():
        return default_state()
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    state = default_state()
    state.update(data)
    state["openocd"].update(data.get("openocd", {}))
    state["context"].update(data.get("context", {}))
    return state


def save_state(state):
    with state_file().open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


def merge_context(state, args):
    for field in ("elf", "svd", "build_cmd", "flash_elf", "source_root"):
        value = getattr(args, field, None)
        if value:
            state["context"][field] = value

    if getattr(args, "openocd_config", None):
        state["openocd"]["config"] = args.openocd_config
    if getattr(args, "openocd_host", None):
        state["openocd"]["host"] = args.openocd_host
    if getattr(args, "gdb_port", None):
        state["openocd"]["gdb_port"] = args.gdb_port
    if getattr(args, "tcl_port", None):
        state["openocd"]["tcl_port"] = args.tcl_port
    if getattr(args, "telnet_port", None):
        state["openocd"]["telnet_port"] = args.telnet_port


def require_context(state, name):
    value = state["context"].get(name)
    if not value:
        raise Stm32DebugError(
            f"Missing required context '{name}'. Pass '--{name.replace('_', '-')}' or run 'stm32-debug start' first."
        )
    return value


def is_pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def terminate_pid(pid):
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
        time.sleep(0.5)
        if is_pid_alive(pid):
            os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def clear_openocd_state(state):
    state["openocd"]["pid"] = None
    state["openocd"]["managed"] = False


def start_openocd(state):
    openocd = state["openocd"]
    manager = OpenOCDManager(openocd["config"], openocd["host"], openocd["tcl_port"])
    if manager.is_running():
        openocd["managed"] = False
        openocd["pid"] = None
        save_state(state)
        return "OpenOCD is already running."

    log_path = state_dir() / "openocd.log"
    last_error = None
    for attempt in range(1, 4):
        with log_path.open("a", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                ["openocd", "-f", openocd["config"]],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )

        deadline = time.time() + 8
        while time.time() < deadline:
            if manager.is_running():
                openocd["pid"] = process.pid
                openocd["log_file"] = str(log_path)
                openocd["managed"] = True
                save_state(state)
                return (
                    f"OpenOCD started (pid={process.pid}) with config '{openocd['config']}'."
                )
            if process.poll() is not None:
                last_error = "OpenOCD exited unexpectedly while starting."
                break
            time.sleep(0.2)
        else:
            terminate_pid(process.pid)
            last_error = f"Timed out waiting for OpenOCD on TCL port {openocd['tcl_port']}."

        clear_openocd_state(state)
        save_state(state)
        if attempt < 3:
            time.sleep(attempt)

    raise Stm32DebugError(last_error or "Failed to start OpenOCD.")


def stop_openocd(state):
    openocd = state["openocd"]
    message = "OpenOCD is not managed by stm32-debug."
    if openocd.get("managed") and openocd.get("pid"):
        terminate_pid(openocd["pid"])
        message = f"Stopped OpenOCD pid={openocd['pid']}."
    openocd["pid"] = None
    openocd["managed"] = False
    save_state(state)
    return message


def ensure_openocd(state, auto_start=True):
    openocd = state["openocd"]
    if openocd.get("managed") and openocd.get("pid") and not is_pid_alive(openocd["pid"]):
        clear_openocd_state(state)
        save_state(state)
    manager = OpenOCDManager(openocd["config"], openocd["host"], openocd["tcl_port"])
    if manager.is_running():
        return False
    if not auto_start:
        raise Stm32DebugError("OpenOCD is not running. Run 'stm32-debug start' first.")
    start_openocd(state)
    return True


def gdb_base_commands():
    return [
        "set confirm off",
        "set pagination off",
        "set width 0",
        "set height 0",
        "set print pretty on",
    ]


def gdb_connect_commands(state):
    host = state["openocd"]["host"]
    port = state["openocd"]["gdb_port"]
    return [f"target remote {host}:{port}"]


def gdb_item_command(item):
    if item["kind"] == "breakpoint":
        if item.get("condition"):
            return f"break {item['location']} if {item['condition']}"
        return f"break {item['location']}"
    if item["kind"] == "watch_var":
        return f"watch {item['expression']}"
    if item["kind"] == "watch_addr":
        return f"watch *(unsigned int*)({item['expression']})"
    raise Stm32DebugError(f"Unknown GDB item kind: {item['kind']}")


def gdb_apply_points_commands(state):
    commands = []
    for item in state["breakpoints"]:
        commands.append(gdb_item_command(item))
    for item in state["watchpoints"]:
        commands.append(gdb_item_command(item))
    return commands


def run_gdb(state, extra_commands, batch=True):
    elf = require_context(state, "elf")
    command_file = None
    commands = []
    commands.extend(gdb_base_commands())
    commands.append(f"file {elf}")
    commands.extend(gdb_connect_commands(state))
    commands.extend(gdb_apply_points_commands(state))
    commands.extend(extra_commands)

    try:
        if batch:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
                command_file = handle.name
                for command in commands:
                    handle.write(command + "\n")
            result = subprocess.run(
                ["gdb-multiarch", "--batch", "-x", command_file],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0 and result.stderr:
                return result.stdout + ("\n" if result.stdout else "") + result.stderr
            return result.stdout or result.stderr

        cmd = ["gdb-multiarch", elf]
        for command in gdb_base_commands():
            cmd.extend(["-ex", command])
        for command in gdb_connect_commands(state):
            cmd.extend(["-ex", command])
        for command in gdb_apply_points_commands(state):
            cmd.extend(["-ex", command])
        for command in extra_commands:
            cmd.extend(["-ex", command])
        os.execvp(cmd[0], cmd)
    finally:
        if command_file and os.path.exists(command_file):
            os.unlink(command_file)


def load_interpretations():
    root = workspace_root()
    yaml_candidates = [root / ".stm32-debug.yaml", root / ".stm32-debug.yml"]
    json_candidate = root / ".stm32-debug.json"

    if json_candidate.exists():
        with json_candidate.open("r", encoding="utf-8") as handle:
            return json.load(handle).get("variables", {})

    for path in yaml_candidates:
        if path.exists():
            return parse_simple_yaml(path)

    return {}


def parse_simple_yaml(path):
    variables = {}
    current_var = None
    in_variables = False
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.rstrip()
            if not line or line.lstrip().startswith("#"):
                continue
            if re.fullmatch(r"variables:\s*", line):
                in_variables = True
                continue
            if not in_variables:
                continue
            var_match = re.fullmatch(r"\s{2}([A-Za-z_][A-Za-z0-9_]*)\s*:\s*", line)
            if var_match:
                current_var = var_match.group(1)
                variables[current_var] = {}
                continue
            value_match = re.fullmatch(r"\s{4}([^:]+)\s*:\s*(.+)\s*", line)
            if value_match and current_var:
                key = value_match.group(1).strip().strip("\"'")
                value = value_match.group(2).strip().strip("\"'")
                variables[current_var][key] = value
    return variables


def interpret_value(target_name, raw_value):
    simple_name = re.sub(r"[^A-Za-z0-9_].*$", "", target_name)
    variables = load_interpretations()
    mapping = variables.get(simple_name, {})
    return mapping.get(str(raw_value))


def append_history(entries):
    path = history_file()
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=True) + "\n")


def detect_variable_type(var_name, source_root):
    if not source_root:
        return None
    root = Path(source_root)
    if not root.exists():
        return None

    patterns = {
        "uint8_t": ("uint", 1),
        "int8_t": ("int", 1),
        "uint16_t": ("uint", 2),
        "int16_t": ("int", 2),
        "uint32_t": ("uint", 4),
        "int32_t": ("int", 4),
        "float": ("float", 4),
        "double": ("float", 8),
        "char": ("int", 1),
        "short": ("int", 2),
        "int": ("int", 4),
        "long": ("int", 4),
    }
    token = re.escape(var_name)
    regex = re.compile(rf"\b([A-Za-z_][A-Za-z0-9_\s\*]+)\b{token}\b")
    for ext in (".c", ".h", ".cpp", ".hpp"):
        for path in root.rglob(f"*{ext}"):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line in text.splitlines():
                match = regex.search(line)
                if not match:
                    continue
                type_text = match.group(1)
                for key, result in patterns.items():
                    if key in type_text:
                        return {
                            "display_type": result[0],
                            "size_bytes": result[1],
                            "source": str(path),
                            "type_name": key,
                        }
    return None


def parse_target(state, target, default_type):
    svd = state["context"].get("svd")
    elf = state["context"].get("elf")
    source_root = state["context"].get("source_root")

    if target.startswith("0x"):
        address = int(target, 16)
        return {
            "target": target,
            "name": target,
            "address": address,
            "size_bytes": 4,
            "display_type": default_type,
            "kind": "addr",
            "raw_type": "addr",
        }

    if "->" in target:
        if not svd:
            raise Stm32DebugError(f"Target '{target}' looks like a register, but no SVD is configured.")
        peripheral, register = target.split("->", 1)
        address = get_register_address(svd, peripheral, register)
        return {
            "target": target,
            "name": f"{peripheral}.{register}",
            "address": address,
            "size_bytes": 4,
            "display_type": default_type,
            "kind": "register",
            "raw_type": "register",
        }

    if "." in target and svd:
        parts = target.split(".")
        if len(parts) in (2, 3):
            try:
                get_peripheral_info(svd, parts[0])
                if len(parts) == 2:
                    address = get_register_address(svd, parts[0], parts[1])
                    return {
                        "target": target,
                        "name": target,
                        "address": address,
                        "size_bytes": 4,
                        "display_type": default_type,
                        "kind": "register",
                        "raw_type": "register",
                    }
                field = get_field_info(svd, parts[0], parts[1], parts[2])
                return {
                    "target": target,
                    "name": target,
                    "address": field["address"],
                    "size_bytes": 4,
                    "display_type": default_type,
                    "kind": "field",
                    "raw_type": "field",
                    "field": field,
                }
            except Exception:
                pass

    if not elf:
        raise Stm32DebugError(f"Target '{target}' looks like a variable, but no ELF is configured.")

    address = get_address_from_gdb(elf, target)
    type_info = detect_variable_type(target, source_root)
    size_bytes = type_info["size_bytes"] if type_info else get_type_size_from_gdb(elf, target)
    display_type = type_info["display_type"] if type_info else default_type
    return {
        "target": target,
        "name": target,
        "address": address,
        "size_bytes": size_bytes,
        "display_type": display_type,
        "kind": "variable",
        "raw_type": "variable",
        "source_type": type_info,
    }


def read_target(state, parsed_target):
    width_bits = select_width(parsed_target["size_bytes"])
    response = openocd_read_memory(
        parsed_target["address"],
        width=width_bits,
        count=1,
        host=state["openocd"]["host"],
        port=state["openocd"]["tcl_port"],
    )
    values = parse_values(response)
    if not values:
        raise Stm32DebugError(f"Read failed for {parsed_target['name']}.")
    raw_value = values[0]
    value_width = width_bits
    if parsed_target.get("field"):
        field = parsed_target["field"]
        raw_value = (raw_value & field["mask"]) >> field["bit_offset"]
        value_width = field["bit_width"]
    display = format_value(raw_value, parsed_target["display_type"], value_width)
    interpretation = interpret_value(parsed_target["name"], raw_value)
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "target": parsed_target["target"],
        "name": parsed_target["name"],
        "address": f"0x{parsed_target['address']:08X}",
        "raw_value": raw_value,
        "display_value": display,
        "interpretation": interpretation,
        "kind": parsed_target["kind"],
    }


def parse_id_tokens(tokens):
    ids = set()
    for token in tokens:
        if "-" in token:
            start, end = token.split("-", 1)
            ids.update(range(int(start), int(end) + 1))
        else:
            ids.add(int(token))
    return ids


def build_location_commands():
    return [
        r"echo Location:\n",
        "frame",
        r"echo \nSource:\n",
        "info line *$pc",
        r"echo \n",
    ]


def cmd_start(args, state):
    merge_context(state, args)
    message = start_openocd(state)
    print(message)
    return 0


def cmd_stop(args, state):
    print(stop_openocd(state))
    return 0


def cmd_status(args, state):
    openocd = state["openocd"]
    managed = openocd.get("managed")
    pid = openocd.get("pid")
    print(f"OpenOCD config: {openocd['config']}")
    print(f"GDB port: {openocd['gdb_port']}, TCL port: {openocd['tcl_port']}")
    print(f"Managed by stm32-debug: {'yes' if managed else 'no'}")
    print(f"PID: {pid if pid else 'n/a'}")
    print(f"ELF: {state['context'].get('elf') or 'n/a'}")
    print(f"SVD: {state['context'].get('svd') or 'n/a'}")
    print(f"Breakpoints: {len(state['breakpoints'])}")
    print(f"Watchpoints: {len(state['watchpoints'])}")
    return 0


def cmd_read(args, state):
    merge_context(state, args)
    save_state(state)
    ensure_openocd(state, auto_start=not args.no_auto_openocd)

    results = []
    for target in args.targets:
        parsed = parse_target(state, target, args.type)
        result = read_target(state, parsed)
        results.append(result)
        line = f"{result['name']} = {result['display_value']} @ {result['address']}"
        if result["interpretation"]:
            line += f" ({result['interpretation']})"
        print(line)

    append_history(results)
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=True))
    return 0


def cmd_generate_script(args, state):
    merge_context(state, args)
    save_state(state)
    path = Path(args.output) if args.output else Path(tempfile.mkstemp(suffix=".gdb")[1])
    lines = ["set pagination off"]
    for target in args.targets:
        parsed = parse_target(state, target, args.type)
        width_bits = select_width(parsed["size_bytes"])
        if width_bits == 8:
            fmt = "x/1bx"
        elif width_bits == 16:
            fmt = "x/1hx"
        else:
            fmt = "x/1wx"
        lines.append(f"{fmt} 0x{parsed['address']:08X}  # {parsed['name']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(path)
    return 0


def cmd_break(args, state):
    merge_context(state, args)
    changed = False
    if args.list:
        for item in state["breakpoints"]:
            line = f"{item['id']}: {item['location']}"
            if item.get("condition"):
                line += f" if {item['condition']}"
            print(line)
        return 0

    if args.delete:
        delete_ids = parse_id_tokens(args.delete)
        state["breakpoints"] = [bp for bp in state["breakpoints"] if bp["id"] not in delete_ids]
        changed = True

    if args.clear_all:
        state["breakpoints"] = []
        changed = True

    if args.clear_file:
        state["breakpoints"] = [
            bp for bp in state["breakpoints"] if not bp["location"].startswith(args.clear_file + ":")
        ]
        changed = True

    if args.clear_func:
        state["breakpoints"] = [
            bp for bp in state["breakpoints"] if not fnmatch.fnmatch(bp["location"], args.clear_func)
        ]
        changed = True

    for location in args.locations:
        item = {
            "id": state["next_breakpoint_id"],
            "kind": "breakpoint",
            "location": location,
            "condition": args.condition,
        }
        state["breakpoints"].append(item)
        state["next_breakpoint_id"] += 1
        changed = True
        print(
            f"Breakpoint {item['id']} recorded at {location}"
            + (f" if {args.condition}" if args.condition else "")
        )

    if changed:
        save_state(state)
    return 0


def cmd_watch(args, state):
    merge_context(state, args)
    changed = False
    if args.list:
        for item in state["watchpoints"]:
            print(f"{item['id']}: {item['expression']}")
        return 0

    if args.delete:
        delete_ids = parse_id_tokens(args.delete)
        state["watchpoints"] = [wp for wp in state["watchpoints"] if wp["id"] not in delete_ids]
        changed = True

    if args.expression:
        kind = "watch_addr" if args.expression.startswith("0x") else "watch_var"
        item = {
            "id": state["next_watchpoint_id"],
            "kind": kind,
            "expression": args.expression,
        }
        state["watchpoints"].append(item)
        state["next_watchpoint_id"] += 1
        changed = True
        print(f"Watchpoint {item['id']} recorded for {args.expression}")

    if changed:
        save_state(state)
    return 0


def cmd_continue(args, state):
    merge_context(state, args)
    save_state(state)
    ensure_openocd(state, auto_start=not args.no_auto_openocd)
    output = run_gdb(
        state,
        [
            "monitor halt",
            "continue",
            r"echo \n=== Paused ===\n",
            *build_location_commands(),
            r"echo Call Stack:\n",
            "bt",
            r"echo \nLocal Variables:\n",
            "info locals",
        ],
    )
    print(output.rstrip())
    return 0


def cmd_run_alias(args, state):
    return cmd_continue(args, state)


def cmd_step_like(args, state, command):
    merge_context(state, args)
    save_state(state)
    ensure_openocd(state, auto_start=not args.no_auto_openocd)
    output = run_gdb(
        state,
        [
            "monitor halt",
            f"{command} {args.count}",
            r"echo \n=== After step ===\n",
            *build_location_commands(),
            r"echo Local Variables:\n",
            "info locals",
        ],
    )
    print(output.rstrip())
    return 0


def cmd_finish(args, state):
    merge_context(state, args)
    save_state(state)
    ensure_openocd(state, auto_start=not args.no_auto_openocd)
    output = run_gdb(
        state,
        [
            "monitor halt",
            "finish",
            r"echo \n=== After finish ===\n",
            *build_location_commands(),
            r"echo Local Variables:\n",
            "info locals",
        ],
    )
    print(output.rstrip())
    return 0


def cmd_restart(args, state):
    merge_context(state, args)
    save_state(state)
    ensure_openocd(state, auto_start=not args.no_auto_openocd)
    output = run_gdb(
        state,
        [
            "monitor reset halt",
            r"echo Restarted and halted.\n",
            *build_location_commands(),
        ],
    )
    print(output.rstrip())
    return 0


def cmd_backtrace(args, state):
    merge_context(state, args)
    save_state(state)
    ensure_openocd(state, auto_start=not args.no_auto_openocd)
    command = "bt" if args.depth is None else f"bt {args.depth}"
    output = run_gdb(state, ["monitor halt", command])
    print(output.rstrip())
    return 0


def cmd_locals(args, state):
    merge_context(state, args)
    save_state(state)
    ensure_openocd(state, auto_start=not args.no_auto_openocd)
    output = run_gdb(state, ["monitor halt", "info locals"])
    print(output.rstrip())
    return 0


def cmd_registers(args, state):
    merge_context(state, args)
    save_state(state)
    ensure_openocd(state, auto_start=not args.no_auto_openocd)
    command = "info registers" if not args.names else "info registers " + " ".join(args.names)
    output = run_gdb(state, ["monitor halt", command])
    print(output.rstrip())
    return 0


def cmd_frame(args, state):
    merge_context(state, args)
    save_state(state)
    ensure_openocd(state, auto_start=not args.no_auto_openocd)
    output = run_gdb(
        state,
        ["monitor halt", f"frame {args.index}", "bt", "info locals"],
    )
    print(output.rstrip())
    return 0


def cmd_info(args, state):
    merge_context(state, args)
    save_state(state)
    ensure_openocd(state, auto_start=not args.no_auto_openocd)
    output = run_gdb(
        state,
        ["monitor halt", *build_location_commands(), "info registers pc sp lr xpsr"],
    )
    print(output.rstrip())
    return 0


def cmd_interactive(args, state):
    merge_context(state, args)
    save_state(state)
    ensure_openocd(state, auto_start=not args.no_auto_openocd)
    run_gdb(state, [], batch=False)
    return 0


def cmd_history(args, state):
    path = history_file()
    if not path.exists():
        print("No history recorded yet.")
        return 0
    entries = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if args.targets and entry["target"] not in args.targets and entry["name"] not in args.targets:
                continue
            entries.append(entry)
    for entry in entries[-args.limit :]:
        line = f"{entry['timestamp']} {entry['name']} = {entry['display_value']}"
        if entry.get("interpretation"):
            line += f" ({entry['interpretation']})"
        print(line)
    return 0


def shell_split_or_none(command):
    if not command:
        return None
    return shlex.split(command)


def cmd_cycle(args, state):
    merge_context(state, args)
    save_state(state)
    build_cmd = args.build_cmd or state["context"].get("build_cmd")
    flash_elf = args.flash_elf or state["context"].get("flash_elf") or state["context"].get("elf")

    if build_cmd:
        print(f"Running build: {build_cmd}")
        result = subprocess.run(shell_split_or_none(build_cmd), text=True)
        if result.returncode != 0:
            raise Stm32DebugError(f"Build failed with exit code {result.returncode}.")

    if args.flash and flash_elf:
        print(f"Flashing: {flash_elf}")
        result = subprocess.run(
            [str(workspace_root() / "scripts" / "flash.sh"), flash_elf],
            text=True,
        )
        if result.returncode != 0:
            raise Stm32DebugError(f"Flash failed with exit code {result.returncode}.")

    if args.wait > 0:
        print(f"Waiting {args.wait:.1f}s...")
        time.sleep(args.wait)

    if args.break_at:
        item = {
            "id": state["next_breakpoint_id"],
            "kind": "breakpoint",
            "location": args.break_at,
            "condition": None,
        }
        state["breakpoints"].append(item)
        state["next_breakpoint_id"] += 1
        save_state(state)

    read_args = argparse.Namespace(
        elf=args.elf,
        svd=args.svd,
        build_cmd=args.build_cmd,
        flash_elf=args.flash_elf,
        source_root=args.source_root,
        openocd_config=args.openocd_config,
        openocd_host=args.openocd_host,
        gdb_port=args.gdb_port,
        tcl_port=args.tcl_port,
        telnet_port=args.telnet_port,
        no_auto_openocd=args.no_auto_openocd,
        type=args.type,
        targets=args.targets,
        json=args.json,
    )
    return cmd_read(read_args, state)


def cmd_analyze(args, state):
    values = {}
    for file_name in args.files:
        path = Path(file_name)
        if not path.exists():
            raise Stm32DebugError(f"File not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise Stm32DebugError(f"Unsupported JSON format in {path}") from exc
                if isinstance(payload, list):
                    for item in payload:
                        values.setdefault(item["name"], []).append(item["raw_value"])
                break
            else:
                if isinstance(data, list):
                    for item in data:
                        values.setdefault(item["name"], []).append(item["raw_value"])
                elif isinstance(data, dict) and "name" in data:
                    values.setdefault(data["name"], []).append(data["raw_value"])
    for name, samples in values.items():
        print(
            f"{name}: count={len(samples)} min={min(samples)} max={max(samples)} "
            f"last={samples[-1]}"
        )
    return 0


def add_common_context(parser):
    parser.add_argument("--elf", help="ELF file path")
    parser.add_argument("--svd", help="SVD file path")
    parser.add_argument("--source-root", help="Source tree used for type inference")
    parser.add_argument("--build-cmd", help="Build command used by cycle")
    parser.add_argument("--flash-elf", help="ELF file used by cycle flashing")
    parser.add_argument("--openocd-config", default=None, help="OpenOCD config file")
    parser.add_argument("--openocd-host", default=None, help="OpenOCD host")
    parser.add_argument("--gdb-port", type=int, default=None, help="OpenOCD GDB port")
    parser.add_argument("--tcl-port", type=int, default=None, help="OpenOCD TCL port")
    parser.add_argument("--telnet-port", type=int, default=None, help="OpenOCD telnet port")
    parser.add_argument(
        "--no-auto-openocd",
        action="store_true",
        help="Fail if OpenOCD is not already running",
    )


def build_parser():
    parser = argparse.ArgumentParser(prog="stm32-debug")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="Start OpenOCD and store context")
    add_common_context(start_parser)
    start_parser.set_defaults(func=cmd_start)

    stop_parser = subparsers.add_parser("stop", help="Stop managed OpenOCD")
    stop_parser.set_defaults(func=cmd_stop)

    status_parser = subparsers.add_parser("status", help="Show current session state")
    status_parser.set_defaults(func=cmd_status)

    read_parser = subparsers.add_parser("read", help="Read variables, registers, fields, or addresses")
    add_common_context(read_parser)
    read_parser.add_argument("targets", nargs="+", help="Targets like counter, GPIOA->ODR, RCC.CFGR.SW0, 0x20000000")
    read_parser.add_argument("--type", choices=["float", "int", "uint", "hex"], default="hex")
    read_parser.add_argument("--json", action="store_true", help="Also print JSON output")
    read_parser.set_defaults(func=cmd_read)

    script_parser = subparsers.add_parser("generate-script", help="Generate a GDB memory-read script")
    add_common_context(script_parser)
    script_parser.add_argument("targets", nargs="+")
    script_parser.add_argument("--type", choices=["float", "int", "uint", "hex"], default="hex")
    script_parser.add_argument("--output", help="Output .gdb file path")
    script_parser.set_defaults(func=cmd_generate_script)

    break_parser = subparsers.add_parser("break", help="Manage logical breakpoints")
    add_common_context(break_parser)
    break_parser.add_argument("locations", nargs="*", help="Breakpoint locations like main.c:145 or my_func")
    break_parser.add_argument("--condition", help="Breakpoint condition")
    break_parser.add_argument("--list", action="store_true", help="List breakpoints")
    break_parser.add_argument("--delete", nargs="+", help="Delete breakpoint ids or ranges")
    break_parser.add_argument("--clear-all", action="store_true")
    break_parser.add_argument("--clear-file", help="Delete breakpoints matching file prefix")
    break_parser.add_argument("--clear-func", help="Delete breakpoints matching glob")
    break_parser.set_defaults(func=cmd_break)

    watch_parser = subparsers.add_parser("watch", help="Manage watchpoints")
    add_common_context(watch_parser)
    watch_parser.add_argument("expression", nargs="?", help="Variable name or address")
    watch_parser.add_argument("--list", action="store_true")
    watch_parser.add_argument("--delete", nargs="+")
    watch_parser.set_defaults(func=cmd_watch)

    continue_parser = subparsers.add_parser("continue", help="Continue execution to next stop")
    add_common_context(continue_parser)
    continue_parser.set_defaults(func=cmd_continue)

    run_parser = subparsers.add_parser("run", help="Alias of continue")
    add_common_context(run_parser)
    run_parser.set_defaults(func=cmd_run_alias)

    step_parser = subparsers.add_parser("step", help="Single-step into functions")
    add_common_context(step_parser)
    step_parser.add_argument("count", nargs="?", type=int, default=1)
    step_parser.set_defaults(func=lambda args, state: cmd_step_like(args, state, "step"))

    next_parser = subparsers.add_parser("next", help="Single-step without entering functions")
    add_common_context(next_parser)
    next_parser.add_argument("count", nargs="?", type=int, default=1)
    next_parser.set_defaults(func=lambda args, state: cmd_step_like(args, state, "next"))

    finish_parser = subparsers.add_parser("finish", help="Run until current function returns")
    add_common_context(finish_parser)
    finish_parser.set_defaults(func=cmd_finish)

    restart_parser = subparsers.add_parser("restart", help="Reset and halt target")
    add_common_context(restart_parser)
    restart_parser.set_defaults(func=cmd_restart)

    bt_parser = subparsers.add_parser("backtrace", help="Show backtrace")
    add_common_context(bt_parser)
    bt_parser.add_argument("depth", nargs="?", type=int)
    bt_parser.set_defaults(func=cmd_backtrace)

    locals_parser = subparsers.add_parser("locals", help="Show local variables")
    add_common_context(locals_parser)
    locals_parser.set_defaults(func=cmd_locals)

    regs_parser = subparsers.add_parser("registers", help="Show CPU registers")
    add_common_context(regs_parser)
    regs_parser.add_argument("names", nargs="*")
    regs_parser.set_defaults(func=cmd_registers)

    frame_parser = subparsers.add_parser("frame", help="Select a stack frame and show locals")
    add_common_context(frame_parser)
    frame_parser.add_argument("index", type=int)
    frame_parser.set_defaults(func=cmd_frame)

    info_parser = subparsers.add_parser("info", help="Show current file/line/function and key registers")
    add_common_context(info_parser)
    info_parser.set_defaults(func=cmd_info)

    gdb_parser = subparsers.add_parser("interactive", help="Start interactive GDB session")
    add_common_context(gdb_parser)
    gdb_parser.set_defaults(func=cmd_interactive)

    gdb_alias_parser = subparsers.add_parser("gdb", help="Alias of interactive")
    add_common_context(gdb_alias_parser)
    gdb_alias_parser.set_defaults(func=cmd_interactive)

    history_parser = subparsers.add_parser("history", help="Show recent read history")
    history_parser.add_argument("targets", nargs="*")
    history_parser.add_argument("--limit", type=int, default=10)
    history_parser.set_defaults(func=cmd_history)

    cycle_parser = subparsers.add_parser("cycle", help="Build, flash, wait, then read")
    add_common_context(cycle_parser)
    cycle_parser.add_argument("targets", nargs="+")
    cycle_parser.add_argument("--type", choices=["float", "int", "uint", "hex"], default="hex")
    cycle_parser.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS)
    cycle_parser.add_argument("--flash", action="store_true", help="Flash after build")
    cycle_parser.add_argument("--break-at", help="Record a breakpoint before reading")
    cycle_parser.add_argument("--json", action="store_true")
    cycle_parser.set_defaults(func=cmd_cycle)

    analyze_parser = subparsers.add_parser("analyze", help="Summarize JSON result files")
    analyze_parser.add_argument("files", nargs="+")
    analyze_parser.set_defaults(func=cmd_analyze)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    state = load_state()
    try:
        return args.func(args, state)
    except Stm32DebugError as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
