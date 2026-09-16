"""Tool input schemas that MCP clients with strict schema converters accept.

Pydantic writes optional parameters as ``{"anyOf": [{"type": "integer"}, {"type": "null"}], "default": null}``.
Claude Desktop's bridge for local servers turns every property that has a ``default`` into a mandatory one,
so a call that leaves such a parameter out is rejected before it reaches the server. Optional parameters
are therefore published the way TypeScript servers publish them: a plain type that is simply not listed
in ``required``. Defaults move into the description; pydantic still applies them when the tool runs.
"""

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import Tool


def plain_property(prop: dict[str, Any]) -> dict[str, Any]:
    prop = {k: v for k, v in prop.items() if k != "title"}
    variants = prop.pop("anyOf", None)
    if variants is not None:
        non_null = [v for v in variants if v.get("type") != "null"]
        if len(non_null) == 1:
            prop = {**non_null[0], **prop}
        else:
            prop["anyOf"] = non_null
    if "default" in prop:
        default = prop.pop("default")
        description = prop.get("description", "")
        if default is not None and "по умолчанию" not in description.lower():
            note = f"По умолчанию {json.dumps(default, ensure_ascii=False)}."
            prop["description"] = f"{description.rstrip('.')}. {note}" if description else note
    return prop


def plain_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "object",
        "properties": {name: plain_property(prop) for name, prop in schema.get("properties", {}).items()},
    }
    if schema.get("required"):
        result["required"] = schema["required"]
    if "$defs" in schema:
        result["$defs"] = schema["$defs"]
    return result


class PlainSchemaFastMCP(FastMCP):
    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        for tool in tools:
            tool.inputSchema = plain_input_schema(tool.inputSchema)
        return tools
