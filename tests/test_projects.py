from typing import Any

from bitrix24_mcp_server.client import php_query

from .conftest import payload

NO_ITEMS = {"error": 0, "error_description": "Could not load list"}

PROJECT_STAGES = {
    "12": {"ID": "12", "TITLE": "В работе", "SORT": "200", "SYSTEM_TYPE": "WORK"},
    "11": {"ID": "11", "TITLE": "Новые", "SORT": "100", "SYSTEM_TYPE": "NEW"},
    "13": {"ID": "13", "TITLE": "Готово", "SORT": "300", "SYSTEM_TYPE": "FINISH"},
}

SPRINT = {"id": 5, "groupId": 40, "name": "Спринт 7", "status": "active", "dateStart": "2026-09-14T09:00:00+03:00"}
SPRINT_STAGES = [
    {"id": "52", "name": "В работе", "sort": "200", "type": "WORK", "sprintId": "5"},
    {"id": "51", "name": "Новые", "sort": "100", "type": "NEW", "sprintId": "5"},
    {"id": "53", "name": "Готово", "sort": "300", "type": "FINISH", "sprintId": "5"},
]


def task(task_id: int, stage: int = 0, **fields: Any) -> dict[str, Any]:
    return {
        "id": str(task_id),
        "title": f"Задача {task_id}",
        "status": "3",
        "stageId": str(stage),
        "groupId": "40",
        "responsibleId": "1",
        "responsible": {"id": "1", "name": "Анна Смирнова"},
        **fields,
    }


def task_list(tasks: list[dict[str, Any]], page_size: int = 50, stage_filter: bool = True):
    """tasks.task.list over an in-memory table, honouring ID, STAGE_ID and STAGES_ID filters and paging.

    ``_board_stage`` answers the STAGE_ID filter and ``_board_column`` the STAGES_ID filter; a table without
    ``_board_column`` values behaves like a portal that ignores STAGES_ID.
    """

    def handler(params: dict[str, Any]) -> dict[str, Any]:
        flt = params.get("filter", {})
        rows = tasks
        if "ID" in flt:
            rows = [t for t in rows if int(t["id"]) == flt["ID"]]
        if stage_filter and "STAGE_ID" in flt:
            rows = [t for t in rows if t.get("_board_stage") == flt["STAGE_ID"]]
        if "STAGES_ID" in flt and any("_board_column" in t for t in tasks):
            rows = [t for t in rows if t.get("_board_column") == flt["STAGES_ID"]]
        start = params.get("start", 0)
        page = [{k: v for k, v in t.items() if not k.startswith("_")} for t in rows[start : start + page_size]]
        body: dict[str, Any] = {"result": {"tasks": page}, "total": len(rows)}
        if start + page_size < len(rows):
            body["next"] = start + page_size
        return body

    return handler


def scrum_task_get(fields: dict[int, dict[str, Any]]):
    def handler(params: dict[str, Any]) -> dict[str, Any]:
        task_id = int(params["id"])
        if task_id not in fields:
            return {"error": 0, "error_description": "Task not found"}
        return {"result": fields[task_id]}

    return handler


def test_php_query_encodes_nested_params():
    query = php_query({"id": 5, "filter": {"GROUP_ID": 3, "%NAME": "план б"}, "select": ["ID", "NAME"], "on": True})
    assert query == (
        "id=5&filter[GROUP_ID]=3&filter[%25NAME]=%D0%BF%D0%BB%D0%B0%D0%BD%20%D0%B1&select[0]=ID&select[1]=NAME&on=Y"
    )


async def test_batch_splits_into_chunks_of_50_and_collects_errors(bitrix):
    bitrix.on("tasks.api.scrum.task.get", handler=scrum_task_get({i: {"storyPoints": str(i)} for i in range(1, 60)}))
    results, errors = await bitrix.client().batch(
        {f"t{i}": ("tasks.api.scrum.task.get", {"id": i}) for i in range(1, 62)}
    )
    assert [len(b) for b in bitrix.batches] == [50, 11]
    assert bitrix.batches[0]["t1"] == "tasks.api.scrum.task.get?id=1"
    assert results["t59"] == {"storyPoints": "59"}
    assert set(errors) == {"t60", "t61"}
    assert str(errors["t60"]) == "0: Task not found"


