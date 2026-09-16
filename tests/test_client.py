import httpx
import pytest

from bitrix24_mcp_server.client import Bitrix24Client, Bitrix24Error

from .conftest import WEBHOOK


@pytest.mark.parametrize(
    "url",
    [
        "https://example.bitrix24.ru/rest/1/abc",
        "https://example.bitrix24.ru/rest/1/abc/",
        "  https://b24.company.local/rest/15/x9y8z7/  ",
    ],
)
def test_accepts_webhook_urls(url):
    assert Bitrix24Client(url).portal_url.startswith("https://")


@pytest.mark.parametrize(
    "url",
    ["", "https://example.bitrix24.ru/", "https://example.bitrix24.ru/rest/abc/", "example.bitrix24.ru/rest/1/abc/"],
)
def test_rejects_malformed_webhook_urls(url):
    with pytest.raises(ValueError):
        Bitrix24Client(url)


async def test_call_posts_json_to_method_url(bitrix):
    bitrix.on("crm.item.get", {"item": {"id": 5}})
    result = await bitrix.client().call("crm.item.get", {"entityTypeId": 2, "id": 5})
    assert result == {"item": {"id": 5}}
    assert bitrix.calls == [("crm.item.get", {"entityTypeId": 2, "id": 5})]


async def test_rest_v3_methods_use_api_prefix():
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"result": {"result": True}})

    client = Bitrix24Client(WEBHOOK, transport=httpx.MockTransport(handle))
    await client.call("tasks.task.chat.message.send", {}, api_v3=True)
    await client.call("user.current")
    assert seen == [
        "https://example.bitrix24.ru/rest/api/7/s3cr3t-token/tasks.task.chat.message.send",
        "https://example.bitrix24.ru/rest/7/s3cr3t-token/user.current.json",
    ]


async def test_api_error_is_raised_with_code_and_description(bitrix):
    bitrix.on("crm.item.get", handler=lambda _: {"error": "NOT_FOUND", "error_description": "Element not found"})
    with pytest.raises(Bitrix24Error) as exc_info:
        await bitrix.client().call("crm.item.get", {"id": 1})
    assert exc_info.value.code == "NOT_FOUND"
    assert str(exc_info.value) == "NOT_FOUND: Element not found"


async def test_retries_when_portal_throttles(bitrix):
    answers = iter([{"error": "QUERY_LIMIT_EXCEEDED"}, {"error": "QUERY_LIMIT_EXCEEDED"}, {"result": 42}])
    bitrix.on("user.current", handler=lambda _: next(answers))
    assert await bitrix.client().call("user.current") == 42
    assert len(bitrix.called("user.current")) == 3


async def test_gives_up_after_max_retries(bitrix):
    bitrix.on("user.current", handler=lambda _: {"error": "QUERY_LIMIT_EXCEEDED"})
    with pytest.raises(Bitrix24Error, match="QUERY_LIMIT_EXCEEDED"):
        await bitrix.client(max_retries=2).call("user.current")
    assert len(bitrix.called("user.current")) == 3


async def test_errors_never_leak_the_webhook_token():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"proxy failed for {request.url}")

    client = Bitrix24Client(WEBHOOK, transport=httpx.MockTransport(handle))
    with pytest.raises(Bitrix24Error) as exc_info:
        await client.call("user.current")
    assert exc_info.value.code == "HTTP_500"
    assert "s3cr3t-token" not in str(exc_info.value)


async def test_network_errors_are_retried_then_reported():
    attempts = []

    def handle(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    client = Bitrix24Client(WEBHOOK, transport=httpx.MockTransport(handle), max_retries=1, retry_delay=0)
    with pytest.raises(Bitrix24Error, match="NETWORK_ERROR"):
        await client.call("user.current")
    assert len(attempts) == 2


async def test_lost_answers_are_not_retried():
    attempts = []

    def handle(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("no answer", request=request)

    client = Bitrix24Client(WEBHOOK, transport=httpx.MockTransport(handle), max_retries=3, retry_delay=0)
    with pytest.raises(Bitrix24Error, match="NETWORK_ERROR"):
        await client.call("task.commentitem.add", {"TASKID": 1})
    assert len(attempts) == 1
