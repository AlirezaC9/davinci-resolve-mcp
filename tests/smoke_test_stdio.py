"""Smoke test: start the MCP server over stdio and call a few READ-ONLY tools against the running Resolve.

    uv run python tests/smoke_test_stdio.py            # needs DaVinci Resolve Studio running
    uv run python tests/smoke_test_stdio.py --offline  # only checks that the server starts and lists tools
"""

import json
import os
import subprocess
import sys
import time

OFFLINE = "--offline" in sys.argv
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

proc = subprocess.Popen([sys.executable, "-c", "from davinci_resolve_mcp import main; main()"], cwd=ROOT,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)


def send(msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def recv(id_):
    while True:
        line = proc.stdout.readline()
        if not line:
            raise SystemExit("server exited: " + proc.stderr.read()[-2000:])
        m = json.loads(line)
        if m.get("id") == id_:
            return m


def call(i, name, args):
    t = time.time()
    send({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}})
    res = recv(i).get("result", {})
    kinds = [c["type"] for c in res.get("content", [])]
    text = " ".join(c.get("text", "")[:200].replace("\n", " ") for c in res.get("content", []) if c["type"] == "text")
    print(f"{name:14} {time.time() - t:5.1f}s error={bool(res.get('isError'))} content={kinds} {text}")
    return res


send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}}})
print("server:", recv(1)["result"]["serverInfo"])
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
tools = recv(2)["result"]["tools"]
print("tools:", len(tools))
assert len(tools) > 90, "tool registration incomplete"
if not OFFLINE:
    st = call(3, "status", {})
    if '"timeline"' in json.dumps(st):
        call(4, "timeline_get", {"markers": False})
        call(5, "frame_preview", {"width": 320})
proc.terminate()
print("OK")
