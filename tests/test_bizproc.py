from datetime import datetime, timedelta, timezone
from typing import Any

from .conftest import payload

DEAL_TEMPLATE = {
    "ID": "279",
    "NAME": "Рассрочка",
    "MODULE_ID": "crm",
    "ENTITY": "CCrmDocumentDeal",
    "DOCUMENT_TYPE": ["crm", "CCrmDocumentDeal", "DEAL"],
    "AUTO_EXECUTE": "0",
    "PARAMETERS": {
        "product": {"Name": "Продукт", "Type": "string", "Required": "1", "Multiple": "0", "Default": ""},
        "months": {
            "Name": "Срок",
            "Type": "select",
            "Required": "0",
            "Options": {"6": "6 месяцев", "12": "12 месяцев"},
            "Default": "6",
        },
    },
    "VARIABLES": {"Approver": {"Name": "Согласующий", "Type": "user", "Required": "0", "Default": ""}},
    "CONSTANTS": [],
    "TEMPLATE": [
        {
            "Type": "SequentialWorkflowActivity",
            "Name": "Template",
            "Properties": {"Title": "Последовательный бизнес-процесс"},
            "Children": [
                {"Type": "SetFieldActivity", "Name": "A1", "Properties": {"Title": "Изменение документа"}},
                {
                    "Type": "IfElseActivity",
                    "Name": "A2",
                    "Properties": {},
                    "Children": [{"Type": "IfElseBranchActivity", "Name": "A3", "Properties": {"Title": "Да"}}],
                },
            ],
        }
    ],
}
LIST_TEMPLATE = {
    "ID": "301",
    "NAME": "Отпуск",
    "MODULE_ID": "lists",
    "ENTITY": "BizprocDocument",
    "DOCUMENT_TYPE": ["lists", "BizprocDocument", "iblock_19"],
    "AUTO_EXECUTE": "1",
    "PARAMETERS": [],
}


def template_list(templates: list[dict[str, Any]], page_size: int = 50):
    def handler(params: dict[str, Any]) -> dict[str, Any]:
        flt = params.get("FILTER") or {}
        rows = [
            t
            for t in templates
            if all(str(t.get(k)) == str(v) for k, v in flt.items() if k in ("ID", "MODULE_ID", "ENTITY"))
        ]
        start = params.get("start", 0)
        select = params.get("SELECT") or ["ID"]
        page = [{k: t[k] for k in select if k in t} for t in rows[start : start + page_size]]
        body: dict[str, Any] = {"result": page, "total": len(rows)}
        if start + page_size < len(rows):
            body["next"] = start + page_size
        return body

    return handler


def bp_task(**overrides: Any) -> dict[str, Any]:
    return {
        "ID": "1477",
        "NAME": "Согласовать рассрочку",
        "STATUS": "0",
        "USER_ID": "11",
        "ACTIVITY": "ApproveActivity",
        "WORKFLOW_ID": "67a2ffdb2c57a3.35276854",
        "WORKFLOW_TEMPLATE_NAME": "Рассрочка",
        "DOCUMENT_NAME": "Сделка «Поставка»",
        "DOCUMENT_URL": "/crm/deal/details/12/",
        "PARAMETERS": {
            "CommentLabel": "Комментарий",
            "CommentRequired": "YR",
            "ShowComment": "Y",
            "StatusYesLabel": "Одобрить",
            "StatusNoLabel": "Отказать",
        },
        **overrides,
    }


async def test_bp_templates_lists_parameters_and_filters(connect, bitrix):
    extra = [{**LIST_TEMPLATE, "ID": str(400 + i), "NAME": f"Шаблон {i}"} for i in range(55)]
    bitrix.on("bizproc.workflow.template.list", handler=template_list([DEAL_TEMPLATE, LIST_TEMPLATE, *extra]))
    async with connect() as session:
        deals = payload(await session.call_tool("bp_templates", {"document": "deal"}))
        found = payload(await session.call_tool("bp_templates", {"query": "ОТПУ"}))
        everything = payload(await session.call_tool("bp_templates", {}))

    assert deals == [
        {
            "id": 279,
            "name": "Рассрочка",
            "document": "deal",
            "document_type": ["crm", "CCrmDocumentDeal", "DEAL"],
            "auto_execute": "вручную",
            "parameters": [
                {"code": "product", "name": "Продукт", "type": "string", "required": True},
                {
                    "code": "months",
                    "name": "Срок",
                    "type": "select",
                    "options": {"6": "6 месяцев", "12": "12 месяцев"},
                    "default": "6",
                },
            ],
        }
    ]
    assert [t["id"] for t in found] == [301]
    assert found[0]["document"] == "lists:iblock_19"
    assert found[0]["auto_execute"] == "при создании"
    assert len(everything) == 57
    first_call = bitrix.called("bizproc.workflow.template.list")[0]
    assert first_call["FILTER"] == {"MODULE_ID": "crm", "ENTITY": "CCrmDocumentDeal"}


