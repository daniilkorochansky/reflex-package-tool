# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------------------------------------------
#   Reflex Package Tool — A tool for working with .package game archives.
#   Copyright (C) 2026  Daniil Korochansky
#
#   This file is part of Reflex Package Tool.
#
#   Reflex Package Tool is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   Reflex Package Tool is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with Reflex Package Tool.  If not, see <https://www.gnu.org/licenses/>.
# -------------------------------------------------------------------------------------------------------------------

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import importlib.util
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Explicit wxPython imports.
import wx
import wx.adv
import wx.dataview as dv


APP_NAME = "Reflex Package Tool"

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

SOURCE_VERSION = "v1.2.3"
BXML_HEADER = struct.Struct("<9I")
BXML_SIGNATURE = 0x4C4D5842
ATTR_STRUCT = struct.Struct("<IIHH")
NODE_STRUCT = struct.Struct("<IiIIIIII")


class PackageDropTarget(wx.FileDropTarget):
    def __init__(self, window):
        super().__init__()
        self.window = window

    def OnDropFiles(self, x, y, filenames):
        valid_files = [f for f in filenames if f.lower().endswith(".package")]

        if valid_files:
            self.window.handle_dropped_files(valid_files[0])
            return True
        else:
            return False

# ---------------------------------------------------------------------------
# BXML / database
# ---------------------------------------------------------------------------

@dataclass
class BxmlHeader:
    signature: int
    version: int
    str_count: int
    pool_pointer: int
    pool_size: int
    attr_count: int
    node_count: int
    unknown: int
    zsize: int


@dataclass
class AssetInfo:
    name: str
    type: str
    package_name: str
    package_offset: int
    heap_offset: int
    heap_size: Optional[int]
    absolute_offset: int
    compressed: bool
    codec: Optional[str]


def load_bxml_database_tool(database_path: Path):
    """
    Load bxml_database_tool.py from beside this script.

    We deliberately use that project tool as the decoder instead of creating
    a second incompatible BXML decoder.
    """
    candidates = [
        Path(__file__).with_name("bxml_database_tool.py"),
        database_path.with_name("bxml_database_tool.py"),
        Path.cwd() / "bxml_database_tool.py",
    ]

    tool_path = next((p for p in candidates if p.exists()), None)

    if tool_path is None:
        raise RuntimeError(
            "bxml_database_tool.py was not found.\n\n"
            "Place bxml_database_tool.py beside Reflex_Package_Tool.py."
        )

    spec = importlib.util.spec_from_file_location(
        "reflex_bxml_database_tool",
        tool_path,
    )

    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load bxml_database_tool.py")

    module = importlib.util.module_from_spec(spec)

    # dataclasses (used by bxml_database_tool.py) expects the module to be
    # present in sys.modules while class decorators are being evaluated.
    # Without this, Python 3.13/3.14 can fail with:
    #   'NoneType' object has no attribute '__dict__'
    sys.modules[spec.name] = module

    spec.loader.exec_module(module)
    return module


def database_entry_is_compressed(entry: dict) -> bool:
    """
    Return whether Database explicitly contains a <Compress> element
    for this resource.

    The package format used by Reflex only needs two states here:
      - no Compress element -> raw/uncompressed resource
      - Compress element    -> package-compressed resource

    bxml_database_tool versions may expose this as either a boolean
    'compressed' field or a 'compress' field. Missing means no Compress.
    """
    if "compressed" in entry:
        return bool(entry["compressed"])

    if "compress" in entry:
        value = entry["compress"]
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, dict):
            return True
        text = str(value).strip().lower()
        return text not in {"", "0", "false", "none", "null"}

    return False


def parse_database(database_path: Path):
    """
    Decode the Database BXML and build the real resource table from
    Packages/Package/Asset/Heap.

    The BXML numeric pool is a value pool, not a generic INFO table.
    Older versions tried to interpret the pool as 20-byte INFO records.
    That happened to work for some simple databases but fails for track
    databases where the pool also contains Package/Heap/Compress values.

    The bxml_database_tool provides database_assets() so the same logic is
    used consistently by the package tool.
    """
    tool = load_bxml_database_tool(database_path)
    parsed = tool.decode(str(database_path))

    h = parsed.header

    if h.signature != BXML_SIGNATURE:
        raise ValueError("Database is not BXML")

    if not hasattr(tool, "database_assets"):
        raise ValueError(
            "This bxml_database_tool.py is too old. "
            "Use the bundled database tool v2 or newer."
        )

    entries = tool.database_assets(parsed)

    assets_by_offset: dict[int, list[AssetInfo]] = {}

    for entry in entries:
        item = AssetInfo(
            name=entry["name"],
            type=entry["type"],
            package_name=entry["package_name"],
            package_offset=entry["package_offset"],
            heap_offset=entry["heap_offset"],
            heap_size=entry["heap_size"],
            absolute_offset=entry["absolute_offset"],
            compressed=database_entry_is_compressed(entry),
            codec=entry.get("codec"),
        )

        assets_by_offset.setdefault(
            entry["absolute_offset"],
            [],
        ).append(item)

    return parsed, h.pool_pointer, h.pool_size, assets_by_offset



def parse_numeric(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None

    value = value.strip()

    # BXML editor emits values such as:
    # _uint:123
    # _int:123
    value = re.sub(
        r"^_(?:u?int|float|bool|vector3):",
        "",
        value,
        flags=re.IGNORECASE,
    )

    try:
        return int(value, 0)
    except ValueError:
        try:
            return int(value, 10)
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Exact INFO parser
# ---------------------------------------------------------------------------

@dataclass
class InfoEntry:
    index: int
    offset: int
    stored_size: int
    base_offset: int


def parse_info_entries(parsed, info_off: int, info_size: int):
    raw = parsed.raw
    limit = info_off + info_size

    if info_off < 0 or limit > len(raw):
        raise ValueError(
            f"Invalid INFO region: 0x{info_off:X} + "
            f"0x{info_size:X} exceeds BXML raw size 0x{len(raw):X}"
        )

    pos = info_off
    base_offset = 0
    entries: list[InfoEntry] = []
    index = 0

    while pos + 12 <= limit:
        relative_offset, size, zero = struct.unpack_from(
            "<III",
            raw,
            pos,
        )
        pos += 12

        if size == 0:
            base_offset = relative_offset
            continue

        if pos + 8 > limit:
            raise ValueError(
                "Truncated INFO entry after offset/size record"
            )

        zero2, one = struct.unpack_from(
            "<II",
            raw,
            pos,
        )
        pos += 8

        absolute_offset = relative_offset + base_offset

        entries.append(
            InfoEntry(
                index=index,
                offset=absolute_offset,
                stored_size=size,
                base_offset=base_offset,
            )
        )

        index += 1

    if pos != limit:
        # INFO_SIZE can contain alignment/padding in some files.
        # Do not reject harmless trailing bytes.
        if any(raw[pos:limit]):
            raise ValueError(
                f"Unexpected non-zero data at end of INFO region "
                f"(0x{pos:X}-0x{limit:X})"
            )

    return entries


# ---------------------------------------------------------------------------
# Package mode
# ---------------------------------------------------------------------------

def detect_mode(package_path: Path):
    with package_path.open("rb") as f:
        head = f.read(12)

    if len(head) != 12:
        raise ValueError("Package is smaller than the mode header")

    test0, test1, test2 = struct.unpack("<III", head)

    mode = 0 if (
        test1 <= 0x10000
        and test2 <= 0x10000
    ) else 1

    return mode, test0, test1, test2


def deflate_decode(data: bytes, expected_size: int):
    """
    'deflate' equivalent for the package chunks.

    Try raw DEFLATE first, then zlib-wrapped DEFLATE as a compatibility
    fallback. The raw form is the normal package form.
    """
    errors = []

    for wbits in (-15, 15):
        try:
            out = zlib.decompress(data, wbits)

            if len(out) != expected_size:
                raise ValueError(
                    f"decoded {len(out)} bytes, expected {expected_size}"
                )

            return out
        except Exception as exc:
            errors.append(exc)

    raise ValueError(
        "Deflate decompression failed: "
        + "; ".join(str(e) for e in errors)
    )



def pe_machine(path: Path) -> Optional[int]:
    """Return PE Machine value, or None if the file is not a PE DLL."""
    try:
        data = path.read_bytes()
        if len(data) < 0x40 or data[:2] != b"MZ":
            return None

        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]

        if pe_offset + 6 > len(data):
            return None

        if data[pe_offset:pe_offset + 4] != b"PE\0\0":
            return None

        return struct.unpack_from(
            "<H",
            data,
            pe_offset + 4,
        )[0]
    except Exception:
        return None


def machine_name(machine: Optional[int]) -> str:
    return {
        0x014C: "x86 (32-bit)",
        0x8664: "x64 (64-bit)",
        0xAA64: "ARM64",
    }.get(machine, f"unknown (0x{machine:04X})" if machine else "unknown")


