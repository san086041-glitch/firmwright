"""测试用的最小 MCP 服务器（stdio）：两个工具 echo（只读）和 add。用法：python fake_mcp_server.py [工具数量]

工具数量大于 2 时额外生成 dummy_N 工具，用来测"工具多时改用 search_tool / use_tool"。
"""

import json
import sys

N = int(sys.argv[1]) if len(sys.argv) > 1 else 2

TOOLS = [
    {"name": "echo", "description": "Echo back the given text. 原样返回文字。",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "add", "description": "Add two integers and return the sum.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}}},
] + [{"name": f"dummy_{i}", "description": f"Dummy tool number {i} for padding the list.",
      "inputSchema": {"type": "object", "properties": {}}} for i in range(max(0, N - 2))]


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if rid is None:
        continue  # 通知
    if method == "initialize":
        send({"id": rid, "result": {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                                    "serverInfo": {"name": "fake-mcp", "version": "0.0.1"}}})
    elif method == "tools/list":
        send({"id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name, args = params["name"], params.get("arguments") or {}
        if name == "echo":
            send({"id": rid, "result": {"content": [{"type": "text", "text": args.get("text", "")}]}})
        elif name == "add":
            send({"id": rid, "result": {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}})
        else:
            send({"id": rid, "result": {"content": [{"type": "text", "text": "unknown"}], "isError": True}})
    else:
        send({"id": rid, "error": {"code": -32601, "message": "method not found"}})
