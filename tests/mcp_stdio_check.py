"""MCP-level check over stdio: spawn windesktopctl-mcp, list tools, call windows,
observe, screenshot (expect image content back). Run:

    .venv\\Scripts\\python tests\\mcp_stdio_check.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / ".venv" / "Scripts" / "windesktopctl-mcp.exe"


async def main() -> int:
    if not EXE.exists():
        print("windesktopctl-mcp.exe not found - run `pip install -e .` first")
        return 1
    params = StdioServerParameters(command=str(EXE), args=[])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("tools:", names)
            expected = {"observe", "screenshot", "act", "windows", "focus",
                        "wait_for", "window", "launch", "browser", "challenge",
                        "challenge_answer", "pause", "resume", "status"}
            if len(names) != len(expected):
                print(f"note: {len(names)} tools registered (expected {len(expected)})")
            missing = expected - set(names)
            if missing:
                print("MISSING TOOLS:", missing)
                return 1

            r = await session.call_tool("windows", {})
            print("\n--- windows ---")
            for c in r.content:
                print(getattr(c, "text", c))

            r = await session.call_tool("observe", {"window": "focused", "max_elements": 40})
            print("\n--- observe ---")
            for c in r.content:
                print(getattr(c, "text", c))

            r = await session.call_tool(
                "wait_for", {"cond": "changed", "timeout": 1.0, "window": "focused"}
            )
            print("\n--- wait_for(changed,1s) ---")
            for c in r.content:
                print(getattr(c, "text", c))

            r = await session.call_tool(
                "screenshot", {"target": "focused", "max_width": 640, "marks": True}
            )
            print("\n--- screenshot ---")
            kinds = []
            for c in r.content:
                kinds.append(getattr(c, "type", "?"))
                if getattr(c, "type", "") == "text":
                    print("info:", c.text)
                elif getattr(c, "type", "") == "image":
                    print(f"image: mimeType={c.mimeType} b64_len={len(c.data)}")
            print("content types:", kinds)
            if "image" not in kinds:
                print("FAIL: no image content in screenshot result")
                return 1
            print("\nMCP stdio check PASSED")
            return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