def xmem_decompress(data: bytes, expected_size: int):
    """
    XMemDecompress through an external 32-bit helper.

    The supplied mszip.dll is x86 while the main wxPython application can be
    x64. The helper is therefore a small x86 process that loads mszip.dll and
    calls XMemDecompress.
    """
    if os.name != "nt":
        raise RuntimeError("XMemDecompress requires Windows.")

    helper_candidates = [
        Path(__file__).resolve().parent / "xmem_helper.exe",
        Path.cwd() / "xmem_helper.exe",
    ]

    helper = next((p for p in helper_candidates if p.exists()), None)

    if helper is None:
        raise RuntimeError(
            "Requires the 32-bit XMem helper.\n\n"
            "Place xmem_helper.exe beside "
            "Reflex Package Tool.exe"
        )

    tool_dir = helper.resolve().parent
    dll = tool_dir / "mszip.dll"

    if not dll.exists():
        raise RuntimeError(
            "xmem_helper.exe was found, but mszip.dll is missing.\n\n"
            f"Expected:\n{dll}"
        )

    with tempfile.TemporaryDirectory(prefix="reflex_xmem_") as tmp:
        tmp_dir = Path(tmp)
        input_path = tmp_dir / "compressed.bin"
        output_path = tmp_dir / "decoded.bin"

        input_path.write_bytes(data)

        command = [
            str(helper),
            str(dll),
            str(input_path),
            str(output_path),
            str(expected_size),
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(tool_dir),
            )
        except OSError as exc:
            raise RuntimeError(
                f"Could not start xmem_helper.exe:\n{exc}"
            ) from exc

        if result.returncode != 0:
            details = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit code {result.returncode}"
            )
            raise RuntimeError(
                "XMemDecompress helper failed:\n\n" + details
            )

        if not output_path.exists():
            raise RuntimeError(
                "XMemDecompress helper completed without producing output."
            )

        decoded = output_path.read_bytes()

    if len(decoded) > expected_size:
        raise ValueError(
            f"XMemDecompress returned {len(decoded)} bytes, "
            f"which exceeds requested size {expected_size}"
        )

    # XMem can report fewer bytes than the requested SIZE. QuickBMS uses a
    # zero-initialized output buffer and then writes SIZE bytes, so preserve
    # the remaining bytes as zero padding.
    if len(decoded) < expected_size:
        decoded += b"\x00" * (expected_size - len(decoded))

    return decoded


def xmem_compress(data: bytes) -> bytes:
    """
    XMemCompress through a separate 32-bit helper.

    Compression gets its own helper so the proven Extract path remains
    exactly as-is.
    """
    if os.name != "nt":
        raise RuntimeError("XMem compression requires Windows.")

    helper_candidates = [
        Path(__file__).resolve().parent / "xmem_compress_helper.exe",
        Path.cwd() / "xmem_compress_helper.exe",
    ]

    helper = next((p for p in helper_candidates if p.exists()), None)

    if helper is None:
        raise RuntimeError(
            "Pack requires xmem_compress_helper.exe."
        )

    tool_dir = helper.resolve().parent
    dll = tool_dir / "mszip.dll"

    if not dll.exists():
        raise RuntimeError(
            "xmem_compress_helper.exe was found, "
            "but mszip.dll is missing.\n\n"
            f"Expected:\n{dll}"
        )

    with tempfile.TemporaryDirectory(prefix="reflex_xmem_pack_") as tmp:
        tmp_dir = Path(tmp)
        input_path = tmp_dir / "decoded.bin"
        output_path = tmp_dir / "compressed.bin"

        input_path.write_bytes(data)
        max_output = max(len(data) * 2 + 65536, 65536)

        command = [
            str(helper),
            str(dll),
            str(input_path),
            str(output_path),
            str(max_output),
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(tool_dir),
            )
        except OSError as exc:
            raise RuntimeError(
                f"Could not start xmem_compress_helper.exe:\n{exc}"
            ) from exc

        if result.returncode != 0:
            details = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit code {result.returncode}"
            )
            raise RuntimeError(
                "XMemCompress helper failed:\n\n" + details
            )

        if not output_path.exists():
            raise RuntimeError(
                "XMemCompress helper completed without producing output."
            )

        return output_path.read_bytes()


def read_mode0_size_check_and_decoded_size(
    package: Path,
    resource: "Resource",
) -> tuple[int, int]:
    """
    Read the original MODE 0 resource layout.

    Returns:
        (SIZE_CHECK, decoded_allocation_size)

    SIZE_CHECK is the logical resource size stored after the XMEM chunk
    stream. decoded_allocation_size is the total SIZE of all chunks.

    Reflex can allocate a full 64 KiB XMEM block for a texture while the
    actual DDS resource is smaller. Those trailing bytes are zero padding
    and must remain part of the XMEM input. SIZE_CHECK must nevertheless
    remain the original logical size.
    """
    if resource.asset is not None and not resource.asset.compressed:
        raise ValueError(
            f"Resource #{resource.index} is not package-compressed."
        )

    with package.open("rb") as f:
        f.seek(resource.info.offset)

        raw = f.read(4)
        if len(raw) != 4:
            raise ValueError(
                f"Cannot read FILE_ZSIZE for resource #{resource.index}."
            )

        file_zsize = struct.unpack("<I", raw)[0]

        if file_zsize < 4:
            raise ValueError(
                f"Invalid FILE_ZSIZE {file_zsize} for resource "
                f"#{resource.index}."
            )

        resource_end = resource.info.offset + file_zsize

        if resource_end > package.stat().st_size:
            raise ValueError(
                f"Resource #{resource.index} exceeds Package."
            )

        current = resource.info.offset + 4
        decoded_allocation_size = 0

        while current < resource_end:
            f.seek(current)
            header = f.read(8)

            if len(header) != 8:
                raise ValueError(
                    f"Truncated MODE 0 chunk for resource "
                    f"#{resource.index}."
                )

            size, zsize = struct.unpack("<II", header)

            if size <= 0 or zsize <= 0:
                raise ValueError(
                    f"Invalid chunk sizes for resource "
                    f"#{resource.index}: SIZE={size}, ZSIZE={zsize}"
                )

            data_end = current + 8 + zsize

            if data_end > resource_end:
                raise ValueError(
                    f"MODE 0 chunk exceeds resource "
                    f"#{resource.index}."
                )

            decoded_allocation_size += size
            current = data_end

        if current != resource_end:
            raise ValueError(
                f"Invalid MODE 0 chunk stream for resource "
                f"#{resource.index}."
            )

        f.seek(resource_end)
        raw_size_check = f.read(4)

        if len(raw_size_check) != 4:
            raise ValueError(
                f"Missing SIZE_CHECK for resource #{resource.index}."
            )

        size_check = struct.unpack("<I", raw_size_check)[0]

    if size_check > decoded_allocation_size:
        raise ValueError(
            f"Original SIZE_CHECK {size_check} exceeds decoded allocation "
            f"size {decoded_allocation_size} for resource #{resource.index}."
        )

    return size_check, decoded_allocation_size


def pack_mode0_resource(
    decoded: bytes,
    size_check: int | None = None,
) -> bytes:
    """
    Build one MODE 0 package resource.

    `decoded` is the complete XMEM input, including any zero padding needed
    to preserve the original chunk allocation.

    `size_check` is the logical resource size stored after the chunk stream.
    When omitted, the decoded size is used.
    """
    if not decoded:
        raise ValueError("Cannot pack an empty resource.")

    if size_check is None:
        size_check = len(decoded)

    if size_check < 0 or size_check > len(decoded):
        raise ValueError(
            f"SIZE_CHECK {size_check} is outside decoded size "
            f"{len(decoded)}."
        )

    output = bytearray(b"\x00\x00\x00\x00")
    chunk_size = 0x10000

    for pos in range(0, len(decoded), chunk_size):
        chunk = decoded[pos:pos + chunk_size]
        compressed = xmem_compress(chunk)

        if len(compressed) < len(chunk):
            payload = compressed
        else:
            payload = chunk

        output.extend(
            struct.pack(
                "<II",
                len(chunk),
                len(payload),
            )
        )
        output.extend(payload)

    file_zsize = len(output)
    struct.pack_into("<I", output, 0, file_zsize)

    # SIZE_CHECK is the logical decoded resource size, not the total XMEM
    # allocation size. For example:
    #   Chunk SIZE = 65536
    #   SIZE_CHECK = 21992
    output.extend(struct.pack("<I", size_check))

    return bytes(output)

def _patch_u32(buf: bytearray, offset: int, value: int, label: str):
    if value < 0 or value > 0xFFFFFFFF:
        raise ValueError(f"{label} is outside uint32 range: {value}")
    if offset < 0 or offset + 4 > len(buf):
        raise ValueError(f"{label} points outside BXML raw data")
    struct.pack_into("<I", buf, offset, value)


