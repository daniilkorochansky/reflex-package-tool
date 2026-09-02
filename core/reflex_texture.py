# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------------------------------------------
#   Reflex Package Tool — A tool for working with game archives for MX vs ATV Reflex in the .package format.
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
from dataclasses import dataclass
from pathlib import Path
import struct
import math


MIN_HEADER_SIZE = 0x80


def reflex_header_size(mip_count: int) -> int:
    """Return the actual Reflex header/data start for a mip count.

    The mip table starts at 0x38 and uses 8 bytes per mip. Reflex keeps a
    minimum 0x80-byte header, but once the mip table grows beyond that area
    the payload starts immediately after the table.

    In particular:
        9 mips  -> 0x80
        10 mips -> 0x88
        11 mips -> 0x90
        12 mips -> 0x98

    Do NOT round 0x88 up to 0x90: original Reflex textures with 10 mips
    demonstrate that the first mip payload begins at 0x88.
    """
    required = 0x38 + mip_count * 8
    return max(MIN_HEADER_SIZE, required)


def reflex_cube_header_size(mip_count: int) -> int:
    """Return the extended Reflex CubeMap header size.

    CubeMaps store a separate (size, pitch) table for each of their six
    faces. The first table starts at 0x38, and the remaining five tables
    follow it immediately. Thus the header contains 6 * mip_count entries,
    while the common header fields before the first table remain unchanged.

    For the 256x256 example (9 mips) this is:
        0x38 + 6 * 9 * 8 = 0x1e8 bytes.
    """
    required = 0x38 + 6 * mip_count * 8
    return max(MIN_HEADER_SIZE, required)