async def test_bp_template_outline_and_full_structure(connect, bitrix):
    bitrix.on("bizproc.workflow.template.list", handler=template_list([DEAL_TEMPLATE]))
    async with connect() as session:
        brief = payload(await session.call_tool("bp_template", {"id": 279}))
        full = payload(await session.call_tool("bp_template", {"id": 279, "detail": "full"}))
        missing = await session.call_tool("bp_template", {"id": 5})

    assert brief["outline"] == [
        "Последовательный бизнес-процесс [SequentialWorkflowActivity]",
        "  Изменение документа [SetFieldActivity]",
        "  [IfElseActivity]",
        "    Да [IfElseBranchActivity]",
    ]
    assert brief["actions_count"] == 4
    assert brief["variables"] == [{"code": "Approver", "name": "Согласующий", "type": "user"}]
    assert "template" not in brief
    assert full["template"] == DEAL_TEMPLATE["TEMPLATE"]
    assert missing.isError
    assert "не найден" in missing.content[0].text


async def test_bp_start_for_a_deal(connect, bitrix):
    bitrix.on("bizproc.workflow.template.list", handler=template_list([DEAL_TEMPLATE]))
    bitrix.on("crm.item.get", {"item": {"id": 12, "title": "Поставка"}})
    bitrix.on("bizproc.workflow.start", "66e81a641752f8.56521481")
    async with connect() as session:
        pending = payload(
            await session.call_tool(
                "bp_start", {"template_id": 279, "element_id": 12, "parameters": {"product": "Курс", "months": "12"}}
            )
        )
        assert pending["preview"].splitlines() == [
            "Запуск бизнес-процесса «Рассрочка» (шаблон #279)",
            "Портал: https://example.bitrix24.ru",
            "• Документ: сделки #12 «Поставка»",
            "• product: Курс",
            "• months: 12",
        ]
        assert bitrix.called("bizproc.workflow.start") == []
        done = payload(await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]}))

    assert done["result"]["workflow_id"] == "66e81a641752f8.56521481"
    assert bitrix.called("bizproc.workflow.start") == [
        {
            "TEMPLATE_ID": 279,
            "DOCUMENT_ID": ["crm", "CCrmDocumentDeal", "DEAL_12"],
            "PARAMETERS": {"product": "Курс", "months": "12"},
        }
    ]


async def test_bp_start_checks_parameters(connect, bitrix):
    bitrix.on("bizproc.workflow.template.list", handler=template_list([DEAL_TEMPLATE]))
    async with connect() as session:
        missing = await session.call_tool("bp_start", {"template_id": 279, "element_id": 12})
        unknown = await session.call_tool(
            "bp_start", {"template_id": 279, "element_id": 12, "parameters": {"product": "x", "color": "red"}}
        )
    assert missing.isError
    assert "обязательные параметры шаблона: product" in missing.content[0].text
    assert unknown.isError
    assert "нет параметров: color" in unknown.content[0].text


async def test_bp_start_for_a_list_element(connect, bitrix):
    bitrix.on("bizproc.workflow.template.list", handler=template_list([LIST_TEMPLATE]))
    bitrix.on("bizproc.workflow.start", "wf.1")
    async with connect(confirm_tasks=False) as session:
        pending = payload(await session.call_tool("bp_start", {"template_id": 301, "element_id": 77}))
        assert "Документ: lists:77" in pending["preview"]  # business processes always need approval
        await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]})
    assert bitrix.called("bizproc.workflow.start")[0]["DOCUMENT_ID"] == ["lists", "BizprocDocument", "77"]


def instance(workflow_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "ID": workflow_id,
        "TEMPLATE_ID": "279",
        "MODULE_ID": "crm",
        "ENTITY": "CCrmDocumentDeal",
        "DOCUMENT_ID": "DEAL_12",
        "STARTED": "2026-09-16T10:00:00+03:00",
        "STARTED_BY": "11",
        "MODIFIED": "2026-09-16T10:05:00+03:00",
        "OWNED_UNTIL": None,
        **fields,
    }


