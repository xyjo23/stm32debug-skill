#!/usr/bin/env python3
import argparse
import atexit
import os
import re
import socket
import struct
import subprocess
import sys
import time

from read_svd import get_field_info, get_register_address


DEFAULT_OPENOCD_CONFIG = os.environ.get("OPENOCD_CONFIG", "board/stm32f7discovery.cfg")


def get_address_from_gdb(elf_file, expression):
    """Use GDB to resolve the address of a C expression."""
    cmd = [
        "gdb-multiarch",
        "--batch",
        "-ex",
        f"file {elf_file}",
        "-ex",
        f"print &({expression})",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        match = re.search(r"0x([0-9a-fA-F]+)", result.stdout)
        if match:
            return int(match.group(1), 16)
        raise ValueError(
            f"Error resolving '{expression}': GDB output parse failed.\nOutput: {result.stdout}"
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Error running GDB for '{expression}'.\nStdout: {exc.stdout}\nStderr: {exc.stderr}"
        ) from exc
    except FileNotFoundError as exc:
        raise RuntimeError("Error: gdb-multiarch not found. Please install it.") from exc


def get_type_size_from_gdb(elf_file, expression):
    cmd = [
        "gdb-multiarch",
        "--batch",
        "-ex",
        f"file {elf_file}",
        "-ex",
        f"print sizeof({expression})",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        match = re.search(r"=\s*(\d+)", result.stdout)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return 4


class OpenOCDManager:
    def __init__(self, config_path, host, tcl_port, startup_delay=1.5):
        self.config_path = config_path
        self.host = host
        self.tcl_port = tcl_port
        self.startup_delay = startup_delay
        self.process = None

    def is_running(self):
        try:
            with socket.create_connection((self.host, self.tcl_port), timeout=0.5):
                return True
        except OSError:
            return False

    def ensure_running(self):
        if self.is_running():
            return False

        self.process = subprocess.Popen(
            ["openocd", "-f", self.config_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 8
        while time.time() < deadline:
            if self.is_running():
                return True
            if self.process.poll() is not None:
                raise RuntimeError("OpenOCD exited unexpectedly while starting.")
            time.sleep(0.2)

        raise RuntimeError(
            f"Timed out waiting for OpenOCD TCL port {self.tcl_port}. "
            f"Check config '{self.config_path}'."
        )

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process = None


def openocd_read_memory(address, width=32, count=1, host="127.0.0.1", port=6666):
    """Read memory via the OpenOCD TCL port."""
    if width == 8:
        cmd = "mdb"
    elif width == 16:
        cmd = "mdh"
    else:
        cmd = "mdw"

    full_cmd = f"{cmd} 0x{address:08X} {count}"
    with socket.create_connection((host, port), timeout=2) as sock:
        sock.sendall((full_cmd + "\x1a").encode("utf-8"))
        data = b""
        while True:
            chunk = sock.recv(1024)
            if not chunk:
                break
            data += chunk
            if b"\x1a" in chunk:
                break
    return data.decode("utf-8").strip().replace("\x1a", "")


def parse_values(response):
    if not response:
        return []
    values = []
    try:
        _, payload = response.split(":", 1)
    except ValueError:
        return values
    for token in payload.split():
        try:
            values.append(int(token, 16))
        except ValueError:
            continue
    return values


def select_width(size_bytes):
    if size_bytes <= 1:
        return 8
    if size_bytes <= 2:
        return 16
    return 32


def format_value(raw_value, value_type, width_bits):
    if value_type == "float":
        if width_bits != 32:
            raise ValueError("Float display is only supported for 32-bit values.")
        packed = struct.pack("I", raw_value & 0xFFFFFFFF)
        return f"{struct.unpack('f', packed)[0]:.6f}"
    if value_type == "int":
        sign_bit = 1 << (width_bits - 1)
        mask = (1 << width_bits) - 1
        raw_value &= mask
        if raw_value & sign_bit:
            raw_value -= (1 << width_bits)
        return str(raw_value)
    if value_type == "uint":
        return str(raw_value)
    return f"0x{raw_value:X}"


def resolve_target(args):
    if args.var:
        if not args.elf:
            raise ValueError("Error: --elf required for --var")
        print(f"Resolving address of '{args.var}' using GDB...")
        address = get_address_from_gdb(args.elf, args.var)
        size = get_type_size_from_gdb(args.elf, args.var)
        return {
            "name": args.var,
            "address": address,
            "size_bytes": size,
            "bitfield": None,
        }

    if args.reg:
        if not args.svd:
            raise ValueError("Error: --svd required for --reg")
        peripheral = args.reg[0]
        register = args.reg[1] if len(args.reg) > 1 else None
        field = args.reg[2] if len(args.reg) > 2 else None
        address = get_register_address(args.svd, peripheral, register)
        name = peripheral if register is None else f"{peripheral}.{register}"
        bitfield = None
        if field is not None:
            field_info = get_field_info(args.svd, peripheral, register, field)
            name = f"{name}.{field}"
            bitfield = field_info
        return {
            "name": name,
            "address": address,
            "size_bytes": 4,
            "bitfield": bitfield,
        }

    if args.addr is not None:
        return {
            "name": args.label or f"0x{args.addr:08X}",
            "address": args.addr,
            "size_bytes": args.size,
            "bitfield": None,
        }

    raise ValueError("Error: specify one of --var, --reg, or --addr")


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Read or monitor STM32 variables, registers, register fields, or direct addresses."
        )
    )
    parser.add_argument("--elf", help="Path to ELF file (required for --var)")
    parser.add_argument("--var", help="C expression to monitor, e.g. counter or my_struct.member")
    parser.add_argument("--svd", help="Path to SVD file (required for --reg)")
    parser.add_argument(
        "--reg",
        nargs="+",
        help="Peripheral [register] [field], e.g. GPIOA MODER MODER7",
    )
    parser.add_argument(
        "--addr",
        type=lambda value: int(value, 0),
        help="Direct address to read, e.g. 0x20000000",
    )
    parser.add_argument("--label", help="Optional display name for --addr")
    parser.add_argument("--size", type=int, default=4, help="Read size in bytes for --addr")
    parser.add_argument(
        "--type",
        choices=["float", "int", "uint", "hex"],
        default="hex",
        help="Display format",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="Update interval in seconds")
    parser.add_argument("--count", type=int, default=0, help="Number of samples (0 = forever)")
    parser.add_argument(
        "--openocd-config",
        default=DEFAULT_OPENOCD_CONFIG,
        help=f"OpenOCD config used for auto-start (default: {DEFAULT_OPENOCD_CONFIG})",
    )
    parser.add_argument("--openocd-host", default="127.0.0.1", help="OpenOCD TCL host")
    parser.add_argument("--openocd-port", type=int, default=6666, help="OpenOCD TCL port")
    parser.add_argument(
        "--no-auto-openocd",
        action="store_true",
        help="Fail instead of auto-starting OpenOCD when the TCL port is not available",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        target = resolve_target(args)
    except (RuntimeError, ValueError, KeyError) as exc:
        print(exc)
        return 1

    manager = OpenOCDManager(args.openocd_config, args.openocd_host, args.openocd_port)
    started_by_script = False

    try:
        if not manager.is_running():
            if args.no_auto_openocd:
                raise RuntimeError(
                    "OpenOCD TCL port is not reachable. Start OpenOCD manually or remove --no-auto-openocd."
                )
            started_by_script = manager.ensure_running()
            if started_by_script:
                atexit.register(manager.stop)

        width_bits = select_width(target["size_bytes"])
        description = (
            f"Monitoring {target['name']} at 0x{target['address']:08X} "
            f"(Size: {target['size_bytes']} bytes, Interval: {args.interval}s)"
        )
        if target["bitfield"]:
            field = target["bitfield"]
            msb = field["bit_offset"] + field["bit_width"] - 1
            description += f", Field: [{msb}:{field['bit_offset']}]"
        if started_by_script:
            description += " [OpenOCD auto-started]"
        print(description)

        samples = 0
        while True:
            response = openocd_read_memory(
                target["address"],
                width=width_bits,
                count=1,
                host=args.openocd_host,
                port=args.openocd_port,
            )
            values = parse_values(response)
            if not values:
                print(f"[{time.strftime('%H:%M:%S')}] {target['name']} = Read Error")
            else:
                raw_value = values[0]
                if target["bitfield"]:
                    field = target["bitfield"]
                    raw_value = (raw_value & field["mask"]) >> field["bit_offset"]
                    value_width = field["bit_width"]
                else:
                    value_width = width_bits
                display_value = format_value(raw_value, args.type, value_width)
                print(f"[{time.strftime('%H:%M:%S')}] {target['name']} = {display_value}")

            samples += 1
            if args.count > 0 and samples >= args.count:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    finally:
        if started_by_script:
            manager.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