def _patch_database_for_pack(
    database: Path,
    relocations: dict[int, tuple[int, int]],
    resources: list[Resource],
):
    """
    Patch the Database BXML Heap.offset/Heap.size values in-place.

    Database resources are identified from Packages/Package/Asset/Heap.
    We preserve the original BXML raw layout and only patch the numeric
    values referenced by those Heap attributes.

    This deliberately does NOT interpret the BXML numeric pool as a
    separate INFO table. The pool is shared by all numeric BXML attributes
    (Package offsets, Heap offsets/sizes, compression flags, etc.).
    """
    if not relocations:
        return database.read_bytes()

    tool = load_bxml_database_tool(database)
    parsed = tool.decode(str(database))
    raw = bytearray(parsed.raw)

    if not hasattr(tool, "database_assets"):
        raise ValueError(
            "This bxml_database_tool.py is too old. "
            "Use the bundled database tool v2 or newer."
        )

    database_entries = tool.database_assets(parsed)

    if len(database_entries) != len(resources):
        raise ValueError(
            "Database resource count changed unexpectedly: "
            f"database={len(database_entries)}, tool={len(resources)}"
        )

    # Build a map from resource index to the original absolute Heap offset.
    resource_by_index = {
        resource.index: resource
        for resource in resources
    }

    # Build parent/attribute lookup from the BXML node table.
    parent = [None] * len(parsed.nodes)
    for parent_index, node in enumerate(parsed.nodes):
        first = node.level
        count = node.children
        for child_index in range(first, first + count):
            if 0 <= child_index < len(parent):
                parent[child_index] = parent_index

    def pool_u32(attr):
        if not attr.uses_pool:
            raise ValueError("Expected a numeric BXML pool attribute")

        pool_pos = parsed.header.pool_pointer + attr.value
        pool_end = (
            parsed.header.pool_pointer
            + parsed.header.pool_size
        )

        if pool_pos < parsed.header.pool_pointer or pool_pos + 4 > pool_end:
            raise ValueError("BXML numeric pool reference is out of range.")

        return struct.unpack_from("<I", raw, pool_pos)[0]

    def patch_pool_u32(attr, value, label):
        if not attr.uses_pool:
            raise ValueError(
                f"{label}: expected numeric BXML pool attribute"
            )

        pool_pos = parsed.header.pool_pointer + attr.value
        pool_end = (
            parsed.header.pool_pointer
            + parsed.header.pool_size
        )

        if pool_pos < parsed.header.pool_pointer or pool_pos + 4 > pool_end:
            raise ValueError(
                f"{label}: BXML numeric pool reference is out of range."
            )

        _patch_u32(raw, pool_pos, value, label)

    # Locate every Heap node and remember its Package parent and attributes.
    heap_entries = []

    for node_index, node in enumerate(parsed.nodes):
        if parsed.strings[node.name] != "Heap":
            continue

        attrs = parsed.attrs[
            node.attr_index:
            node.attr_index + node.attr_count
        ]

        attr_map = {
            parsed.strings[attr.name]: attr
            for attr in attrs
        }

        offset_attr = attr_map.get("offset")
        size_attr = attr_map.get("size")

        if offset_attr is None or not offset_attr.uses_pool:
            continue

        cur = parent[node_index]
        package_node = None

        while cur is not None:
            parent_node = parsed.nodes[cur]
            if parsed.strings[parent_node.name] == "Package":
                package_node = parent_node
                break
            cur = parent[cur]

        if package_node is None:
            continue

        package_attrs = parsed.attrs[
            package_node.attr_index:
            package_node.attr_index + package_node.attr_count
        ]

        package_offset_attr = None
        for attr in package_attrs:
            if (
                parsed.strings[attr.name] == "offset"
                and attr.uses_pool
            ):
                package_offset_attr = attr
                break

        if package_offset_attr is None:
            package_offset = 0
        else:
            package_offset = pool_u32(package_offset_attr)

        old_heap_offset = pool_u32(offset_attr)
        old_absolute = package_offset + old_heap_offset

        heap_entries.append({
            "node_index": node_index,
            "offset_attr": offset_attr,
            "size_attr": size_attr,
            "package_offset": package_offset,
            "old_absolute": old_absolute,
        })

    # Match by the original absolute Heap offset. This preserves the
    # existing duplicate-name handling and does not depend on names.
    relocation_by_old = {}

    for resource in resources:
        if resource.index not in relocations:
            continue

        relocation_by_old[resource.info.offset] = (
            resource.index,
            relocations[resource.index],
        )

    patched_assets = set()

    for entry in heap_entries:
        match = relocation_by_old.get(entry["old_absolute"])
        if match is None:
            continue

        resource_index, (new_absolute, new_stored_size) = match
        package_offset = entry["package_offset"]
        new_heap_offset = new_absolute - package_offset

        if new_heap_offset < 0:
            raise ValueError(
                f"Resource #{resource_index} new offset "
                f"precedes its Package offset."
            )

        patch_pool_u32(
            entry["offset_attr"],
            new_heap_offset,
            f"Heap offset for resource #{resource_index}",
        )

        size_attr = entry["size_attr"]
        if size_attr is not None and size_attr.uses_pool:
            patch_pool_u32(
                size_attr,
                new_stored_size,
                f"Heap size for resource #{resource_index}",
            )

        patched_assets.add(resource_index)

    missing_assets = set(relocations) - patched_assets
    if missing_assets:
        raise ValueError(
            "Could not locate Database Heap entries for resource(s): "
            + ", ".join(
                f"#{i}" for i in sorted(missing_assets)
            )
        )

    compressed = zlib.compress(bytes(raw))
    header = struct.pack(
        "<9I",
        parsed.header.signature,
        parsed.header.version,
        parsed.header.str_count,
        parsed.header.pool_pointer,
        parsed.header.pool_size,
        parsed.header.attr_count,
        parsed.header.node_count,
        parsed.header.unknown,
        len(compressed),
    )

    return header + compressed



def build_mode0_package(
    package: Path,
    database: Path,
    resources: list[Resource],
    replacements: dict[int, Replacement],
    output_package: Path,
    output_database: Path,
):
    """
    Pack MODE 0 using append-only replacement.

    The .package is treated as storage for compressed resource blocks while
    the external database is the ToC. Existing resource blocks are never
    moved or overwritten. Every replacement is appended to the end of the
    package and its database offset/size are relocated to the new block.

    Unreferenced old blocks remain physically present in the package and can
    be removed later by a dedicated compact/repack operation.
    """
    mode, _, _, _ = detect_mode(package)

    if mode != 0:
        raise ValueError(
            "MODE 0 Pack only.\n\n"
            f"The selected Package is MODE {mode}."
        )

    original = package.read_bytes()
    result = bytearray(original)
    relocations = {}

    for resource in resources:
        replacement = replacements.get(resource.index)
        if replacement is None:
            continue

        decoded = replacement.source_file.read_bytes()

        if is_script_resource(resource):
            decoded = process_script_pack(decoded)

        if not decoded:
            raise ValueError(
                f"Replacement file is empty:\n{replacement.source_file}"
            )

        # Resources without <Compress> are stored raw.
        # Resources with <Compress codec="LZX"> use the existing compressed
        # package-block path.
        if resource.asset is not None and not resource.asset.compressed:
            packed = decoded
        else:
            codec = (resource.asset.codec or "").strip().lower() if resource.asset else ""
            if codec not in ("lzx", ""):
                raise ValueError(
                    f"Unsupported compression codec for "
                    f"{resource.asset.name if resource.asset else resource.index}: "
                    f"{resource.asset.codec!r}"
                )

            # Preserve the original MODE 0 XMEM allocation and logical
            # SIZE_CHECK for replacements that fit inside it. This is
            # especially important for DDS textures: a 21992-byte DDS can
            # occupy a 65536-byte XMEM chunk in the original Package.
            original_size_check, original_decoded_size = (
                read_mode0_size_check_and_decoded_size(
                    package,
                    resource,
                )
            )

            if is_script_resource(resource):
                # Script packing creates its own 4-byte payload-size header.
                # Keep the existing script behavior unchanged.
                packed = pack_mode0_resource(decoded)
            elif len(decoded) <= original_decoded_size:
                padded = decoded + b"\x00" * (
                    original_decoded_size - len(decoded)
                )

                packed = pack_mode0_resource(
                    padded,
                    size_check=original_size_check,
                )
            else:
                # A larger replacement requires additional MODE 0 chunks.
                # In that case the replacement itself defines the logical
                # SIZE_CHECK.
                packed = pack_mode0_resource(
                    decoded,
                    size_check=len(decoded),
                )


        # The old block is deliberately left untouched. The external ToC will
        # point at the new block appended at EOF.
        new_offset = len(result)
        result.extend(packed)

        relocations[resource.index] = (
            new_offset,
            len(packed),
        )

    if not relocations:
        raise ValueError("No replacements are registered.")

    new_database = _patch_database_for_pack(
        database,
        relocations,
        resources,
    )

    output_package.write_bytes(result)
    output_database.write_bytes(new_database)

    return output_package, output_database, relocations





def is_script_resource(resource: "Resource") -> bool:
    """Return True only for resources whose database type is .script."""
    return bool(
        resource.asset
        and str(resource.asset.type).strip().lower().lstrip(".") == "script"
    )

def process_script_extract(resource_data: bytes) -> bytes:
    """
    .script resources contain a 4-byte little-endian payload size followed
    by the actual script payload. Package-level padding has already been
    removed using SIZE_CHECK.
    """
    if len(resource_data) < 4:
        raise ValueError(
            "Script resource is too small to contain its size header."
        )

    payload_size = struct.unpack("<I", resource_data[:4])[0]

    if payload_size > len(resource_data) - 4:
        raise ValueError(
            f"Script payload size {payload_size} exceeds available data "
            f"{len(resource_data) - 4}."
        )

    return resource_data[4:4 + payload_size]


def process_script_pack(script_data: bytes) -> bytes:
    """Prepend a fresh little-endian payload-size field to a .script."""
    if len(script_data) > 0xFFFFFFFF:
        raise ValueError("Script is too large for a 32-bit size field.")

    return struct.pack("<I", len(script_data)) + script_data



