"""Async client for the Bitrix24 REST API over an incoming webhook."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import quote

import httpx

from . import __version__

_WEBHOOK_RE = re.compile(r"^(?P<base>https?://[^/\s]+)/rest/(?P<user>\d+)/(?P<token>[^/\s]+)/?$")

# Bitrix24 answers these when the portal is throttling requests; waiting and retrying helps.
_RETRYABLE_ERRORS = frozenset({"QUERY_LIMIT_EXCEEDED"})
# Only failures before the request reached the portal are retried: repeating a write whose answer
# was lost could create a duplicate comment or task.
_RETRYABLE_TRANSPORT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

BATCH_LIMIT = 50


def php_query(params: dict[str, Any]) -> str:
    """Encode params the way PHP's http_build_query does (``filter[GROUP_ID]=5``), as ``batch`` expects."""
    pairs: list[str] = []

    def add(key: str, value: Any) -> None:
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                add(f"{key}[{sub_key}]", sub_value)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                add(f"{key}[{index}]", item)
        elif value is not None:
            if isinstance(value, bool):
                value = "Y" if value else "N"
            pairs.append(f"{quote(key, safe='[]')}={quote(str(value), safe='')}")

    for key, value in params.items():
        add(str(key), value)
    return "&".join(pairs)


class Bitrix24Error(Exception):
    """Error returned by the Bitrix24 REST API (or a transport failure while calling it)."""

    def __init__(self, code: str, description: str = "", status_code: int | None = None):
        super().__init__(code, description, status_code)
        self.code = code
        self.description = description
        self.status_code = status_code

    def __str__(self) -> str:
        return f"{self.code}: {self.description}" if self.description else self.code


class Bitrix24Client:
    """Calls REST methods through a webhook URL like ``https://portal.bitrix24.ru/rest/1/abc123/``."""

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        match = _WEBHOOK_RE.match(webhook_url.strip())
        if not match:
            raise ValueError(
                "Некорректный адрес вебхука. Ожидается вид https://<портал>/rest/<id пользователя>/<токен>/"
            )
        self.portal_url = match["base"]
        self._user_id = match["user"]
        self._token = match["token"]
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._http = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": f"bitrix24-mcp-server/{__version__}"},
        )

    def _url(self, method: str, api_v3: bool) -> str:
        if api_v3:
            return f"{self.portal_url}/rest/api/{self._user_id}/{self._token}/{method}"
        return f"{self.portal_url}/rest/{self._user_id}/{self._token}/{method}.json"

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***")

    async def call(self, method: str, params: dict[str, Any] | None = None, *, api_v3: bool = False) -> Any:
        """Call a method and return its ``result``."""
        return (await self.call_raw(method, params, api_v3=api_v3)).get("result")

    async def call_raw(
        self, method: str, params: dict[str, Any] | None = None, *, api_v3: bool = False
    ) -> dict[str, Any]:
        """Call a method and return the whole response (``result``, ``next``, ``total``, ...)."""
        url = self._url(method, api_v3)
        for attempt in range(self._max_retries + 1):
            is_last = attempt == self._max_retries
            try:
                response = await self._http.post(url, json=params or {})
            except httpx.TransportError as exc:
                if isinstance(exc, _RETRYABLE_TRANSPORT_ERRORS) and not is_last:
                    await asyncio.sleep(self._retry_delay * 2**attempt)
                    continue
                raise Bitrix24Error("NETWORK_ERROR", self._redact(str(exc) or type(exc).__name__)) from None

            try:
                data = response.json()
            except ValueError:
                raise Bitrix24Error(
                    f"HTTP_{response.status_code}",
                    self._redact(response.text[:300]) or "пустой ответ",
                    response.status_code,
                ) from None

            if isinstance(data, dict) and "error" in data:
                error = data["error"]
                if isinstance(error, dict):  # REST 3.0: {"error": {"code": ..., "message": ...}}
                    code, description = str(error.get("code")), str(error.get("message") or "")
                else:
                    code, description = str(error), str(data.get("error_description") or "")
                if code in _RETRYABLE_ERRORS and not is_last:
                    await asyncio.sleep(self._retry_delay * 2**attempt)
                    continue
                raise Bitrix24Error(code, self._redact(description), response.status_code)
            if response.status_code >= 400 or not isinstance(data, dict):
                raise Bitrix24Error(f"HTTP_{response.status_code}", self._redact(str(data)[:300]), response.status_code)
            return data
        raise AssertionError("unreachable")

    async def batch(
        self, commands: dict[str, tuple[str, dict[str, Any]]]
    ) -> tuple[dict[str, Any], dict[str, Bitrix24Error]]:
        """Run many independent calls, 50 per request. Returns results and errors keyed like ``commands``."""
        results: dict[str, Any] = {}
        errors: dict[str, Bitrix24Error] = {}
        items = list(commands.items())
        for offset in range(0, len(items), BATCH_LIMIT):
            cmd = {
                key: f"{method}?{php_query(params)}" if params else method
                for key, (method, params) in items[offset : offset + BATCH_LIMIT]
            }
            data = await self.call("batch", {"halt": 0, "cmd": cmd}) or {}
            if isinstance(data.get("result"), dict):
                results.update(data["result"])
            if isinstance(data.get("result_error"), dict):
                for key, error in data["result_error"].items():
                    errors[key] = Bitrix24Error(str(error.get("error")), str(error.get("error_description") or ""))
        return results, errors

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        """Download a file by a portal link such as ``DOWNLOAD_URL`` from task.item.getfiles."""
        if url.startswith("/"):
            url = self.portal_url + url
        elif not url.startswith(self.portal_url + "/"):
            raise Bitrix24Error("DOWNLOAD_ERROR", "Ссылка ведёт не на портал вебхука")
        try:
            async with self._http.stream("GET", url, follow_redirects=True) as response:
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise Bitrix24Error("FILE_TOO_LARGE", f"Файл больше {max_bytes} байт")
                    chunks.append(chunk)
        except httpx.TransportError as exc:
            raise Bitrix24Error("NETWORK_ERROR", self._redact(str(exc) or type(exc).__name__)) from None
        content = b"".join(chunks)
        content_type = response.headers.get("content-type", "")
        if response.status_code >= 400 or content_type.startswith("application/json"):
            # Bitrix24 answers a refused download with a small JSON body instead of the file.
            try:
                data = json.loads(content)
            except ValueError:
                data = None
            refused = isinstance(data, dict) and data.get("status") == "error" and "errors" in data
            if response.status_code >= 400 or refused:
                message = self._redact(content[:300].decode("utf-8", "replace"))
                if isinstance(data, dict) and data.get("errors"):
                    message = "; ".join(str(e.get("message")) for e in data["errors"])
                elif isinstance(data, dict) and data.get("error"):
                    message = self._redact(str(data.get("error_description") or data["error"]))
                raise Bitrix24Error("DOWNLOAD_ERROR", message, response.status_code)
        return content

    async def aclose(self) -> None:
        await self._http.aclose()
