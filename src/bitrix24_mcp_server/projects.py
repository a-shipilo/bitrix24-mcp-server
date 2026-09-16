"""Projects (workgroups) with their kanban boards, and Scrum: sprints, sprint boards and backlog."""

import asyncio
import contextlib
from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .approval import ApprovalPolicy
from .client import Bitrix24Client, Bitrix24Error
from .crm import compact
from .preview import build_summary, quoted
from .tasks import TASK_STATUSES, fetch_task, task_title

BOARD_TASK_SELECT = ["ID", "TITLE", "STATUS", "PRIORITY", "RESPONSIBLE_ID", "DEADLINE", "STAGE_ID"]
GROUP_TYPES = {"group": "группа", "project": "проект", "scrum": "скрам"}
SPRINT_STATUSES = {"planned": "запланирован", "active": "активный", "completed": "завершён"}

MAX_BOARD_TASKS = 500
MAX_DESCRIPTION_LENGTH = 300
# Scrum list methods report an empty result as this error instead of returning [].
NO_ITEMS_ERROR = "Could not load list"

GroupIdArg = Annotated[int, Field(description="ID проекта, группы или скрама (из projects_list)", gt=0)]
SprintIdArg = Annotated[int, Field(description="ID спринта (из scrum_sprints или sprint_board)", gt=0)]
TaskIdArg = Annotated[int, Field(description="ID задачи", gt=0)]
MaxTasksArg = Annotated[int, Field(description="Сколько задач загрузить максимум", ge=1, le=MAX_BOARD_TASKS)]

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
_MOVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def describe_group(group: dict[str, Any], is_scrum: bool) -> dict[str, Any]:
    description = str(group.get("DESCRIPTION") or "")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        description = description[:MAX_DESCRIPTION_LENGTH] + "…"
    group_type = "scrum" if is_scrum else "project" if group.get("PROJECT") == "Y" else "group"
    return compact(
        {
            "id": int(group["ID"]),
            "name": group.get("NAME"),
            "type": group_type,
            "type_name": GROUP_TYPES[group_type],
            "description": description,
            "closed": group.get("CLOSED") == "Y",
            "owner_id": group.get("OWNER_ID"),
            "members": group.get("NUMBER_OF_MEMBERS"),
            "last_activity": group.get("DATE_ACTIVITY"),
        }
    )


def describe_sprint(sprint: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "id": int(sprint["id"]),
            "group_id": int(sprint["groupId"]) if sprint.get("groupId") else None,
            "name": sprint.get("name"),
            "status": sprint.get("status"),
            "status_name": SPRINT_STATUSES.get(str(sprint.get("status"))),
            "goal": sprint.get("goal"),
            "date_start": sprint.get("dateStart"),
            "date_end": sprint.get("dateEnd"),
        }
    )


def brief_task(task: dict[str, Any]) -> dict[str, Any]:
    responsible = task.get("responsible") or {}
    return compact(
        {
            "id": int(task["id"]),
            "title": task.get("title"),
            "status": TASK_STATUSES.get(str(task.get("status")), task.get("status")),
            "responsible": responsible.get("name") or task.get("responsibleId"),
            "deadline": task.get("deadline"),
            "high_priority": True if str(task.get("priority")) == "2" else None,
        }
    )


def as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def project_stages(raw: Any) -> list[dict[str, Any]]:
    """``task.stages.get`` returns an object keyed by stage ID (or ``[]`` when there are none)."""
    items = raw.values() if isinstance(raw, dict) else raw or []
    stages = [
        {"id": int(s["ID"]), "title": s.get("TITLE"), "type": s.get("SYSTEM_TYPE"), "sort": int(s.get("SORT") or 0)}
        for s in items
    ]
    return sorted(stages, key=lambda s: (s["sort"], s["id"]))


def sprint_stages(raw: Any) -> list[dict[str, Any]]:
    stages = [
        {"id": int(s["id"]), "title": s.get("name"), "type": s.get("type"), "sort": int(s.get("sort") or 0)}
        for s in raw or []
    ]
    return sorted(stages, key=lambda s: (s["sort"], s["id"]))


