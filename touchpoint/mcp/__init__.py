"""Touchpoint MCP server.

Exposes Touchpoint's UI-automation API as an MCP (Model Context
Protocol) tool server, ready for use from Claude Desktop, Cursor,
Copilot agents, or any MCP-compatible client.

Install::

    pip install touchpoint-py

Run::

    touchpoint-mcp          # stdio transport (default)
    python -m touchpoint.mcp.server
"""