async def test_projects_list_marks_scrum_groups(connect, bitrix):
    bitrix.on(
        "sonet_group.get",
        [
            {"ID": "40", "NAME": "Мобильное приложение", "PROJECT": "Y", "CLOSED": "N", "NUMBER_OF_MEMBERS": "8"},
            {"ID": "41", "NAME": "Переезд офиса", "PROJECT": "Y", "DESCRIPTION": "x" * 500},
            {"ID": "42", "NAME": "Бухгалтерия", "PROJECT": "N"},
        ],
        total=3,
    )

    def backlog(params):
        if params["id"] == "40":
            return {"result": {"id": 2, "groupId": 40}}
        return {"error": 0, "error_description": "Backlog not found"}

    bitrix.on("tasks.api.scrum.backlog.get", handler=backlog)
    async with connect() as session:
        result = payload(await session.call_tool("projects_list", {"query": "моб"}))

    scrum, project, group = result["projects"]
    assert scrum == {
        "id": 40,
        "name": "Мобильное приложение",
        "type": "scrum",
        "type_name": "скрам",
        "closed": False,
        "members": "8",
    }
    assert (project["type"], group["type"]) == ("project", "group")
    assert len(project["description"]) == 301
    assert result["total"] == 3
    [params] = bitrix.called("sonet_group.get")
    assert params["FILTER"] == {"ACTIVE": "Y", "CLOSED": "N", "%NAME": "моб"}
    assert len(bitrix.batches) == 1


async def test_project_board_groups_tasks_by_stage(connect, bitrix):
    tasks = [task(1, 11), task(2, 0), task(3, 12, priority="2"), task(4, 99)] + [task(i, 13) for i in range(5, 60)]
    bitrix.on("task.stages.get", PROJECT_STAGES)
    bitrix.on("tasks.task.list", handler=task_list(tasks))
    async with connect() as session:
        board = payload(await session.call_tool("project_board", {"group_id": 40}))

    assert [s["title"] for s in board["stages"]] == ["Новые", "В работе", "Готово"]
    new, work, done = board["stages"]
    assert [t["id"] for t in new["tasks"]] == [1, 2]  # stage 0 is the first column
    assert work["tasks"] == [
        {"id": 3, "title": "Задача 3", "status": "Выполняется", "responsible": "Анна Смирнова", "high_priority": True}
    ]
    assert done["task_count"] == 55
    assert [t["id"] for t in board["tasks_without_stage"]] == [4]
    assert board["tasks_loaded"] == board["tasks_total"] == 59
    assert board["truncated"] is False
    list_calls = bitrix.called("tasks.task.list")
    assert [c["start"] for c in list_calls] == [0, 50]
    assert list_calls[0]["filter"] == {"GROUP_ID": 40, "!REAL_STATUS": 5}


async def test_project_board_respects_max_tasks(connect, bitrix):
    bitrix.on("task.stages.get", PROJECT_STAGES)
    bitrix.on("tasks.task.list", handler=task_list([task(i, 11) for i in range(1, 121)]))
    async with connect() as session:
        board = payload(await session.call_tool("project_board", {"group_id": 40, "max_tasks": 60}))
    assert board["tasks_loaded"] == 60
    assert board["tasks_total"] == 120
    assert board["truncated"] is True
    assert len(bitrix.called("tasks.task.list")) == 2


async def test_task_move_stage_with_confirmation(connect, bitrix):
    bitrix.on("tasks.task.get", {"task": task(7, 0)})
    bitrix.on("task.stages.get", PROJECT_STAGES)
    bitrix.on("task.stages.movetask", True)
    async with connect() as session:
        pending = payload(await session.call_tool("task_move_stage", {"id": 7, "stage_id": 12}))
        assert "Перенос задачи #7 «Задача 7» в канбане проекта #40" in pending["preview"]
        assert "Стадия: Новые → В работе" in pending["preview"]
        assert bitrix.called("task.stages.movetask") == []
        done = payload(await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]}))
    assert done["result"] == {"id": 7, "stage_id": 12, "stage": "В работе"}
    assert bitrix.called("task.stages.movetask") == [{"id": 7, "stageId": 12}]


