#!/usr/bin/env python3
import os
import sys
import xml.etree.ElementTree as ET


def _text(node, tag, default=None):
    child = node.find(tag) if node is not None else None
    if child is None or child.text is None:
        return default
    return child.text.strip()


def _parse_int(value, default=None):
    if value is None:
        return default
    value = value.strip()
    base = 16 if value.lower().startswith("0x") else 10
    return int(value, base)


def load_svd(svd_file):
    if not os.path.exists(svd_file):
        raise FileNotFoundError(f"SVD file '{svd_file}' not found.")
    try:
        tree = ET.parse(svd_file)
    except Exception as exc:
        raise ValueError(f"Error parsing SVD file: {exc}") from exc
    return tree.getroot()


def find_peripheral(root, peripheral_name):
    for peripheral in root.findall(".//peripheral"):
        if _text(peripheral, "name") == peripheral_name:
            return peripheral
    return None


def find_register(peripheral_node, register_name):
    registers = peripheral_node.find("registers")
    if registers is None:
        return None
    for register in registers.findall("register"):
        if _text(register, "name") == register_name:
            return register
    return None


def find_field(register_node, field_name):
    fields = register_node.find("fields")
    if fields is None:
        return None
    for field in fields.findall("field"):
        if _text(field, "name") == field_name:
            return field
    return None


def get_peripheral_info(svd_file, peripheral_name):
    root = load_svd(svd_file)
    peripheral = find_peripheral(root, peripheral_name)
    if peripheral is None:
        raise KeyError(f"Peripheral '{peripheral_name}' not found.")
    return {
        "name": peripheral_name,
        "base_address": _parse_int(_text(peripheral, "baseAddress")),
        "description": _text(peripheral, "description", ""),
        "node": peripheral,
    }


def get_register_info(svd_file, peripheral_name, register_name):
    peripheral_info = get_peripheral_info(svd_file, peripheral_name)
    register = find_register(peripheral_info["node"], register_name)
    if register is None:
        raise KeyError(f"Register '{register_name}' not found in {peripheral_name}.")
    offset = _parse_int(_text(register, "addressOffset"))
    size = _parse_int(_text(register, "size"), 32)
    return {
        "peripheral": peripheral_name,
        "name": register_name,
        "address_offset": offset,
        "address": peripheral_info["base_address"] + offset,
        "size": size,
        "description": _text(register, "description", ""),
        "node": register,
    }


def get_field_info(svd_file, peripheral_name, register_name, field_name):
    register_info = get_register_info(svd_file, peripheral_name, register_name)
    field = find_field(register_info["node"], field_name)
    if field is None:
        raise KeyError(
            f"Field '{field_name}' not found in {peripheral_name}.{register_name}."
        )
    bit_offset = _parse_int(_text(field, "bitOffset"), 0)
    bit_width = _parse_int(_text(field, "bitWidth"), 1)
    mask = ((1 << bit_width) - 1) << bit_offset
    return {
        "peripheral": peripheral_name,
        "register": register_name,
        "name": field_name,
        "address": register_info["address"],
        "bit_offset": bit_offset,
        "bit_width": bit_width,
        "mask": mask,
        "description": _text(field, "description", ""),
    }


def get_register_address(svd_file, peripheral_name, register_name=None):
    if register_name is None:
        return get_peripheral_info(svd_file, peripheral_name)["base_address"]
    return get_register_info(svd_file, peripheral_name, register_name)["address"]


def parse_svd(svd_file, peripheral_name, register_name=None, field_name=None):
    peripheral_info = get_peripheral_info(svd_file, peripheral_name)
    print(
        f"Peripheral: {peripheral_name} "
        f"(Base: 0x{peripheral_info['base_address']:08X})"
    )
    if peripheral_info["description"]:
        print(f"Description: {peripheral_info['description']}")

    if register_name is None:
        registers = peripheral_info["node"].find("registers")
        if registers is None:
            print("No registers found.")
            return
        print("Registers:")
        for register in registers.findall("register"):
            name = _text(register, "name", "<unnamed>")
            offset = _parse_int(_text(register, "addressOffset"), 0)
            address = peripheral_info["base_address"] + offset
            print(f"  {name:<16} (Offset: 0x{offset:04X}, Address: 0x{address:08X})")
        return

    register_info = get_register_info(svd_file, peripheral_name, register_name)
    print(
        f"Register: {register_name} "
        f"(Address: 0x{register_info['address']:08X}, Size: {register_info['size']} bits)"
    )
    if register_info["description"]:
        print(f"Description: {register_info['description']}")

    if field_name is not None:
        field_info = get_field_info(svd_file, peripheral_name, register_name, field_name)
        msb = field_info["bit_offset"] + field_info["bit_width"] - 1
        print(
            f"Field: {field_name} [{msb}:{field_info['bit_offset']}] "
            f"Mask: 0x{field_info['mask']:08X}"
        )
        if field_info["description"]:
            print(f"Description: {field_info['description']}")
        return

    fields = register_info["node"].find("fields")
    if fields is None:
        print("No fields found.")
        return
    print("Fields:")
    for field in fields.findall("field"):
        field_name = _text(field, "name", "<unnamed>")
        bit_offset = _parse_int(_text(field, "bitOffset"), 0)
        bit_width = _parse_int(_text(field, "bitWidth"), 1)
        description = _text(field, "description", "")
        msb = bit_offset + bit_width - 1
        print(f"  {field_name:<16} [{msb}:{bit_offset}] - {description}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 read_svd.py <svd_file> <peripheral> [register] [field]")
        sys.exit(1)

    try:
        svd_file = sys.argv[1]
        peripheral = sys.argv[2]
        register = sys.argv[3] if len(sys.argv) > 3 else None
        field = sys.argv[4] if len(sys.argv) > 4 else None
        parse_svd(svd_file, peripheral, register, field)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(exc)
        sys.exit(1)
