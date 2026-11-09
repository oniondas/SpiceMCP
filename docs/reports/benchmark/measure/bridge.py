"""Lets a model that only has a shell drive the real SpiceMCP server over MCP.

    python bridge.py serve <project_dir> <log.json> [port]   # keeps one MCP session open
    python bridge.py tools                                   # the tool list the model sees
    python bridge.py call <tool> '<json args>'               # one tool call, answer printed

The server runs exactly as a desktop client would start it (stdio, srv.py routes
simulations to ngspice), so long calls answer "still running" after 50 s as usual.
Every request and answer is logged with its time for token counting."""
import asyncio, json, os, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
PORT = int(os.environ.get("BRIDGE_PORT", "8765"))


async def serve(project, log_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    Path(project).mkdir(parents=True, exist_ok=True)
    p = StdioServerParameters(command=sys.executable, args=[str(HERE / "srv.py")],
                              env={**os.environ, "SPICEMCP_PROJECT": str(project),
                                   "PYTHONPATH": str(REPO)}, cwd=str(project))
    log = {"calls": []}
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tl = (await s.list_tools()).tools
            log["surface"] = {"instructions": init.instructions, "tools": [
                {"name": t.name, "description": t.description,
                 "input_schema": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)}
                for t in tl]}
            Path(log_path).write_text(json.dumps(log, indent=1, default=str))
            lock = asyncio.Lock()

            async def handle(reader, writer):
                req = json.loads((await reader.read(10_000_000)).decode())
                async with lock:
                    if req["tool"] == "__tools__":
                        ans = json.dumps(log["surface"], indent=1, default=str)
                    else:
                        t = time.time()
                        try:
                            res = await s.call_tool(req["tool"], req["args"])
                            ans = "".join(c.text for c in res.content if hasattr(c, "text"))
                        except Exception as e:
                            ans = json.dumps({"error": repr(e)})
                        log["calls"].append({"tool": req["tool"], "args": req["args"], "answer": ans,
                                             "secs": round(time.time() - t, 1), "at": t})
                        Path(log_path).write_text(json.dumps(log, indent=1, default=str))
                writer.write(ans.encode())
                await writer.drain()
                writer.close()

            srv = await asyncio.start_server(handle, "127.0.0.1", PORT)
            print("ready", flush=True)
            async with srv:
                await srv.serve_forever()


def send(tool, args):
    import socket
    s = socket.create_connection(("127.0.0.1", PORT))
    s.sendall(json.dumps({"tool": tool, "args": args}).encode())
    s.shutdown(socket.SHUT_WR)
    out = b""
    while chunk := s.recv(65536):
        out += chunk
    print(out.decode())


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "serve":
        asyncio.run(serve(sys.argv[2], sys.argv[3]))
    elif cmd == "tools":
        send("__tools__", {})
    else:
        send(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {})
