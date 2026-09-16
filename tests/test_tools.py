import mcp.types as types
from mcp.shared.memory import create_connected_server_and_client_session

from bitrix24_mcp_server.approval import ApprovalGate
from bitrix24_mcp_server.server import create_server

from .conftest import payload

DEAL = {"id": 12, "title": "Поставка", "stageId": "NEW", "opportunity": 1000, "comments": None, "contactIds": []}


def elicitation_answer(approve: bool | None, seen: list[str] | None = None):
    async def callback(context, params: types.ElicitRequestParams):
        if seen is not None:
            seen.append(params.message)
        if approve is None:
            return types.ElicitResult(action="cancel")
        return types.ElicitResult(action="accept", content={"approve": approve})

    return callback


async def test_write_tools_are_marked_as_non_read_only(connect):
    async with connect() as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
    assert tools["crm_list"].annotations.readOnlyHint is True
    for name in ("crm_create", "crm_update", "crm_delete", "task_create", "task_delete", "confirm_action"):
        assert tools[name].annotations.readOnlyHint is False, name
    assert tools["crm_delete"].annotations.destructiveHint is True


async def test_crm_list_drops_empty_fields_and_paginates(connect, bitrix):
    items = [{"id": i, "title": f"Сделка {i}", "comments": None, "contactIds": []} for i in range(50)]
    bitrix.on("crm.item.list", {"items": items}, total=120, next=50)
    async with connect() as session:
        result = payload(
            await session.call_tool(
                "crm_list", {"entity": "deal", "filter": {"stageId": "NEW"}, "start": 50, "limit": 10}
            )
        )
    assert result["items"][0] == {"id": 0, "title": "Сделка 0"}
    assert len(result["items"]) == 10
    assert result["total"] == 120
    assert result["next_start"] == 60
    assert bitrix.called("crm.item.list") == [
        {"entityTypeId": 2, "filter": {"stageId": "NEW"}, "select": ["*"], "order": {"id": "desc"}, "start": 50}
    ]


async def test_crm_update_without_elicitation_waits_for_confirm_action(connect, bitrix):
    bitrix.on("crm.item.get", {"item": DEAL})
    bitrix.on("crm.item.update", {"item": {**DEAL, "stageId": "WON"}})
    async with connect() as session:
        pending = payload(
            await session.call_tool("crm_update", {"entity": "deal", "id": 12, "fields": {"stageId": "WON"}})
        )
        assert pending["status"] == "confirmation_required"
        assert "Изменение сделки #12 «Поставка»" in pending["preview"]
        assert "stageId: NEW → WON" in pending["preview"]
        assert bitrix.called("crm.item.update") == []

        done = payload(await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]}))
        assert done["status"] == "done"
        assert done["result"]["item"]["stageId"] == "WON"
        assert bitrix.called("crm.item.update") == [{"entityTypeId": 2, "id": 12, "fields": {"stageId": "WON"}}]

        again = await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]})
        assert again.isError
    assert len(bitrix.called("crm.item.update")) == 1


async def test_cancel_action_discards_operation(connect, bitrix):
    bitrix.on("crm.item.get", {"item": DEAL})
    bitrix.on("crm.item.delete", [])
    async with connect() as session:
        pending = payload(await session.call_tool("crm_delete", {"entity": "deal", "id": 12}))
        cancelled = payload(await session.call_tool("cancel_action", {"confirmation_id": pending["confirmation_id"]}))
        assert cancelled["status"] == "cancelled"
        assert (await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]})).isError
    assert bitrix.called("crm.item.delete") == []


async def test_crm_delete_with_elicitation_accepted(connect, bitrix):
    bitrix.on("crm.item.get", {"item": DEAL})
    bitrix.on("crm.item.delete", [])
    prompts: list[str] = []
    async with connect(elicitation_callback=elicitation_answer(True, prompts)) as session:
        result = payload(await session.call_tool("crm_delete", {"entity": "deal", "id": 12}))
    assert result == {"status": "done", "result": {"id": 12, "deleted": True}}
    assert prompts and prompts[0].startswith("Удаление сделки #12 «Поставка»")
    assert bitrix.called("crm.item.delete") == [{"entityTypeId": 2, "id": 12}]


