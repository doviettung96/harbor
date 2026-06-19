from __future__ import annotations

import sys
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


EXPECTED_TOOLS = {
    "list_projects",
    "list_tasks",
    "get_task",
    "move_task",
    "get_transition_status",
    "check_conflicts",
    "get_notifications",
    "read_pane_content",
    "send_to_task",
    "create_task",
    "create_tasks_batch",
    "update_task",
    "delete_task",
    "list_resources",
    "acquire_runtime",
    "release_runtime",
}


async def _main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "harbor", "mcp-serve"],
        cwd=str(repo_root),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    if names != EXPECTED_TOOLS:
        print("MCP tool list mismatch", file=sys.stderr)
        print(f"expected: {sorted(EXPECTED_TOOLS)}", file=sys.stderr)
        print(f"actual:   {sorted(names)}", file=sys.stderr)
        return 1
    print("mcp tools/list ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(anyio.run(_main))