def get_resource_file_size(
    package_path: Path,
    entry: InfoEntry,
    mode: int,
    resource: Resource,
) -> int:
    """Return the real extracted file size, excluding package padding."""
    if resource.asset is not None and not resource.asset.compressed:
        if resource.asset.heap_size is None:
            raise ValueError(
                f"Uncompressed resource #{resource.index} has no Heap.size."
            )
        return resource.asset.heap_size

    with package_path.open("rb") as f:
        f.seek(entry.offset)
        raw_size = f.read(4)
        if len(raw_size) != 4:
            raise ValueError(f"Cannot read FILE_ZSIZE at 0x{entry.offset:X}")

        file_zsize = struct.unpack("<I", raw_size)[0]
        if file_zsize < 4:
            raise ValueError(f"Invalid FILE_ZSIZE {file_zsize}")

        next_offset = entry.offset + file_zsize
        if next_offset > package_path.stat().st_size:
            raise ValueError("Compressed resource exceeds package.")

        current = entry.offset + 4
        decoded_size = 0

        while current < next_offset:
            f.seek(current)
            if mode == 0:
                header = f.read(8)
                if len(header) != 8:
                    raise ValueError(f"Truncated chunk at 0x{current:X}")
                size, zsize = struct.unpack("<II", header)
                data_offset = current + 8
            else:
                header = f.read(4)
                if len(header) != 4:
                    raise ValueError(f"Truncated chunk at 0x{current:X}")
                size_be, zsize_be = struct.unpack("<HH", header)
                size = int.from_bytes(struct.pack("<H", size_be), "big") + 1
                zsize = int.from_bytes(struct.pack("<H", zsize_be), "big") + 1
                data_offset = current + 4

            if size <= 0 or zsize <= 0 or data_offset + zsize > next_offset:
                raise ValueError(f"Invalid chunk at 0x{current:X}")

            decoded_size += size
            current = data_offset + zsize

        if mode == 0:
            size_check_raw = f.read(4)
            if len(size_check_raw) != 4:
                raise ValueError("Missing SIZE_CHECK after resource.")
            decoded_size = struct.unpack("<I", size_check_raw)[0]

    if is_script_resource(resource):
        if decoded_size < 4:
            raise ValueError("Script resource is too small.")
        decoded_size -= 4

    return decoded_size


def unpack_resource(
    package_path: Path,
    entry: InfoEntry,
    mode: int,
    resource: Resource,
):
    """
    The output is the decoded resource, NOT the encrypted/compressed package
    block and NOT the FILE_ZSIZE header.
    """
    # Resources without <Compress> are stored raw in the Package.
    # Do NOT interpret the first four bytes as FILE_ZSIZE in this case.
    # Database Heap.size is the authoritative raw resource size.
    if resource.asset is not None and not resource.asset.compressed:
        if resource.asset.heap_size is None:
            raise ValueError(
                f"Uncompressed resource #{resource.index} has no Heap.size."
            )

        raw_size = resource.asset.heap_size
        package_size = package_path.stat().st_size

        if entry.offset < 0 or entry.offset + raw_size > package_size:
            raise ValueError(
                "Uncompressed resource exceeds package: "
                f"0x{entry.offset:X}+0x{raw_size:X}"
            )

        with package_path.open("rb") as f:
            f.seek(entry.offset)
            data = f.read(raw_size)

        if len(data) != raw_size:
            raise ValueError(
                f"Could not read uncompressed resource: "
                f"{len(data)} != {raw_size}"
            )

        return data, raw_size

    with package_path.open("rb") as f:
        f.seek(entry.offset)

        raw_size = f.read(4)

        if len(raw_size) != 4:
            raise ValueError(
                f"Cannot read FILE_ZSIZE at 0x{entry.offset:X}"
            )

        file_zsize = struct.unpack("<I", raw_size)[0]

        if file_zsize < 4:
            raise ValueError(
                f"Invalid FILE_ZSIZE {file_zsize} at "
                f"0x{entry.offset:X}"
            )

        next_offset = entry.offset + file_zsize

        file_size = package_path.stat().st_size

        if next_offset > file_size:
            raise ValueError(
                f"Compressed resource exceeds package: "
                f"0x{entry.offset:X}+0x{file_zsize:X}"
            )

        output = bytearray()

        current = entry.offset + 4

        while current < next_offset:
            f.seek(current)

            if mode == 0:
                header = f.read(8)

                if len(header) != 8:
                    raise ValueError(
                        f"Truncated chunk at 0x{current:X}"
                    )

                size, zsize = struct.unpack("<II", header)
                data_offset = current + 8

            else:
                header = f.read(4)

                if len(header) != 4:
                    raise ValueError(
                        f"Truncated chunk at 0x{current:X}"
                    )

                # QuickBMS reads short in little-endian, then reverseshort.
                # Therefore the actual value is big-endian + 1.
                size_be, zsize_be = struct.unpack("<HH", header)

                size = (
                    int.from_bytes(
                        struct.pack("<H", size_be),
                        "big",
                    )
                    + 1
                )

                zsize = (
                    int.from_bytes(
                        struct.pack("<H", zsize_be),
                        "big",
                    )
                    + 1
                )

                data_offset = current + 4

            if size <= 0 or zsize <= 0:
                raise ValueError(
                    f"Invalid chunk sizes at 0x{current:X}: "
                    f"SIZE={size}, ZSIZE={zsize}"
                )

            if data_offset + zsize > next_offset:
                raise ValueError(
                    f"Chunk exceeds compressed resource at "
                    f"0x{current:X}: ZSIZE={zsize}"
                )

            f.seek(data_offset)
            compressed = f.read(zsize)

            if len(compressed) != zsize:
                raise ValueError(
                    f"Short compressed chunk at 0x{data_offset:X}"
                )

            if zsize == size:
                decoded = compressed
            elif mode == 1:
                decoded = deflate_decode(
                    compressed,
                    size,
                )
            else:
                decoded = xmem_decompress(
                    compressed,
                    size,
                )

            if len(decoded) != size:
                raise ValueError(
                    f"Decoded chunk size mismatch: "
                    f"{len(decoded)} != {size}"
                )

            output.extend(decoded)

            current = data_offset + zsize

        # MODE 0 stores the real decoded resource size after the chunk
        # stream. XMemDecompress works with block-sized output buffers, so
        # the decoded chunks can contain zero padding after the real resource.
        # SIZE_CHECK is therefore the authoritative decoded resource size.
        if mode == 0:
            if current + 4 > file_size:
                raise ValueError(
                    "Missing SIZE_CHECK after resource"
                )

            f.seek(current)
            size_check_raw = f.read(4)

            if len(size_check_raw) != 4:
                raise ValueError(
                    "Could not read SIZE_CHECK after resource"
                )

            size_check = struct.unpack("<I", size_check_raw)[0]

            if size_check > len(output):
                raise ValueError(
                    f"SIZE_CHECK {size_check} exceeds decoded size "
                    f"{len(output)}"
                )
            
            if resource.asset.type == "script":
                output = output[:size_check] #Remove padding bytes from clear data

        return bytes(output), file_zsize


# ---------------------------------------------------------------------------
# Resource names / extraction
# ---------------------------------------------------------------------------

@dataclass
class Resource:
    index: int
    info: InfoEntry
    asset: Optional[AssetInfo]


def safe_name(name: str, fallback: str):
    if not name:
        return fallback

    value = re.sub(
        r'[<>:"/\\|?*\x00-\x1F]',
        "_",
        name,
    )

    value = value.rstrip(" .")

    return value or fallback


def resource_file_name(resource: Resource):
    if resource.asset:
        base = safe_name(
            resource.asset.name,
            f"resource_{resource.index:04d}",
        )

        typ = safe_name(
            resource.asset.type,
            "bin",
        )

        if typ.lower().lstrip(".") == "script":
            typ = "lua"

        elif typ.lower().lstrip(".") == "perfdat":
            typ = "bxml"

        elif typ.lower().lstrip(".") == "uicmpnt":
            typ = "bxml"

        elif typ.lower().lstrip(".") == "material":
            typ = "bxml"

        elif typ.lower().lstrip(".") == "adv":
            typ = "bxml"

        return f"{base}.{typ}"

    return f"resource_{resource.index:04d}.bin"


def resource_folder_name(resource: Resource):
    if resource.asset and resource.asset.name:
        return safe_name(
            resource.asset.name,
            f"resource_{resource.index:04d}",
        )

    return f"resource_{resource.index:04d}"


def extract_resource(
    package: Path,
    database: Path,
    resource: Resource,
    destination: Path,
    mode: int,
):
    decoded, file_zsize = unpack_resource(
        package,
        resource.info,
        mode,
        resource,
    )

    folder_name = resource_folder_name(resource)
    folder = destination / folder_name

    # Keep extraction lossless when duplicate resource names occur.
    if folder.exists():
        folder = destination / (
            f"{folder_name}_{resource.index:04d}"
        )

    folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    filename = resource_file_name(resource)

    # Hide the internal .script payload-size header from extracted files.
    if filename.lower().endswith(".lua"):
        decoded = process_script_extract(decoded)

    resource_path = folder / filename
    resource_path.write_bytes(decoded)

    sha256 = hashlib.sha256(decoded).hexdigest()

    asset = resource.asset

    metadata = {
        "format": "MXvsATV Reflex Package Resource",
        "version": 1,

        "package": package.name,
        "database": database.name,

        "resource_index": resource.index,

        # Exact information required by the future packer.
        "package_offset": resource.info.offset,
        "package_file_zsize": file_zsize,
        "package_mode": mode,
        "database_compressed": (
            asset.compressed if asset else None
        ),

        # Database identity.
        "database_name": asset.name if asset else "",
        "database_type": asset.type if asset else "",
        "database_package_offset": (
            asset.package_offset if asset else None
        ),
        "database_heap_offset": (
            asset.heap_offset if asset else None
        ),
        "database_heap_size": (
            asset.heap_size if asset else None
        ),

        "decoded_size": len(decoded),
        "resource_file": filename,
        "sha256": sha256,
    }

    (folder / "resource.json").write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return folder, resource_path, metadata


