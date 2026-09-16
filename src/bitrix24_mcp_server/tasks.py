"""Task, checklist and user tools."""

import contextlib
import re
from collections.abc import Callable
from pathlib import Path
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
    "TAGS",
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


def tag_names(tags: Any) -> list[str]:
    """Bitrix24 returns tags as {"562": {"id": 562, "title": "hds-api-server"}, ...}."""
    items = tags.values() if isinstance(tags, dict) else tags or []
    return [str(t.get("title") if isinstance(t, dict) else t) for t in items]


def describe_task(task: dict[str, Any]) -> dict[str, Any]:
    task = compact(task)
    if "tags" in task:
        task["tags"] = tag_names(task["tags"])
    status = str(task.get("status", ""))
    if status in TASK_STATUSES:
        task["statusName"] = TASK_STATUSES[status]
    return task


async def fetch_task(client: Bitrix24Client, task_id: int, select: list[str] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"taskId": task_id}
    if select:
        params["select"] = select
    result = await client.call("tasks.task.get", params)
    if not result or not result.get("task"):
        raise ToolError(f"Задача #{task_id} не найдена")
    return result["task"]


def task_title(task_id: int, task: dict[str, Any]) -> str:
    """Genitive phrase for previews: «задачи #5 «Отчёт»»."""
    return f"задачи #{task_id}{quoted(task.get('title'))}"


MAX_FILE_BYTES = 20 * 1024 * 1024
TEXT_ENCODINGS = ("utf-8-sig", "cp1251")
MAX_COMMENT_PAGES = 20
_USER_TAG = re.compile(r"\[USER=\d+[^\]]*\](.*?)\[/USER\]", re.S)
NO_DISK_SCOPE = (
    "Битрикс24 не отдал файл вебхуку. Добавьте вебхуку право «Диск» (disk) в разделе Разработчикам → Входящий вебхук"
)
NO_IM_SCOPE = (
    "Комментарии этой задачи хранятся в её чате. Чтобы читать их, добавьте вебхуку право "
    "«Чат и уведомления» (im) в разделе Разработчикам → Входящий вебхук"
)


def as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def describe_files(files: Any) -> list[dict[str, Any]]:
    """Attachments without their download links: those carry the webhook credentials."""
    items = files.values() if isinstance(files, dict) else files or []
    return [{"id": as_int(f.get("ATTACHMENT_ID")), "name": f.get("NAME"), "size": as_int(f.get("SIZE"))} for f in items]


def decode_text(content: bytes, encoding: str | None) -> tuple[str, str]:
    if encoding:
        return content.decode(encoding), encoding
    for candidate in TEXT_ENCODINGS:
        try:
            return content.decode(candidate), candidate
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1"), "latin-1"


def plain_chat_text(text: str) -> str:
    return _USER_TAG.sub(r"\1", text or "")


