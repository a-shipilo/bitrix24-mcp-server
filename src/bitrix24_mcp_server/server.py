"""Server entry point: configuration from environment variables and tool registration."""

import argparse
import logging
import os
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .approval import APPROVAL_MODES, ApprovalGate, ApprovalPolicy
from .client import Bitrix24Client
from .crm import register_crm_tools
from .projects import register_project_tools
from .tasks import register_task_tools

INSTRUCTIONS = """\
Сервер работает с CRM, задачами, проектами и скрамом Битрикс24 через входящий вебхук.

CRM (crm_*): поля в camelCase (title, stageId, opportunity, assignedById, ufCrm...).
Названия полей и варианты списков — crm_fields, стадии сделок и статусы лидов — crm_stages.
Задачи (task_*, tasks_list): поля запросов в UPPER_CASE (TITLE, RESPONSIBLE_ID), ответы в camelCase.
ID сотрудников — users_search и user_current.
Проекты и скрамы — projects_list. Канбан проекта — project_board, перенос по стадиям — task_move_stage.
Скрам: спринты — scrum_sprints, доска спринта — sprint_board, перенос — sprint_move_task, бэклог — scrum_backlog.

Любое создание, изменение или удаление требует согласия пользователя.
Если инструмент вернул status=confirmation_required, операция ещё не выполнена:
покажите пользователю preview и вызывайте confirm_action только после его явного согласия.
Никогда не подтверждайте операцию сами. При отказе вызовите cancel_action.
"""

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", "нет"}


def create_server(
    client: Bitrix24Client | None,
    gate: ApprovalGate,
    *,
    confirm_tasks: bool = True,
    config_error: str | None = None,
) -> FastMCP:
    mcp = FastMCP("bitrix24", instructions=INSTRUCTIONS, log_level="WARNING")

    def get_client() -> Bitrix24Client:
        if client is None:
            raise ToolError(config_error or "Клиент Битрикс24 не настроен")
        return client

    register_crm_tools(mcp, get_client, gate)
    task_approval = ApprovalPolicy(gate, required=confirm_tasks)
    register_task_tools(mcp, get_client, task_approval)
    register_project_tools(mcp, get_client, task_approval)
    _register_approval_tools(mcp, gate)
    return mcp


ConfirmationId = Annotated[str, Field(description="confirmation_id из ответа инструмента", min_length=1)]


def _register_approval_tools(mcp: FastMCP, gate: ApprovalGate) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
    async def confirm_action(confirmation_id: ConfirmationId) -> dict[str, Any]:
        """Выполнить подготовленную операцию. Вызывайте ТОЛЬКО после того, как пользователь увидел
        preview и явно согласился выполнить именно эту операцию."""
        return await gate.confirm(confirmation_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    def cancel_action(confirmation_id: ConfirmationId) -> dict[str, Any]:
        """Отменить подготовленную операцию, если пользователь отказался."""
        return gate.cancel(confirmation_id)


def build_from_env() -> FastMCP:
    mode = os.environ.get("BITRIX24_CONFIRM_MODE", "auto").strip().lower() or "auto"
    if mode not in APPROVAL_MODES:
        raise SystemExit(f"BITRIX24_CONFIRM_MODE: допустимые значения — {', '.join(APPROVAL_MODES)}")

    client: Bitrix24Client | None = None
    config_error: str | None = None
    webhook_url = os.environ.get("BITRIX24_WEBHOOK_URL", "").strip()
    if not webhook_url:
        config_error = (
            "Не задан BITRIX24_WEBHOOK_URL. Добавьте адрес входящего вебхука в блок env "
            "настроек MCP-сервера и перезапустите клиент."
        )
    else:
        try:
            client = Bitrix24Client(webhook_url)
        except ValueError as exc:
            config_error = f"BITRIX24_WEBHOOK_URL: {exc}"
    if config_error:
        # Keep serving so the assistant can tell the user what is wrong.
        logger.error(config_error)

    return create_server(
        client,
        ApprovalGate(mode),  # type: ignore[arg-type]
        confirm_tasks=_env_flag("BITRIX24_CONFIRM_TASKS", True),
        config_error=config_error,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="bitrix24-mcp-server",
        description="MCP-сервер для CRM и задач Битрикс24 (stdio). Настройка через переменные окружения "
        "BITRIX24_WEBHOOK_URL, BITRIX24_CONFIRM_MODE, BITRIX24_CONFIRM_TASKS.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    build_from_env().run("stdio")


__all__ = ["create_server", "build_from_env", "main"]
