import asyncio
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from guest_support.server import create_server

EXPECTED_TOOLS = {
    "get_reservation",
    "search_available_homes",
    "hold_booking",
    "issue_refund",
    "message_host",
    "handoff_to_safety_agent",
}


def test_real_mcp_discovery_and_all_six_calls(store):
    async def exercise():
        async with Client(create_server(store)) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == EXPECTED_TOOLS
            for tool in tools:
                assert "safety" in tool.description.lower()
                assert "reservation_id" in tool.inputSchema["properties"]
            looked_up = await client.call_tool(
                "get_reservation", {"reservation_id": "RES-2026-10001"}
            )
            assert looked_up.data["nights"] == 3
            searched = await client.call_tool(
                "search_available_homes", {"reservation_id": "RES-2026-10001"}
            )
            home = searched.data["homes"][0]
            message = await client.call_tool(
                "message_host",
                {
                    "reservation_id": "RES-2026-10001",
                    "message": "Please confirm access.",
                    "request_id": "mcp-smoke-1",
                    "listing_id": home["listing_id"],
                },
            )
            assert message.data["status"] == "recorded_locally"
            held = await client.call_tool(
                "hold_booking",
                {
                    "reservation_id": "RES-2026-10001",
                    "listing_id": home["listing_id"],
                    "guest_accepted": True,
                },
            )
            assert held.data["status"] == "on_hold"
            refunded = await client.call_tool(
                "issue_refund",
                {
                    "reservation_id": "RES-2026-10004",
                    "amount_eur": "168.00",
                    "reason": "guest_declined_rebooking",
                },
            )
            assert refunded.data["amount_eur"] == "168.00"
            handed_off = await client.call_tool(
                "handoff_to_safety_agent",
                {
                    "reservation_id": "RES-2026-10007",
                    "conditions": ["injury_or_medical_need"],
                    "note": "Guest reports bleeding and can hardly stand.",
                },
            )
            assert handed_off.data["live_agent_connected"] is False
            with pytest.raises(ToolError, match="SAFETY_HANDOFF_REQUIRED"):
                await client.call_tool(
                    "issue_refund",
                    {
                        "reservation_id": "RES-2026-10007",
                        "amount_eur": "1.00",
                        "reason": "partial_refund",
                    },
                )
            with pytest.raises(ToolError):
                await client.call_tool(
                    "hold_booking",
                    {
                        "reservation_id": "RES-2026-10001",
                        "listing_id": "LST-200001",
                        "guest_accepted": True,
                    },
                )

    asyncio.run(exercise())


def test_stdio_entry_point_discovery():
    async def exercise():
        async with Client(Path(__file__).resolve().parents[1] / "server.py") as client:
            assert {tool.name for tool in await client.list_tools()} == EXPECTED_TOOLS

    asyncio.run(exercise())
