"""Tiny FastMCP server for upstream integration tests.

Usage:
    python _tiny_mcp_server.py            # stdio
    python _tiny_mcp_server.py --http --port 18777
"""

import argparse
import struct
import zlib

from mcp.server.fastmcp import FastMCP, Image

_ap = argparse.ArgumentParser()
_ap.add_argument("--http", action="store_true")
_ap.add_argument("--port", type=int, default=18777)
_args = _ap.parse_args()

_kwargs = {"host": "127.0.0.1", "port": _args.port} if _args.http else {}
mcp = FastMCP("tiny-test", **_kwargs)


@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo:{text}"


def _png_bytes() -> bytes:
    # 1x1 red PNG
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\x00\x00"
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@mcp.tool()
def make_image() -> Image:
    """Return a tiny PNG image."""
    return Image(data=_png_bytes(), format="png")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


if __name__ == "__main__":
    mcp.run(transport="streamable-http" if _args.http else "stdio")
