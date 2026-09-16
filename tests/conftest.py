import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from bitrix24_mcp_server.approval import ApprovalGate
from bitrix24_mcp_server.client import Bitrix24Client
from bitrix24_mcp_server.server import create_server

WEBHOOK = "https://example.bitrix24.ru/rest/7/s3cr3t-token/"

Handler = Callable[[dict[str, Any]], Any]


class FakeBitrix:
    """In-process stand-in for a Bitrix24 portal: maps REST method names to handlers."""

    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.batches: list[dict[str, str]] = []
        self.files: dict[str, tuple[int, str, bytes]] = {}
        self.downloads: list[str] = []

    def serve_file(self, attached_id: int, content: bytes, *, status: int = 200, content_type: str = "text/plain"):
        self.files[str(attached_id)] = (status, content_type, content)

    def file_link(self, attached_id: int) -> str:
        return f"/bitrix/tools/disk/uf.php?attachedId={attached_id}&auth%5Bap%5D=s3cr3t-token&action=download&ncc=1"

    def on(self, method: str, result: Any = None, *, handler: Handler | None = None, **extra: Any) -> None:
        self.handlers[method] = handler or (lambda _params: {"result": result, **extra})

    def called(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == method]

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/bitrix/tools/disk/uf.php":
                attached_id = request.url.params["attachedId"]
                self.downloads.append(attached_id)
                status, content_type, content = self.files[attached_id]
                return httpx.Response(status, content=content, headers={"content-type": content_type})
            method = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
            if "/rest/api/" in request.url.path:
                method = f"v3:{method}"
            params = json.loads(request.content or b"{}")
            if method == "batch":
                return httpx.Response(200, json=self._batch(params["cmd"]))
            body = self._dispatch(method, params)
            status = (404 if body["error"] == "ERROR_METHOD_NOT_FOUND" else 400) if "error" in body else 200
            return httpx.Response(status, json=body)

        return httpx.MockTransport(handle)

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        handler = self.handlers.get(method)
        if handler is None:
            return {"error": "ERROR_METHOD_NOT_FOUND", "error_description": method}
        return handler(params)

    def _batch(self, commands: dict[str, str]) -> dict[str, Any]:
        self.batches.append(commands)
        results, errors = {}, {}
        for key, command in commands.items():
            method, _, query = command.partition("?")
            body = self._dispatch(method, dict(parse_qsl(query)))
            if "error" in body:
                errors[key] = body
            else:
                results[key] = body["result"]
        return {"result": {"result": results or [], "result_error": errors or []}}

    def client(self, **kwargs: Any) -> Bitrix24Client:
        return Bitrix24Client(WEBHOOK, transport=self.transport(), retry_delay=0, **kwargs)


@pytest.fixture
def bitrix() -> FakeBitrix:
    return FakeBitrix()


@pytest.fixture
def connect(bitrix: FakeBitrix):
    @asynccontextmanager
    async def _connect(
        *, mode: str = "auto", confirm_tasks: bool = True, auto_approve: tuple[str, ...] = (), elicitation_callback=None
    ):
        server = create_server(
            bitrix.client(), ApprovalGate(mode), confirm_tasks=confirm_tasks, auto_approve=auto_approve
        )
        async with create_connected_server_and_client_session(
            server, elicitation_callback=elicitation_callback
        ) as session:
            yield session

    return _connect


def payload(result) -> Any:
    """Structured tool output; non-object return values are wrapped as {"result": ...}."""
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data
