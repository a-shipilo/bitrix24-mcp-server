"""Task, checklist and user tools."""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .approval import ApprovalPolicy
from .client import Bitrix24Client, Bitrix24Error
from .crm import PAGE_SIZE, compact
from .preview import build_summary, change_lines, field_lines, quoted

TASK_STATUSES = {
    "1": "Новая",
    "2": "Ждёт выполнения",
    "3": "Выполняется",
    "4": "Ждёт контроля",
    "5": "Завершена",
    "6": "Отложена",
    "7": "Отклонена",
}

DEFAULT_TASK_SELECT = [
    "ID",
    "TITLE",
    "STATUS",
    "PRIORITY",
    "RESPONSIBLE_ID",
    "CREATED_BY",
    "GROUP_ID",
    "STAGE_ID",
    "DEADLINE",
    "CREATED_DATE",
    "CHANGED_DATE",
    "CLOSED_DATE",
]

USER_FIELDS = ["ID", "NAME", "LAST_NAME", "SECOND_NAME", "EMAIL", "WORK_POSITION", "UF_DEPARTMENT", "ACTIVE"]

TaskIdArg = Annotated[int, Field(description="ID задачи", gt=0)]
TaskFieldsArg = Annotated[
    dict[str, Any],
    Field(
        description=(
            "Поля задачи в UPPER_CASE: TITLE, DESCRIPTION, RESPONSIBLE_ID, DEADLINE "
            '(ISO 8601, например "2026-09-30T18:00:00+03:00"), PRIORITY (0 — низкий, 1 — средний, 2 — высокий), '
            "GROUP_ID, ACCOMPLICES и AUDITORS (списки ID пользователей), "
            'UF_CRM_TASK — привязка к CRM, например ["D_12", "C_5"] (L_ лид, D_ сделка, C_ контакт, CO_ компания)'
        )
    ),
]

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
_CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
_CHANGE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


def describe_task(task: dict[str, Any]) -> dict[str, Any]:
    task = compact(task)
    status = str(task.get("status", ""))
    if status in TASK_STATUSES:
        task["statusName"] = TASK_STATUSES[status]
    return task


async def fetch_task(client: Bitrix24Client, task_id: int) -> dict[str, Any]:
    result = await client.call("tasks.task.get", {"taskId": task_id})
    if not result or not result.get("task"):
        raise ToolError(f"Задача #{task_id} не найдена")
    return result["task"]


def task_title(task_id: int, task: dict[str, Any]) -> str:
    """Genitive phrase for previews: «задачи #5 «Отчёт»»."""
    return f"задачи #{task_id}{quoted(task.get('title'))}"