# ---------------------------------------------------------------------------
# Replace support
# ---------------------------------------------------------------------------

@dataclass
class Replacement:
    resource_index: int
    source_file: Path
    resource_json: Optional[Path]


def load_resource_metadata(folder: Path):
    metadata_path = folder / "resource.json"

    if not metadata_path.exists():
        raise ValueError(
            "resource.json was not found in the selected resource folder.\n\n"
            "Select the folder created by Extract."
        )

    try:
        metadata = json.loads(
            metadata_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError(
            f"Could not read resource.json:\n{exc}"
        ) from exc

    required = [
        "resource_index",
        "package_offset",
        "package_mode",
        "database_name",
        "database_type",
    ]

    missing = [
        key for key in required
        if key not in metadata
    ]

    if missing:
        raise ValueError(
            "resource.json is missing required fields:\n"
            + ", ".join(missing)
        )

    return metadata, metadata_path


def find_resource_file(folder: Path, metadata: dict):
    resource_file = metadata.get("resource_file")

    if resource_file:
        candidate = folder / resource_file
        if candidate.is_file():
            return candidate

    # Fallback: exactly one file other than resource.json.
    candidates = [
        p for p in folder.iterdir()
        if p.is_file()
        and p.name.lower() != "resource.json"
    ]

    if len(candidates) == 1:
        return candidates[0]

    if not candidates:
        raise ValueError(
            "No resource file was found in the selected folder."
        )

    raise ValueError(
        "More than one resource file was found and resource.json "
        "does not identify which one to replace."
    )


def validate_replacement(
    package: Path,
    database: Path,
    resource: Resource,
    folder: Path,
):
    """
    Validate a replacement folder without changing the original Package.

    Checks the resource.json identity and records the selected file.
    """
    metadata, metadata_path = load_resource_metadata(folder)
    source_file = find_resource_file(folder, metadata)

    if not source_file.is_file():
        raise ValueError(
            f"Replacement file does not exist:\n{source_file}"
        )

    try:
        json_index = int(metadata["resource_index"])
        json_offset = int(metadata["package_offset"])
        json_mode = int(metadata["package_mode"])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "resource.json contains invalid resource identity values."
        ) from exc

    if json_index != resource.index:
        raise ValueError(
            "The selected folder belongs to a different resource.\n\n"
            f"Package resource: #{resource.index}\n"
            f"resource.json:    #{json_index}"
        )

    if json_offset != resource.info.offset:
        raise ValueError(
            "The selected folder belongs to a different Package offset.\n\n"
            f"Package offset:   0x{resource.info.offset:X}\n"
            f"resource.json:    0x{json_offset:X}"
        )

    # Verify database identity when available.
    if resource.asset:
        json_name = str(metadata.get("database_name", ""))
        json_type = str(metadata.get("database_type", ""))

        if json_name != resource.asset.name:
            raise ValueError(
                "Database resource name does not match.\n\n"
                f"Package: {resource.asset.name}\n"
                f"Folder:  {json_name}"
            )

        if json_type != resource.asset.type:
            raise ValueError(
                "Database resource type does not match.\n\n"
                f"Package: {resource.asset.type}\n"
                f"Folder:  {json_type}"
            )

    if json_mode not in (0, 1):
        raise ValueError(
            f"Unsupported package mode in resource.json: {json_mode}"
        )

    return Replacement(
        resource_index=resource.index,
        source_file=source_file,
        resource_json=metadata_path,
    )


# ---------------------------------------------------------------------------
# Pack support
# ---------------------------------------------------------------------------

@dataclass
class PackedResource:
    resource: Resource
    decoded_data: bytes
    package_block: bytes


def deflate_encode(data: bytes) -> bytes:
    """
    Produce a raw DEFLATE stream.

    This is used for MODE 1. The chunk framing is intentionally kept separate
    from compression so it can be adjusted after comparison with the BMS
    encoder.
    """
    compressor = zlib.compressobj(
        level=9,
        method=zlib.DEFLATED,
        wbits=-15,
    )

    return (
        compressor.compress(data)
        + compressor.flush()
    )


def pack_mode1_chunks(decoded: bytes) -> bytes:
    """
    Build MODE 1 chunk data.

    The BMS decoder reads two big-endian 16-bit values and adds one:
        SIZE  = BE16 + 1
        ZSIZE = BE16 + 1

    Therefore a chunk can encode up to 65536 bytes.

    For each chunk:
        uint16_be(SIZE - 1)
        uint16_be(ZSIZE - 1)
        compressed bytes

    If compression does not make the data smaller, store it raw.
    """
    output = bytearray()
    pos = 0

    while pos < len(decoded):
        chunk = decoded[pos:pos + 0x10000]
        compressed = deflate_encode(chunk)

        if len(compressed) >= len(chunk):
            payload = chunk
        else:
            payload = compressed

        size = len(chunk)
        zsize = len(payload)

        if size < 1 or size > 0x10000:
            raise ValueError("MODE 1 chunk SIZE is out of range")

        if zsize < 1 or zsize > 0x10000:
            raise ValueError("MODE 1 chunk ZSIZE is out of range")

        output.extend(
            struct.pack(
                ">HH",
                size - 1,
                zsize - 1,
            )
        )
        output.extend(payload)

        pos += size

    return bytes(output)


def pack_mode1_resource(decoded: bytes) -> bytes:
    """
    Build a complete MODE 1 resource block.

    FILE_ZSIZE is the total number of bytes occupied by:
        FILE_ZSIZE itself + chunk stream
    """
    chunks = pack_mode1_chunks(decoded)
    file_zsize = 4 + len(chunks)

    return (
        struct.pack("<I", file_zsize)
        + chunks
    )


def collect_replacement_data(
    package: Path,
    resource: Resource,
    replacement: Replacement,
):
    """
    Read the replacement resource exactly as supplied by the modder.

    v17 does not decode the replacement because Replace already represents
    the decoded resource file.
    """
    data = replacement.source_file.read_bytes()

    if not data:
        raise ValueError(
            f"Replacement file is empty:\n"
            f"{replacement.source_file}"
        )

    return data


def build_mode1_package(
    package: Path,
    resources: list[Resource],
    replacements: dict[int, Replacement],
    output: Path,
):

    mode, _, _, _ = detect_mode(package)

    if mode != 1:
        raise ValueError("This first pack implementation not supported.")

    with package.open("rb") as source, output.open("wb") as target:
        for resource in resources:
            replacement = replacements.get(resource.index)

            if replacement is None:
                source.seek(resource.info.offset)
                original = source.read(resource.info.stored_size)

                if len(original) != resource.info.stored_size:
                    raise IOError(
                        f"Could not read original resource "
                        f"#{resource.index}"
                    )

                target.write(original)
                continue

            decoded = collect_replacement_data(
                package,
                resource,
                replacement,
            )

            if is_script_resource(resource):
                decoded = process_script_pack(decoded)

            packed = pack_mode1_resource(decoded)
            target.write(packed)

    return output


# ---------------------------------------------------------------------------
# Package defragmentation
# ---------------------------------------------------------------------------

def read_package_block(package: Path, resource: Resource, mode: int) -> bytes:
    """Read the complete physical block referenced by a Database resource."""
    offset = resource.info.offset
    package_size = package.stat().st_size
    if offset < 0 or offset >= package_size:
        raise ValueError(f"Resource #{resource.index} points outside the Package: 0x{offset:X}")

    if resource.asset is not None and not resource.asset.compressed:
        if resource.asset.heap_size is None:
            raise ValueError(f"Uncompressed resource #{resource.index} has no Heap.size.")
        block_size = int(resource.asset.heap_size)
        if block_size <= 0:
            raise ValueError(f"Invalid Heap.size for resource #{resource.index}: {block_size}")
    else:
        with package.open("rb") as f:
            f.seek(offset)
            raw = f.read(4)
        if len(raw) != 4:
            raise ValueError(f"Cannot read FILE_ZSIZE for resource #{resource.index}")
        file_zsize = struct.unpack("<I", raw)[0]
        if file_zsize < 4:
            raise ValueError(f"Invalid FILE_ZSIZE {file_zsize} for resource #{resource.index}")
        block_size = file_zsize + 4 if mode == 0 else file_zsize

    if offset + block_size > package_size:
        raise ValueError(f"Resource #{resource.index} exceeds Package.")

    with package.open("rb") as f:
        f.seek(offset)
        block = f.read(block_size)
    if len(block) != block_size:
        raise IOError(f"Could not read complete block for resource #{resource.index}")
    return block