async def test_crm_create_with_elicitation_declined_or_cancelled(connect, bitrix):
    bitrix.on("crm.item.add", {"item": {"id": 99}})
    for answer in (False, None):
        async with connect(elicitation_callback=elicitation_answer(answer)) as session:
            result = payload(await session.call_tool("crm_create", {"entity": "contact", "fields": {"name": "Иван"}}))
        assert result["status"] == "rejected"
    assert bitrix.called("crm.item.add") == []


async def test_token_mode_ignores_client_elicitation(connect, bitrix):
    bitrix.on("crm.item.add", {"item": {"id": 99}})
    async with connect(mode="token", elicitation_callback=elicitation_answer(True)) as session:
        result = payload(await session.call_tool("crm_create", {"entity": "lead", "fields": {"title": "Заявка"}}))
    assert result["status"] == "confirmation_required"
    assert bitrix.called("crm.item.add") == []


async def test_update_of_missing_item_fails_before_asking(connect, bitrix):
    bitrix.on("crm.item.get", handler=lambda _: {"error": "NOT_FOUND", "error_description": "Element not found"})
    async with connect() as session:
        result = await session.call_tool("crm_update", {"entity": "deal", "id": 404, "fields": {"title": "x"}})
    assert result.isError
    assert "NOT_FOUND" in result.content[0].text


async def test_crm_add_comment_requires_approval(connect, bitrix):
    bitrix.on("crm.item.get", {"item": {"id": 3, "name": "Иван", "lastName": "Петров"}})
    bitrix.on("crm.timeline.comment.add", 555)
    async with connect() as session:
        pending = payload(
            await session.call_tool("crm_add_comment", {"entity": "contact", "id": 3, "text": "Перезвонить"})
        )
        assert "Комментарий к контакту #3 «Иван Петров»" in pending["preview"]
        done = payload(await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]}))
    assert done["result"] == {"comment_id": 555}
    assert bitrix.called("crm.timeline.comment.add") == [
        {"fields": {"ENTITY_ID": 3, "ENTITY_TYPE": "contact", "COMMENT": "Перезвонить"}}
    ]


async def test_crm_stages_for_all_deal_pipelines(connect, bitrix):
    bitrix.on("crm.category.list", {"categories": [{"id": 0, "name": "Общая"}, {"id": 3, "name": "Опт"}]})
    bitrix.on(
        "crm.status.list",
        handler=lambda p: {
            "result": [{"STATUS_ID": f"{p['filter']['ENTITY_ID']}:NEW", "NAME": "Новая", "EXTRA": {"SEMANTICS": None}}]
        },
    )
    async with connect() as session:
        result = payload(await session.call_tool("crm_stages", {}))
    assert [c["name"] for c in result] == ["Общая", "Опт"]
    assert result[1]["stages"] == [{"id": "DEAL_STAGE_3:NEW", "name": "Новая"}]
    assert {p["filter"]["ENTITY_ID"] for p in bitrix.called("crm.status.list")} == {"DEAL_STAGE", "DEAL_STAGE_3"}


async def test_crm_find_by_contact_info(connect, bitrix):
    bitrix.on("crm.duplicate.findbycomm", {"CONTACT": ["3", "8"]})
    async with connect() as session:
        result = payload(
            await session.call_tool(
                "crm_find_by_contact_info", {"type": "PHONE", "values": ["+79990000000"], "entity": "contact"}
            )
        )
    assert result == {"contact": [3, 8]}
    assert bitrix.called("crm.duplicate.findbycomm") == [
        {"type": "PHONE", "values": ["+79990000000"], "entity_type": "CONTACT"}
    ]


async def test_task_update_preview_maps_upper_case_fields(connect, bitrix):
    bitrix.on("tasks.task.get", {"task": {"id": "8", "title": "Отчёт", "responsibleId": "1", "deadline": None}})
    async with connect() as session:
        pending = payload(
            await session.call_tool("task_update", {"id": 8, "fields": {"RESPONSIBLE_ID": 5, "DEADLINE": "2026-10-01"}})
        )
    assert "Изменение задачи #8 «Отчёт»" in pending["preview"]
    assert "RESPONSIBLE_ID: 1 → 5" in pending["preview"]
    assert "DEADLINE: — → 2026-10-01" in pending["preview"]
    assert bitrix.called("tasks.task.update") == []


