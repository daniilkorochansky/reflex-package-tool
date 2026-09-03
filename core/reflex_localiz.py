#!/usr/bin/env python3
"""MX vs ATV Reflex .localiz codec.

The module can be used both as a CLI and as a normal Python library.
The parser understands the XMEM/decompression padding found after the exact
resource payload and deliberately keeps that padding out of the in-memory
model.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

HEADER = struct.Struct("<III")
U32 = struct.Struct("<I")
FORMAT_VERSION = 2


class LocalizError(ValueError):
    """Raised when a .localiz resource is malformed or inconsistent."""


@dataclass(frozen=True)
class LocalizEntry:
    key: str
    value: str
    length: int
    # For entries decoded from the game, this is the original reserved
    # UTF-16 size. The encoder preserves it by space-padding shorter edits.
    original_length: int | None = None

    @classmethod
    def from_pair(cls, key: str, value: str) -> "LocalizEntry":
        _validate_string_pair(key, value)
        return cls(key=key, value=value, length=_utf16_units(value))

    @property
    def utf16_length(self) -> int:
        return _utf16_units(self.value)

    @property
    def encoded_length(self) -> int:
        """UTF-16 code units reserved on disk for this entry."""
        return self.original_length if self.original_length is not None else self.utf16_length


@dataclass
class LocalizFile:
    version: int
    entries: list[LocalizEntry]
    # Some Reflex resources contain additional unkeyed, NUL-terminated
    # UTF-16 strings in the string pool. They are preserved here as groups
    # inserted before entry N. The list therefore has key_count + 1 groups.
    orphan_strings_before: list[list[str]] | None = None
    # Physical size of the source resource, including XMEM zero padding.
    # When saving a decoded resource, this size is preserved automatically
    # whenever the rebuilt payload still fits.
    original_physical_size: int | None = None
    # Bytes physically stored after the declared string pool. These bytes are
    # preserved verbatim because this resource family may contain non-zero
    # trailing data, not merely zero padding.
    trailing_data: bytes = b""

    @property
    def key_count(self) -> int:
        return len(self.entries)

    @property
    def total_chars(self) -> int:
        total = sum(e.encoded_length + 1 for e in self.entries)
        if self.orphan_strings_before:
            total += sum(
                _utf16_units(s) + 1
                for group in self.orphan_strings_before
                for s in group
            )
        return total

    @property
    def key_blob_size(self) -> int:
        return sum(len(e.key.encode("utf-8")) + 1 for e in self.entries)

    @property
    def payload_size(self) -> int:
        return HEADER.size + self.key_blob_size + self.key_count * 4 + 4 + self.total_chars * 2

    def get(self, key: str, default: str | None = None) -> str | None:
        for entry in self.entries:
            if entry.key == key:
                return entry.value
        return default

    def require(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def set(self, key: str, value: str, *, create: bool = True) -> bool:
        """Set a value by key. Returns True when an existing entry was changed."""
        _validate_string_pair(key, value)
        for i, entry in enumerate(self.entries):
            if entry.key == key:
                current = entry.original_length
                new_len = _utf16_units(value)
                # Shorter values keep their original slot size (space padded).
                # Longer values are now allowed: the slot grows and the string
                # pool is rebuilt accordingly. This is the experimental behavior
                # needed to test longer translations in Reflex.
                reserved = new_len if current is None or new_len > current else current
                self.entries[i] = LocalizEntry(
                    key=key, value=value, length=reserved,
                    original_length=reserved,
                )
                return True
        if not create:
            raise KeyError(key)
        self.entries.append(LocalizEntry.from_pair(key, value))
        if self.orphan_strings_before is not None:
            # New entries are appended after all existing records. Preserve
            # the final orphan group and create a new empty trailing group.
            self.orphan_strings_before.append([])
        return False

    def remove(self, key: str) -> bool:
        for i, entry in enumerate(self.entries):
            if entry.key == key:
                del self.entries[i]
                if self.orphan_strings_before is not None:
                    # Keep orphan records in their original relative order.
                    # Merge the removed entry's surrounding orphan groups.
                    if i + 1 < len(self.orphan_strings_before):
                        self.orphan_strings_before[i].extend(
                            self.orphan_strings_before[i + 1]
                        )
                        del self.orphan_strings_before[i + 1]
                return True
        return False

    def to_dict(self) -> dict[str, str]:
        return {e.key: e.value for e in self.entries}


@dataclass(frozen=True)
class LocalizInfo:
    version: int
    key_count: int
    key_blob_size: int
    total_chars: int
    payload_size: int
    physical_size: int
    padding_size: int
    padding_is_zero: bool

    @property
    def has_padding(self) -> bool:
        return self.padding_size > 0


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16le", errors="surrogatepass")) // 2


def _validate_string_pair(key: str, value: str) -> None:
    if not isinstance(key, str) or not isinstance(value, str):
        raise TypeError("keys and values must both be strings")
    if "\x00" in key:
        raise ValueError(f"key {key!r} contains NUL")
    if "\x00" in value:
        raise ValueError(f"value for key {key!r} contains NUL; NUL is the on-disk terminator")


def _find_nul(data: bytes, start: int) -> int:
    end = data.find(b"\x00", start)
    if end < 0:
        raise LocalizError(f"unterminated key at offset 0x{start:X}")
    return end


def _decode_key(raw: bytes, offset: int) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LocalizError(f"invalid UTF-8 key at offset 0x{offset:X}: {exc}") from exc


def _read_utf16_records(data: bytes, start: int, total_chars: int) -> list[str]:
    """Read the NUL-terminated UTF-16LE records from the string pool."""
    end = start + total_chars * 2
    if end > len(data):
        raise LocalizError(
            f"string data exceeds file: end 0x{end:X}, size 0x{len(data):X}"
        )

    records: list[str] = []
    pos = start
    while pos < end:
        nul = None
        scan = pos
        while scan + 1 < end:
            if data[scan:scan + 2] == b"\x00\x00":
                nul = scan
                break
            scan += 2
        if nul is None:
            raise LocalizError(
                f"unterminated UTF-16 string at offset 0x{pos:X}"
            )
        raw = data[pos:nul]
        try:
            value = raw.decode("utf-16le", errors="strict")
        except UnicodeDecodeError as exc:
            raise LocalizError(
                f"invalid UTF-16LE value at offset 0x{pos:X}: {exc}"
            ) from exc
        records.append(value)
        pos = nul + 2

    if pos != end:
        raise LocalizError(
            f"string pool parsing ended at 0x{pos:X}, expected 0x{end:X}"
        )
    return records


def _align_string_lengths(
    expected_lengths: Sequence[int],
    records: Sequence[str],
) -> tuple[list[int], list[int]]:
    """Align keyed lengths with NUL-terminated records.

    Normal Reflex resources have one record per key. Some resources contain
    extra unkeyed records in the pool. This routine finds an exact alignment
    where every keyed length matches a record length and only extra records
    may be skipped.
    """
    n = len(expected_lengths)
    m = len(records)

    # dp[i][j] = minimum number of skipped records needed to match the
    # first i keys against the first j records.
    inf = 10**9
    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    prev: list[list[tuple[int, int, str] | None]] = [
        [None] * (m + 1) for _ in range(n + 1)
    ]
    dp[0][0] = 0

    for i in range(n + 1):
        for j in range(m + 1):
            cur = dp[i][j]
            if cur == inf:
                continue

            if j < m:
                # Skip an unkeyed/orphan record.
                if cur + 1 < dp[i][j + 1]:
                    dp[i][j + 1] = cur + 1
                    prev[i][j + 1] = (i, j, "skip")

                # Match this record to the next key only when the stored
                # length exactly equals the decoded record length.
                if i < n and expected_lengths[i] == _utf16_units(records[j]):
                    if cur < dp[i + 1][j + 1]:
                        dp[i + 1][j + 1] = cur
                        prev[i + 1][j + 1] = (i, j, "match")

    if dp[n][m] == inf:
        raise LocalizError(
            "cannot align the localization length table with the UTF-16 "
            "string pool; the resource may use another .localiz variant"
        )

    mapping: list[int] = []
    skipped: list[int] = []
    i, j = n, m
    while i or j:
        step = prev[i][j]
        if step is None:
            raise LocalizError("internal string-pool alignment failure")
        pi, pj, kind = step
        if kind == "match":
            mapping.append(j - 1)
        else:
            skipped.append(j - 1)
        i, j = pi, pj

    mapping.reverse()
    skipped.reverse()
    return mapping, skipped


def parse_localiz(data: bytes, *, validate_padding: bool = False) -> LocalizFile:
    """Parse a .localiz buffer.

    Standard resources contain one NUL-terminated UTF-16 string per key.
    Some Reflex resources additionally contain unkeyed strings in the same
    pool. Those records are detected by exact alignment with the length table
    and preserved in ``orphan_strings_before``.
    """
    if len(data) < HEADER.size:
        raise LocalizError("file is smaller than the 12-byte header")

    version, key_count, key_blob_size = HEADER.unpack_from(data, 0)
    key_blob_start = HEADER.size
    key_blob_end = key_blob_start + key_blob_size
    if key_blob_end > len(data):
        raise LocalizError(
            f"key blob exceeds file: end 0x{key_blob_end:X}, size 0x{len(data):X}"
        )

    keys: list[str] = []
    pos = key_blob_start
    for _ in range(key_count):
        end = _find_nul(data, pos)
        if end >= key_blob_end:
            raise LocalizError(
                f"key terminator lies outside key blob at 0x{end:X}"
            )
        keys.append(_decode_key(data[pos:end], pos))
        pos = end + 1

    if pos != key_blob_end:
        raise LocalizError(
            f"key blob size mismatch: parsed through 0x{pos:X}, "
            f"declared end is 0x{key_blob_end:X}"
        )

    lengths_start = key_blob_end
    lengths_end = lengths_start + key_count * 4
    if lengths_end + 4 > len(data):
        raise LocalizError("file ends before the length table / total_chars field")

    lengths = struct.unpack_from(f"<{key_count}I", data, lengths_start)
    total_chars = U32.unpack_from(data, lengths_end)[0]

    string_data_start = lengths_end + 4
    string_data_size = total_chars * 2
    string_data_end = string_data_start + string_data_size
    records = _read_utf16_records(data, string_data_start, total_chars)

    mapping, skipped = _align_string_lengths(lengths, records)

    if len(mapping) != key_count:
        raise LocalizError(
            f"string pool contains {len(mapping)} keyed records, expected {key_count}"
        )

    orphan_before: list[list[str]] = [[] for _ in range(key_count + 1)]
    mapped_set = set(mapping)
    key_index = 0
    for record_index, value in enumerate(records):
        if record_index in mapped_set:
            key_index += 1
        else:
            orphan_before[key_index].append(value)

    entries: list[LocalizEntry] = []
    for i, key in enumerate(keys):
        value = records[mapping[i]]
        length = lengths[i]
        actual_length = _utf16_units(value)
        if actual_length != length:
            raise LocalizError(
                f"length mismatch for key {key!r}: stored {length}, "
                f"decoded {actual_length}"
            )
        if "\x00" in value:
            raise LocalizError(
                f"value for key {key!r} contains embedded NUL"
            )
        entries.append(LocalizEntry(key=key, value=value, length=length, original_length=length))

    if not any(orphan_before):
        orphan_before = None

    if validate_padding and any(data[string_data_end:]):
        raise LocalizError(
            "bytes after the exact payload are not all zero padding"
        )

    return LocalizFile(
        version=version,
        entries=entries,
        orphan_strings_before=orphan_before,
        original_physical_size=len(data),
        trailing_data=data[string_data_end:],
    )


def inspect_localiz(data: bytes) -> LocalizInfo:
    """Return binary layout information without building a full model."""
    if len(data) < HEADER.size:
        raise LocalizError("file is smaller than the 12-byte header")
    version, key_count, key_blob_size = HEADER.unpack_from(data, 0)
    lengths_start = HEADER.size + key_blob_size
    lengths_end = lengths_start + key_count * 4
    if lengths_end + 4 > len(data):
        raise LocalizError("file ends before the length table / total_chars field")
    lengths = struct.unpack_from(f"<{key_count}I", data, lengths_start)
    total_chars = U32.unpack_from(data, lengths_end)[0]
    # total_chars is the physical UTF-16 code-unit count of the complete
    # string pool. Some resources contain additional unkeyed strings, so it
    # is not necessarily sum(lengths) + key_count.
    payload_size = lengths_end + 4 + total_chars * 2
    if payload_size > len(data):
        raise LocalizError("declared payload extends past the physical file size")
    padding = data[payload_size:]
    return LocalizInfo(
        version=version,
        key_count=key_count,
        key_blob_size=key_blob_size,
        total_chars=total_chars,
        payload_size=payload_size,
        physical_size=len(data),
        padding_size=len(padding),
        padding_is_zero=all(b == 0 for b in padding),
    )


def make_localiz(
    entries: Mapping[str, str] | Iterable[tuple[str, str]],
    *,
    version: int = FORMAT_VERSION,
) -> LocalizFile:
    if isinstance(entries, Mapping):
        iterable = entries.items()
    else:
        iterable = entries
    out: list[LocalizEntry] = []
    seen: set[str] = set()
    for key, value in iterable:
        if key in seen:
            raise ValueError(f"duplicate key: {key!r}")
        seen.add(key)
        out.append(LocalizEntry.from_pair(key, value))
    _validate_version(version)
    return LocalizFile(version=version, entries=out)


def _validate_version(version: int) -> None:
    if not isinstance(version, int) or version < 0 or version > 0xFFFFFFFF:
        raise ValueError("version must fit uint32")


def encode_localiz(
    localiz: LocalizFile | Mapping[str, str] | Iterable[tuple[str, str]],
    *,
    version: int | None = None,
    pad_to: int | None = None,
) -> bytes:
    """Encode a LocalizFile. Decoded entries retain their original reserved UTF-16 length; shorter edits are space-padded."""
    if isinstance(localiz, LocalizFile):
        model = localiz
        version = model.version if version is None else version
    else:
        model = make_localiz(localiz, version=FORMAT_VERSION if version is None else version)
        version = model.version

    _validate_version(version)
    key_blob_parts: list[bytes] = []
    lengths_parts: list[bytes] = []
    strings_parts: list[bytes] = []
    seen: set[str] = set()

    orphan_groups = model.orphan_strings_before
    if orphan_groups is not None and len(orphan_groups) != len(model.entries) + 1:
        raise ValueError(
            "orphan_strings_before must contain key_count + 1 groups"
        )

    for entry_index, entry in enumerate(model.entries):
        if orphan_groups:
            for orphan in orphan_groups[entry_index]:
                _validate_string_pair(f"<orphan {entry_index}>", orphan)
                strings_parts.append(
                    orphan.encode("utf-16le", errors="surrogatepass") + b"\x00\x00"
                )

        _validate_string_pair(entry.key, entry.value)
        if entry.key in seen:
            raise ValueError(f"duplicate key: {entry.key!r}")
        seen.add(entry.key)

        key_raw = entry.key.encode("utf-8")
        value_raw = entry.value.encode("utf-16le", errors="surrogatepass")
        units = len(value_raw) // 2
        reserved = entry.encoded_length
        if units > reserved:
            raise ValueError(
                f"value for key {entry.key!r} is {units} UTF-16 units, "
                f"but only {reserved} units are reserved"
            )
        if units < reserved:
            value_raw += (" " * (reserved - units)).encode("utf-16le")
        key_blob_parts.append(key_raw + b"\x00")
        lengths_parts.append(U32.pack(reserved))
        strings_parts.append(value_raw + b"\x00\x00")

    if orphan_groups:
        for orphan in orphan_groups[len(model.entries)]:
            _validate_string_pair("<orphan after entries>", orphan)
            strings_parts.append(
                orphan.encode("utf-16le", errors="surrogatepass") + b"\x00\x00"
            )

    key_blob = b"".join(key_blob_parts)
    length_table = b"".join(lengths_parts)
    string_blob = b"".join(strings_parts)
    total_chars = len(string_blob) // 2

    out = bytearray(HEADER.pack(version, len(model.entries), len(key_blob)))
    out += key_blob
    out += length_table
    out += U32.pack(total_chars)
    out += string_blob

    # Preserve the exact bytes that followed the declared payload in a decoded
    # resource. They are not assumed to be zero padding. This is critical for
    # MXRaven_Default_Strings, whose trailing region contains non-zero bytes.
    if isinstance(localiz, LocalizFile):
        out += model.trailing_data

    # Explicit pad_to is still supported for callers creating/rebuilding files
    # from scratch. For decoded resources, exact trailing bytes take precedence
    # over synthetic zero padding. If the payload grows, the resource grows;
    # never truncate or overwrite the preserved trailing region.
    if pad_to is not None:
        if not isinstance(pad_to, int) or pad_to <= 0:
            raise ValueError("pad_to must be a positive integer")
        if len(out) > pad_to:
            raise ValueError(
                f"encoded resource is {len(out)} bytes, larger than pad_to={pad_to}"
            )
        out += b"\x00" * (pad_to - len(out))

    return bytes(out)


def decode_file(input_path: str | Path) -> LocalizFile:
    return parse_localiz(Path(input_path).read_bytes())


def encode_file(
    localiz: LocalizFile | Mapping[str, str] | Iterable[tuple[str, str]],
    output_path: str | Path,
    *,
    version: int | None = None,
    pad_to: int | None = None,
) -> int:
    data = encode_localiz(localiz, version=version, pad_to=pad_to)
    Path(output_path).write_bytes(data)
    return len(data)


def load_json(path: Path) -> tuple[int | None, list[tuple[str, str]], list[list[str]] | None, list[int] | None, int | None, bytes]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(obj, dict) and "entries" in obj:
        version = obj.get("version")
        raw_entries = obj["entries"]
        if not isinstance(raw_entries, list):
            raise ValueError("'entries' must be a list")
        entries: list[tuple[str, str]] = []
        for i, item in enumerate(raw_entries):
            if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not isinstance(item.get("value"), str):
                raise ValueError(f"invalid entry {i}: expected {{'key': str, 'value': str}}")
            entries.append((item["key"], item["value"]))
        orphan_strings_before = obj.get("orphan_strings_before")
        original_lengths = obj.get("original_lengths")
        if original_lengths is not None:
            if (not isinstance(original_lengths, list) or len(original_lengths) != len(entries)
                    or any(not isinstance(n, int) or n < 0 for n in original_lengths)):
                raise ValueError("'original_lengths' must be a list of key_count non-negative integers")
        original_physical_size = obj.get("original_physical_size")
        trailing_data_hex = obj.get("trailing_data_hex", "")
        if not isinstance(trailing_data_hex, str) or len(trailing_data_hex) % 2:
            raise ValueError("'trailing_data_hex' must be an even-length hex string")
        try:
            trailing_data = bytes.fromhex(trailing_data_hex)
        except ValueError as exc:
            raise ValueError("'trailing_data_hex' is not valid hexadecimal") from exc
        if original_physical_size is not None:
            if not isinstance(original_physical_size, int) or original_physical_size <= 0:
                raise ValueError("'original_physical_size' must be a positive integer")
        if orphan_strings_before is not None:
            if (
                not isinstance(orphan_strings_before, list)
                or len(orphan_strings_before) != len(entries) + 1
                or any(not isinstance(group, list) or any(not isinstance(s, str) for s in group)
                       for group in orphan_strings_before)
            ):
                raise ValueError(
                    "'orphan_strings_before' must be a list of key_count + 1 string lists"
                )
        return version, entries, orphan_strings_before, original_lengths, original_physical_size, trailing_data
    if isinstance(obj, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in obj.items()):
        return None, list(obj.items()), None, None, None, b""
    raise ValueError("JSON must be {'version': ..., 'entries': [...]} or a simple {key: value} object")


def dump_json(model: LocalizFile, path: Path, *, pretty: bool = True) -> None:
    obj = {
        "version": model.version,
        "entries": [{"key": e.key, "value": e.value} for e in model.entries],
    }
    if model.original_physical_size is not None:
        obj["original_physical_size"] = model.original_physical_size
    if model.trailing_data:
        obj["trailing_data_hex"] = model.trailing_data.hex()
    if any(e.original_length is not None for e in model.entries):
        obj["original_lengths"] = [e.original_length for e in model.entries]
    if model.orphan_strings_before:
        obj["orphan_strings_before"] = model.orphan_strings_before
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2 if pretty else None, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def cmd_decode(args: argparse.Namespace) -> int:
    src, dst = Path(args.input), Path(args.output)
    data = src.read_bytes()
    model = parse_localiz(data)
    if args.format == "json":
        dump_json(model, dst, pretty=not args.compact)
    else:
        with dst.open("w", encoding="utf-8", newline="") as f:
            for e in model.entries:
                f.write(f"{e.key}\t{e.value}\n")
    info = inspect_localiz(data)
    print(f"decoded {src}: version={info.version}, keys={info.key_count}, payload={info.payload_size}, padding={info.padding_size}")
    print(f"wrote {dst}")
    return 0


def cmd_encode(args: argparse.Namespace) -> int:
    src, dst = Path(args.input), Path(args.output)
    json_version, entries, orphan_strings_before, original_lengths, original_physical_size, trailing_data = load_json(src)
    version = args.version if args.version is not None else (json_version if json_version is not None else FORMAT_VERSION)
    model = make_localiz(entries, version=version)
    model.orphan_strings_before = orphan_strings_before
    model.original_physical_size = original_physical_size
    model.trailing_data = trailing_data
    if original_lengths is not None:
        model.entries = [
            LocalizEntry(key=e.key, value=e.value, length=original_lengths[i], original_length=original_lengths[i])
            for i, e in enumerate(model.entries)
        ]
    data = encode_localiz(model, pad_to=args.pad_to)
    dst.write_bytes(data)
    print(f"encoded {dst}: version={model.version}, keys={model.key_count}, size={len(data)} (0x{len(data):X})")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    src = Path(args.input)
    info = inspect_localiz(src.read_bytes())
    print(f"file:          {src}")
    print(f"physical_size: {info.physical_size} (0x{info.physical_size:X})")
    print(f"payload_size:  {info.payload_size} (0x{info.payload_size:X})")
    print(f"version:       {info.version}")
    print(f"key_count:     {info.key_count}")
    print(f"key_blob_size: {info.key_blob_size} (0x{info.key_blob_size:X})")
    print(f"total_chars:   {info.total_chars}")
    print(f"xmem_padding:  {info.padding_size} bytes")
    print(f"padding_zero:  {info.padding_is_zero}")
    return 0


def cmd_replace(args: argparse.Namespace) -> int:
    src, dst = Path(args.input), Path(args.output)
    model = decode_file(src)
    changed = model.set(args.key, args.value, create=not args.no_create)
    encode_file(model, dst, pad_to=args.pad_to)
    print(f"replaced {args.key!r}: {'existing entry changed' if changed else 'new entry added'}")
    print(f"wrote {dst}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    src = Path(args.input)
    info = inspect_localiz(src.read_bytes())
    parse_localiz(src.read_bytes(), validate_padding=args.strict_padding)
    print(f"valid: {src}")
    print(f"payload={info.payload_size}, padding={info.padding_size}, padding_zero={info.padding_is_zero}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="localiz_codec", description="Codec for MX vs ATV Reflex .localiz resources")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("decode", help="decode .localiz -> JSON/TSV")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--format", choices=("json", "tsv"), default="json")
    p.add_argument("--compact", action="store_true")
    p.set_defaults(func=cmd_decode)

    p = sub.add_parser("encode", help="encode JSON -> .localiz")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--version", type=int, default=None)
    p.add_argument("--pad-to", type=int, default=None, help="pad with zeroes to exactly N bytes")
    p.set_defaults(func=cmd_encode)

    p = sub.add_parser("replace", help="replace one localization value")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("key")
    p.add_argument("value")
    p.add_argument("--no-create", action="store_true", help="fail when key does not exist")
    p.add_argument("--pad-to", type=int, default=None)
    p.set_defaults(func=cmd_replace)

    p = sub.add_parser("inspect", help="inspect binary layout")
    p.add_argument("input")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("validate", help="strictly validate a resource")
    p.add_argument("input")
    p.add_argument("--strict-padding", action="store_true", help="also require all post-payload bytes to be zero")
    p.set_defaults(func=cmd_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, LocalizError, json.JSONDecodeError, UnicodeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "FORMAT_VERSION", "LocalizError", "LocalizEntry", "LocalizFile", "LocalizInfo",
    "parse_localiz", "inspect_localiz", "make_localiz", "encode_localiz",
    "decode_file", "encode_file", "load_json", "dump_json", "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
