"""Minimal MCP stdio client: every check talks to SpiceMCP the way an assistant does,
by launching `python -m spicemcp.server` and calling its tools over MCP."""
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parents[1] / "results"
OUT.mkdir(exist_ok=True)


async def session(project, fn):
    p = StdioServerParameters(command=sys.executable, args=["-m", "spicemcp.server"],
                              env={**os.environ, "SPICEMCP_PROJECT": str(project),
                                   "PYTHONPATH": str(ROOT)}, cwd=str(ROOT))
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


async def call(s, name, **args):
    res = await s.call_tool(name, args)
    txt = res.content[0].text if res.content else "{}"
    try:
        return json.loads(txt)
    except ValueError:
        return {"raw": txt}
