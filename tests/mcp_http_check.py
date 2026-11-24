"""MCP HTTP check: start `deskctl-mcp --http` with DESKCTL_TOKEN, confirm
unauthenticated requests get 401 and a correct Bearer token gets through.
Run:

    .venv\\Scripts\\python tests\\mcp_http_check.py
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / ".venv" / "Scripts" / "deskctl-mcp.exe"
PORT = 18765
TOKEN = "deskctl-check-token"
BASE = f"http://127.0.0.1:{PORT}"

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "deskctl-check", "version": "0"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def main() -> int:
    if not EXE.exists():
        print("deskctl-mcp.exe not found - run `pip install -e .` first")
        return 1
    import httpx

    env = dict(os.environ)
    env["DESKCTL_TOKEN"] = TOKEN
    proc = subprocess.Popen(
        [str(EXE), "--http", "--host", "127.0.0.1", "--port", str(PORT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline and not _port_open():
            if proc.poll() is not None:
                out, err = proc.communicate(timeout=5)
                print("server exited early:\n", err.decode(errors="replace")[:2000])
                return 1
            time.sleep(0.25)
        if not _port_open():
            print("FAIL: server did not start listening")
            proc.terminate()
            return 1

        # 1) no token -> 401
        r = httpx.post(f"{BASE}/mcp", json=INIT, headers=HEADERS, timeout=10)
        print("no token      ->", r.status_code)
        assert r.status_code == 401, r.status_code

        # 2) wrong token -> 401
        r = httpx.post(
            f"{BASE}/mcp", json=INIT,
            headers={**HEADERS, "Authorization": "Bearer wrong"}, timeout=10,
        )
        print("wrong token   ->", r.status_code)
        assert r.status_code == 401, r.status_code

        # 3) correct token -> not 401 (MCP initialize should succeed)
        r = httpx.post(
            f"{BASE}/mcp", json=INIT,
            headers={**HEADERS, "Authorization": f"Bearer {TOKEN}"}, timeout=10,
        )
        print("right token   ->", r.status_code)
        print("body[:200]:", r.text[:200])
        assert r.status_code != 401, "authenticated request still rejected"
        assert r.status_code == 200, r.status_code

        # 4) valid token but evil Host header -> 421 (DNS rebinding protection)
        r = httpx.post(
            f"{BASE}/mcp", json=INIT,
            headers={**HEADERS, "Authorization": f"Bearer {TOKEN}",
                     "Host": "evil.example.com"},
            timeout=10,
        )
        print("evil host     ->", r.status_code)
        assert r.status_code == 421, r.status_code

        print("\nHTTP bearer check PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