async def test_bp_instances_names_templates_and_filters_documents(connect, bitrix):
    bitrix.on("bizproc.workflow.template.list", handler=template_list([DEAL_TEMPLATE]))
    bitrix.on(
        "bizproc.workflow.instances",
        [instance("wf.1"), instance("wf.2", TEMPLATE_ID="900", STARTED_BY="0")],
        total=2,
    )
    async with connect() as session:
        result = payload(await session.call_tool("bp_instances", {"document": "deal", "element_id": 12}))
        no_type = await session.call_tool("bp_instances", {"element_id": 12})

    first, robot = result["instances"]
    assert first == {
        "id": "wf.1",
        "template_id": 279,
        "template": "Рассрочка",
        "document": "crm:DEAL_12",
        "started": "2026-09-16T10:00:00+03:00",
        "started_by": "11",
        "modified": "2026-09-16T10:05:00+03:00",
    }
    assert robot["template"] == "#900 (роботы CRM или недоступный шаблон)"
    assert "started_by" not in robot
    assert result["next_start"] is None
    assert bitrix.called("bizproc.workflow.instances")[0]["FILTER"] == {
        "MODULE_ID": "crm",
        "ENTITY": "CCrmDocumentDeal",
        "DOCUMENT_ID": "DEAL_12",
    }
    assert no_type.isError