def build_columns(
    stages: list[dict[str, Any]], rows: dict[int, dict[str, Any]], stage_of: dict[int, int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Put task rows into stage columns; returns the columns and rows whose stage is unknown."""
    columns = {s["id"]: {"id": s["id"], "title": s["title"], "type": s["type"], "tasks": []} for s in stages}
    unplaced = []
    for task_id, row in rows.items():
        column = columns.get(stage_of.get(task_id, -1))
        (column["tasks"] if column else unplaced).append(row)
    for column in columns.values():
        column["task_count"] = len(column["tasks"])
        points = [float(t["story_points"]) for t in column["tasks"] if _is_number(t.get("story_points"))]
        if points:
            column["story_points"] = sum(points)
    return list(columns.values()), unplaced


def direct_stage_map(stages: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> dict[int, int]:
    """Map tasks by their ``stageId``; 0 means the first column, as in Bitrix24 kanban."""
    first = stages[0]["id"] if stages else 0
    return {int(t["id"]): as_int(t.get("stageId")) or first for t in tasks}


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


async def list_or_empty(client: Bitrix24Client, method: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return await client.call(method, params) or []
    except Bitrix24Error as exc:
        if exc.description == NO_ITEMS_ERROR:
            return []
        raise


async def load_tasks(
    client: Bitrix24Client, task_filter: dict[str, Any], max_tasks: int
) -> tuple[list[dict[str, Any]], int]:
    """Load up to ``max_tasks`` tasks page by page; returns them and the total count."""
    tasks: list[dict[str, Any]] = []
    total = 0
    start = 0
    while len(tasks) < max_tasks:
        response = await client.call_raw(
            "tasks.task.list",
            {"filter": task_filter, "select": BOARD_TASK_SELECT, "order": {"ID": "asc"}, "start": start},
        )
        page = (response.get("result") or {}).get("tasks") or []
        tasks.extend(page)
        total = int(response.get("total") or len(tasks))
        if not page or response.get("next") is None:
            break
        start = int(response["next"])
    return tasks[:max_tasks], max(total, len(tasks))


async def scrum_details(
    client: Bitrix24Client, group_id: int, task_ids: list[int]
) -> tuple[dict[int, dict[str, Any]], dict[int, str]]:
    """Story points, epic and backlog order of Scrum tasks, plus epic names of the Scrum."""
    if not task_ids:
        return {}, {}
    commands = {f"t{task_id}": ("tasks.api.scrum.task.get", {"id": task_id}) for task_id in task_ids}
    (fields, _errors), epics = await asyncio.gather(
        client.batch(commands),
        list_or_empty(client, "tasks.api.scrum.epic.list", {"filter": {"GROUP_ID": group_id}}),
    )
    details = {int(key[1:]): value for key, value in fields.items() if isinstance(value, dict)}
    names = {int(e.get("id") or e.get("ID")): e.get("name") or e.get("NAME") for e in epics}
    return details, names


def scrum_row(task: dict[str, Any], details: dict[str, Any] | None, epics: dict[int, str]) -> dict[str, Any]:
    row = brief_task(task)
    details = details or {}
    epic_id = as_int(details.get("epicId"))
    row.update(
        compact(
            {
                "story_points": details.get("storyPoints"),
                "epic": epics.get(epic_id, epic_id) if epic_id else None,
            }
        )
    )
    return row


async def placement_by_filter(
    client: Bitrix24Client, base_filter: dict[str, Any], stages: list[dict[str, Any]], key: str, max_tasks: int
) -> dict[int, int] | None:
    """Ask the task list for each column in turn; None if the portal ignores or rejects the filter."""
    try:
        pages = await asyncio.gather(
            *(load_tasks(client, {**base_filter, key: stage["id"]}, max_tasks) for stage in stages)
        )
    except Bitrix24Error:
        return None
    mapping: dict[int, int] = {}
    for stage, (found, _) in zip(stages, pages, strict=True):
        for task in found:
            task_id = int(task["id"])
            if task_id in mapping:  # the filter was ignored, the answer is meaningless
                return None
            mapping[task_id] = stage["id"]
    return mapping or None


async def resolve_sprint_stages(
    client: Bitrix24Client,
    base_filter: dict[str, Any],
    stages: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    max_tasks: int,
) -> dict[int, int] | None:
    """Find which sprint kanban column each task is in, or None if the portal does not tell.

    The sprint board keeps its columns apart from the task's own STAGE_ID, and the two can disagree:
    a task put on the board through tasks.api.scrum.kanban.addTask keeps STAGE_ID = 0. The STAGES_ID
    filter reads the board itself, so it goes first. Unlike project kanban, 0 does not mean the first column.
    """
    if not tasks or not stages:
        return None
    for key in ("STAGES_ID", "STAGE_ID"):
        mapping = await placement_by_filter(client, base_filter, stages, key, max_tasks)
        if mapping is not None:
            return mapping
    stage_ids = {s["id"] for s in stages}
    direct = {int(t["id"]): as_int(t.get("stageId")) for t in tasks if as_int(t.get("stageId")) in stage_ids}
    return direct or None


def register_project_tools(mcp: FastMCP, get_client: Callable[[], Bitrix24Client], approval: ApprovalPolicy) -> None:
    @mcp.tool(annotations=_READ)
    async def projects_list(
        query: Annotated[str | None, Field(description="Часть названия")] = None,
        include_closed: Annotated[bool, Field(description="Включать архивные")] = False,
        start: Annotated[int, Field(description="Смещение для постраничного вывода", ge=0)] = 0,
    ) -> dict[str, Any]:
        """Найти проекты, рабочие группы и скрамы, доступные пользователю вебхука.
        Для type=scrum используйте scrum_sprints, sprint_board и scrum_backlog, для остальных — project_board."""
        client = get_client()
        group_filter: dict[str, Any] = {"ACTIVE": "Y"}
        if not include_closed:
            group_filter["CLOSED"] = "N"
        if query:
            group_filter["%NAME"] = query
        # sonet_group.get works with the sonet_group scope; socialnetwork.api.workgroup.list needs another one.
        response = await client.call_raw(
            "sonet_group.get", {"FILTER": group_filter, "ORDER": {"NAME": "ASC"}, "start": start}
        )
        groups = response.get("result") or []
        # The list does not say which groups are Scrum; only Scrum groups have a backlog.
        backlogs, _ = await client.batch(
            {f"g{g['ID']}": ("tasks.api.scrum.backlog.get", {"id": int(g["ID"])}) for g in groups}
        )
        return {
            "projects": [describe_group(g, f"g{g['ID']}" in backlogs) for g in groups],
            "total": response.get("total", len(groups)),
            "next_start": response.get("next"),
        }

    @mcp.tool(annotations=_READ)
    async def project_board(
        group_id: GroupIdArg,
        include_completed: Annotated[bool, Field(description="Показывать завершённые задачи")] = False,
        max_tasks: MaxTasksArg = 200,
    ) -> dict[str, Any]:
        """Канбан проекта: стадии по порядку и задачи на каждой стадии.
        Для скрама используйте sprint_board."""
        client = get_client()
        task_filter: dict[str, Any] = {"GROUP_ID": group_id}
        if not include_completed:
            task_filter["!REAL_STATUS"] = 5
        raw_stages, (tasks, total) = await asyncio.gather(
            client.call("task.stages.get", {"entityId": group_id}), load_tasks(client, task_filter, max_tasks)
        )
        stages = project_stages(raw_stages)
        rows = {int(t["id"]): brief_task(t) for t in tasks}
        columns, unplaced = build_columns(stages, rows, direct_stage_map(stages, tasks))
        result: dict[str, Any] = {
            "project_id": group_id,
            "stages": columns,
            "tasks_loaded": len(tasks),
            "tasks_total": total,
            "truncated": len(tasks) < total,
        }
        if unplaced:
            result["tasks_without_stage"] = unplaced
        return result

    @mcp.tool(annotations=_MOVE, description=approval.describe(
        "task_move_stage",
        "Перенести задачу проекта на другую стадию канбана (ID стадий — в project_board). "
        "Для задач скрама используйте sprint_move_task."
    ))  # fmt: skip
    async def task_move_stage(
        id: TaskIdArg,
        stage_id: Annotated[int, Field(description="ID стадии канбана проекта", gt=0)],
        ctx: Context,
    ) -> dict[str, Any]:
        client = get_client()
        task = await fetch_task(client, id)
        group_id = as_int(task.get("groupId"))
        if not group_id:
            raise ToolError(f"Задача #{id} не входит в проект, у неё нет стадий канбана")
        stages = project_stages(await client.call("task.stages.get", {"entityId": group_id}))
        titles = {s["id"]: s["title"] for s in stages}
        if stage_id not in titles:
            available = ", ".join(f"{s['id']} «{s['title']}»" for s in stages) or "нет"
            raise ToolError(f"В канбане проекта #{group_id} нет стадии {stage_id}. Стадии проекта: {available}")
        current = direct_stage_map(stages, [task]).get(id)

        async def run() -> dict[str, Any]:
            await client.call("task.stages.movetask", {"id": id, "stageId": stage_id})
            return {"id": id, "stage_id": stage_id, "stage": titles[stage_id]}

        summary = build_summary(
            f"Перенос {task_title(id, task)} в канбане проекта #{group_id}",
            client.portal_url,
            [f"Стадия: {titles.get(current, '—')} → {titles[stage_id]}"],
        )
        return await approval.request(ctx, "task_move_stage", summary, run)

    @mcp.tool(annotations=_READ)
    async def scrum_sprints(
        group_id: GroupIdArg,
        status: Annotated[
            Literal["active", "planned", "completed"] | None,
            Field(description="active — текущий, planned — запланированные, completed — завершённые"),
        ] = None,
    ) -> list[dict[str, Any]]:
        """Спринты скрама, от новых к старым."""
        sprint_filter: dict[str, Any] = {"GROUP_ID": group_id}
        if status:
            sprint_filter["STATUS"] = status
        sprints = await list_or_empty(
            get_client(), "tasks.api.scrum.sprint.list", {"filter": sprint_filter, "order": {"DATE_START": "desc"}}
        )
        return [describe_sprint(s) for s in sprints]

    @mcp.tool(annotations=_READ)
    async def sprint_board(
        sprint_id: Annotated[
            int | None, Field(description="ID спринта; не указан — активный спринт group_id", gt=0)
        ] = None,
        group_id: Annotated[int | None, Field(description="ID скрама, если sprint_id не указан", gt=0)] = None,
        max_tasks: MaxTasksArg = 200,
    ) -> dict[str, Any]:
        """Канбан спринта: стадии и задачи на них со story points и эпиками."""
        client = get_client()
        if sprint_id is not None:
            sprint = await client.call("tasks.api.scrum.sprint.get", {"id": sprint_id})
        elif group_id is not None:
            active = await list_or_empty(
                client, "tasks.api.scrum.sprint.list", {"filter": {"GROUP_ID": group_id, "STATUS": "active"}}
            )
            if not active:
                raise ToolError(f"В скраме #{group_id} нет активного спринта. Все спринты покажет scrum_sprints")
            sprint = active[0]
        else:
            raise ToolError("Укажите sprint_id или group_id")
        sprint_id, scrum_id = int(sprint["id"]), int(sprint["groupId"])

        notes = []
        try:
            stages = sprint_stages(await client.call("tasks.api.scrum.kanban.getStages", {"sprintId": sprint_id}))
        except Bitrix24Error as exc:
            stages = []
            notes.append(f"Стадии спринта недоступны: {exc}")

        base_filter = {"GROUP_ID": scrum_id, "SPRINT_ID": sprint_id}
        tasks, total = await load_tasks(client, base_filter, max_tasks)
        details, epics = await scrum_details(client, scrum_id, [int(t["id"]) for t in tasks])
        rows = {int(t["id"]): scrum_row(t, details.get(int(t["id"])), epics) for t in tasks}

        stage_of = await resolve_sprint_stages(client, base_filter, stages, tasks, max_tasks) if stages else None
        result: dict[str, Any] = {"sprint": describe_sprint(sprint)}
        if stage_of is None:
            result["stages"] = [{"id": s["id"], "title": s["title"], "type": s["type"]} for s in stages]
            result["tasks"] = list(rows.values())
            if stages:
                notes.append("Портал не сообщил, на какой стадии находится каждая задача")
        else:
            result["stages"], unplaced = build_columns(stages, rows, stage_of)
            if unplaced:
                result["tasks_without_stage"] = unplaced
        result.update(tasks_loaded=len(tasks), tasks_total=total, truncated=len(tasks) < total)
        if notes:
            result["notes"] = notes
        return result

    @mcp.tool(annotations=_MOVE, description=approval.describe(
        "sprint_move_task",
        "Перенести задачу на другую стадию канбана спринта (ID стадий — в sprint_board)."
    ))  # fmt: skip
    async def sprint_move_task(
        sprint_id: SprintIdArg,
        task_id: TaskIdArg,
        stage_id: Annotated[int, Field(description="ID стадии канбана спринта", gt=0)],
        ctx: Context,
    ) -> dict[str, Any]:
        client = get_client()
        sprint, task, scrum_task, raw_stages = await asyncio.gather(
            client.call("tasks.api.scrum.sprint.get", {"id": sprint_id}),
            fetch_task(client, task_id),
            client.call("tasks.api.scrum.task.get", {"id": task_id}),
            client.call("tasks.api.scrum.kanban.getStages", {"sprintId": sprint_id}),
        )
        stages = sprint_stages(raw_stages)
        titles = {s["id"]: s["title"] for s in stages}
        if stage_id not in titles:
            available = ", ".join(f"{s['id']} «{s['title']}»" for s in stages) or "нет"
            raise ToolError(f"В спринте #{sprint_id} нет стадии {stage_id}. Стадии спринта: {available}")
        if as_int((scrum_task or {}).get("entityId")) != sprint_id:
            raise ToolError(f"Задача #{task_id} не входит в спринт #{sprint_id}")
        base_filter = {"GROUP_ID": int(sprint["groupId"]), "SPRINT_ID": sprint_id, "ID": task_id}
        current = ((await resolve_sprint_stages(client, base_filter, stages, [task], 1)) or {}).get(task_id)

        async def run() -> dict[str, Any]:
            # addTask places a task into a column; removing it first keeps the card from ending up twice.
            with contextlib.suppress(Bitrix24Error):  # the task may not be on the board yet
                await client.call("tasks.api.scrum.kanban.deleteTask", {"sprintId": sprint_id, "taskId": task_id})
            try:
                await client.call(
                    "tasks.api.scrum.kanban.addTask", {"sprintId": sprint_id, "taskId": task_id, "stageId": stage_id}
                )
            except Bitrix24Error as exc:
                fallback = current or stages[0]["id"]
                try:
                    await client.call(
                        "tasks.api.scrum.kanban.addTask",
                        {"sprintId": sprint_id, "taskId": task_id, "stageId": fallback},
                    )
                except Bitrix24Error:
                    raise ToolError(
                        f"Не удалось перенести задачу ({exc}) и вернуть её на доску спринта. "
                        "Задача осталась в спринте, но её нужно заново поставить на стадию."
                    ) from exc
                raise ToolError(
                    f"Не удалось перенести задачу ({exc}). Она осталась на стадии «{titles[fallback]}»"
                ) from exc
            return {"task_id": task_id, "sprint_id": sprint_id, "stage_id": stage_id, "stage": titles[stage_id]}

        summary = build_summary(
            f"Перенос {task_title(task_id, task)} в спринте{quoted(sprint.get('name'))}",
            client.portal_url,
            [f"Стадия: {titles.get(current, '—')} → {titles[stage_id]}"],
        )
        return await approval.request(ctx, "sprint_move_task", summary, run)

    @mcp.tool(annotations=_READ)
    async def scrum_backlog(group_id: GroupIdArg, max_tasks: MaxTasksArg = 100) -> dict[str, Any]:
        """Бэклог скрама в порядке приоритета, со story points и эпиками."""
        client = get_client()
        backlog = await client.call("tasks.api.scrum.backlog.get", {"id": group_id})
        tasks, total = await load_tasks(client, {"GROUP_ID": group_id, "BACKLOG_ID": int(backlog["id"])}, max_tasks)
        details, epics = await scrum_details(client, group_id, [int(t["id"]) for t in tasks])
        tasks.sort(key=lambda t: as_int((details.get(int(t["id"])) or {}).get("sort")))
        return {
            "backlog_id": int(backlog["id"]),
            "tasks": [scrum_row(t, details.get(int(t["id"])), epics) for t in tasks],
            "tasks_loaded": len(tasks),
            "tasks_total": total,
            "truncated": len(tasks) < total,
        }