def _database_info_records(parsed):
    raw = parsed.raw
    info_off = parsed.header.pool_pointer
    info_end = info_off + parsed.header.pool_size
    records = {}
    pos = info_off
    base_offset = 0
    index = 0
    while pos + 12 <= info_end:
        record_pos = pos
        relative_offset, size, _zero = struct.unpack_from("<III", raw, pos)
        pos += 12
        if size == 0:
            base_offset = relative_offset
            continue
        if pos + 8 > info_end:
            raise ValueError("Database INFO region is truncated.")
        pos += 8
        records[index] = {
            "record_pos": record_pos,
            "base_offset": base_offset,
            "absolute_offset": relative_offset + base_offset,
            "stored_size": size,
        }
        index += 1
    if pos != info_end and any(raw[pos:info_end]):
        raise ValueError("Database INFO region has unexpected non-zero data.")
    return records


def _patch_database_for_defragment(database: Path, relocations, resources):
    """Patch INFO, Package.offset and Heap.offset/size for a compacted Package.

    Database stores Heap.offset relative to its containing Package.offset.
    During defragmentation Package.offset values move as well, so Heap.offset
    cannot simply be calculated against the original Package offset.

    The INFO table uses the same base-offset concept: records with SIZE == 0
    establish BASE_OFF for the following records. Those base records must be
    relocated together with their corresponding Database Package offsets.
    """
    tool = load_bxml_database_tool(database)
    parsed = tool.decode(str(database))
    raw = bytearray(parsed.raw)
    entries = tool.database_assets(parsed)

    if len(entries) != len(resources):
        raise ValueError(
            "Database resource count changed unexpectedly: "
            f"database={len(entries)}, tool={len(resources)}"
        )

    h = parsed.header
    pool_start = h.pool_pointer
    pool_end = h.pool_pointer + h.pool_size

    def pool_pos(attr):
        if not attr.uses_pool:
            raise ValueError("Expected numeric BXML pool attribute.")
        pos = pool_start + attr.value
        if pos < pool_start or pos + 4 > pool_end:
            raise ValueError("BXML numeric pool reference is out of range.")
        return pos

    def pool_u32(attr):
        return struct.unpack_from("<I", raw, pool_pos(attr))[0]

    def patch_pool_u32(attr, value, label):
        _patch_u32(raw, pool_pos(attr), value, label)

    # ------------------------------------------------------------------
    # 1. Determine the new Package.offset for every original Package base.
    # ------------------------------------------------------------------
    # A Database Asset already exposes the Package.offset used to calculate
    # its absolute Heap position. Group resources by that original base and
    # place the new Package base at the first compacted block belonging to it.
    package_new_offsets = {}
    for resource in resources:
        relocation = relocations.get(resource.index)
        if relocation is None or resource.asset is None:
            continue

        old_package_offset = int(resource.asset.package_offset)
        new_absolute, _new_size = relocation

        current = package_new_offsets.get(old_package_offset)
        if current is None or new_absolute < current:
            package_new_offsets[old_package_offset] = new_absolute

    if not package_new_offsets:
        raise ValueError("Could not determine Database Package offsets.")

    # ------------------------------------------------------------------
    # 2. Locate Package nodes and patch their offset attributes.
    # ------------------------------------------------------------------
    package_offset_attrs = {}

    for node_index, node in enumerate(parsed.nodes):
        if parsed.strings[node.name] != "Package":
            continue

        attrs = parsed.attrs[node.attr_index:node.attr_index + node.attr_count]
        offset_attr = next(
            (
                attr for attr in attrs
                if parsed.strings[attr.name] == "offset"
                and attr.uses_pool
            ),
            None,
        )

        if offset_attr is None:
            continue

        old_offset = pool_u32(offset_attr)
        new_offset = package_new_offsets.get(old_offset)

        if new_offset is None:
            # Package nodes without resources represented in the current ToC
            # are left untouched. This is safer than inventing a new base.
            continue

        patch_pool_u32(
            offset_attr,
            new_offset,
            f"Package offset for 0x{old_offset:X}",
        )
        package_offset_attrs[node_index] = (old_offset, new_offset)

    # ------------------------------------------------------------------
    # 3. Locate Heap nodes and patch Heap.offset/size using the NEW package
    #    base, not the original one.
    # ------------------------------------------------------------------
    parent = [None] * len(parsed.nodes)
    for parent_index, node in enumerate(parsed.nodes):
        for child_index in range(node.level, node.level + node.children):
            if 0 <= child_index < len(parent):
                parent[child_index] = parent_index

    relocation_by_old = {
        resource.info.offset: (resource.index, relocations[resource.index])
        for resource in resources
        if resource.index in relocations
    }

    patched_assets = set()

    for node_index, node in enumerate(parsed.nodes):
        if parsed.strings[node.name] != "Heap":
            continue

        attrs = parsed.attrs[node.attr_index:node.attr_index + node.attr_count]
        attr_map = {parsed.strings[attr.name]: attr for attr in attrs}
        offset_attr = attr_map.get("offset")
        size_attr = attr_map.get("size")

        if offset_attr is None or not offset_attr.uses_pool:
            continue

        # Find the containing Package node.
        cur = parent[node_index]
        package_node_index = None
        while cur is not None:
            if parsed.strings[parsed.nodes[cur].name] == "Package":
                package_node_index = cur
                break
            cur = parent[cur]

        if package_node_index is None:
            continue

        package_pair = package_offset_attrs.get(package_node_index)
        if package_pair is None:
            continue

        old_package_offset, new_package_offset = package_pair
        old_heap_offset = pool_u32(offset_attr)
        old_absolute = old_package_offset + old_heap_offset

        match = relocation_by_old.get(old_absolute)
        if match is None:
            continue

        resource_index, (new_absolute, new_stored_size) = match
        new_heap_offset = new_absolute - new_package_offset

        if new_heap_offset < 0:
            raise ValueError(
                f"New Heap offset for resource #{resource_index} is negative."
            )

        patch_pool_u32(
            offset_attr,
            new_heap_offset,
            f"Heap offset for resource #{resource_index}",
        )

        if size_attr is not None and size_attr.uses_pool:
            patch_pool_u32(
                size_attr,
                new_stored_size,
                f"Heap size for resource #{resource_index}",
            )

        patched_assets.add(resource_index)

    missing = set(relocations) - patched_assets
    if missing:
        raise ValueError(
            "Could not locate Database Heap entries for resource(s): "
            + ", ".join(f"#{i}" for i in sorted(missing))
        )

    # ------------------------------------------------------------------
    # 4. Patch the binary INFO table.
    #
    # SIZE == 0 records establish BASE_OFF. They correspond to .package
    # offsets. Every normal INFO record then stores OFFSET relative to that
    # base. Relocate both parts so INFO and Packages/Package/Heap remain
    # internally consistent.
    # ------------------------------------------------------------------
    info_off = h.pool_pointer
    info_end = info_off + h.pool_size
    pos = info_off
    current_old_base = 0
    current_new_base = 0
    resource_index = 0

    while pos + 12 <= info_end:
        record_pos = pos
        relative_offset, size, _zero = struct.unpack_from(
            "<III",
            raw,
            pos,
        )
        pos += 12

        if size == 0:
            current_old_base = relative_offset
            current_new_base = package_new_offsets.get(
                current_old_base,
                current_old_base,
            )
            _patch_u32(
                raw,
                record_pos,
                current_new_base,
                f"INFO base offset 0x{current_old_base:X}",
            )
            continue

        if pos + 8 > info_end:
            raise ValueError("Database INFO region is truncated.")

        pos += 8

        if resource_index >= len(resources):
            raise ValueError("Database INFO/resource count mismatch.")

        resource = resources[resource_index]
        relocation = relocations.get(resource.index)
        if relocation is None:
            raise ValueError(
                f"Missing relocation for INFO resource #{resource.index}"
            )

        new_absolute, new_stored_size = relocation
        new_relative = new_absolute - current_new_base

        if new_relative < 0:
            raise ValueError(
                f"New INFO offset for resource #{resource.index} is negative."
            )

        _patch_u32(
            raw,
            record_pos,
            new_relative,
            f"INFO offset for resource #{resource.index}",
        )
        _patch_u32(
            raw,
            record_pos + 4,
            new_stored_size,
            f"INFO size for resource #{resource.index}",
        )

        resource_index += 1

    if pos != info_end and any(raw[pos:info_end]):
        raise ValueError("Database INFO region has unexpected non-zero trailing data.")

    if resource_index != len(resources):
        raise ValueError(
            "Database INFO/resource count mismatch: "
            f"INFO={resource_index}, resources={len(resources)}"
        )

    compressed = zlib.compress(bytes(raw))
    header = struct.pack(
        "<9I",
        parsed.header.signature,
        parsed.header.version,
        parsed.header.str_count,
        parsed.header.pool_pointer,
        parsed.header.pool_size,
        parsed.header.attr_count,
        parsed.header.node_count,
        parsed.header.unknown,
        len(compressed),
    )
    return header + compressed