async def test_bp_instances_finds_stuck_processes(connect, bitrix):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(minutes=30)).isoformat()
    fresh = (now + timedelta(minutes=1)).isoformat()
    pages = [
        {"result": [instance(f"wf.{i}", OWNED_UNTIL=old) for i in range(49)] + [instance("wf.new", OWNED_UNTIL=fresh)]},
        {"result": [instance("wf.late", OWNED_UNTIL=old), instance("wf.free")]},
    ]
    bitrix.on("bizproc.workflow.template.list", handler=template_list([DEAL_TEMPLATE]))
    bitrix.on(
        "bizproc.workflow.instances",
        handler=lambda p: {**pages[p["start"] // 50], **({"next": 50} if p["start"] == 0 else {})},
    )
    async with connect() as session:
        result = payload(await session.call_tool("bp_instances", {"stuck_only": True, "limit": 200}))
    ids = [i["id"] for i in result["instances"]]
    assert len(ids) == 50
    assert "wf.new" not in ids and "wf.late" in ids and "wf.free" not in ids
    assert result["total"] == 50
    assert result["instances"][0]["locked_until"] == old
    calls = bitrix.called("bizproc.workflow.instances")
    assert [c["start"] for c in calls] == [0, 50]
    assert calls[0]["ORDER"] == {"OWNED_UNTIL": "desc"}


async def test_bp_terminate_and_kill(connect, bitrix):
    bitrix.on("bizproc.workflow.template.list", handler=template_list([DEAL_TEMPLATE]))
    bitrix.on("bizproc.workflow.instances", [instance("wf.1")])
    bitrix.on("crm.item.get", {"item": {"id": 12, "title": "Поставка"}})
    bitrix.on("bizproc.workflow.terminate", True)
    bitrix.on("bizproc.workflow.kill", True)
    async with connect() as session:
        stop = payload(await session.call_tool("bp_terminate", {"workflow_id": "wf.1", "status": "Отменено"}))
        assert stop["preview"].splitlines() == [
            "Остановка бизнес-процесса wf.1",
            "Портал: https://example.bitrix24.ru",
            "• Шаблон: Рассрочка",
            "• Документ: сделки #12 «Поставка»",
            "• Запущен: 2026-09-16T10:00:00+03:00",
            "• Статус: Отменено",
        ]
        kill = payload(await session.call_tool("bp_kill", {"workflow_id": "wf.1"}))
        assert kill["preview"].startswith("Удаление бизнес-процесса со всеми данными wf.1")
        assert bitrix.called("bizproc.workflow.terminate") == bitrix.called("bizproc.workflow.kill") == []
        await session.call_tool("confirm_action", {"confirmation_id": stop["confirmation_id"]})
        await session.call_tool("confirm_action", {"confirmation_id": kill["confirmation_id"]})
    assert bitrix.called("bizproc.workflow.terminate") == [{"ID": "wf.1", "STATUS": "Отменено"}]
    assert bitrix.called("bizproc.workflow.kill") == [{"ID": "wf.1"}]


async def test_bp_terminate_unknown_workflow(connect, bitrix):
    bitrix.on("bizproc.workflow.instances", [])
    async with connect() as session:
        result = await session.call_tool("bp_terminate", {"workflow_id": "wf.gone"})
    assert result.isError
    assert "не найден" in result.content[0].text


async def test_bp_tasks_describe_decisions_and_fields(connect, bitrix):
    request = bp_task(
        ID="1480",
        ACTIVITY="RequestInformationOptionalActivity",
        PARAMETERS={
            "ShowComment": "N",
            "StatusOkLabel": "Сохранить",
            "Fields": [
                {"Id": "phone", "Name": "Телефон", "Type": "string", "Required": True, "Default": ""},
                {"Id": "size", "Name": "Размер", "Type": "select", "Options": {"s": "S", "m": "M"}},
            ],
        },
    )
    bitrix.on("bizproc.task.list", [bp_task(), request], total=2)
    async with connect() as session:
        result = payload(await session.call_tool("bp_tasks", {}))
    approve, info = result["tasks"]
    assert approve["decisions"] == {"approve": "Одобрить", "reject": "Отказать"}
    assert approve["kind"] == "утверждение"
    assert approve["status"] == "ожидает"
    assert approve["document_url"] == "https://example.bitrix24.ru/crm/deal/details/12/"
    assert approve["comment"] == {"required": "YR", "label": "Комментарий"}
    assert info["decisions"] == {"ok": "Сохранить", "cancel": "Отказаться"}
    assert info["fields"] == [
        {"code": "phone", "name": "Телефон", "type": "string", "required": True},
        {"code": "size", "name": "Размер", "type": "select", "options": {"s": "S", "m": "M"}},
    ]
    assert "comment" not in info
    assert bitrix.called("bizproc.task.list")[0]["FILTER"] == {"STATUS": 0}


async def test_bp_task_complete_validates_before_asking(connect, bitrix):
    tasks = {
        "1477": bp_task(),
        "1478": bp_task(ID="1478", STATUS="1"),
        "1479": bp_task(
            ID="1479",
            ACTIVITY="RequestInformationActivity",
            PARAMETERS={"Fields": [{"Id": "phone", "Name": "Телефон", "Required": True, "Default": ""}]},
        ),
    }
    bitrix.on("bizproc.task.list", handler=lambda p: {"result": [tasks[str(p["FILTER"]["ID"])]]})
    async with connect() as session:
        wrong = await session.call_tool("bp_task_complete", {"task_id": 1477, "decision": "ok"})
        no_comment = await session.call_tool("bp_task_complete", {"task_id": 1477, "decision": "reject"})
        done_already = await session.call_tool("bp_task_complete", {"task_id": 1478, "decision": "approve"})
        no_fields = await session.call_tool("bp_task_complete", {"task_id": 1479, "decision": "ok"})
    assert "допустимы решения: approve, reject" in wrong.content[0].text
    assert "нужен комментарий" in no_comment.content[0].text
    assert "уже не ожидает решения: одобрено" in done_already.content[0].text
    assert "Не заполнены обязательные поля: phone" in no_fields.content[0].text
    assert bitrix.called("bizproc.task.complete") == []


async def test_bp_task_complete_with_confirmation(connect, bitrix):
    bitrix.on("bizproc.task.list", [bp_task()])
    bitrix.on("bizproc.task.complete", True)
    async with connect() as session:
        pending = payload(
            await session.call_tool(
                "bp_task_complete", {"task_id": 1477, "decision": "reject", "comment": "Нет документов"}
            )
        )
        assert pending["preview"].splitlines() == [
            "Задание бизнес-процесса #1477 «Согласовать рассрочку»",
            "Портал: https://example.bitrix24.ru",
            "• Решение: Отказать",
            "• Документ: Сделка «Поставка»",
            "• Процесс: Рассрочка",
            "• Комментарий: Нет документов",
        ]
        await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]})
    assert bitrix.called("bizproc.task.complete") == [{"TASK_ID": 1477, "STATUS": "no", "COMMENT": "Нет документов"}]


async def test_bp_task_delegate(connect, bitrix):
    bitrix.on("bizproc.task.list", handler=lambda p: {"result": [bp_task(ID=str(p["FILTER"]["ID"]))]})
    bitrix.on("user.get", [{"ID": "5", "NAME": "Анна", "LAST_NAME": "Смирнова"}])
    bitrix.on("user.current", {"ID": "11"})
    bitrix.on("bizproc.task.delegate", True)
    async with connect() as session:
        pending = payload(await session.call_tool("bp_task_delegate", {"task_ids": [1477, 1490], "to_user_id": 5}))
        assert pending["preview"].splitlines()[2:] == [
            "• Кому: Анна Смирнова (#5)",
            "• Задание #1477: Согласовать рассрочку — Сделка «Поставка»",
            "• Задание #1490: Согласовать рассрочку — Сделка «Поставка»",
        ]
        await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]})
    assert bitrix.called("bizproc.task.delegate") == [{"TASK_IDS": [1477, 1490], "FROM_USER_ID": 11, "TO_USER_ID": 5}]
