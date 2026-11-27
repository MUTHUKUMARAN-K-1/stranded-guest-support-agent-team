"""Print the actual tools/list response, without touching the booking database."""

import asyncio
import json

from fastmcp import Client

from .server import mcp


async def main():
    async with Client(mcp) as client:
        tools = await client.list_tools()
        print(f"Discovered {len(tools)} tools:")
        for tool in tools:
            print(f"- {tool.name}")
        print("\nDescriptions and input schemas:")
        print(
            json.dumps(
                [tool.model_dump(mode="json", exclude_none=True) for tool in tools], indent=2
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
