"""Count the tools an assistant sees and measure their descriptions."""
import asyncio, json, tempfile
from mcpc import OUT, session

async def go(s):
    tl = (await s.list_tools()).tools
    rows = []
    for t in tl:
        props = t.inputSchema.get("properties", {})
        untyped = [k for k, v in props.items()
                   if not any(x in v for x in ("type", "anyOf", "$ref", "enum"))]
        rows.append({"name": t.name, "desc_chars": len(t.description or ""),
                     "desc_lines": len((t.description or "").strip().splitlines()),
                     "n_inputs": len(props), "untyped_inputs": untyped})
    return {"n_tools": len(tl), "tools": rows,
            "tool_list_chars": len(json.dumps([t.model_dump(exclude_none=True) for t in tl]))}

r = asyncio.run(session(tempfile.mkdtemp(), go))
(OUT / "tools.json").write_text(json.dumps(r, indent=1))
print(r["n_tools"], "tools; longest description", max(x["desc_chars"] for x in r["tools"]))