def register_task_tools(mcp: FastMCP, get_client: Callable[[], Bitrix24Client], approval: ApprovalPolicy) -> None:
    def write_tool(annotations: ToolAnnotations, description: str):
        def register(fn):
            return mcp.tool(annotations=annotations, description=approval.describe(fn.__name__, description))(fn)

        return register

    @mcp.tool(annotations=_READ)
    async def tasks_list(
        filter: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Фильтр в UPPER_CASE с префиксами >, >=, <, <=, !, %. Статусы (REAL_STATUS): "
                    "2 — ждёт выполнения, 3 — выполняется, 4 — ждёт контроля, 5 — завершена, 6 — отложена; "
                    "STATUS: -1 — просрочена. GROUP_ID — проект, STAGE_ID — стадия канбана, TAG — тег, "
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
        """Получить задачу со всеми заполненными полями, включая описание и список вложений (files).
        Содержимое вложения — task_file_read, комментарии — task_comments."""
        client = get_client()
        # Without an explicit select Bitrix24 leaves out custom fields, attachments among them.
        task = describe_task(await fetch_task(client, id, ["*", "UF_*", "TAGS"]))
        if task.get("ufTaskWebdavFiles"):
            with contextlib.suppress(Bitrix24Error):  # the task itself is still useful without the file list
                task["files"] = describe_files(await client.call("task.item.getfiles", {"TASKID": id}))
        return task

    @mcp.tool(annotations=_READ)
    async def task_file_read(
        task_id: TaskIdArg,
        attachment_id: Annotated[int, Field(description="ID вложения из files в task_get", gt=0)],
        save_to: Annotated[
            str | None,
            Field(
                description=(
                    "Абсолютный путь, куда сохранить файл на этом компьютере. "
                    "Нужен для больших и двоичных файлов; папка должна существовать"
                )
            ),
        ] = None,
        overwrite: Annotated[bool, Field(description="Перезаписать файл, если он уже есть")] = False,
        encoding: Annotated[
            str | None, Field(description="Кодировка текста; по умолчанию UTF-8, при ошибке — Windows-1251")
        ] = None,
        offset: Annotated[int, Field(description="С какого символа отдавать текст", ge=0)] = 0,
        max_chars: Annotated[int, Field(description="Сколько символов текста отдать", ge=1, le=500_000)] = 100_000,
    ) -> dict[str, Any]:
        """Прочитать вложение задачи: текстовые файлы (CSV, TSV, JSON, TXT) возвращаются текстом,
        любой файл можно сохранить на диск через save_to."""
        client = get_client()
        raw_files = await client.call("task.item.getfiles", {"TASKID": task_id}) or []
        raw_files = list(raw_files.values()) if isinstance(raw_files, dict) else raw_files
        raw = next((f for f in raw_files if as_int(f.get("ATTACHMENT_ID")) == attachment_id), None)
        if raw is None:
            names = ", ".join(f"{f['id']} «{f['name']}»" for f in describe_files(raw_files)) or "нет"
            raise ToolError(f"У задачи #{task_id} нет вложения {attachment_id}. Вложения: {names}")
        [file] = describe_files([raw])

        target = None
        if save_to:
            target = Path(save_to).expanduser()
            if not target.is_absolute():
                raise ToolError("save_to должен быть абсолютным путём")
            if target.is_dir():
                target = target / Path(str(file["name"])).name
            if not target.parent.is_dir():
                raise ToolError(f"Папки {target.parent} нет")
            if target.exists() and not overwrite:
                raise ToolError(f"Файл {target} уже существует; передайте overwrite=true, чтобы перезаписать")

        try:
            content = await client.download(str(raw.get("DOWNLOAD_URL") or ""), max_bytes=MAX_FILE_BYTES)
        except Bitrix24Error as exc:
            if exc.code == "DOWNLOAD_ERROR" and "permission" in exc.description.lower():
                raise ToolError(NO_DISK_SCOPE) from exc
            raise

        result: dict[str, Any] = {**file, "downloaded_bytes": len(content)}
        if target is not None:
            target.write_bytes(content)
            result["saved_to"] = str(target)
            return result
        if b"\x00" in content[:8192]:
            raise ToolError("Файл двоичный: сохраните его на диск через save_to")
        try:
            text, used_encoding = decode_text(content, encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            raise ToolError(f"Не удалось прочитать файл в кодировке {encoding}: {exc}") from exc
        chunk = text[offset : offset + max_chars]
        result.update(
            encoding=used_encoding,
            total_chars=len(text),
            offset=offset,
            text=chunk,
            next_offset=offset + len(chunk) if offset + len(chunk) < len(text) else None,
        )
        return result

    @mcp.tool(annotations=_READ)
    async def task_comments(
        id: TaskIdArg,
        limit: Annotated[int, Field(description="Сколько последних комментариев вернуть", ge=1, le=500)] = 50,
        include_system: Annotated[
            bool, Field(description="Включать служебные сообщения чата (смена статуса, сроков и т. п.)")
        ] = False,
    ) -> dict[str, Any]:
        """Последние комментарии задачи по порядку: сообщения чата задачи в новой карточке
        или ленту комментариев в старой."""
        client = get_client()
        found = await client.call("tasks.task.get", {"taskId": id, "select": ["ID", "CHAT_ID"]})
        if not found or not found.get("task"):
            raise ToolError(f"Задача #{id} не найдена")
        chat_id = as_int(found["task"].get("chatId"))
        if not chat_id:
            items = await client.call("task.commentitem.getlist", {"TASKID": id, "ORDER": {"ID": "desc"}}) or []
            comments = [
                compact(
                    {
                        "id": as_int(c.get("ID")),
                        "date": c.get("POST_DATE"),
                        "author": c.get("AUTHOR_NAME"),
                        "author_id": as_int(c.get("AUTHOR_ID")),
                        "text": c.get("POST_MESSAGE"),
                        "files": [f["name"] for f in describe_files(c.get("ATTACHED_OBJECTS"))],
                    }
                )
                for c in items[:limit]
            ]
            return {"source": "comments", "comments": comments[::-1]}

        messages: list[dict[str, Any]] = []
        users: dict[int, str] = {}
        files: dict[int, str] = {}
        last_id = None
        try:
            for _ in range(MAX_COMMENT_PAGES):
                params: dict[str, Any] = {"DIALOG_ID": f"chat{chat_id}", "LIMIT": 50}
                if last_id is not None:
                    params["LAST_ID"] = last_id
                page = await client.call("im.dialog.messages.get", params) or {}
                batch = page.get("messages") or []
                users.update({as_int(u.get("id")): u.get("name") for u in page.get("users") or []})
                files.update({as_int(f.get("id")): f.get("name") for f in page.get("files") or []})
                messages.extend(m for m in batch if include_system or as_int(m.get("author_id")))
                if len(batch) == 0 or len(messages) >= limit:
                    break
                last_id = min(as_int(m.get("id")) for m in batch)
        except Bitrix24Error as exc:
            if exc.code == "insufficient_scope":
                raise ToolError(NO_IM_SCOPE) from exc
            raise
        messages = sorted(messages, key=lambda m: as_int(m.get("id")))[-limit:]
        comments = []
        for m in messages:
            author_id = as_int(m.get("author_id"))
            file_ids = (m.get("params") or {}).get("FILE_ID") if isinstance(m.get("params"), dict) else None
            comments.append(
                compact(
                    {
                        "id": as_int(m.get("id")),
                        "date": m.get("date"),
                        "author": users.get(author_id) if author_id else "система",
                        "author_id": author_id or None,
                        "text": plain_chat_text(m.get("text")),
                        "files": [files.get(as_int(f), f) for f in file_ids or []],
                    }
                )
            )
        return {"source": "task_chat", "chat_id": chat_id, "comments": comments}

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

        return await approval.request(
            ctx, "task_create", build_summary("Создание задачи", client.portal_url, field_lines(fields)), run
        )

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
        return await approval.request(ctx, "task_update", summary, run)

    @write_tool(_CHANGE, "Завершить задачу.")
    async def task_complete(id: TaskIdArg, ctx: Context) -> dict[str, Any]:
        client = get_client()
        current = await fetch_task(client, id)

        async def run() -> dict[str, Any]:
            result = await client.call("tasks.task.complete", {"taskId": id})
            return {"id": id, "task": describe_task((result or {}).get("task") or {})}

        return await approval.request(
            ctx, "task_complete", build_summary(f"Завершение {task_title(id, current)}", client.portal_url), run
        )

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
        return await approval.request(ctx, "task_delete", summary, run)

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
        return await approval.request(ctx, "task_add_comment", summary, run)

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
        return await approval.request(ctx, "task_checklist_add", summary, run)

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
        return await approval.request(ctx, "task_checklist_complete", summary, run)

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
