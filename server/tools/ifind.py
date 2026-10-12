"""Optional MCP transport; only explicitly allowlisted read-only tools may run."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from mcp import ClientSession

from server.config import Settings, endpoint_is_safe
from server.tools.fuyao import redact


class IfindClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @asynccontextmanager
    async def session(self):
        settings = self.settings
        if not endpoint_is_safe(settings.ifind_mcp_url):
            raise ValueError("invalid_ifind_endpoint")
        headers = {}
        if settings.ifind_api_key:
            headers[settings.ifind_auth_header] = (settings.ifind_auth_prefix + " " + settings.ifind_api_key).strip()
        async with asyncio.timeout(settings.timeout_seconds):
            if settings.ifind_transport == "streamable_http":
                # Installed MCP 2.x uses httpx2 and yields two transport streams.
                import httpx2
                from mcp.client.streamable_http import streamable_http_client
                async with httpx2.AsyncClient(headers=headers, timeout=settings.timeout_seconds) as client:
                    async with streamable_http_client(settings.ifind_mcp_url, http_client=client) as streams:
                        async with ClientSession(*streams[:2]) as session:
                            await session.initialize()
                            yield session
            elif settings.ifind_transport == "sse":
                from mcp.client.sse import sse_client
                async with sse_client(settings.ifind_mcp_url, headers=headers, timeout=settings.timeout_seconds) as streams:
                    async with ClientSession(*streams[:2]) as session:
                        await session.initialize()
                        yield session
            else:
                raise ValueError("unsupported_ifind_transport")

    async def discover(self) -> list[dict]:
        import mcp.types as types
        tools, cursor = [], None
        async with self.session() as session:
            for _ in range(5):
                page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
                tools.extend(tool.model_dump(mode="json", by_alias=True) for tool in page.tools)
                cursor = page.nextCursor
                if not cursor:
                    break
            if cursor or len(tools) > 100:
                raise ValueError("mcp_discovery_limit")
        return redact(tools, self.settings.secrets)

    async def call(self, name: str, arguments: dict) -> dict:
        async with self.session() as session:
            result = await session.call_tool(name, arguments)
            return redact(result.model_dump(mode="json", by_alias=True), self.settings.secrets)