async def test_task_move_stage_rejects_unknown_stage_and_tasks_outside_projects(connect, bitrix):
    bitrix.on("task.stages.get", PROJECT_STAGES)
    bitrix.on("tasks.task.get", {"task": task(7, 0)})
    async with connect() as session:
        wrong_stage = await session.call_tool("task_move_stage", {"id": 7, "stage_id": 51})
        bitrix.on("tasks.task.get", {"task": task(8, 0, groupId="0")})
        no_project = await session.call_tool("task_move_stage", {"id": 8, "stage_id": 12})
    assert wrong_stage.isError
    assert "11 «Новые», 12 «В работе», 13 «Готово»" in wrong_stage.content[0].text
    assert no_project.isError
    assert "не входит в проект" in no_project.content[0].text


async def test_scrum_sprints_treats_no_items_error_as_empty(connect, bitrix):
    bitrix.on("tasks.api.scrum.sprint.list", handler=lambda _: NO_ITEMS)
    async with connect() as session:
        assert payload(await session.call_tool("scrum_sprints", {"group_id": 40, "status": "planned"})) == []
    assert bitrix.called("tasks.api.scrum.sprint.list") == [
        {"filter": {"GROUP_ID": 40, "STATUS": "planned"}, "order": {"DATE_START": "desc"}}
    ]


def setup_sprint(bitrix, tasks, scrum_fields, *, stage_filter=True):
    bitrix.on("tasks.api.scrum.sprint.list", [SPRINT])
    bitrix.on("tasks.api.scrum.sprint.get", SPRINT)
    bitrix.on("tasks.api.scrum.kanban.getStages", SPRINT_STAGES)
    bitrix.on("tasks.task.list", handler=task_list(tasks, stage_filter=stage_filter))
    bitrix.on("tasks.api.scrum.task.get", handler=scrum_task_get(scrum_fields))
    bitrix.on("tasks.api.scrum.epic.list", [{"id": 9, "groupId": 40, "name": "Онбординг"}])


async def test_sprint_board_reads_columns_from_the_board(connect, bitrix):
    # Task 4 sits in "Готово" on the board, but its own stageId was reset to 0 (as after kanban.addTask).
    tasks = [
        task(1, 51, _board_column=51),
        task(2, 52, _board_column=52),
        task(3, 52, _board_column=52),
        task(4, 0, _board_column=53),
    ]
    fields = {
        1: {"entityId": 5, "storyPoints": "3", "epicId": 9},
        2: {"entityId": 5, "storyPoints": "5", "epicId": 0},
        3: {"entityId": 5, "storyPoints": "?", "epicId": 77},
        4: {"entityId": 5, "storyPoints": "2"},
    }
    setup_sprint(bitrix, tasks, fields)
    async with connect() as session:
        board = payload(await session.call_tool("sprint_board", {"group_id": 40}))

    assert board["sprint"]["name"] == "Спринт 7"
    assert board["sprint"]["status_name"] == "активный"
    new, work, done = board["stages"]
    assert [t["id"] for t in new["tasks"]] == [1]
    assert [t["id"] for t in done["tasks"]] == [4]
    assert new["tasks"][0]["story_points"] == "3"
    assert new["tasks"][0]["epic"] == "Онбординг"
    assert new["story_points"] == 3
    assert work["story_points"] == 5  # "?" is not a number
    assert work["tasks"][1]["epic"] == 77  # unknown epic keeps its ID
    assert "notes" not in board and "tasks_without_stage" not in board
    list_filters = [c["filter"] for c in bitrix.called("tasks.task.list")]
    assert list_filters[0] == {"GROUP_ID": 40, "SPRINT_ID": 5}
    assert sorted(f["STAGES_ID"] for f in list_filters[1:]) == [51, 52, 53]
    assert len(bitrix.batches) == 1
    assert bitrix.called("tasks.api.scrum.sprint.list")[0]["filter"] == {"GROUP_ID": 40, "STATUS": "active"}


