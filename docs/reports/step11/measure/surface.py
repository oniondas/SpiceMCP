"""Tokens re-sent every turn (instructions + tool list), the way step 9 counted them.

    PYTHONPATH=<repo> TOKENIZER_JSON=... python surface.py"""
import asyncio
import json
import os

from tokenizers import Tokenizer

from spicemcp import server

T = Tokenizer.from_file(os.environ["TOKENIZER_JSON"])


async def main():
    tl = await server.mcp.list_tools()
    tools = [{"name": t.name, "description": t.description, "input_schema": t.inputSchema}
             for t in tl]
    n = lambda s: len(T.encode(s).ids)
    print(json.dumps({"tools": len(tools), "instructions": n(server.INSTRUCTIONS),
                      "tool_list": n(json.dumps(tools)),
                      "per_tool": {t["name"]: n(json.dumps(t)) for t in tools}}))

asyncio.run(main())
