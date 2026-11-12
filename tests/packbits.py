"""TIFF PackBits decoded, as a printer does with a compressed raster line
(the QL and P-touch tests read back what the backends send)."""

from __future__ import annotations


def unpackbits(data: bytes) -> bytes:
    """The inverse of packbits(); ValueError for data cut off in the middle."""
    out = bytearray()
    i = 0
    while i < len(data):
        header = data[i]
        if header < 128:
            chunk = data[i + 1 : i + 2 + header]
            if len(chunk) != header + 1:
                raise ValueError("PackBits literal cut off")
            out += chunk
            i += header + 2
        elif header > 128:
            if i + 1 >= len(data):
                raise ValueError("PackBits run cut off")
            out += bytes((data[i + 1],)) * (257 - header)
            i += 2
        else:  # 128: no operation
            i += 1
    return bytes(out)