async def test_sprint_board_falls_back_to_stage_filter(connect, bitrix):
    tasks = [task(1, 0, _board_stage=52), task(2, 0, _board_stage=53), task(3, 0)]
    setup_sprint(bitrix, tasks, {})
    async with connect() as session:
        board = payload(await session.call_tool("sprint_board", {"sprint_id": 5}))
    new, work, done = board["stages"]
    assert [t["id"] for t in work["tasks"]] == [1]
    assert [t["id"] for t in done["tasks"]] == [2]
    assert new["tasks"] == []
    assert [t["id"] for t in board["tasks_without_stage"]] == [3]
    filters = [c["filter"] for c in bitrix.called("tasks.task.list")]
    assert sorted(f["STAGES_ID"] for f in filters if "STAGES_ID" in f) == [51, 52, 53]
    assert sorted(f["STAGE_ID"] for f in filters if "STAGE_ID" in f) == [51, 52, 53]


async def test_sprint_board_falls_back_to_task_stage_ids(connect, bitrix):
    tasks = [task(1, 51), task(2, 52), task(3, 0)]
    setup_sprint(bitrix, tasks, {})
    async with connect() as session:
        board = payload(await session.call_tool("sprint_board", {"sprint_id": 5}))
    new, work, _ = board["stages"]
    assert [t["id"] for t in new["tasks"]] == [1]
    assert [t["id"] for t in work["tasks"]] == [2]
    assert [t["id"] for t in board["tasks_without_stage"]] == [3]  # 0 is not the first column in a sprint


async def test_sprint_board_without_stage_information(connect, bitrix):
    tasks = [task(1, 0), task(2, 0)]
    setup_sprint(bitrix, tasks, {}, stage_filter=False)
    async with connect() as session:
        board = payload(await session.call_tool("sprint_board", {"sprint_id": 5}))
    assert [s["title"] for s in board["stages"]] == ["Новые", "В работе", "Готово"]
    assert [t["id"] for t in board["tasks"]] == [1, 2]
    assert board["notes"] == ["Портал не сообщил, на какой стадии находится каждая задача"]


async def test_sprint_board_needs_an_active_sprint(connect, bitrix):
    bitrix.on("tasks.api.scrum.sprint.list", handler=lambda _: NO_ITEMS)
    async with connect() as session:
        result = await session.call_tool("sprint_board", {"group_id": 40})
        missing_args = await session.call_tool("sprint_board", {})
    assert result.isError
    assert "нет активного спринта" in result.content[0].text
    assert missing_args.isError


def board_move(tasks: list[dict[str, Any]], *, moves: bool = True):
    """task.stages.movetask that moves the card on the fake board and updates the task's own stage."""

    def handler(params: dict[str, Any]) -> dict[str, Any]:
        if moves:
            for t in tasks:
                # Like the portal, movetask leaves alone a task that is not on the board.
                if int(t["id"]) == params["id"] and "_board_column" in t:
                    t["_board_column"] = params["stageId"]
                    t["stageId"] = str(params["stageId"])
        return {"result": True}

    return handler


async def test_sprint_move_task(connect, bitrix):
    tasks = [task(3, 0, _board_column=51)]  # on the board in "Новые" with a stale stageId
    setup_sprint(bitrix, tasks, {3: {"entityId": 5}})
    bitrix.on("tasks.task.get", {"task": task(3, 0)})
    bitrix.on("task.stages.movetask", handler=board_move(tasks))
    async with connect() as session:
        pending = payload(await session.call_tool("sprint_move_task", {"sprint_id": 5, "task_id": 3, "stage_id": 53}))
        assert "Перенос задачи #3 «Задача 3» в спринте «Спринт 7»" in pending["preview"]
        assert "Стадия: Новые → Готово" in pending["preview"]
        assert bitrix.called("task.stages.movetask") == []
        done = payload(await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]}))
    assert done["result"] == {"task_id": 3, "sprint_id": 5, "stage_id": 53, "stage": "Готово"}
    assert bitrix.called("task.stages.movetask") == [{"id": 3, "stageId": 53}]
    assert bitrix.called("tasks.api.scrum.kanban.deleteTask") == []
    assert bitrix.called("tasks.api.scrum.kanban.addTask") == []
    assert tasks[0]["stageId"] == "53"