def build_defragmented_package(package, database, resources, mode, output_package, output_database, progress_callback=None):
    if progress_callback is None:
        progress_callback = lambda value: None
    if mode not in (0, 1):
        raise ValueError(f"Unsupported Package mode: {mode}")
    if not resources:
        raise ValueError("Database does not contain any resources.")

    unique = {}
    total = max(len(resources), 1)
    for pos, resource in enumerate(resources, 1):
        if resource.info.offset not in unique:
            unique[resource.info.offset] = read_package_block(package, resource, mode)
        progress_callback(int(pos * 40 / total))

    ordered = sorted(unique.items(), key=lambda item: item[0])
    relocations = {}
    current_offset = 12
    for old_offset, block in ordered:
        for resource in resources:
            if resource.info.offset == old_offset:
                relocations[resource.index] = (current_offset, len(block))
        current_offset += len(block)

    with package.open("rb") as source, output_package.open("wb") as target:
        header = source.read(12)
        if len(header) != 12:
            raise ValueError("Package header is truncated.")
        target.write(header)
        for _, block in ordered:
            target.write(block)
    progress_callback(55)

    output_database.write_bytes(_patch_database_for_defragment(database, relocations, resources))
    progress_callback(60)
    return output_package, output_database, relocations


def verify_defragmented_package(original_package, original_database, new_package, new_database, resources, mode, progress_callback=None):
    if progress_callback is None:
        progress_callback = lambda value: None
    if detect_mode(new_package)[0] != mode:
        raise ValueError("Verification failed: Package mode changed unexpectedly.")

    tool = load_bxml_database_tool(new_database)
    parsed = tool.decode(str(new_database))
    entries = tool.database_assets(parsed)
    if len(entries) != len(resources):
        raise ValueError(f"Verification failed: resource count changed ({len(resources)} -> {len(entries)}).")
    new_by_index = {int(e["index"]): e for e in entries}
    if set(new_by_index) != {r.index for r in resources}:
        raise ValueError("Verification failed: Database resource indices changed.")

    cache = {}
    total = max(len(resources), 1)
    for pos, resource in enumerate(resources, 1):
        if resource.info.offset not in cache:
            cache[resource.info.offset] = read_package_block(original_package, resource, mode)
        expected = cache[resource.info.offset]
        entry = new_by_index[resource.index]
        new_offset = int(entry["absolute_offset"])
        if new_offset < 12 or new_offset + len(expected) > new_package.stat().st_size:
            raise ValueError(f"Verification failed for resource #{resource.index}: block is outside the new Package.")
        with new_package.open("rb") as f:
            f.seek(new_offset)
            actual = f.read(len(expected))
        if actual != expected:
            raise ValueError(f"Verification failed for resource #{resource.index}: package block data differs.")
        heap_size = entry.get("heap_size")
        if heap_size is not None and int(heap_size) != len(expected):
            raise ValueError(f"Verification failed for resource #{resource.index}: database Heap.size is incorrect.")
        progress_callback(int(pos * 95 / total))

    expected_size = 12 + sum(len(block) for block in cache.values())
    if new_package.stat().st_size != expected_size:
        raise ValueError("Verification failed: unexpected trailing data in the new .package.")

    progress_callback(100)


class DefragmentDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Optimization", size=(430, 145), style=(wx.DEFAULT_DIALOG_STYLE & ~wx.CLOSE_BOX) | wx.RESIZE_BORDER)
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)
        self.status = wx.StaticText(panel, label="Optimization...")
        self.progress = wx.Gauge(panel, range=100, style=wx.GA_HORIZONTAL)
        root.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 14)
        root.Add(self.progress, 0, wx.EXPAND | wx.ALL, 14)
        panel.SetSizer(root)
        self.CentreOnParent()

    def set_status(self, text):
        self.status.SetLabel(text)

    def set_progress(self, value):
        self.progress.SetValue(max(0, min(100, int(value))))

# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(
            None,
            title=APP_NAME,
            size=(1120, 680),
        )

        self.package: Optional[Path] = None
        self.database: Optional[Path] = None
        self.mode: Optional[int] = None
        self.resources: list[Resource] = []

        self._build_ui()
        self.Centre()

    def handle_dropped_files(self, file):
        self.open_package(self, package_path=file)

    def _build_ui(self):
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        toolbar = wx.ToolBar(
            panel,
            style=wx.TB_HORIZONTAL | wx.TB_TEXT | wx.TB_FLAT | wx.TB_NODIVIDER,
        )

        self.tool_open = toolbar.AddTool(
            wx.ID_OPEN,
            "Open...",
            wx.ArtProvider.GetBitmap(
                wx.ART_FILE_OPEN,
                wx.ART_TOOLBAR,
            ),
        )
        toolbar.AddSeparator()
        self.tool_extract = toolbar.AddTool(
            wx.ID_ANY,
            "Extract",
            wx.ArtProvider.GetBitmap(
                wx.ART_GO_UP,
                wx.ART_TOOLBAR,
            ),
        )

        self.tool_pack = toolbar.AddTool(
            wx.ID_ANY,
            "Replace",
            wx.ArtProvider.GetBitmap(
                wx.ART_GO_DOWN,
                wx.ART_TOOLBAR,
            ),
        )

        toolbar.Realize()
        root.Add(
            toolbar,
            0,
            wx.EXPAND,
        )

        menu_bar = wx.MenuBar()

        file_menu = wx.Menu()
        file_open = file_menu.Append(wx.ID_OPEN, "Open...\tCtrl+O")
        file_menu.AppendSeparator()
        file_extract = file_menu.Append(wx.ID_ANY, "Extract\tCtrl+E")
        file_pack = file_menu.Append(wx.ID_ANY, "Replace\tCtrl+R")
        file_menu.AppendSeparator()
        file_exit = file_menu.Append(wx.ID_EXIT, "Exit")
        menu_bar.Append(file_menu, "File")

        tools_menu = wx.Menu()
        file_defragment = tools_menu.Append(wx.ID_ANY, "Package Optimization")
        menu_bar.Append(tools_menu, "Tools")

        help_menu = wx.Menu()
        help_about = help_menu.Append(wx.ID_ABOUT, "About")
        menu_bar.Append(help_menu, "Help")

        self.SetMenuBar(menu_bar)

        self.Bind(wx.EVT_MENU, self.open_package, file_open)
        self.Bind(wx.EVT_MENU, self.extract_selected, file_extract)
        self.Bind(wx.EVT_MENU, self.pack, file_pack)
        self.Bind(wx.EVT_MENU, self.defragment, file_defragment)
        self.Bind(wx.EVT_MENU, self.on_exit, file_exit)
        self.Bind(wx.EVT_MENU, self.show_about, help_about)


        self.list = dv.DataViewListCtrl(
            panel,
            style=(
                dv.DV_ROW_LINES
                | dv.DV_VERT_RULES
                | dv.DV_SINGLE
            ),
        )

        self.list.AppendTextColumn(
            "#",
            width=45,
            mode=dv.DATAVIEW_CELL_INERT,
        )

        self.list.AppendTextColumn(
            "Name",
            width=260,
            mode=dv.DATAVIEW_CELL_INERT,
        )

        self.list.AppendTextColumn(
            "Type",
            width=70,
            mode=dv.DATAVIEW_CELL_INERT,
        )

        self.list.AppendTextColumn(
            "Package",
            width=260,
            mode=dv.DATAVIEW_CELL_INERT,
        )

        self.list.AppendTextColumn(
            "Offset",
            width=120,
            mode=dv.DATAVIEW_CELL_INERT,
        )

        root.Add(
            self.list,
            1,
            wx.EXPAND | wx.LEFT | wx.RIGHT,
            8,
        )

        panel.SetSizer(root)

        drop_target = PackageDropTarget(self)
        self.list.SetDropTarget(drop_target)

        self.statusbar = self.CreateStatusBar(2)
        self.SetStatusText("No package", 0)
        self.SetStatusText("0 resources", 1)
        self.statusbar.SetStatusWidths([-1, 220])
        self.statusbar.WindowStyle ^= wx.STB_SHOW_TIPS

        self.Bind(
            wx.EVT_TOOL,
            self.open_package,
            id=self.tool_open.Id,
        )

        self.Bind(
            wx.EVT_TOOL,
            self.extract_selected,
            id=self.tool_extract.Id,
        )

        self.Bind(
            wx.EVT_TOOL,
            self.pack,
            id=self.tool_pack.Id,
        )

        self.list.Bind(
            dv.EVT_DATAVIEW_ITEM_CONTEXT_MENU,
            self.on_context_menu,
        )

        self.list.Bind(
            dv.EVT_DATAVIEW_ITEM_ACTIVATED,
            self.extract_selected,
        )

    def open_package(self, event=None, package_path=None):
        wildcard = (
            "MX vs ATV Reflex Package (*.package)|*.package|"
            "All files (*.*)|*.*"
        )

        if package_path is not None:
            package = Path(package_path)
        else:
            with wx.FileDialog(self,"Open .package",wildcard=wildcard,style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return

                package = Path(dialog.GetPath())

        database = package.with_suffix(".database")

        if not database.exists():
            wx.MessageBox(
                "The matching database was not found:\n\n"
                f"{database}",
                "Database not found",
                wx.OK | wx.ICON_ERROR,
            )
            return

        try:
            parsed, info_off, info_size, assets_by_offset = (
                parse_database(database)
            )

            database_entries = load_bxml_database_tool(
                database
            ).database_assets(parsed)

            mode, test0, test1, test2 = detect_mode(
                package,
            )

            resources = []

            for entry in database_entries:
                info = InfoEntry(
                    index=entry["index"],
                    offset=entry["absolute_offset"],
                    stored_size=entry["heap_size"],
                    base_offset=entry["package_offset"],
                )

                assets = assets_by_offset.get(
                    entry["absolute_offset"],
                    [],
                )

                asset = assets[0] if assets else None

                resources.append(
                    Resource(
                        index=entry["index"],
                        info=info,
                        asset=asset,
                    )
                )

        except Exception as exc:
            wx.MessageBox(
                str(exc),
                "Package read failed",
                wx.OK | wx.ICON_ERROR,
            )
            return

        self.package = package
        self.database = database
        self.mode = mode
        self.resources = resources

        self.list.DeleteAllItems()

        matched = 0

        for resource in resources:
            if resource.asset:
                matched += 1

                name = resource.asset.name or (
                    f"resource_{resource.index:04d}"
                )

                typ = resource.asset.type or "bin"

            else:
                name = f"resource_{resource.index:04d}"
                typ = "bin"

            package_name = (
                resource.asset.package_name
                if resource.asset
                else ""
            )

            self.list.AppendItem([
                str(resource.index),
                name,
                typ,
                package_name,
                f"0x{resource.info.offset:X}",
            ])


        self.SetStatusText(
            f"{package.name}",
            0,
        )

        self.SetStatusText(
            f"{len(resources)} resources | "
            f"{matched} database matches",
            1,
        )

        self.statusbar.SetToolTip(f"{package.resolve()}")

    def selected_resource(self):
        row = self.list.GetSelectedRow()

        if row < 0 or row >= len(self.resources):
            wx.MessageBox(
                "Select a resource first.",
                "Extract",
                wx.OK | wx.ICON_INFORMATION,
            )
            return None

        return self.resources[row]

    def extract_selected(self, event=None):
        resource = self.selected_resource()

        if resource is None:
            return

        with wx.DirDialog(
            self,
            "Select extraction directory",
            style=(
                wx.DD_DEFAULT_STYLE
                | wx.DD_DIR_MUST_EXIST
            ),
        ) as dialog:

            if dialog.ShowModal() != wx.ID_OK:
                return

            destination = Path(dialog.GetPath())

        try:
            folder, resource_path, metadata = extract_resource(
                self.package,
                self.database,
                resource,
                destination,
                self.mode,
            )

        except Exception as exc:
            wx.MessageBox(
                str(exc),
                "Extract failed",
                wx.OK | wx.ICON_ERROR,
            )
            return

        wx.MessageBox(
            f"Extracted successfully:\n\n"
            f"{folder}\n\n"
            f"{resource_path.name}\n"
            f"resource.json",
            "Extract",
            wx.OK | wx.ICON_INFORMATION,
        )

    def on_context_menu(self, event):
        item = event.GetItem()

        if not item.IsOk():
            return

        self.list.SetCurrentItem(item)

        menu = wx.Menu()

        extract_item = menu.Append(
            wx.ID_ANY,
            "Extract",
        )

        pack_item = menu.Append(
            wx.ID_ANY,
            "Replace",
        )
        menu.AppendSeparator()
        copy_item = menu.Append(
            wx.ID_ANY,
            "Copy Resource Name",
        )

        self.Bind(
            wx.EVT_MENU,
            self.extract_selected,
            extract_item,
        )

        self.Bind(
            wx.EVT_MENU,
            self.pack,
            pack_item,
        )

        self.Bind(
            wx.EVT_MENU,
            self.copy_resource_name,
            copy_item,
        )

        self.PopupMenu(menu)
        menu.Destroy()

    def pack(self, event=None):
        resource = self.selected_resource()
        if resource is None:
            return

        if self.package is None or self.database is None:
            wx.MessageBox(
                "Open a Package first.",
                "Replace",
                wx.OK | wx.ICON_INFORMATION,
            )
            return

        if self.mode != 0:
            wx.MessageBox(
                "Only MODE 0 Replace is currently supported.",
                "Replace",
                wx.OK | wx.ICON_INFORMATION,
            )
            return

        with wx.DirDialog(
            self,
            "Select extracted resource folder",
            style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            folder = Path(dialog.GetPath())

        try:
            replacement = validate_replacement(
                self.package,
                self.database,
                resource,
                folder,
            )
        except Exception as exc:
            wx.MessageBox(str(exc), "Replace failed", wx.OK | wx.ICON_ERROR)
            return

        replacements = {resource.index: replacement}

        with wx.FileDialog(
            self,
            "Save Package",
            defaultDir=str(self.package.parent),
            defaultFile=f"{self.package.stem}{self.package.suffix}",
            wildcard=(
                "MX vs ATV Reflex Package (*.package)|*.package|"
                "All files (*.*)|*.*"
            ),
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return

            output_package = Path(dialog.GetPath())

        # Always keep the matching external Database beside the selected
        # Package and use the same base filename.
        output_database = output_package.with_suffix(".database")

        try:
            packed_package, packed_database, relocations = build_mode0_package(
                self.package,
                self.database,
                self.resources,
                replacements,
                output_package,
                output_database,
            )
        except Exception as exc:
            wx.MessageBox(str(exc), "Replace failed", wx.OK | wx.ICON_ERROR)
            return

        wx.MessageBox(
            "Package created successfully.\n\n"
            f"Package:\n{packed_package}\n\n"
            f"Database:\n{packed_database}\n\n"
            f"Resource: #{resource.index}\n",
            "Replace",
            wx.OK | wx.ICON_INFORMATION,
        )

        self.open_package(package_path=packed_package)

    def defragment(self, event=None):
        if self.package is None or self.database is None:
            wx.MessageBox("Open a Package first.", "Optimization", wx.OK | wx.ICON_INFORMATION)
            return
        if not self.resources:
            wx.MessageBox("The current .database does not contain any resources.", "Optimization", wx.OK | wx.ICON_INFORMATION)
            return

        dialog = DefragmentDialog(self)
        self.Enable(False)
        temp_dir = Path(tempfile.mkdtemp(prefix="reflex_optimization_"))
        temp_package = temp_dir / self.package.name
        temp_database = temp_dir / self.database.name

        def cleanup():
            try:
                for path in temp_dir.iterdir():
                    if path.is_file():
                        path.unlink()
                temp_dir.rmdir()
            except Exception:
                pass

        def success():
            dialog.EndModal(wx.ID_OK)
            dialog.Destroy()
            self.Enable(True)
            self.Raise()
            wx.MessageBox("Optimization completed successfully.\n\nVerification passed successfully.", "Optimization", wx.OK | wx.ICON_INFORMATION)

            with wx.FileDialog(self, "Save Optimized Package", defaultDir=str(self.package.parent), defaultFile=f"{self.package.stem}{self.package.suffix}", wildcard="MX vs ATV Reflex Package (*.package)|*.package|All files (*.*)|*.*", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as save_dialog:
                if save_dialog.ShowModal() != wx.ID_OK:
                    cleanup()
                    return
                output_package = Path(save_dialog.GetPath())
                output_database = output_package.with_suffix(".database")
                try:
                    output_package.write_bytes(temp_package.read_bytes())
                    output_database.write_bytes(temp_database.read_bytes())
                except Exception as exc:
                    wx.MessageBox(str(exc), "Save failed", wx.OK | wx.ICON_ERROR)
                    cleanup()
                    return
            cleanup()
            self.open_package(package_path=output_package)

        def failure(message):
            dialog.EndModal(wx.ID_CANCEL)
            dialog.Destroy()
            self.Enable(True)
            self.Raise()
            cleanup()
            wx.MessageBox(message, "Optimization failed", wx.OK | wx.ICON_ERROR)

        def worker():
            try:
                build_defragmented_package(self.package, self.database, self.resources, self.mode, temp_package, temp_database, progress_callback=lambda v: wx.CallAfter(dialog.set_progress, v))
                wx.CallAfter(dialog.set_status, "Verification...")
                wx.CallAfter(dialog.set_progress, 0)
                verify_defragmented_package(self.package, self.database, temp_package, temp_database, self.resources, self.mode, progress_callback=lambda v: wx.CallAfter(dialog.set_progress, v))
                wx.CallAfter(success)
            except Exception as exc:
                wx.CallAfter(failure, str(exc))

        import threading
        threading.Thread(target=worker, name="ReflexPackageOptimization", daemon=True).start()
        dialog.ShowModal()

    def on_exit(self, event=None):
        self.Close()


    def show_about(self, event=None):
        wx.MessageBox(
            f"{APP_NAME}\n\n"
            "A tool for working with .package game archives.\n\nVersion: 1.2.4\nAuthor: Daniil Korochansky\nLicense: GPLv3.0",
            "About",
            wx.OK | wx.ICON_INFORMATION,
        )

    def copy_resource_name(self, event=None):
        resource = self.selected_resource()

        if resource is None:
            return

        name = (
            resource.asset.name
            if resource.asset
            else f"resource_{resource.index:04d}"
        )

        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(
                wx.TextDataObject(name)
            )
            wx.TheClipboard.Close()


class ReflexPackageApp(wx.App):
    def OnInit(self):
        self.frame = MainFrame()
        self.frame.SetMinSize((780, 440))
        self.frame.SetSize((780, 640))
        self.frame.SetIcon(wx.Icon(resource_path("icon.ico")))
        
        self.frame.Show()
        return True


def main():
    app = ReflexPackageApp(False)
    app.MainLoop()


if __name__ == "__main__":
    main()