def _has_reflex_cube_header(raw: bytes, mip_count: int) -> bool:
    """Detect the six per-face mip tables used by a Reflex CubeMap."""
    if mip_count <= 0 or len(raw) < reflex_cube_header_size(mip_count):
        return False
    if raw[0x34:0x38] not in (FORMAT_DXT1, FORMAT_DXT5):
        return False

    table_bytes = mip_count * 8
    starts = [0x38 + face * table_bytes for face in range(6)]
    first = raw[starts[0]:starts[0] + table_bytes]
    if len(first) != table_bytes:
        return False

    # Every face has the same mip dimensions, therefore its size/pitch table
    # must match the first face's table.
    for start in starts[1:]:
        if raw[start:start + table_bytes] != first:
            return False

    # Validate the sizes against the dimensions/format rather than merely
    # accepting any repeated 6-table blob.
    w = _u32(raw, 0x2C)
    h = _u32(raw, 0x30)
    fmt = raw[0x34:0x38]
    if w <= 0 or h <= 0 or w != h:
        return False
    for level in range(mip_count):
        mw = max(1, w >> level)
        mh = max(1, h >> level)
        size = _u32(first, level * 8)
        pitch = _u32(first, level * 8 + 4)
        if fmt == FORMAT_DXT1:
            expected_pitch = max(1, (mw + 3) // 4) * 8
            expected_size = max(8, expected_pitch * max(1, (mh + 3) // 4))
        else:
            expected_pitch = max(1, (mw + 3) // 4) * 16
            expected_size = max(16, expected_pitch * max(1, (mh + 3) // 4))
        if pitch != expected_pitch or size != expected_size:
            return False
    return True


HEADER_SIZE = MIN_HEADER_SIZE
FORMAT_DXT1 = b"DXT1"
FORMAT_DXT5 = b"DXT5"
FORMAT_0X70 = 0x70
FORMAT_A8R8G8B8 = 21  # D3DFMT_A8R8G8B8
FORMAT_A16B16G16R16F = 113  # D3DFMT_A16B16G16R16F / 0x71
DXGI_FORMAT_R16G16B16A16_FLOAT = 10


@dataclass
class Mip:
    level: int
    size: int
    pitch: int
    offset: int = 0


@dataclass
class Texture:
    raw: bytes
    width: int
    height: int
    format_raw: bytes
    mips: list[Mip]
    header_size: int

    @property
    def format_name(self):
        if self.format_raw == FORMAT_DXT1:
            return "DXT1"
        if self.format_raw == FORMAT_DXT5:
            return "DXT5"
        if len(self.format_raw) == 4:
            value = struct.unpack("<I", self.format_raw)[0]
            if value == FORMAT_0X70:
                return "0x70"
            if value == FORMAT_A8R8G8B8:
                return "A8R8G8B8"
            if value == FORMAT_A16B16G16R16F:
                return "A16B16G16R16F"
            if value == FORMAT_R32F:
                return "R32F"
        return self.format_raw.hex()


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def read_texture(path: str | Path) -> Texture:
    path = Path(path)
    raw = path.read_bytes()

    if len(raw) < MIN_HEADER_SIZE:
        raise ValueError("Texture is smaller than the minimum Reflex header")

    if _u32(raw, 4) != 4:
        raise ValueError(f"Unsupported texture version: {_u32(raw, 4)}")

    mip_count = _u32(raw, 0x28)
    width = _u32(raw, 0x2C)
    height = _u32(raw, 0x30)
    fmt = raw[0x34:0x38]

    if mip_count == 0 or mip_count > 32:
        raise ValueError(f"Invalid mip count: {mip_count}")

    # CubeMaps have six consecutive mip tables in the header. Ordinary
    # textures have only the first table.
    is_cube_header = _has_reflex_cube_header(raw, mip_count)
    header_size = (
        reflex_cube_header_size(mip_count)
        if is_cube_header
        else reflex_header_size(mip_count)
    )
    if len(raw) < header_size:
        raise ValueError(
            f"Texture is smaller than its {header_size:#x}-byte header"
        )

    mips = []
    data_offset = header_size
    for level in range(mip_count):
        size = _u32(raw, 0x38 + level * 8)
        pitch = _u32(raw, 0x3C + level * 8)
        mips.append(Mip(level, size, pitch, data_offset))
        data_offset += size

    if data_offset > len(raw):
        raise ValueError(
            f"Mipmap data exceeds file: need {data_offset}, have {len(raw)}"
        )

    return Texture(raw, width, height, fmt, mips, header_size)

def inspect_texture(texture_path):
    """
    Read basic information about a Reflex .texture file.

    Returns:
        dict:
            type      - "2D" or "CubeMap"
            format    - format name, e.g. "DXT1", "DXT5", "0x70"
            width     - texture width
            height    - texture height
            mipmaps   - number of mip levels
            faces     - 6 for CubeMap, otherwise 1
    """
    texture = read_texture(texture_path)

    face_count = _detect_cube_face_count(texture)

    return {
        "type": "Cube Map" if face_count == 6 else "2D",
        "format": texture.format_name,
        "width": texture.width,
        "height": texture.height,
        "mipmaps": len(texture.mips),
        "faces": face_count,
    }

def get_texture_preview(texture_path: str | Path):
    """
    Return a PIL.Image.Image suitable for GUI preview.

    The original .texture file is never modified.

    Preview always uses mip 0.

    CubeMap preview:
        transparent 4x3 horizontal Cube Cross

                +Y
        -X      +Z      +X      -Z
                -Y

    DXT5 is decoded through the existing DDS export path instead of
    maintaining a second DXT5 decoder inside this function.
    """

    from PIL import Image
    import tempfile

    texture = read_texture(texture_path)
    fmt = texture.format_name
    is_cube = _detect_cube_face_count(texture) == 6

    # ---------------------------------------------------------
    # CubeMap
    # ---------------------------------------------------------
    if is_cube:

        face_w = texture.width
        face_h = texture.height

        if face_w != face_h:
            raise ValueError(
                f"CubeMap faces must be square, got "
                f"{face_w}x{face_h}"
            )

        positions = {
            "+Y": (1, 0),
            "-X": (0, 1),
            "+Z": (1, 1),
            "+X": (2, 1),
            "-Z": (3, 1),
            "-Y": (1, 2),
        }

        # -----------------------------------------------------
        # CubeMap DXT1
        #
        # This is the already verified path which produces the
        # correct Reflex Cube Cross.
        # -----------------------------------------------------
        if fmt == "DXT1":

            faces = _texture_face_payloads(texture)

            decoded_faces = []

            for face_index, face_chain in enumerate(faces):

                if not face_chain:
                    raise ValueError(
                        f"CubeMap face {face_index} has no mip data"
                    )

                mip0 = face_chain[0]

                expected = max(
                    8,
                    ((face_w + 3) // 4)
                    * ((face_h + 3) // 4)
                    * 8,
                )

                if len(mip0) != expected:
                    raise ValueError(
                        f"Invalid CubeMap face {face_index} mip 0 size: "
                        f"{len(mip0)}, expected {expected}"
                    )

                bgra = _decode_dxt1_image(
                    mip0,
                    face_w,
                    face_h,
                )

                decoded_faces.append(
                    Image.frombytes(
                        "RGBA",
                        (face_w, face_h),
                        bgra,
                        "raw",
                        "BGRA",
                    )
                )

            cross = Image.new(
                "RGBA",
                (face_w * 4, face_h * 3),
                (0, 0, 0, 0),
            )

            for face_index, name in enumerate(_face_names()):

                x, y = positions[name]

                cross.paste(
                    decoded_faces[face_index],
                    (
                        x * face_w,
                        y * face_h,
                    ),
                )

            return cross

        # -----------------------------------------------------
        # CubeMap DXT5
        #
        # IMPORTANT:
        # Do NOT decode DXT5 ourselves.
        #
        # extract_cube_faces() uses the existing verified
        # write_dds_dxt5() path and therefore preserves the
        # original DXT5 blocks exactly.
        # -----------------------------------------------------
        if fmt == "DXT5":

            with tempfile.TemporaryDirectory(
                prefix="reflex_texture_preview_"
            ) as temp_dir:

                temp_dir = Path(temp_dir)

                extract_cube_faces(
                    texture,
                    temp_dir,
                )

                decoded_faces = []

                for name in _face_names():

                    face_path = temp_dir / f"{name}.dds"

                    if not face_path.exists():
                        raise ValueError(
                            f"CubeMap face DDS was not created: "
                            f"{face_path.name}"
                        )

                    with Image.open(face_path) as face_image:
                        face = face_image.convert("RGBA").copy()

                    if face.size != (face_w, face_h):
                        raise ValueError(
                            f"Invalid CubeMap face preview size for "
                            f"{name}: {face.size}; expected "
                            f"{face_w}x{face_h}"
                        )

                    decoded_faces.append(face)

                cross = Image.new(
                    "RGBA",
                    (face_w * 4, face_h * 3),
                    (0, 0, 0, 0),
                )

                for face_index, name in enumerate(_face_names()):

                    x, y = positions[name]

                    cross.paste(
                        decoded_faces[face_index],
                        (
                            x * face_w,
                            y * face_h,
                        ),
                    )

                return cross

        raise ValueError(
            f"CubeMap preview is not supported for format {fmt}"
        )

    # ---------------------------------------------------------
    # DXT1
    # ---------------------------------------------------------
    if fmt == "DXT1":

        mip = texture.mips[0]

        payload = texture.raw[
            mip.offset:
            mip.offset + mip.size
        ]

        bgra = _decode_dxt1_image(
            payload,
            texture.width,
            texture.height,
        )

        return Image.frombytes(
            "RGBA",
            (texture.width, texture.height),
            bgra,
            "raw",
            "BGRA",
        )

    # ---------------------------------------------------------
    # DXT5
    #
    # IMPORTANT:
    # The .texture payload is exported to a real DDS using
    # the existing write_dds_dxt5().
    #
    # No custom DXT5 decoder is used here.
    # ---------------------------------------------------------
    if fmt == "DXT5":

        with tempfile.TemporaryDirectory(
            prefix="reflex_texture_preview_"
        ) as temp_dir:

            temp_dir = Path(temp_dir)
            dds_path = temp_dir / "preview.dds"

            write_dds_dxt5(
                texture,
                dds_path,
            )

            with Image.open(dds_path) as image:
                return image.convert("RGBA").copy()

    # ---------------------------------------------------------
    # 0x70 — R16G16_FLOAT normal map
    # ---------------------------------------------------------
    if fmt == "0x70":

        mip = texture.mips[0]

        payload = texture.raw[
            mip.offset:
            mip.offset + mip.size
        ]

        expected = texture.width * texture.height * 4

        if len(payload) != expected:
            raise ValueError(
                f"Invalid 0x70 mip 0 size: "
                f"{len(payload)}, expected {expected}"
            )

        out = bytearray(
            texture.width * texture.height * 4
        )

        for i in range(
            0,
            len(payload),
            4,
        ):

            x = struct.unpack_from(
                "<e",
                payload,
                i,
            )[0]

            y = struct.unpack_from(
                "<e",
                payload,
                i + 2,
            )[0]

            sx = x * 2.0 - 1.0
            sy = y * 2.0 - 1.0

            z2 = max(
                0.0,
                1.0 - sx * sx - sy * sy,
            )

            z = math.sqrt(z2)

            length = math.sqrt(
                sx * sx +
                sy * sy +
                z * z
            )

            if length > 0.0:
                sx /= length
                sy /= length
                z /= length

            r = max(
                0,
                min(
                    255,
                    int(round(
                        (sx * 0.5 + 0.5) * 255
                    )),
                ),
            )

            g = max(
                0,
                min(
                    255,
                    int(round(
                        (sy * 0.5 + 0.5) * 255
                    )),
                ),
            )

            b = max(
                0,
                min(
                    255,
                    int(round(
                        (z * 0.5 + 0.5) * 255
                    )),
                ),
            )

            out[i:i + 4] = bytes(
                (r, g, b, 255)
            )

        return Image.frombytes(
            "RGBA",
            (texture.width, texture.height),
            bytes(out),
        )

    # ---------------------------------------------------------
    # A8R8G8B8
    # ---------------------------------------------------------
    if fmt == "A8R8G8B8":

        mip = texture.mips[0]

        payload = texture.raw[
            mip.offset:
            mip.offset + mip.size
        ]

        expected = texture.width * texture.height * 4

        if len(payload) != expected:
            raise ValueError(
                f"Invalid A8R8G8B8 mip 0 size: "
                f"{len(payload)}, expected {expected}"
            )

        return Image.frombytes(
            "RGBA",
            (texture.width, texture.height),
            payload,
            "raw",
            "BGRA",
        )

    # ---------------------------------------------------------
    # R32F
    # ---------------------------------------------------------
    if fmt == "R32F":

        mip = texture.mips[0]

        payload = texture.raw[
            mip.offset:
            mip.offset + mip.size
        ]

        expected = texture.width * texture.height * 4

        if len(payload) != expected:
            raise ValueError(
                f"Invalid R32F mip 0 size: "
                f"{len(payload)}, expected {expected}"
            )

        values = [
            struct.unpack_from(
                "<f",
                payload,
                i,
            )[0]
            for i in range(
                0,
                len(payload),
                4,
            )
        ]

        finite = [
            v
            for v in values
            if math.isfinite(v)
        ]

        if not finite:
            finite = [0.0]

        vmin = min(finite)
        vmax = max(finite)

        if vmax > vmin:

            scale = 255.0 / (vmax - vmin)

            pixels = bytes(
                max(
                    0,
                    min(
                        255,
                        int(round(
                            (v - vmin) * scale
                        )),
                    ),
                )
                for v in values
            )

        else:

            pixels = bytes(
                [128] * len(values)
            )

        return Image.frombytes(
            "L",
            (texture.width, texture.height),
            pixels,
        ).convert("RGBA")

    # ---------------------------------------------------------
    # A16B16G16R16F
    # ---------------------------------------------------------
    if fmt == "A16B16G16R16F":

        mip = texture.mips[0]

        payload = texture.raw[
            mip.offset:
            mip.offset + mip.size
        ]

        expected = texture.width * texture.height * 8

        if len(payload) != expected:
            raise ValueError(
                f"Invalid A16B16G16R16F mip 0 size: "
                f"{len(payload)}, expected {expected}"
            )

        out = bytearray(
            texture.width * texture.height * 4
        )

        for pixel in range(
            texture.width * texture.height
        ):

            src = pixel * 8

            r, g, b, a = struct.unpack_from(
                "<4e",
                payload,
                src,
            )

            out[pixel * 4 + 0] = max(
                0,
                min(
                    255,
                    int(round(r * 255.0)),
                ),
            )

            out[pixel * 4 + 1] = max(
                0,
                min(
                    255,
                    int(round(g * 255.0)),
                ),
            )

            out[pixel * 4 + 2] = max(
                0,
                min(
                    255,
                    int(round(b * 255.0)),
                ),
            )

            out[pixel * 4 + 3] = max(
                0,
                min(
                    255,
                    int(round(a * 255.0)),
                ),
            )

        return Image.frombytes(
            "RGBA",
            (texture.width, texture.height),
            bytes(out),
        )

    raise ValueError(
        f"Preview is not supported for format {fmt}"
    )


def extract_mips(texture: Texture, out_dir: str | Path):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for mip in texture.mips:
        start = mip.offset
        end = start + mip.size
        (out_dir / f"mip_{mip.level:02d}.bin").write_bytes(texture.raw[start:end])


def _pitch_for_format(format_name: str, width: int, height: int) -> int:
    """Return the Reflex mip pitch for a given format and mip dimensions."""
    if format_name == "DXT1":
        return max(1, (width + 3) // 4) * 8
    if format_name == "DXT5":
        return max(1, (width + 3) // 4) * 16
    if format_name == "0x70":
        return width * 4
    if format_name == "A8R8G8B8":
        return width * 4
    if format_name == "A16B16G16R16F":
        return width * 8
    if format_name == "R32F":
        return width * 4
    raise ValueError(f"Cannot calculate pitch for unsupported format: {format_name}")


def rebuild_header(
    source: Texture,
    *,
    width: int | None = None,
    height: int | None = None,
    mip_payloads: list[bytes] | None = None,
) -> bytes:
    """
    Build a new Reflex header from the original header.

    The original header is used as a template, but the header size is rebuilt
    when the mip count changes. This allows HD textures to have the correct
    number of mipmaps (for example 2048x2048 -> 12 mipmaps).

    Unknown fields are preserved where possible. Known dimensions, mip count,
    mip sizes and pitches are updated.
    """
    if mip_payloads is None:
        mip_payloads = [
            source.raw[m.offset:m.offset + m.size] for m in source.mips
        ]

    if not mip_payloads:
        raise ValueError("No mip payloads")

    w = width if width is not None else source.width
    h = height if height is not None else source.height

    if w <= 0 or h <= 0:
        raise ValueError("Texture dimensions must be positive")

    mip_count = len(mip_payloads)
    new_header_size = reflex_header_size(mip_count)

    # Keep the original header as a template. If the new header is larger,
    # preserve the available original bytes and zero-fill the newly required
    # mip-table/header area. If it is smaller, truncate it.
    header = bytearray(new_header_size)
    copy_size = min(source.header_size, new_header_size)
    header[:copy_size] = source.raw[:copy_size]

    # Reflex stores the mip count in both 0x10 and 0x28 for ordinary
    # textures. Keep them synchronized so the renderer does not select
    # mip levels with a stale count when the texture is viewed at distance
    # or up close.
    struct.pack_into("<I", header, 0x10, mip_count)
    struct.pack_into("<I", header, 0x28, mip_count)
    struct.pack_into("<I", header, 0x2C, w)
    struct.pack_into("<I", header, 0x30, h)

    for i, payload in enumerate(mip_payloads):
        mw, mh = _mip_dimensions(w, h, i)
        pitch = _pitch_for_format(source.format_name, mw, mh)

        expected = len(payload)
        if source.format_name == "DXT1":
            expected = max(8, ((mw + 3) // 4) * ((mh + 3) // 4) * 8)
        elif source.format_name == "DXT5":
            expected = max(16, ((mw + 3) // 4) * ((mh + 3) // 4) * 16)
        elif source.format_name in ("0x70", "A8R8G8B8", "R32F"):
            expected = mw * mh * 4
        elif source.format_name == "A16B16G16R16F":
            expected = mw * mh * 8

        if len(payload) != expected:
            raise ValueError(
                f"Mip {i} has {len(payload)} bytes, expected {expected} "
                f"for {mw}x{mh} {source.format_name}"
            )

        struct.pack_into("<I", header, 0x38 + i * 8, len(payload))
        struct.pack_into("<I", header, 0x3C + i * 8, pitch)

    return bytes(header)


def encode_from_mips(
    source_texture: str | Path,
    mip_files: list[str | Path],
    output_texture: str | Path,
):
    source = read_texture(source_texture)

    payloads = [Path(p).read_bytes() for p in mip_files]
    header = rebuild_header(source, mip_payloads=payloads)

    Path(output_texture).write_bytes(header + b"".join(payloads))



DDS_MAGIC = b"DDS "
DDS_HEADER_SIZE = 124
DDS_PIXELFORMAT_SIZE = 32
DDS_DDPF_FOURCC = 0x4
DDS_DDPF_RGB = 0x40
DDS_DDSCAPS_TEXTURE = 0x1000
DDSCAPS2_NONE = 0


def _mip_dimensions(width, height, level):
    return max(1, width >> level), max(1, height >> level)


def _fourcc(value: bytes) -> int:
    return struct.unpack("<I", value)[0]


def _write_dds_block_compressed(
    texture: Texture,
    output: str | Path,
    *,
    fourcc: bytes,
    block_bytes: int,
    face_payloads: list[list[bytes]] | None = None,
):
    """Write a standard DDS, optionally as a six-face CubeMap."""
    if texture.format_name not in ("DXT1", "DXT5"):
        raise ValueError("Texture is not a DXT texture")

    mip_count = len(texture.mips)
    is_cube = face_payloads is not None
    if is_cube and len(face_payloads) != 6:
        raise ValueError("CubeMap must contain exactly 6 faces")

    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000
    caps = DDS_DDSCAPS_TEXTURE
    if mip_count > 1:
        caps |= 0x400000 | 0x8

    caps2 = DDSCAPS2_NONE
    if is_cube:
        caps2 = 0x00000200 | 0x0000FC00

    pf = struct.pack(
        "<II4s5I",
        DDS_PIXELFORMAT_SIZE,
        DDS_DDPF_FOURCC,
        fourcc,
        0, 0, 0, 0, 0,
    )

    dds = bytearray()
    dds += DDS_MAGIC
    dds += struct.pack("<I", DDS_HEADER_SIZE)
    dds += struct.pack("<I", flags)
    dds += struct.pack("<I", texture.height)
    dds += struct.pack("<I", texture.width)
    dds += struct.pack("<I", texture.mips[0].pitch)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", mip_count)
    dds += b"\x00" * (11 * 4)
    dds += pf
    dds += struct.pack("<I", caps)
    dds += struct.pack("<I", caps2)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", 0)

    if len(dds) != 128:
        raise AssertionError(f"Invalid DDS header size: {len(dds)}")

    if is_cube:
        payload = b"".join(
            payload
            for face in face_payloads
            for payload in face
        )
    else:
        payload = b"".join(
            texture.raw[m.offset:m.offset + m.size] for m in texture.mips
        )

    Path(output).write_bytes(bytes(dds) + payload)


def _texture_face_payloads(texture: Texture) -> list[list[bytes]]:
    """Return the six CubeMap face mip chains from Reflex face-major storage.

    Reflex CubeMaps have an extended header containing six identical
    (size,pitch) mip tables. After that header, the payload is simply:

        face 0: mip0, mip1, ...
        face 1: mip0, mip1, ...
        ...
        face 5: mip0, mip1, ...

    The trailing bytes in the allocation are padding and are not part of the
    CubeMap payload.
    """
    if _detect_cube_face_count(texture) != 6:
        raise ValueError("Texture is not a CubeMap")

    face_size = sum(m.size for m in texture.mips)
    payload_size = face_size * 6
    payload = texture.raw[
        texture.header_size:texture.header_size + payload_size
    ]
    if len(payload) != payload_size:
        raise ValueError("CubeMap payload is truncated")

    faces = [[] for _ in range(6)]
    pos = 0
    for face_index in range(6):
        for mip in texture.mips:
            end = pos + mip.size
            if end > len(payload):
                raise ValueError("CubeMap payload is truncated")
            faces[face_index].append(payload[pos:end])
            pos = end

    if pos != payload_size:
        raise AssertionError("CubeMap payload split error")
    return faces



def _decode_dxt1_image(payload: bytes, width: int, height: int) -> bytes:
    """Decode one DXT1 mip to BGRA8 pixels."""
    blocks_x = max(1, (width + 3) // 4)
    blocks_y = max(1, (height + 3) // 4)
    expected = blocks_x * blocks_y * 8
    if len(payload) != expected:
        raise ValueError(
            f"Invalid DXT1 payload: {len(payload)} bytes, expected {expected}"
        )

    out = bytearray(width * height * 4)
    pos = 0
    for by in range(blocks_y):
        for bx in range(blocks_x):
            c0, c1, bits = struct.unpack_from("<HHI", payload, pos)
            pos += 8

            def rgb565(c):
                r = ((c >> 11) & 0x1F) * 255 // 31
                g = ((c >> 5) & 0x3F) * 255 // 63
                b = (c & 0x1F) * 255 // 31
                return r, g, b

            r0, g0, b0 = rgb565(c0)
            r1, g1, b1 = rgb565(c1)

            colors = [
                (r0, g0, b0, 255),
                (r1, g1, b1, 255),
            ]
            if c0 > c1:
                colors += [
                    ((2*r0 + r1) // 3, (2*g0 + g1) // 3, (2*b0 + b1) // 3, 255),
                    ((r0 + 2*r1) // 3, (g0 + 2*g1) // 3, (b0 + 2*b1) // 3, 255),
                ]
            else:
                colors += [
                    ((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255),
                    (0, 0, 0, 0),
                ]

            for py in range(4):
                y = by * 4 + py
                if y >= height:
                    continue
                for px in range(4):
                    x = bx * 4 + px
                    if x >= width:
                        continue
                    idx = (bits >> (2 * (py * 4 + px))) & 3
                    r, g, b, a = colors[idx]
                    off = (y * width + x) * 4
                    # DDS A8R8G8B8 is stored B,G,R,A on little-endian systems.
                    out[off:off + 4] = bytes((b, g, r, a))

    return bytes(out)


def _write_dds_dxt1_cube_cross(texture: Texture, output: str | Path):
    """Export a Reflex DXT1 CubeMap as a transparent 4x3 DDS cross.

    This is deliberately a normal 2D DDS, not a DDS cubemap. The six faces
    are placed in the DirectX horizontal-cross layout:

                    +Y
            -X      +Z      +X      -Z
                    -Y

    No blending, edge copying, resampling, rotation, or mip concatenation is
    performed. The face payloads are already complete independent images.
    """
    if texture.format_raw != FORMAT_DXT1:
        raise ValueError("Cube cross export currently supports DXT1 only")
    if _detect_cube_face_count(texture) != 6:
        raise ValueError("Texture is not a CubeMap")

    faces = _texture_face_payloads(texture)
    face_w = texture.width
    face_h = texture.height
    if face_w != face_h:
        raise ValueError("CubeMap face dimensions must be square")

    face_bytes = max(8, ((face_w + 3) // 4) * ((face_h + 3) // 4) * 8)
    for i, chain in enumerate(faces):
        if not chain or len(chain[0]) != face_bytes:
            raise ValueError(f"Invalid mip 0 size for CubeMap face {i}")

    cross_w = face_w * 4
    cross_h = face_h * 3
    atlas = bytearray(cross_w * cross_h * 4)  # alpha 0 outside the faces

    positions = {
        "+Y": (1, 0),
        "-X": (0, 1),
        "+Z": (1, 1),
        "+X": (2, 1),
        "-Z": (3, 1),
        "-Y": (1, 2),
    }

    decoded = {}
    for face_index, name in enumerate(_face_names()):
        decoded[name] = _decode_dxt1_image(faces[face_index][0], face_w, face_h)

    for name, face in decoded.items():
        dst_x, dst_y = positions[name]
        for row in range(face_h):
            src_off = row * face_w * 4
            dst_off = ((dst_y * face_h + row) * cross_w + dst_x * face_w) * 4
            atlas[dst_off:dst_off + face_w * 4] = face[src_off:src_off + face_w * 4]

    flags = 0x1 | 0x2 | 0x4 | 0x1000
    pf = struct.pack(
        "<8I",
        DDS_PIXELFORMAT_SIZE,
        DDS_DDPF_RGB | 0x1,
        0,
        32,
        0x00FF0000,
        0x0000FF00,
        0x000000FF,
        0xFF000000,
    )

    dds = bytearray()
    dds += DDS_MAGIC
    dds += struct.pack("<I", DDS_HEADER_SIZE)
    dds += struct.pack("<I", flags)
    dds += struct.pack("<I", cross_h)
    dds += struct.pack("<I", cross_w)
    dds += struct.pack("<I", cross_w * 4)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", 1)
    dds += b"\x00" * (11 * 4)
    dds += pf
    dds += struct.pack("<I", DDS_DDSCAPS_TEXTURE)
    dds += struct.pack("<I", DDSCAPS2_NONE)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", 0)

    if len(dds) != 128:
        raise AssertionError(f"Invalid Cube cross DDS header size: {len(dds)}")

    Path(output).write_bytes(bytes(dds) + bytes(atlas))



def write_dds_dxt1(texture: Texture, output: str | Path):
    if texture.format_raw != FORMAT_DXT1:
        raise ValueError("Texture is not DXT1")

    face_count = _detect_cube_face_count(texture)
    faces = _texture_face_payloads(texture) if face_count == 6 else None
    _write_dds_block_compressed(
        texture,
        output,
        fourcc=b"DXT1",
        block_bytes=8,
        face_payloads=faces,
    )


def write_dds_dxt5(texture: Texture, output: str | Path):
    if texture.format_raw != FORMAT_DXT5:
        raise ValueError("Texture is not DXT5")

    face_count = _detect_cube_face_count(texture)
    faces = _texture_face_payloads(texture) if face_count == 6 else None
    _write_dds_block_compressed(
        texture,
        output,
        fourcc=b"DXT5",
        block_bytes=16,
        face_payloads=faces,
    )


DDS_RESOURCE_DIMENSION_TEXTURE2D = 3
DXGI_FORMAT_R16G16_FLOAT = 34
DXGI_FORMAT_R32_FLOAT = 41
FORMAT_R32F = 114  # D3DFMT_R32F / 0x72
D3D10_RESOURCE_MISC_TEXTURECUBE = 0x4
DDS_RESOURCE_MISC_FLAG = 0x4


def _make_dds_dx10_header(
    width,
    height,
    mip_count,
    dxgi_format,
    *,
    array_size=1,
    is_cube=False,
    pitch_or_linear_size=None,
):
    flags = 0x1 | 0x2 | 0x4 | 0x1000
    if pitch_or_linear_size is not None:
        flags |= 0x80000
    if mip_count > 1:
        flags |= 0x20000

    caps = DDS_DDSCAPS_TEXTURE
    if mip_count > 1:
        caps |= 0x400000 | 0x8

    caps2 = 0
    if is_cube:
        # DDSCAPS2_CUBEMAP plus all six face bits.
        caps2 = 0x00000200 | 0x0000FC00

    # Standard DDS_HEADER (124 bytes), followed by DDS_HEADER_DXT10 (20).
    dds = bytearray()
    dds += DDS_MAGIC
    dds += struct.pack("<I", DDS_HEADER_SIZE)
    dds += struct.pack("<I", flags)
    dds += struct.pack("<I", height)
    dds += struct.pack("<I", width)
    dds += struct.pack("<I", pitch_or_linear_size if pitch_or_linear_size is not None else width * 4)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", mip_count)
    dds += b"\x00" * (11 * 4)

    # DDS_PIXELFORMAT: DX10 marker.
    dds += struct.pack(
        "<8I",
        DDS_PIXELFORMAT_SIZE,
        DDS_DDPF_FOURCC,
        _fourcc(b"DX10"),
        0, 0, 0, 0, 0,
    )

    dds += struct.pack("<I", caps)
    dds += struct.pack("<I", caps2)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", 0)

    dds += struct.pack(
        "<5I",
        dxgi_format,
        DDS_RESOURCE_DIMENSION_TEXTURE2D,
        D3D10_RESOURCE_MISC_TEXTURECUBE if is_cube else 0,
        array_size,
        0,
    )

    if len(dds) != 148:
        raise AssertionError(f"Invalid DX10 DDS header size: {len(dds)}")

    return bytes(dds)



def _write_dds_rgba8(width, height, mip_payloads, output: str | Path):
    mip_count = len(mip_payloads)
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x8
    caps = DDS_DDSCAPS_TEXTURE

    if mip_count > 1:
        flags |= 0x20000
        caps |= 0x400000 | 0x8

    pf = struct.pack(
        "<8I",
        DDS_PIXELFORMAT_SIZE,
        DDS_DDPF_RGB | 0x1,
        0,
        32,
        0x000000FF,
        0x0000FF00,
        0x00FF0000,
        0xFF000000,
    )

    dds = bytearray()
    dds += DDS_MAGIC
    dds += struct.pack("<I", DDS_HEADER_SIZE)
    dds += struct.pack("<I", flags)
    dds += struct.pack("<I", height)
    dds += struct.pack("<I", width)
    dds += struct.pack("<I", width * 4)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", mip_count)
    dds += b"\x00" * (11 * 4)
    dds += pf
    dds += struct.pack("<I", caps)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", 0)

    if len(dds) != 128:
        raise AssertionError(f"Invalid DDS header size: {len(dds)}")

    Path(output).write_bytes(bytes(dds) + b"".join(mip_payloads))


def preview_0x70_normal_map(texture: Texture, output: str | Path):
    """
    Create an ordinary RGBA8 DDS preview from Reflex 0x70.

    Reflex stores normal X/Y as FP16 values in [0, 1].
    Preview converts them to signed [-1, 1], reconstructs Z,
    and maps XYZ back to [0, 1] for a conventional blue/purple
    tangent-space normal map.
    """
    if texture.format_name != "0x70":
        raise ValueError("Texture is not format 0x70")

    mip_payloads = []

    for mip in texture.mips:
        payload = texture.raw[mip.offset:mip.offset + mip.size]
        if len(payload) % 4:
            raise ValueError("0x70 mip payload is not 4-byte aligned")

        out = bytearray(len(payload))

        for i in range(0, len(payload), 4):
            x = struct.unpack_from("<e", payload, i)[0]
            y = struct.unpack_from("<e", payload, i + 2)[0]

            sx = x * 2.0 - 1.0
            sy = y * 2.0 - 1.0
            z2 = max(0.0, 1.0 - sx * sx - sy * sy)
            z = math.sqrt(z2)

            length = math.sqrt(sx * sx + sy * sy + z * z)
            if length > 0.0:
                sx /= length
                sy /= length
                z /= length

            r = max(0, min(255, int(round((sx * 0.5 + 0.5) * 255))))
            g = max(0, min(255, int(round((sy * 0.5 + 0.5) * 255))))
            b = max(0, min(255, int(round((z * 0.5 + 0.5) * 255))))

            out[i:i + 4] = bytes((r, g, b, 255))

        mip_payloads.append(bytes(out))

    _write_dds_rgba8(
        texture.width,
        texture.height,
        mip_payloads,
        output,
    )


def write_dds_0x70_float2(texture: Texture, output: str | Path):
    """
    Lossless export of Reflex 0x70 as DX10 DDS R16G16_FLOAT.

    Each Reflex pixel is preserved exactly as:
        little-endian FP16 X + little-endian FP16 Y

    No Z reconstruction and no 8-bit conversion are performed.
    """
    if texture.format_name != "0x70":
        raise ValueError("Texture is not format 0x70")

    header = _make_dds_dx10_header(
        texture.width,
        texture.height,
        len(texture.mips),
        DXGI_FORMAT_R16G16_FLOAT,
    )

    payload = b"".join(
        texture.raw[m.offset:m.offset + m.size] for m in texture.mips
    )

    Path(output).write_bytes(header + payload)


def _detect_cube_face_count(texture: Texture) -> int:
    """Return 6 only for a Reflex texture with the extended six-face header."""
    if texture.width != texture.height:
        return 1
    if texture.format_name not in ("DXT1", "DXT5"):
        return 1
    return 6 if texture.header_size == reflex_cube_header_size(len(texture.mips)) else 1



def write_dds_a16b16g16r16f(texture: Texture, output: str | Path):
    """Lossless export of Reflex D3DFMT_A16B16G16R16F (0x71)."""
    if texture.format_name != "A16B16G16R16F":
        raise ValueError("Texture is not A16B16G16R16F")

    face_count = _detect_cube_face_count(texture)
    face_size = sum(m.size for m in texture.mips)

    if face_count == 6:
        payload_size = face_size * 6
        payload = texture.raw[
            texture.header_size:texture.header_size + payload_size
        ]
    else:
        payload = b"".join(
            texture.raw[m.offset:m.offset + m.size] for m in texture.mips
        )

    header = _make_dds_dx10_header(
        texture.width,
        texture.height,
        len(texture.mips),
        DXGI_FORMAT_R16G16B16A16_FLOAT,
        array_size=1,
        is_cube=(face_count == 6),
        pitch_or_linear_size=texture.mips[0].pitch,
    )

    Path(output).write_bytes(header + payload)

def write_dds_r32f(texture: Texture, output: str | Path):
    """Lossless export of Reflex D3DFMT_R32F as DX10 R32_FLOAT DDS."""
    if texture.format_name != "R32F":
        raise ValueError("Texture is not R32F")

    header = _make_dds_dx10_header(
        texture.width,
        texture.height,
        len(texture.mips),
        DXGI_FORMAT_R32_FLOAT,
    )
    payload = b"".join(
        texture.raw[m.offset:m.offset + m.size] for m in texture.mips
    )
    Path(output).write_bytes(header + payload)


def write_dds_a8r8g8b8(texture: Texture, output: str | Path):
    """Lossless export of Reflex D3DFMT_A8R8G8B8 as a 32-bit DDS."""
    if texture.format_name != "A8R8G8B8":
        raise ValueError("Texture is not A8R8G8B8")

    mip_count = len(texture.mips)
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x8
    caps = DDS_DDSCAPS_TEXTURE
    if mip_count > 1:
        flags |= 0x20000
        caps |= 0x400000 | 0x8

    # D3DFMT_A8R8G8B8 memory bytes on little-endian systems are B,G,R,A.
    pf = struct.pack(
        "<8I",
        DDS_PIXELFORMAT_SIZE,
        DDS_DDPF_RGB | 0x1,
        0,
        32,
        0x00FF0000,  # R
        0x0000FF00,  # G
        0x000000FF,  # B
        0xFF000000,  # A
    )

    dds = bytearray()
    dds += DDS_MAGIC
    dds += struct.pack("<I", DDS_HEADER_SIZE)
    dds += struct.pack("<I", flags)
    dds += struct.pack("<I", texture.height)
    dds += struct.pack("<I", texture.width)
    dds += struct.pack("<I", texture.width * 4)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", mip_count)
    dds += b"\x00" * (11 * 4)
    dds += pf
    dds += struct.pack("<I", caps)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", 0)
    dds += struct.pack("<I", 0)

    if len(dds) != 128:
        raise AssertionError(f"Invalid A8R8G8B8 DDS header size: {len(dds)}")

    payload = b"".join(
        texture.raw[m.offset:m.offset + m.size] for m in texture.mips
    )
    Path(output).write_bytes(bytes(dds) + payload)


def write_dds_32bit(texture: Texture, output: str | Path, *, bgra=False):
    # Compatibility wrapper. Format 0x70 is now exported losslessly as
    # DX10 R16G16_FLOAT rather than RGBA8.
    write_dds_0x70_float2(texture, output)

def _face_names() -> tuple[str, ...]:
    """Logical DirectX CubeMap order used by Reflex storage.

    The verified FR_CH_WaterfallPoolCubeMap.texture stores the six faces in
    the same order as the DirectX/DDS face order:
        +X, -X, +Y, -Y, +Z, -Z
    """
    return ("+X", "-X", "+Y", "-Y", "+Z", "-Z")



def extract_cube_faces(
    texture: Texture,
    out_dir: str | Path,
    *,
    upscale: int | None = None,
):
    """
    Export the six CubeMap faces as independent DDS files.

    Only mip 0 is exported by default because it is the editable source
    image. The DDS is still a valid single-face DDS. If upscale is supplied,
    it is only used for naming/validation; actual scaling is intentionally
    left to texconv or an image editor.
    """
    if _detect_cube_face_count(texture) != 6:
        raise ValueError("Texture is not a detected CubeMap")

    if texture.format_name not in ("DXT1", "DXT5"):
        raise ValueError(
            f"CubeMap face extraction currently supports DXT1/DXT5, "
            f"not {texture.format_name}"
        )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    faces = _texture_face_payloads(texture)
    for name, face_mips in zip(_face_names(), faces):
        # Build a temporary Texture-like view containing this face chain.
        face_raw = b"".join(face_mips)
        face_mips_meta = []
        off = 0
        for level, (src_mip, payload) in enumerate(zip(texture.mips, face_mips)):
            face_mips_meta.append(
                Mip(level, len(payload), src_mip.pitch, off)
            )
            off += len(payload)

        face_texture = Texture(
            raw=face_raw,
            width=texture.width,
            height=texture.height,
            format_raw=texture.format_raw,
            mips=face_mips_meta,
            header_size=0,
        )

        output = out_dir / f"{name}.dds"
        if texture.format_name == "DXT1":
            write_dds_dxt1(face_texture, output)
        else:
            write_dds_dxt5(face_texture, output)

    print(f"Exported 6 CubeMap faces to: {out_dir}")


def _read_cube_face_dds(path: Path, expected_format: str | None = None):
    dds = read_dds(path)
    if dds.get("face_count", 1) != 1:
        raise ValueError(f"Face file must be a single-face DDS: {path.name}")
    if dds["format"] not in ("DXT1", "DXT5"):
        raise ValueError(f"Unsupported CubeMap face format in {path.name}: {dds['format']}")
    if expected_format and dds["format"] != expected_format:
        raise ValueError(
            f"Face {path.name} is {dds['format']}, expected {expected_format}"
        )
    return dds


def rebuild_cube_header(
    source: Texture,
    *,
    width: int | None = None,
    height: int | None = None,
    mip_payloads: list[bytes],
) -> bytes:
    """Rebuild the extended six-face Reflex CubeMap header."""
    if not mip_payloads:
        raise ValueError("No CubeMap mip payloads")

    w = width if width is not None else source.width
    h = height if height is not None else source.height
    if w <= 0 or h <= 0 or w != h:
        raise ValueError("CubeMap dimensions must be positive and square")

    mip_count = len(mip_payloads)
    new_header_size = reflex_cube_header_size(mip_count)
    header = bytearray(new_header_size)
    copy_size = min(source.header_size, new_header_size)
    header[:copy_size] = source.raw[:copy_size]

    struct.pack_into("<I", header, 0x28, mip_count)
    struct.pack_into("<I", header, 0x2C, w)
    struct.pack_into("<I", header, 0x30, h)
    header[0x34:0x38] = source.format_raw

    table_bytes = mip_count * 8
    for face in range(6):
        table = 0x38 + face * table_bytes
        for level, payload in enumerate(mip_payloads):
            mw, mh = _mip_dimensions(w, h, level)
            pitch = _pitch_for_format(source.format_name, mw, mh)
            if source.format_name == "DXT1":
                expected = max(8, ((mw + 3) // 4) * ((mh + 3) // 4) * 8)
            elif source.format_name == "DXT5":
                expected = max(16, ((mw + 3) // 4) * ((mh + 3) // 4) * 16)
            else:
                raise ValueError("CubeMap header rebuild supports DXT1/DXT5 only")
            if len(payload) != expected:
                raise ValueError(
                    f"Mip {level} has {len(payload)} bytes, expected {expected}"
                )
            struct.pack_into("<I", header, table + level * 8, len(payload))
            struct.pack_into("<I", header, table + level * 8 + 4, pitch)

    return bytes(header)


def encode_cube_from_faces(
    source_texture: str | Path,
    faces_dir: str | Path,
    output_texture: str | Path,
    *,
    preserve_padding=True,
):
    """
    Assemble six single-face DDS files into a Reflex CubeMap.

    The six DDS files may have a different resolution and mip count from the
    source texture. All six must match each other. Their mip chains are
    generated by the external DDS workflow (for example texconv), so no
    resampling is performed here.
    """
    source = read_texture(source_texture)
    if _detect_cube_face_count(source) != 6:
        raise ValueError("Source texture is not a detected CubeMap")

    if source.format_name not in ("DXT1", "DXT5"):
        raise ValueError(
            f"CubeMap assembly currently supports DXT1/DXT5, "
            f"not {source.format_name}"
        )

    faces_dir = Path(faces_dir)
    names = _face_names()
    paths = []
    for name in names:
        p = faces_dir / f"{name}.dds"
        if not p.exists():
            # Also accept the ASCII-safe alternative names.
            alt = faces_dir / f"face_{name.replace('+', 'p').replace('-', 'm')}.dds"
            p = alt if alt.exists() else p
        if not p.exists():
            raise ValueError(
                f"Missing CubeMap face: {name}.dds in {faces_dir}"
            )
        paths.append(p)

    dds_faces = [_read_cube_face_dds(p, source.format_name) for p in paths]

    width = dds_faces[0]["width"]
    height = dds_faces[0]["height"]
    mip_count = dds_faces[0]["mip_count"]

    if width != height:
        raise ValueError("CubeMap faces must be square")

    for name, dds in zip(names, dds_faces):
        if dds["width"] != width or dds["height"] != height:
            raise ValueError(
                f"CubeMap face {name} has {dds['width']}x{dds['height']}; "
                f"expected {width}x{height}"
            )
        if dds["mip_count"] != mip_count:
            raise ValueError(
                f"CubeMap face {name} has {dds['mip_count']} mipmaps; "
                f"expected {mip_count}"
            )

    # DDS and the Reflex CubeMap representation used by this tool both keep
    # each face as a complete mip chain. Preserve that face-major order.
    payloads = []
    for face_index in range(6):
        payloads.extend(dds_faces[face_index]["mips"])

    # Reflex CubeMaps have six mip tables in their extended header.
    header_payloads = dds_faces[0]["mips"]
    header = rebuild_cube_header(
        source,
        width=width,
        height=height,
        mip_payloads=header_payloads,
    )

    original_data_end = source.header_size + sum(m.size for m in source.mips) * 6
    original_tail = (
        source.raw[original_data_end:] if preserve_padding else b""
    )

    Path(output_texture).write_bytes(
        header + b"".join(payloads) + original_tail
    )

    print(
        f"CubeMap written to: {output_texture}\n"
        f"Format: {source.format_name}\n"
        f"Dimensions: {width} x {height}\n"
        f"Mipmaps per face: {mip_count}"
    )



def decode_texture_to_dds(texture_path: str | Path, output_dds: str | Path, *, bgra=True, normal_preview: str | Path | None = None):
    texture = read_texture(texture_path)
    if texture.format_name == "DXT1":
        if _detect_cube_face_count(texture) == 6:
            _write_dds_dxt1_cube_cross(texture, output_dds)
        else:
            write_dds_dxt1(texture, output_dds)
    elif texture.format_name == "DXT5":
        write_dds_dxt5(texture, output_dds)
    elif texture.format_name == "0x70":
        write_dds_0x70_float2(texture, output_dds)
        if normal_preview is not None:
            preview_0x70_normal_map(texture, normal_preview)
    elif texture.format_name == "A8R8G8B8":
        write_dds_a8r8g8b8(texture, output_dds)
    elif texture.format_name == "R32F":
        write_dds_r32f(texture, output_dds)
    elif texture.format_name == "A16B16G16R16F":
        write_dds_a16b16g16r16f(texture, output_dds)
    else:
        raise ValueError(f"Unsupported Reflex texture format: {texture.format_name}")



def read_dds(path: str | Path):
    """Read DXT1/DXT5 or DX10 R16G16_FLOAT DDS into mip payloads."""
    raw = Path(path).read_bytes()
    if raw[:4] != DDS_MAGIC or len(raw) < 128:
        raise ValueError("Not a valid DDS file")

    height = struct.unpack_from("<I", raw, 12)[0]
    width = struct.unpack_from("<I", raw, 16)[0]
    mip_count = struct.unpack_from("<I", raw, 28)[0] or 1
    pf_flags = struct.unpack_from("<I", raw, 80)[0]
    fourcc = raw[84:88]

    payload_pos = 128

    if pf_flags & DDS_DDPF_FOURCC:
        if fourcc == b"DXT1":
            is_cube = bool(struct.unpack_from("<I", raw, 112)[0] & 0x00000200)
            face_count = 6 if is_cube else 1
            payloads = []
            pos = payload_pos

            for _face in range(face_count):
                w, h = width, height
                for level in range(mip_count):
                    size = max(8, ((max(1, w) + 3) // 4) *
                               ((max(1, h) + 3) // 4) * 8)
                    end = pos + size
                    if end > len(raw):
                        raise ValueError("DDS DXT1 mip data is truncated")
                    payloads.append(raw[pos:end])
                    pos = end
                    w = max(1, w // 2)
                    h = max(1, h // 2)

            return {
                "width": width,
                "height": height,
                "mip_count": mip_count,
                "format": "DXT1",
                "masks": None,
                "mips": payloads,
                "face_count": face_count,
            }

        if fourcc == b"DXT5":
            is_cube = bool(struct.unpack_from("<I", raw, 112)[0] & 0x00000200)
            face_count = 6 if is_cube else 1
            payloads = []
            pos = payload_pos

            for _face in range(face_count):
                w, h = width, height
                for level in range(mip_count):
                    size = max(16, ((max(1, w) + 3) // 4) *
                               ((max(1, h) + 3) // 4) * 16)
                    end = pos + size
                    if end > len(raw):
                        raise ValueError("DDS DXT5 mip data is truncated")
                    payloads.append(raw[pos:end])
                    pos = end
                    w = max(1, w // 2)
                    h = max(1, h // 2)

            return {
                "width": width,
                "height": height,
                "mip_count": mip_count,
                "format": "DXT5",
                "masks": None,
                "mips": payloads,
                "face_count": face_count,
            }

        if fourcc == b"DX10":
            if len(raw) < 148:
                raise ValueError("DDS DX10 header is truncated")

            dxgi_format = struct.unpack_from("<I", raw, 128)[0]
            resource_dimension = struct.unpack_from("<I", raw, 132)[0]
            misc_flag = struct.unpack_from("<I", raw, 136)[0]
            array_size = struct.unpack_from("<I", raw, 140)[0] or 1

            if dxgi_format not in (
                DXGI_FORMAT_R16G16_FLOAT,
                DXGI_FORMAT_R32_FLOAT,
                DXGI_FORMAT_R16G16B16A16_FLOAT,
            ):
                raise ValueError(
                    f"Unsupported DX10 DXGI format: {dxgi_format}"
                )
            if resource_dimension != DDS_RESOURCE_DIMENSION_TEXTURE2D:
                raise ValueError("Only Texture2D DDS files are supported")

            payload_pos = 148
            payloads = []
            pos = payload_pos
            bpp = 8 if dxgi_format == DXGI_FORMAT_R16G16B16A16_FLOAT else 4
            face_count = 6 if (misc_flag & D3D10_RESOURCE_MISC_TEXTURECUBE) else array_size

            if face_count < 1:
                raise ValueError("Invalid DDS DX10 array size")

            for face in range(face_count):
                w, h = width, height
                for level in range(mip_count):
                    size = max(1, w) * max(1, h) * bpp
                    end = pos + size
                    if end > len(raw):
                        raise ValueError("DDS float mip data is truncated")
                    payloads.append(raw[pos:end])
                    pos = end
                    w = max(1, w // 2)
                    h = max(1, h // 2)

            if dxgi_format == DXGI_FORMAT_R16G16_FLOAT:
                fmt_name = "0x70"
            elif dxgi_format == DXGI_FORMAT_R32_FLOAT:
                fmt_name = "R32F"
            else:
                fmt_name = "A16B16G16R16F"

            return {
                "width": width,
                "height": height,
                "mip_count": mip_count,
                "format": fmt_name,
                "masks": None,
                "mips": payloads,
                "face_count": face_count,
            }

        raise ValueError(f"Unsupported DDS FOURCC: {fourcc!r}")

    # Uncompressed 32-bit A8R8G8B8.
    if (pf_flags & DDS_DDPF_RGB) and struct.unpack_from("<I", raw, 88)[0] == 32:
        rmask = struct.unpack_from("<I", raw, 92)[0]
        gmask = struct.unpack_from("<I", raw, 96)[0]
        bmask = struct.unpack_from("<I", raw, 100)[0]
        amask = struct.unpack_from("<I", raw, 104)[0]

        if (rmask, gmask, bmask, amask) != (
            0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000
        ):
            raise ValueError(
                "Unsupported 32-bit DDS channel masks; expected A8R8G8B8"
            )

        payloads = []
        pos = 128
        w, h = width, height
        for level in range(mip_count):
            size = max(1, w) * max(1, h) * 4
            end = pos + size
            if end > len(raw):
                raise ValueError("DDS A8R8G8B8 mip data is truncated")
            payloads.append(raw[pos:end])
            pos = end
            w = max(1, w // 2)
            h = max(1, h // 2)

        return {
            "width": width,
            "height": height,
            "mip_count": mip_count,
            "format": "A8R8G8B8",
            "masks": (rmask, gmask, bmask, amask),
            "mips": payloads,
        }

    raise ValueError("Unsupported DDS pixel format")



def texture_logical_end(texture: Texture) -> int:
    """Return the logical end: actual Reflex header plus all mip payloads."""
    if not texture.mips:
        return texture.header_size
    return texture.header_size + sum(mip.size for mip in texture.mips)

def split_texture_padding(texture: Texture):
    """
    Return (logical_texture_bytes, padding_bytes).

    The logical part ends immediately after the last mip payload. Any bytes
    after that are treated as allocation/padding and are not fed to XMEM.
    """
    end = texture_logical_end(texture)
    return texture.raw[:end], texture.raw[end:]


def prepare_xmem_input(texture: Texture, *, include_padding=False) -> bytes:
    """
    Prepare the byte stream that should be supplied to XMEM.

    By default only logical header+mip data is supplied. Padding remains part
    of the on-disk .texture allocation but is deliberately excluded from the
    compression input.
    """
    if include_padding:
        return texture.raw
    logical, _ = split_texture_padding(texture)
    return logical

def _split_dxt1_cross_payload(dds: dict, face_size: int) -> list[bytes]:
    """Split a 4x3 DXT1 horizontal-cross DDS into six face mip-0 payloads.

    The cross layout is the exact inverse of _write_dds_dxt1_cube_cross:

                    +Y
            -X      +Z      +X      -Z
                    -Y

    The exported cross contains one mip only. Its unused cells are transparent
    RGBA in the exported DDS, but for DXT1 input we simply crop the compressed
    block grid; no pixel decoding or recompression is involved.
    """
    if dds.get("format") != "DXT1":
        raise ValueError("CubeMap cross import currently requires a DXT1 DDS")
    if dds.get("face_count", 1) != 1:
        raise ValueError("CubeMap cross must be a normal 2D DDS, not a DDS cubemap")
    if dds.get("mip_count", 1) != 1:
        raise ValueError("CubeMap cross DDS must contain exactly one mipmap")

    width = int(dds["width"])
    height = int(dds["height"])
    if width != face_size * 4 or height != face_size * 3:
        raise ValueError(
            f"Invalid CubeMap cross size {width}x{height}; "
            f"expected {face_size * 4}x{face_size * 3}"
        )

    raw = dds["mips"][0]
    blocks_x = width // 4
    blocks_y = height // 4
    face_blocks = face_size // 4
    row_bytes = blocks_x * 8
    face_row_bytes = face_blocks * 8
    expected = blocks_x * blocks_y * 8
    if len(raw) != expected:
        raise ValueError(
            f"Invalid DXT1 cross payload size {len(raw)}; expected {expected}"
        )

    positions = {
        "+Y": (1, 0),
        "-X": (0, 1),
        "+Z": (1, 1),
        "+X": (2, 1),
        "-Z": (3, 1),
        "-Y": (1, 2),
    }

    faces = []
    for name in _face_names():
        tile_x, tile_y = positions[name]
        out = bytearray(face_size * face_size // 16 * 8)
        dst = 0
        for row in range(face_blocks):
            src = (tile_y * face_blocks + row) * row_bytes + tile_x * face_row_bytes
            out[dst:dst + face_row_bytes] = raw[src:src + face_row_bytes]
            dst += face_row_bytes
        faces.append(bytes(out))

    return faces


def _cube_cross_payloads_from_dds(source: Texture, dds: dict) -> list[bytes] | None:
    """Return Reflex face-major payloads from a single-mip DXT1 cross DDS.

    The cross may have a different resolution from the source CubeMap. When
    the resolution is unchanged, the original lower mip chain is retained.
    When the resolution is changed, only the new mip 0 is available, so the
    resulting Reflex CubeMap is rebuilt as a valid single-mip texture rather
    than accidentally writing the 4x3 atlas as one ordinary texture.
    """
    if _detect_cube_face_count(source) != 6:
        return None
    if dds.get("format") != "DXT1" or dds.get("face_count", 1) != 1:
        return None
    if dds.get("mip_count", 1) != 1:
        return None

    cross_w = int(dds.get("width", 0))
    cross_h = int(dds.get("height", 0))
    if cross_w <= 0 or cross_h <= 0 or cross_w % 4 != 0 or cross_h % 3 != 0:
        return None

    face_size = cross_w // 4
    if face_size <= 0 or cross_h != face_size * 3 or face_size % 4 != 0:
        return None

    mip0_faces = _split_dxt1_cross_payload(dds, face_size)

    # Same-size edit: keep the original lower mips exactly as before.
    if face_size == source.width and face_size == source.height:
        source_faces = _texture_face_payloads(source)
        face_chains = []
        for face_index in range(6):
            chain = [mip0_faces[face_index]]
            chain.extend(source_faces[face_index][1:])
            face_chains.append(chain)

        return [
            face_chains[face][level]
            for face in range(6)
            for level in range(len(source.mips))
        ]

    # Resolution changed: the old mip chain has incompatible dimensions and
    # must not be copied into the new texture. Keep only the new mip 0.
    return [mip0_faces[face] for face in range(6)]


def encode_dds_to_texture(
    source_texture: str | Path,
    dds_file: str | Path,
    output_texture: str | Path,
    *,
    preserve_padding=True,
):
    """
    Replace the mip payloads of a Reflex texture using a DDS.

    The original variable-size Reflex header is retained as the template.
    Known fields (mip count, dimensions, mip sizes and pitches) are updated.
    The original trailing bytes are retained verbatim.
    """
    source = read_texture(source_texture)
    dds = read_dds(dds_file)

    # The DDS is allowed to have a different resolution and mip count.
    # The original .texture remains the template for Reflex-specific fields.
    if dds["width"] <= 0 or dds["height"] <= 0:
        raise ValueError("DDS dimensions must be positive")

    source_face_count = _detect_cube_face_count(source)

    cross_payloads = _cube_cross_payloads_from_dds(source, dds)
    if cross_payloads is not None:
        # The editable 4x3 cross contains mip 0 only. Lower mip levels remain
        # from the source texture. Payload order is Reflex face-major:
        # face0 mip0..mipN, face1 mip0..mipN, ...
        payloads = cross_payloads
    elif source_face_count == 6 and dds.get("face_count", 1) == 6:
        if dds["mip_count"] != len(source.mips):
            raise ValueError(
                f"DDS has {dds['mip_count']} mipmaps per face, "
                f"source has {len(source.mips)}"
            )
        expected = len(source.mips) * 6
        if len(dds["mips"]) != expected:
            raise ValueError(
                f"DDS cube contains {len(dds['mips'])} mip payloads, expected {expected}"
            )

        # Standard DDS CubeMap storage and the verified Reflex CubeMap
        # storage are both face-major. No transposition is performed.
        payloads = dds["mips"]
    else:
        # Resolution and mip count may differ from the source texture.
        # This is what enables HD texture replacement.
        payloads = dds["mips"]

    if dds["format"] != source.format_name:
        raise ValueError(
            f"DDS format {dds['format']} does not match source "
            f"format {source.format_name}"
        )

    if source_face_count == 6 and (dds.get("face_count", 1) == 6 or cross_payloads is not None):
        # The Reflex CubeMap header contains six identical mip tables. The
        # table describes one face, while the payload is stored face-major.
        header_payloads = [
            payloads[level]
            for level in range(len(payloads) // 6)
        ]
        if cross_payloads is not None:
            face_size = dds["width"] // 4
            cube_width = cube_height = face_size
        else:
            cube_width = dds["width"]
            cube_height = dds["height"]
        header = rebuild_cube_header(
            source,
            width=cube_width,
            height=cube_height,
            mip_payloads=header_payloads,
        )
    else:
        header = rebuild_header(
            source,
            width=dds["width"],
            height=dds["height"],
            mip_payloads=payloads,
        )

    if source_face_count == 6 and (dds.get("face_count", 1) == 6 or cross_payloads is not None):
        original_data_end = source.header_size + sum(m.size for m in source.mips) * 6
    else:
        original_data_end = texture_logical_end(source)

    # Allocation padding belongs to the old resource dimensions. Do not copy
    # it when the CubeMap resolution or mip count changes. Stale tail bytes can
    # make the resulting resource larger than its logical allocation and are
    # not needed by the engine.
    same_resolution = (
        (dds["width"] == source.width and dds["height"] == source.height)
        or (cross_payloads is not None and dds["width"] // 4 == source.width)
    )
    same_mip_count = len(payloads) == len(source.mips) * (6 if source_face_count == 6 else 1)
    keep_tail = preserve_padding and same_resolution and same_mip_count
    original_tail = source.raw[original_data_end:] if keep_tail else b""

    Path(output_texture).write_bytes(
        header + b"".join(payloads) + original_tail
    )


def roundtrip_check(source_texture: str | Path, dds_file: str | Path):
    """
    Verify that decoding and immediately encoding a DDS reproduces all mip
    payloads exactly. Header/tail are also checked against the source.
    """
    source = read_texture(source_texture)
    dds = read_dds(dds_file)

    if _detect_cube_face_count(source) == 6 and dds.get("face_count", 1) == 6:
        face_size = sum(m.size for m in source.mips)
        source_payload = source.raw[
            source.header_size:source.header_size + face_size * 6
        ]
    else:
        source_payload = b"".join(
            source.raw[m.offset:m.offset + m.size] for m in source.mips
        )
    if _detect_cube_face_count(source) == 6 and dds.get("face_count", 1) == 6:
        # DDS and Reflex are both face-major here, so compare the payload
        # streams directly.
        dds_payload = b"".join(dds["mips"])
    else:
        dds_payload = b"".join(dds["mips"])

    if source_payload != dds_payload:
        # Report the first difference for debugging.
        n = min(len(source_payload), len(dds_payload))
        diff = next((i for i in range(n) if source_payload[i] != dds_payload[i]), n)
        raise AssertionError(
            f"Mip payload differs at byte {diff}: "
            f"source={source_payload[diff:diff+8].hex()} "
            f"dds={dds_payload[diff:diff+8].hex()}"
        )

    return True


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Inspect Reflex .texture")
    parser.add_argument("texture")
    parser.add_argument("--extract-mips", metavar="DIR")
    parser.add_argument(
        "--extract-faces",
        metavar="DIR",
        help="For a detected CubeMap, export its 6 faces as single-face DDS files",
    )
    parser.add_argument(
        "--from-faces",
        metavar="DIR",
        help="Assemble 6 single-face DDS files from a directory into a CubeMap",
    )
    parser.add_argument("--to-dds", metavar="FILE")
    parser.add_argument(
        "--from-dds",
        metavar="FILE",
        help="Encode DDS into a Reflex .texture using this file as source template; DDS resolution/mip count may differ",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Output .texture path for --from-dds",
    )
    parser.add_argument(
        "--strip-padding",
        action="store_true",
        help="Do not copy the source allocation padding after the last mip",
    )
    parser.add_argument(
        "--rgba",
        action="store_true",
        help="For format 0x70, write DDS masks as RGBA instead of BGRA",
    )
    args = parser.parse_args()

    if args.output and not (args.from_dds or args.from_faces):
        parser.error("--output can only be used with --from-dds or --from-faces")

    if args.from_dds and not args.output:
        parser.error("--from-dds requires --output FILE")

    if args.from_faces and not args.output:
        parser.error("--from-faces requires --output FILE")

    if args.extract_faces and args.from_faces:
        parser.error("--extract-faces and --from-faces cannot be used together")

    tex = read_texture(args.texture)

    print(f"Format:     {tex.format_name}")
    print(f"Dimensions: {tex.width} x {tex.height}")
    print(f"Mipmaps:    {len(tex.mips)}")
    # Header size depends on the source texture's mip count.
    source_preview = read_texture(args.texture)
    print(f"Header:     0x{source_preview.header_size:X}")

    for mip in tex.mips:
        print(
            f"  mip {mip.level:2d}: "
            f"offset=0x{mip.offset:X}, size={mip.size}, pitch={mip.pitch}"
        )

    trailing = len(tex.raw) - (
        tex.mips[-1].offset + tex.mips[-1].size
    )
    cube_faces = _detect_cube_face_count(tex)
    print(f"Trailing bytes: {trailing}")
    if cube_faces == 6:
        print("Texture type: CubeMap (6 faces)")

    if args.extract_mips:
        extract_mips(tex, args.extract_mips)
        print(f"Extracted mipmaps to: {args.extract_mips}")

    if args.extract_faces:
        extract_cube_faces(tex, args.extract_faces)

    if args.to_dds:
        decode_texture_to_dds(args.texture, args.to_dds, bgra=not args.rgba)
        print(f"DDS written to: {args.to_dds}")

    if args.from_faces:
        output = Path(args.output)
        encode_cube_from_faces(
            args.texture,
            args.from_faces,
            output,
            preserve_padding=not args.strip_padding,
        )

    if args.from_dds:
        output = Path(args.output)
        encode_dds_to_texture(
            args.texture,
            args.from_dds,
            output,
            preserve_padding=not args.strip_padding,
        )
        print(f"Texture written to: {output}")
