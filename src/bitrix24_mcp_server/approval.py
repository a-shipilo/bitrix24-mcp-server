"""User approval for write operations.

Every create/update/delete goes through :class:`ApprovalGate`, which gets the user's consent
in one of two ways:

* **elicitation** – the server asks the MCP client to show a confirmation dialog and waits for
  the answer (supported by Claude Code);
* **token** – the tool does nothing yet: it returns a preview and a one-time ``confirmation_id``.
  The assistant shows the preview to the user, and the operation runs only when
  ``confirm_action`` is called with that id after the user has agreed. Used by clients
  without elicitation, e.g. Claude Desktop.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.types import ClientCapabilities, ElicitationCapability
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ApprovalMode = Literal["auto", "elicitation", "token"]
APPROVAL_MODES: tuple[ApprovalMode, ...] = ("auto", "elicitation", "token")

Operation = Callable[[], Awaitable[Any]]

TOKEN_INSTRUCTIONS = (
    "Операция ЕЩЁ НЕ ВЫПОЛНЕНА. Покажите пользователю preview дословно и спросите, выполнить ли её. "
    "Вызывайте confirm_action с этим confirmation_id только после явного согласия пользователя "
    "в ответ на этот вопрос. Не подтверждайте операцию самостоятельно. "
    "Если пользователь отказался, вызовите cancel_action."
)


class Approval(BaseModel):
    approve: bool = Field(default=True, title="Выполнить операцию", description="Снимите отметку, чтобы отказаться")


@dataclass
class PendingOperation:
    summary: str
    run: Operation
    created_at: float


class ApprovalGate:
    def __init__(
        self,
        mode: ApprovalMode = "auto",
        *,
        ttl_seconds: float = 900,
        max_pending: int = 50,
        clock: Callable[[], float] = time.monotonic,
    ):
        if mode not in APPROVAL_MODES:
            raise ValueError(f"Неизвестный режим подтверждения {mode!r}, допустимо: {', '.join(APPROVAL_MODES)}")
        self.mode = mode
        self.ttl_seconds = ttl_seconds
        self.max_pending = max_pending
        self._clock = clock
        self._pending: dict[str, PendingOperation] = {}

    async def request(self, ctx: Context | None, summary: str, run: Operation) -> dict[str, Any]:
        """Run ``run`` once the user approves ``summary``; otherwise return why it did not run."""
        if self.mode != "token":
            if ctx is not None and _supports_elicitation(ctx):
                try:
                    answer = await ctx.elicit(message=summary, schema=Approval)
                except McpError as exc:
                    if self.mode == "elicitation":
                        raise ToolError(f"Клиент не смог показать запрос подтверждения: {exc}") from exc
                    logger.warning("Elicitation failed, falling back to confirmation token: %s", exc)
                else:
                    if answer.action == "accept" and answer.data.approve:
                        return {"status": "done", "result": await run()}
                    return {
                        "status": "rejected",
                        "message": "Пользователь не подтвердил операцию, она не выполнена.",
                    }
            elif self.mode == "elicitation":
                raise ToolError(
                    "Клиент не поддерживает запрос подтверждения (elicitation). "
                    "Установите BITRIX24_CONFIRM_MODE=auto или token."
                )
        return self._defer(summary, run)

    def _defer(self, summary: str, run: Operation) -> dict[str, Any]:
        self._purge_expired()
        while len(self._pending) >= self.max_pending:
            self._pending.pop(next(iter(self._pending)))
        confirmation_id = secrets.token_hex(4)
        self._pending[confirmation_id] = PendingOperation(summary, run, self._clock())
        return {
            "status": "confirmation_required",
            "confirmation_id": confirmation_id,
            "preview": summary,
            "expires_in_minutes": int(self.ttl_seconds // 60),
            "instructions": TOKEN_INSTRUCTIONS,
        }

    async def confirm(self, confirmation_id: str) -> dict[str, Any]:
        self._purge_expired()
        operation = self._pending.pop(confirmation_id.strip(), None)
        if operation is None:
            raise ToolError(
                "Операция с таким confirmation_id не найдена: она уже выполнена, отменена или устарела. "
                "Подготовьте её заново."
            )
        return {"status": "done", "operation": operation.summary, "result": await operation.run()}

    def cancel(self, confirmation_id: str) -> dict[str, Any]:
        operation = self._pending.pop(confirmation_id.strip(), None)
        if operation is None:
            return {"status": "not_found", "message": "Такой ожидающей операции нет."}
        return {"status": "cancelled", "operation": operation.summary}

    def _purge_expired(self) -> None:
        deadline = self._clock() - self.ttl_seconds
        for key in [k for k, op in self._pending.items() if op.created_at < deadline]:
            del self._pending[key]


class ApprovalPolicy:
    """Approval for a group of tools that the user may exempt from confirmation (e.g. tasks)."""

    NOTE = " Требует подтверждения пользователя."

    def __init__(self, gate: ApprovalGate, *, required: bool = True):
        self.gate = gate
        self.required = required

    def describe(self, description: str) -> str:
        return description + self.NOTE if self.required else description

    async def request(self, ctx: Context, summary: str, run: Operation) -> dict[str, Any]:
        if self.required:
            return await self.gate.request(ctx, summary, run)
        return {"status": "done", "result": await run()}


def _supports_elicitation(ctx: Context) -> bool:
    try:
        session = ctx.session
    except ValueError:  # no active request
        return False
    return session.check_client_capability(ClientCapabilities(elicitation=ElicitationCapability()))
