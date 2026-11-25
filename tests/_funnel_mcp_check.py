"""One-off live verification of the restarted funnel server for the MCP
upstream passthrough feature (public URL). Uses the real token file;
prints tool names + outputs, never the token. Does NOT trigger the real
ida_headless autostart (only probes 'ida', which has no start_command).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from live_shell_check import call  # noqa: E402

import os  # noqa: E402
URL = os.environ.get("WINDESKTOPCTL_URL") or (sys.argv[1] if len(sys.argv) > 1 else "")
if not URL:
    sys.exit("usage: _funnel_mcp_check.py https://<machine>.<tailnet>.ts.net/mcp")
TOKEN = (Path.home() / "AppData/Local/windesktopctl/token").read_text().strip()

MCP_TOOLS = {"mcp_servers", "mcp_tools", "mcp_call"}


async def main():
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    # 401 without token
    async with httpx.AsyncClient(timeout=20) as hc:
        r = await hc.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2025-06-18",
                                                "capabilities": {},
                                                "clientInfo": {"name": "t", "version": "0"}}})
        print(f"no-token initialize -> HTTP {r.status_code}")
        assert r.status_code == 401, r.status_code

    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with streamablehttp_client(URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"TOOLS ({len(names)}): {names}")
            assert len(names) == 30, len(names)
            assert MCP_TOOLS <= set(names)

            print("\n== mcp_servers ==")
            print(await call(s, "mcp_servers"))

            print("\n== mcp_tools ida (GUI down -> clean error, no autostart) ==")
            print((await call(s, "mcp_tools", {"server": "ida", "filter": "func"}))[:500])

            print("\n== mcp_call ida survey_binary (expect connect error) ==")
            print((await call(s, "mcp_call",
                              {"server": "ida", "tool": "survey_binary", "args": {}}))[:400])


asyncio.run(main())
