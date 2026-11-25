"""One-off live verification of the restarted funnel server (public URL).
Uses the real token file; prints tool names + tool outputs, never the token.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from live_shell_check import call  # noqa: E402

import os  # noqa: E402
URL = os.environ.get("WINDESKTOPCTL_URL") or (sys.argv[1] if len(sys.argv) > 1 else "")
if not URL:
    sys.exit("usage: _funnel_live_check.py https://<machine>.<tailnet>.ts.net/mcp (or set WINDESKTOPCTL_URL)")
TOKEN = (Path.home() / "AppData/Local/windesktopctl/token").read_text().strip()

NEW = {"exec","job_output","job_kill","job_list","read_file","write_file",
       "edit_file","list_dir","glob","grep","file_info","download_chunk","upload_chunk"}


async def main():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with streamablehttp_client(URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"TOOLS ({len(names)}):")
            for n in names:
                print("  ", n)
            print("new-tool count:", len(set(names) & NEW))

            print("\n== exec whoami ==")
            print((await call(s, "exec", {"command": "whoami", "shell": "cmd"}))[:400])

            print("\n== read_file README (first 5 lines) ==")
            print((await call(s, "read_file", {
                "path": r"C:\Users\funkm\repo\windesktopctl\README.md",
                "offset": 1, "limit": 5}))[:800])

            print("\n== list_dir POE2Stuff (first entries) ==")
            out = await call(s, "list_dir", {
                "path": r"C:\Users\funkm\repo\POE2Stuff", "max_entries": 12})
            print("\n".join(out.splitlines()[:14]))

            print("\n== scratch write/edit/delete ==")
            sp = r"C:\Users\funkm\repo\windesktopctl\.scratch_test.txt"
            print(await call(s, "write_file", {"path": sp, "text": "scratch-one\nscratch-two\n"}))
            print(await call(s, "edit_file", {"path": sp, "old": "scratch-two", "new": "scratch-TWO"}))
            print(await call(s, "read_file", {"path": sp, "offset": 2, "limit": 1}))
            print(await call(s, "exec", {
                "command": f'del "{sp}"', "shell": "cmd"}))


asyncio.run(main())