def register_task_tools(mcp: FastMCP, get_client: Callable[[], Bitrix24Client], approval: ApprovalPolicy) -> None:
    def write_tool(annotations: ToolAnnotations, description: str):
        return mcp.tool(annotations=annotations, description=approval.describe(description))

    approve = approval.request

    @mcp.tool(annotations=_READ)
    async def tasks_list(
        filter: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Фильтр в UPPER_CASE с префиксами >, >=, <, <=, !, %. Статусы (REAL_STATUS): "
                    "2 — ждёт выполнения, 3 — выполняется, 4 — ждёт контроля, 5 — завершена, 6 — отложена; "
                    "STATUS: -1 — просрочена. GROUP_ID — проект, STAGE_ID — стадия канбана, "
                    "SPRINT_ID и BACKLOG_ID — спринт и бэклог скрама. Пример: "
                    '{"RESPONSIBLE_ID": 1, "!REAL_STATUS": 5, "<DEADLINE": "2026-10-01"}'
                )
            ),
        ] = None,
        select: Annotated[list[str] | None, Field(description="Какие поля вернуть (UPPER_CASE)")] = None,
        order: Annotated[
            dict[str, Literal["asc", "desc", "ASC", "DESC"]] | None,
            Field(description='Сортировка, по умолчанию {"ID": "desc"}'),
        ] = None,
        start: Annotated[int, Field(description="Смещение для постраничного вывода", ge=0)] = 0,
        limit: Annotated[int, Field(description="Сколько задач вернуть (1–50)", ge=1, le=PAGE_SIZE)] = 20,
    ) -> dict[str, Any]:
        """Найти задачи по фильтру. Поля в ответе в camelCase."""
        response = await get_client().call_raw(
            "tasks.task.list",
            {
                "filter": filter or {},
                "select": select or DEFAULT_TASK_SELECT,
                "order": order or {"ID": "desc"},
                "start": start,
            },
        )
        tasks = (response["result"] or {}).get("tasks", [])[:limit]
        total = response.get("total", len(tasks))
        next_start = start + len(tasks)
        return {
            "tasks": [describe_task(t) for t in tasks],
            "total": total,
            "next_start": next_start if next_start < total else None,
        }

    @mcp.tool(annotations=_READ)
    async def task_get(id: TaskIdArg) -> dict[str, Any]:
        """Получить задачу со всеми заполненными полями, включая описание."""
        return describe_task(await fetch_task(get_client(), id))

    @mcp.tool(annotations=_READ)
    async def task_checklist(id: TaskIdArg) -> list[dict[str, Any]]:
        """Пункты чек-листа задачи."""
        items = await get_client().call("task.checklistitem.getlist", {"TASKID": id}) or []
        if isinstance(items, dict):
            items = list(items.values())
        return [
            compact(
                {
                    "id": item.get("ID"),
                    "parent_id": item.get("PARENT_ID"),
                    "title": item.get("TITLE"),
                    "is_complete": item.get("IS_COMPLETE") == "Y",
                }
            )
            for item in items
        ]

    @write_tool(_CREATE, "Создать задачу. Обязательны TITLE и RESPONSIBLE_ID (ID можно найти через users_search).")
    async def task_create(fields: TaskFieldsArg, ctx: Context) -> dict[str, Any]:
        if not fields.get("TITLE"):
            raise ToolError("Не указано название задачи (TITLE)")
        client = get_client()

        async def run() -> dict[str, Any]:
            result = await client.call("tasks.task.add", {"fields": fields})
            return {"id": result["task"]["id"], "task": describe_task(result["task"])}

        return await approve(ctx, build_summary("Создание задачи", client.portal_url, field_lines(fields)), run)

    @write_tool(_CHANGE, "Изменить задачу. Передавайте только изменяемые поля.")
    async def task_update(id: TaskIdArg, fields: TaskFieldsArg, ctx: Context) -> dict[str, Any]:
        if not fields:
            raise ToolError("Не переданы изменяемые поля")
        client = get_client()
        current = await fetch_task(client, id)

        async def run() -> dict[str, Any]:
            result = await client.call("tasks.task.update", {"taskId": id, "fields": fields})
            return {"id": id, "task": describe_task(result["task"])}

        summary = build_summary(
            f"Изменение {task_title(id, current)}", client.portal_url, change_lines(current, fields)
        )
        return await approve(ctx, summary, run)

    @write_tool(_CHANGE, "Завершить задачу.")
    async def task_complete(id: TaskIdArg, ctx: Context) -> dict[str, Any]:
        client = get_client()
        current = await fetch_task(client, id)

        async def run() -> dict[str, Any]:
            result = await client.call("tasks.task.complete", {"taskId": id})
            return {"id": id, "task": describe_task((result or {}).get("task") or {})}

        return await approve(ctx, build_summary(f"Завершение {task_title(id, current)}", client.portal_url), run)

    @write_tool(_CHANGE, "Удалить задачу.")
    async def task_delete(id: TaskIdArg, ctx: Context) -> dict[str, Any]:
        client = get_client()
        current = await fetch_task(client, id)

        async def run() -> dict[str, Any]:
            await client.call("tasks.task.delete", {"taskId": id})
            return {"id": id, "deleted": True}

        summary = build_summary(
            f"Удаление {task_title(id, current)}",
            client.portal_url,
            [
                f"Ответственный: {current.get('responsibleId') or '—'}",
                f"Крайний срок: {current.get('deadline') or '—'}",
            ],
        )
        return await approve(ctx, summary, run)

    @write_tool(_CREATE, "Написать комментарий к задаче (в чат задачи, а на старых порталах — в комментарии).")
    async def task_add_comment(
        id: TaskIdArg,
        text: Annotated[str, Field(description="Текст комментария", min_length=1)],
        ctx: Context,
    ) -> dict[str, Any]:
        client = get_client()
        current = await fetch_task(client, id)

        async def run() -> dict[str, Any]:
            try:
                await client.call("tasks.task.chat.message.send", {"fields": {"taskId": id, "text": text}}, api_v3=True)
                return {"id": id, "sent_to": "task_chat"}
            except Bitrix24Error as chat_error:
                if chat_error.code == "NETWORK_ERROR":
                    raise
                # Portals without the new task card only have the classic comment feed.
                try:
                    comment_id = await client.call(
                        "task.commentitem.add", {"TASKID": id, "FIELDS": {"POST_MESSAGE": text}}
                    )
                except Bitrix24Error as comment_error:
                    raise ToolError(
                        f"Не удалось отправить комментарий: {chat_error}; {comment_error}"
                    ) from comment_error
                return {"id": id, "sent_to": "comments", "comment_id": comment_id}

        summary = build_summary(
            f"Комментарий к задаче #{id}{quoted(current.get('title'))}", client.portal_url, [f"Текст: {text}"]
        )
        return await approve(ctx, summary, run)

    @write_tool(_CREATE, "Добавить пункт в чек-лист задачи.")
    async def task_checklist_add(
        id: TaskIdArg,
        title: Annotated[str, Field(description="Текст пункта чек-листа", min_length=1)],
        ctx: Context,
    ) -> dict[str, Any]:
        client = get_client()
        current = await fetch_task(client, id)

        async def run() -> dict[str, Any]:
            item_id = await client.call("task.checklistitem.add", {"TASKID": id, "FIELDS": {"TITLE": title}})
            return {"id": id, "item_id": item_id}

        summary = build_summary(
            f"Новый пункт чек-листа {task_title(id, current)}", client.portal_url, [f"Пункт: {title}"]
        )
        return await approve(ctx, summary, run)

    @write_tool(_CHANGE, "Отметить пункт чек-листа выполненным.")
    async def task_checklist_complete(
        id: TaskIdArg,
        item_id: Annotated[int, Field(description="ID пункта чек-листа (из task_checklist)", gt=0)],
        ctx: Context,
    ) -> dict[str, Any]:
        client = get_client()
        current = await fetch_task(client, id)

        async def run() -> dict[str, Any]:
            await client.call("task.checklistitem.complete", {"TASKID": id, "ITEMID": item_id})
            return {"id": id, "item_id": item_id, "is_complete": True}

        summary = build_summary(f"Выполнение пункта #{item_id} чек-листа {task_title(id, current)}", client.portal_url)
        return await approve(ctx, summary, run)

    @mcp.tool(annotations=_READ)
    async def users_search(
        query: Annotated[str, Field(description="Имя, фамилия, должность или отдел", min_length=1)],
    ) -> list[dict[str, Any]]:
        """Найти активных сотрудников портала (например, чтобы узнать ID ответственного)."""
        users = await get_client().call("user.search", {"FIND": query, "ACTIVE": True})
        return [compact({k: u.get(k) for k in USER_FIELDS}) for u in users or []]

    @mcp.tool(annotations=_READ)
    async def user_current() -> dict[str, Any]:
        """Пользователь, от имени которого работает вебхук."""
        user = await get_client().call("user.current")
        return compact({k: user.get(k) for k in USER_FIELDS})