async def test_sprint_move_task_puts_a_task_on_the_board_first(connect, bitrix):
    tasks = [task(3, 0), task(4, 51, _board_column=51)]  # task 3 is in the sprint but not on the board

    def add_task(params):
        for t in tasks:
            if int(t["id"]) == params["taskId"]:
                t["_board_column"] = params["stageId"]  # stageId stays 0, as on the portal
        return {"result": True}

    setup_sprint(bitrix, tasks, {3: {"entityId": 5}})
    bitrix.on(
        "tasks.task.get", handler=lambda p: {"result": {"task": next(t for t in tasks if int(t["id"]) == p["taskId"])}}
    )
    bitrix.on("tasks.api.scrum.kanban.addTask", handler=add_task)
    bitrix.on("task.stages.movetask", handler=board_move(tasks))
    async with connect() as session:
        pending = payload(await session.call_tool("sprint_move_task", {"sprint_id": 5, "task_id": 3, "stage_id": 51}))
        assert "Стадия: нет на доске → Новые" in pending["preview"]
        done = payload(await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]}))
    assert done["result"]["stage"] == "Новые"
    assert bitrix.called("tasks.api.scrum.kanban.addTask") == [{"sprintId": 5, "taskId": 3, "stageId": 51}]
    assert bitrix.called("task.stages.movetask") == [{"id": 3, "stageId": 51}]
    assert tasks[0]["stageId"] == "51"


async def test_sprint_move_task_reports_when_the_board_did_not_change(connect, bitrix):
    tasks = [task(3, 52, _board_column=52)]
    setup_sprint(bitrix, tasks, {3: {"entityId": 5}})
    bitrix.on("tasks.task.get", {"task": task(3, 52)})
    bitrix.on("task.stages.movetask", handler=board_move(tasks, moves=False))
    async with connect(confirm_tasks=False) as session:
        result = await session.call_tool("sprint_move_task", {"sprint_id": 5, "task_id": 3, "stage_id": 53})
    assert result.isError
    assert "на доске спринта задача сейчас «В работе»" in result.content[0].text


async def test_sprint_move_task_passes_on_refusals(connect, bitrix):
    setup_sprint(bitrix, [task(3, 52, _board_column=52)], {3: {"entityId": 5}})
    bitrix.on("tasks.task.get", {"task": task(3, 52)})
    bitrix.on(
        "task.stages.movetask",
        handler=lambda _: {"error": "ACCESS_DENIED_MOVE", "error_description": "You cannot move this task"},
    )
    async with connect(confirm_tasks=False) as session:
        result = await session.call_tool("sprint_move_task", {"sprint_id": 5, "task_id": 3, "stage_id": 53})
    assert result.isError
    assert "ACCESS_DENIED_MOVE" in result.content[0].text
    assert len(bitrix.called("task.stages.movetask")) == 1


async def test_sprint_move_task_checks_sprint_membership(connect, bitrix):
    setup_sprint(bitrix, [task(3, 0)], {3: {"entityId": 2}})
    bitrix.on("tasks.task.get", {"task": task(3, 0)})
    async with connect() as session:
        result = await session.call_tool("sprint_move_task", {"sprint_id": 5, "task_id": 3, "stage_id": 52})
    assert result.isError
    assert "не входит в спринт #5" in result.content[0].text


async def test_scrum_backlog_in_priority_order(connect, bitrix):
    bitrix.on("tasks.api.scrum.backlog.get", {"id": 2, "groupId": 40})
    bitrix.on("tasks.task.list", handler=task_list([task(1), task(2), task(3)]))
    bitrix.on(
        "tasks.api.scrum.task.get",
        handler=scrum_task_get(
            {1: {"entityId": 2, "sort": 3}, 2: {"entityId": 2, "sort": 1, "storyPoints": "8"}, 3: {"entityId": 2}}
        ),
    )
    bitrix.on("tasks.api.scrum.epic.list", handler=lambda _: NO_ITEMS)
    async with connect() as session:
        backlog = payload(await session.call_tool("scrum_backlog", {"group_id": 40}))
    assert backlog["backlog_id"] == 2
    assert [t["id"] for t in backlog["tasks"]] == [3, 2, 1]
    assert backlog["tasks"][1]["story_points"] == "8"
    assert bitrix.called("tasks.task.list")[0]["filter"] == {"GROUP_ID": 40, "BACKLOG_ID": 2}