async def test_task_writes_can_skip_approval(connect, bitrix):
    bitrix.on("tasks.task.add", {"task": {"id": "31", "title": "Позвонить", "status": "2"}})
    async with connect(confirm_tasks=False) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        assert "подтверждения" not in tools["task_create"].description
        assert "подтверждения" in tools["crm_create"].description
        result = payload(
            await session.call_tool("task_create", {"fields": {"TITLE": "Позвонить", "RESPONSIBLE_ID": 1}})
        )
    assert result["status"] == "done"
    assert result["result"]["task"]["statusName"] == "Ждёт выполнения"


async def test_task_create_requires_title(connect, bitrix):
    async with connect() as session:
        result = await session.call_tool("task_create", {"fields": {"RESPONSIBLE_ID": 1}})
    assert result.isError
    assert bitrix.calls == []


async def test_task_comment_falls_back_to_classic_comments(connect, bitrix):
    bitrix.on("tasks.task.get", {"task": {"id": "8", "title": "Отчёт"}})
    bitrix.on("task.commentitem.add", 77)
    async with connect(confirm_tasks=False) as session:
        result = payload(await session.call_tool("task_add_comment", {"id": 8, "text": "Готово"}))
    assert result["result"] == {"id": 8, "sent_to": "comments", "comment_id": 77}
    assert bitrix.called("v3:tasks.task.chat.message.send") == [{"fields": {"taskId": 8, "text": "Готово"}}]
    assert bitrix.called("task.commentitem.add") == [{"TASKID": 8, "FIELDS": {"POST_MESSAGE": "Готово"}}]


async def test_task_comment_goes_to_task_chat(connect, bitrix):
    bitrix.on("tasks.task.get", {"task": {"id": "8", "title": "Отчёт"}})
    bitrix.on("v3:tasks.task.chat.message.send", {"result": True})
    async with connect(confirm_tasks=False) as session:
        result = payload(await session.call_tool("task_add_comment", {"id": 8, "text": "Готово"}))
    assert result["result"] == {"id": 8, "sent_to": "task_chat"}


async def test_task_comment_preview_wording(connect, bitrix):
    bitrix.on("tasks.task.get", {"task": {"id": "8", "title": "Отчёт"}})
    async with connect() as session:
        pending = payload(await session.call_tool("task_add_comment", {"id": 8, "text": "Готово"}))
    assert pending["preview"].startswith("Комментарий к задаче #8 «Отчёт»")
    assert bitrix.called("task.commentitem.add") == []


async def test_tasks_list_and_users(connect, bitrix):
    bitrix.on("tasks.task.list", {"tasks": [{"id": "1", "title": "A", "status": "5", "closedDate": None}]}, total=1)
    bitrix.on("user.search", [{"ID": "5", "NAME": "Анна", "LAST_NAME": "Смирнова", "PERSONAL_PHONE": "", "EMAIL": ""}])
    async with connect() as session:
        tasks = payload(await session.call_tool("tasks_list", {"filter": {"RESPONSIBLE_ID": 5}}))
        users = payload(await session.call_tool("users_search", {"query": "Смирнова"}))
    assert tasks == {"tasks": [{"id": "1", "title": "A", "status": "5", "statusName": "Завершена"}], "total": 1,
                     "next_start": None}  # fmt: skip
    assert users == [{"ID": "5", "NAME": "Анна", "LAST_NAME": "Смирнова"}]
    assert bitrix.called("user.search") == [{"FIND": "Смирнова", "ACTIVE": True}]


async def test_missing_configuration_is_reported_by_tools():
    server = create_server(None, ApprovalGate(), config_error="Не задан BITRIX24_WEBHOOK_URL")
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("crm_list", {"entity": "deal"})
    assert result.isError
    assert "BITRIX24_WEBHOOK_URL" in result.content[0].text


async def test_optional_parameters_are_published_without_defaults(connect):
    async with connect() as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
    for tool in tools.values():
        for name, prop in tool.inputSchema["properties"].items():
            assert "default" not in prop and "anyOf" not in prop, (tool.name, name)
    stages = tools["crm_stages"].inputSchema
    assert "required" not in stages
    assert stages["properties"]["category_id"]["type"] == "integer"
    assert stages["properties"]["entity"]["description"].endswith('По умолчанию "deal".')
    assert tools["crm_list"].inputSchema["required"] == ["entity"]


async def test_omitted_optional_parameters_get_python_defaults(connect, bitrix):
    bitrix.on("crm.category.list", {"categories": []})
    async with connect() as session:
        result = await session.call_tool("crm_stages", {"category_id": None})
    assert not result.isError
    assert bitrix.called("crm.category.list") == [{"entityTypeId": 2}]
