"""Business processes (workflows): templates, running processes and their tasks.

An incoming webhook can read workflow templates but cannot create or change them: Bitrix24 allows
bizproc.workflow.template.add/update/delete only from an application.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .approval import ApprovalPolicy
from .client import Bitrix24Client, Bitrix24Error
from .crm import ENTITY_TYPE_IDS, compact, display_name
from .preview import build_summary, field_lines, format_value, quoted

CrmDocument = Literal["lead", "deal", "contact", "company"]

CRM_ENTITIES = {
    "lead": "CCrmDocumentLead",
    "deal": "CCrmDocumentDeal",
    "contact": "CCrmDocumentContact",
    "company": "CCrmDocumentCompany",
}
_CRM_BY_TYPE = {entity.upper(): entity for entity in CRM_ENTITIES}
_GENITIVE = {"lead": "лида", "deal": "сделки", "contact": "контакта", "company": "компании"}

AUTO_EXECUTE = {"0": "вручную", "1": "при создании", "2": "при изменении", "3": "при создании и изменении"}
TASK_STATUSES = {"0": "ожидает", "1": "одобрено", "2": "отклонено", "3": "выполнено", "4": "просрочено"}
TASK_KINDS = {
    "ApproveActivity": "утверждение",
    "ReviewActivity": "ознакомление",
    "RequestInformationActivity": "запрос информации",
    "RequestInformationOptionalActivity": "запрос информации с возможностью отказа",
}
# Which decisions each task kind accepts, and the REST status for each decision.
TASK_DECISIONS = {
    "ApproveActivity": {"approve": "yes", "reject": "no"},
    "ReviewActivity": {"ok": "ok"},
    "RequestInformationActivity": {"ok": "ok"},
    "RequestInformationOptionalActivity": {"ok": "ok", "cancel": "cancel"},
}
DECISION_LABELS = {
    "approve": ("StatusYesLabel", "Утвердить"),
    "reject": ("StatusNoLabel", "Отклонить"),
    "ok": ("StatusOkLabel", "Выполнено"),
    "cancel": ("StatusCancelLabel", "Отказаться"),
}

TEMPLATE_FIELDS = ["ID", "NAME", "MODULE_ID", "ENTITY", "DOCUMENT_TYPE", "AUTO_EXECUTE", "MODIFIED", "SYSTEM_CODE"]
INSTANCE_FIELDS = ["ID", "TEMPLATE_ID", "MODULE_ID", "ENTITY", "DOCUMENT_ID", "STARTED", "STARTED_BY", "MODIFIED"]
TASK_FIELDS = [
    "ID",
    "NAME",
    "DESCRIPTION",
    "STATUS",
    "USER_ID",
    "USER_STATUS",
    "ACTIVITY",
    "PARAMETERS",
    "WORKFLOW_ID",
    "WORKFLOW_TEMPLATE_ID",
    "WORKFLOW_TEMPLATE_NAME",
    "WORKFLOW_STATE",
    "DOCUMENT_NAME",
    "DOCUMENT_URL",
    "MODULE_ID",
    "ENTITY",
    "DOCUMENT_ID",
    "OVERDUE_DATE",
    "MODIFIED",
]
MAX_TEMPLATES = 500
STUCK_AFTER = timedelta(minutes=5)
# CRM automation rules run as workflows too, but their templates are not available through REST.
UNLISTED_TEMPLATE = "роботы CRM или недоступный шаблон"

TemplateIdArg = Annotated[int, Field(description="ID шаблона бизнес-процесса (из bp_templates)", gt=0)]
WorkflowIdArg = Annotated[
    str, Field(description="ID запущенного процесса, например 66e412fdc9bd44.36306599 (из bp_instances)", min_length=1)
]
TaskIdArg = Annotated[int, Field(description="ID задания бизнес-процесса (из bp_tasks)", gt=0)]

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def is_yes(value: Any) -> bool:
    return value in (True, 1, "1", "Y", "y", "true")


def document_type_label(module: str, entity: str, document_type: str) -> str:
    if module == "crm" and document_type in _CRM_BY_TYPE:
        return _CRM_BY_TYPE[document_type]
    return f"{module}:{document_type}"


def describe_fields(fields: Any) -> list[dict[str, Any]]:
    """Template parameters, variables and constants come as an object keyed by field code."""
    if not isinstance(fields, dict):
        return []
    result = []
    for code, field in fields.items():
        field = field or {}
        options = field.get("Options")
        result.append(
            compact(
                {
                    "code": code,
                    "name": field.get("Name") or field.get("Title"),
                    "description": field.get("Description"),
                    "type": field.get("Type"),
                    "required": True if is_yes(field.get("Required")) else None,
                    "multiple": True if is_yes(field.get("Multiple")) else None,
                    "options": options if isinstance(options, dict) else None,
                    "default": field.get("Default"),
                }
            )
        )
    return result


def describe_template(template: dict[str, Any]) -> dict[str, Any]:
    document_type = template.get("DOCUMENT_TYPE") or [template.get("MODULE_ID"), template.get("ENTITY"), ""]
    module, entity, doc_type = (list(document_type) + ["", "", ""])[:3]
    return compact(
        {
            "id": int(template["ID"]),
            "name": template.get("NAME"),
            "document": document_type_label(module, entity, doc_type),
            "document_type": [module, entity, doc_type],
            "auto_execute": AUTO_EXECUTE.get(str(template.get("AUTO_EXECUTE")), template.get("AUTO_EXECUTE")),
            "modified": template.get("MODIFIED"),
            "system_code": template.get("SYSTEM_CODE"),
            "parameters": describe_fields(template.get("PARAMETERS")) if "PARAMETERS" in template else None,
        }
    )


def outline(activity: dict[str, Any], depth: int = 0, lines: list[str] | None = None) -> list[str]:
    """Readable tree of a template: one line per action with its title and type."""
    lines = [] if lines is None else lines
    title = (activity.get("Properties") or {}).get("Title") or ""
    label = f"{title} [{activity.get('Type')}]" if title else f"[{activity.get('Type')}]"
    lines.append("  " * depth + label)
    for child in activity.get("Children") or []:
        outline(child, depth + 1, lines)
    return lines


def count_actions(activities: list[dict[str, Any]]) -> int:
    return sum(1 + count_actions(a.get("Children") or []) for a in activities)


def document_id(template: dict[str, Any], element_id: int) -> list[str]:
    module, entity, doc_type = describe_template(template)["document_type"]
    if module == "crm":
        return [module, entity, f"{doc_type}_{element_id}"]
    return [module, entity, str(element_id)]


def is_stuck(owned_until: Any, now: datetime) -> bool:
    if not owned_until:
        return False
    try:
        locked = datetime.fromisoformat(str(owned_until))
    except ValueError:
        return False
    if locked.tzinfo is None:
        locked = locked.replace(tzinfo=timezone.utc)
    return now - locked > STUCK_AFTER


def template_label(template_id: int, names: dict[int, str]) -> str | None:
    if not template_id:
        return None
    return names.get(template_id) or f"#{template_id} ({UNLISTED_TEMPLATE})"


def describe_task(task: dict[str, Any], portal_url: str) -> dict[str, Any]:
    params = task.get("PARAMETERS") or {}
    kind = task.get("ACTIVITY")
    url = task.get("DOCUMENT_URL")
    decisions = {
        decision: params.get(DECISION_LABELS[decision][0]) or DECISION_LABELS[decision][1]
        for decision in TASK_DECISIONS.get(kind, {})
    }
    return compact(
        {
            "id": int(task["ID"]),
            "name": task.get("NAME"),
            "description": task.get("DESCRIPTION"),
            "kind": TASK_KINDS.get(kind, kind),
            "status": TASK_STATUSES.get(str(task.get("STATUS")), task.get("STATUS")),
            "user_id": task.get("USER_ID"),
            "document": task.get("DOCUMENT_NAME"),
            "document_url": f"{portal_url}{url}" if url and url.startswith("/") else url,
            "workflow_id": task.get("WORKFLOW_ID"),
            "template": task.get("WORKFLOW_TEMPLATE_NAME"),
            "workflow_state": task.get("WORKFLOW_STATE"),
            "overdue_date": task.get("OVERDUE_DATE"),
            "decisions": decisions,
            "comment": (
                {"required": params.get("CommentRequired"), "label": params.get("CommentLabel")}
                if is_yes(params.get("ShowComment")) or params.get("CommentRequired") not in (None, "N")
                else None
            ),
            "fields": [
                compact(
                    {
                        "code": f.get("Id"),
                        "name": f.get("Name"),
                        "type": f.get("Type"),
                        "required": True if f.get("Required") else None,
                        "multiple": True if f.get("Multiple") else None,
                        "options": f.get("Options") if f.get("Type") == "select" else None,
                        "default": f.get("Default"),
                    }
                )
                for f in params.get("Fields") or []
            ],
        }
    )


async def find_stuck(
    client: Bitrix24Client, instance_filter: dict[str, Any], now: datetime, limit: int
) -> list[dict[str, Any]]:
    """Locked processes come first when sorted by lock time, so the scan stops at the first unlocked one."""
    stuck: list[dict[str, Any]] = []
    start = 0
    while len(stuck) < limit:
        response = await client.call_raw(
            "bizproc.workflow.instances",
            {
                "SELECT": [*INSTANCE_FIELDS, "OWNED_UNTIL"],
                "FILTER": instance_filter,
                "ORDER": {"OWNED_UNTIL": "desc"},
                "start": start,
            },
        )
        page = response.get("result") or []
        locked = [i for i in page if i.get("OWNED_UNTIL")]
        stuck.extend(i for i in locked if is_stuck(i["OWNED_UNTIL"], now))
        if len(locked) < len(page) or response.get("next") is None:
            break
        start = int(response["next"])
    return stuck[:limit]


async def load_pages(
    client: Bitrix24Client, method: str, params: dict[str, Any], limit: int
) -> tuple[list[dict[str, Any]], int]:
    items: list[dict[str, Any]] = []
    start = int(params.get("start", 0))
    total = 0
    while len(items) < limit:
        response = await client.call_raw(method, {**params, "start": start})
        page = response.get("result") or []
        items.extend(page)
        total = int(response.get("total") or len(items))
        if not page or response.get("next") is None:
            break
        start = int(response["next"])
    return items[:limit], max(total, len(items))


def register_bizproc_tools(mcp: FastMCP, get_client: Callable[[], Bitrix24Client], approval: ApprovalPolicy) -> None:
    async def fetch_template(template_id: int, select: list[str]) -> dict[str, Any]:
        found = await get_client().call(
            "bizproc.workflow.template.list", {"SELECT": select, "FILTER": {"ID": template_id}}
        )
        if not found:
            raise ToolError(f"Шаблон бизнес-процесса #{template_id} не найден")
        return found[0]

    async def template_names(template_ids: set[int]) -> dict[int, str]:
        if not template_ids:
            return {}
        templates, _ = await load_pages(
            get_client(), "bizproc.workflow.template.list", {"SELECT": ["ID", "NAME"]}, MAX_TEMPLATES
        )
        return {int(t["ID"]): t.get("NAME") for t in templates if int(t["ID"]) in template_ids}

    async def fetch_instance(workflow_id: str) -> dict[str, Any]:
        found = await get_client().call(
            "bizproc.workflow.instances", {"SELECT": INSTANCE_FIELDS, "FILTER": {"ID": workflow_id}}
        )
        if not found:
            raise ToolError(f"Запущенный бизнес-процесс {workflow_id} не найден: возможно, он уже завершён")
        return found[0]

    async def fetch_task(task_id: int) -> dict[str, Any]:
        found = await get_client().call("bizproc.task.list", {"SELECT": TASK_FIELDS, "FILTER": {"ID": task_id}})
        if not found:
            raise ToolError(f"Задание бизнес-процесса #{task_id} не найдено или недоступно")
        return found[0]

    async def document_title(module: str, entity: str, raw_id: str) -> str:
        """«сделка #12 «Поставка»» for CRM documents, the raw ID otherwise."""
        crm_entity = next((name for name, cls in CRM_ENTITIES.items() if cls == entity), None)
        if module != "crm" or crm_entity is None:
            return f"{module}:{raw_id}"
        element_id = raw_id.rsplit("_", 1)[-1]
        title = ""
        if element_id.isdigit():
            try:
                item = await get_client().call(
                    "crm.item.get", {"entityTypeId": ENTITY_TYPE_IDS[crm_entity], "id": int(element_id)}
                )
                title = display_name(crm_entity, item["item"])
            except Bitrix24Error:
                pass
        return f"{_GENITIVE[crm_entity]} #{element_id}{quoted(title)}"

    async def stop_workflow(workflow_id: str, ctx: Context, *, kill: bool, status: str | None = None) -> dict[str, Any]:
        client = get_client()
        instance = await fetch_instance(workflow_id)
        template_id = int(instance.get("TEMPLATE_ID") or 0)
        names, title = await asyncio.gather(
            template_names({template_id} if template_id else set()),
            document_title(instance.get("MODULE_ID", ""), instance.get("ENTITY", ""), str(instance.get("DOCUMENT_ID"))),
        )
        template = template_label(template_id, names)

        async def run() -> dict[str, Any]:
            if kill:
                await client.call("bizproc.workflow.kill", {"ID": workflow_id})
                return {"workflow_id": workflow_id, "deleted": True}
            params = {"ID": workflow_id}
            if status:
                params["STATUS"] = status
            await client.call("bizproc.workflow.terminate", params)
            return {"workflow_id": workflow_id, "terminated": True}

        action = "Удаление бизнес-процесса со всеми данными" if kill else "Остановка бизнес-процесса"
        lines = [
            f"Шаблон: {format_value(template)}",
            f"Документ: {title}",
            f"Запущен: {format_value(instance.get('STARTED'))}",
        ]
        if status:
            lines.append(f"Статус: {status}")
        tool = "bp_kill" if kill else "bp_terminate"
        return await approval.request(
            ctx, tool, build_summary(f"{action} {workflow_id}", client.portal_url, lines), run
        )

    @mcp.tool(annotations=_READ)
    async def bp_templates(
        document: Annotated[
            CrmDocument | None, Field(description="Только шаблоны для лидов, сделок, контактов или компаний")
        ] = None,
        query: Annotated[str | None, Field(description="Часть названия шаблона")] = None,
    ) -> list[dict[str, Any]]:
        """Шаблоны бизнес-процессов из дизайнера: для каких документов, как запускаются, какие параметры
        нужно передать при запуске. Роботы CRM сюда не входят. Нужны права администратора."""
        template_filter: dict[str, Any] = {}
        if document:
            template_filter = {"MODULE_ID": "crm", "ENTITY": CRM_ENTITIES[document]}
        templates, _ = await load_pages(
            get_client(),
            "bizproc.workflow.template.list",
            {"SELECT": [*TEMPLATE_FIELDS, "PARAMETERS"], "FILTER": template_filter, "ORDER": {"ID": "ASC"}},
            MAX_TEMPLATES,
        )
        if query:
            needle = query.casefold()
            templates = [t for t in templates if needle in str(t.get("NAME") or "").casefold()]
        return [describe_template(t) for t in templates]

    @mcp.tool(annotations=_READ)
    async def bp_template(
        id: TemplateIdArg,
        detail: Annotated[
            Literal["outline", "full"],
            Field(description="outline — дерево действий с названиями, full — полная структура шаблона в JSON"),
        ] = "outline",
    ) -> dict[str, Any]:
        """Устройство шаблона бизнес-процесса: действия, параметры, переменные и константы.
        Через вебхук шаблон можно только прочитать: создавать и менять шаблоны Битрикс24 разрешает лишь приложениям."""
        template = await fetch_template(
            id, [*TEMPLATE_FIELDS, "PARAMETERS", "VARIABLES", "CONSTANTS", "TEMPLATE", "USER_ID"]
        )
        activities = template.get("TEMPLATE") or []
        result = describe_template(template)
        result.update(
            compact(
                {
                    "modified_by": template.get("USER_ID"),
                    "variables": describe_fields(template.get("VARIABLES")),
                    "constants": describe_fields(template.get("CONSTANTS")),
                    "actions_count": count_actions(activities),
                }
            )
        )
        if detail == "full":
            result["template"] = activities
        else:
            result["outline"] = [line for activity in activities for line in outline(activity)]
        return result

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
        description=approval.describe("bp_start", "Запустить бизнес-процесс по шаблону для документа."),
    )
    async def bp_start(
        template_id: TemplateIdArg,
        element_id: Annotated[
            int,
            Field(description="ID документа, для которого запускается процесс: сделки, лида, элемента списка", gt=0),
        ],
        ctx: Context,
        parameters: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Значения параметров шаблона по их кодам из bp_templates. "
                    'Сотрудник передаётся как "user_14", список сотрудников — ["user_14", "user_15"]'
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        client = get_client()
        template = await fetch_template(template_id, [*TEMPLATE_FIELDS, "PARAMETERS"])
        described = describe_template(template)
        parameters = parameters or {}
        known = {p["code"] for p in described.get("parameters", [])}
        missing = [
            p["code"] for p in described.get("parameters", []) if p.get("required") and p["code"] not in parameters
        ]
        if missing:
            raise ToolError(f"Не заданы обязательные параметры шаблона: {', '.join(missing)}")
        unknown = sorted(set(parameters) - known)
        if unknown:
            raise ToolError(
                f"У шаблона нет параметров: {', '.join(unknown)}. Параметры: {', '.join(sorted(known)) or 'нет'}"
            )
        doc = document_id(template, element_id)
        title = await document_title(*doc)

        async def run() -> dict[str, Any]:
            params: dict[str, Any] = {"TEMPLATE_ID": template_id, "DOCUMENT_ID": doc}
            if parameters:
                params["PARAMETERS"] = parameters
            workflow_id = await client.call("bizproc.workflow.start", params)
            return {"workflow_id": workflow_id, "template_id": template_id, "document_id": doc}

        summary = build_summary(
            f"Запуск бизнес-процесса «{described.get('name')}» (шаблон #{template_id})",
            client.portal_url,
            [f"Документ: {title}", *field_lines(parameters)],
        )
        return await approval.request(ctx, "bp_start", summary, run)

    @mcp.tool(annotations=_READ)
    async def bp_instances(
        template_id: Annotated[int | None, Field(description="Только процессы этого шаблона", gt=0)] = None,
        document: Annotated[CrmDocument | None, Field(description="Тип документа для element_id")] = None,
        element_id: Annotated[int | None, Field(description="Только процессы этого документа CRM", gt=0)] = None,
        started_by: Annotated[int | None, Field(description="Только запущенные этим сотрудником", gt=0)] = None,
        stuck_only: Annotated[bool, Field(description="Только зависшие процессы")] = False,
        start: Annotated[int, Field(description="Смещение для постраничного вывода", ge=0)] = 0,
        limit: Annotated[int, Field(description="Сколько процессов вернуть (1–200)", ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """Запущенные (ещё не завершённые) бизнес-процессы, от недавно изменённых к старым, включая
        процессы роботов CRM. Зависшим считается процесс, заблокированный дольше 5 минут.
        Нужны права администратора."""
        client = get_client()
        instance_filter: dict[str, Any] = {}
        if template_id:
            instance_filter["TEMPLATE_ID"] = template_id
        if started_by:
            instance_filter["STARTED_BY"] = started_by
        if element_id is not None:
            if document is None:
                raise ToolError("Укажите document — тип документа для element_id")
            instance_filter.update(
                {
                    "MODULE_ID": "crm",
                    "ENTITY": CRM_ENTITIES[document],
                    "DOCUMENT_ID": f"{document.upper()}_{element_id}",
                }
            )
        elif document:
            instance_filter.update({"MODULE_ID": "crm", "ENTITY": CRM_ENTITIES[document]})
        if stuck_only:
            instances = await find_stuck(client, instance_filter, datetime.now(timezone.utc), limit)
            total = len(instances)
        else:
            instances, total = await load_pages(
                client,
                "bizproc.workflow.instances",
                {"SELECT": INSTANCE_FIELDS, "FILTER": instance_filter, "ORDER": {"MODIFIED": "desc"}, "start": start},
                limit,
            )
        names = await template_names({int(i["TEMPLATE_ID"]) for i in instances if i.get("TEMPLATE_ID")})
        rows = [
            compact(
                {
                    "id": i["ID"],
                    "template_id": int(i["TEMPLATE_ID"]) if i.get("TEMPLATE_ID") else None,
                    "template": template_label(int(i.get("TEMPLATE_ID") or 0), names),
                    "document": f"{i.get('MODULE_ID')}:{i.get('DOCUMENT_ID')}",
                    "started": i.get("STARTED"),
                    "started_by": i.get("STARTED_BY") if i.get("STARTED_BY") not in ("0", 0) else None,
                    "modified": i.get("MODIFIED"),
                    "locked_until": i.get("OWNED_UNTIL"),
                }
            )
            for i in instances
        ]
        next_start = None if stuck_only else start + len(instances)
        return {
            "instances": rows,
            "total": total,
            "next_start": next_start if next_start is not None and next_start < total else None,
        }

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=approval.describe(
            "bp_terminate", "Остановить запущенный бизнес-процесс. История процесса сохраняется."
        ),
    )
    async def bp_terminate(
        workflow_id: WorkflowIdArg,
        ctx: Context,
        status: Annotated[str | None, Field(description="Текст статуса, который увидят в документе")] = None,
    ) -> dict[str, Any]:
        return await stop_workflow(workflow_id, ctx, kill=False, status=status)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=approval.describe(
            "bp_kill",
            "Удалить запущенный бизнес-процесс вместе со всеми его данными, например зависший. "
            "Чтобы только остановить процесс, используйте bp_terminate.",
        ),
    )
    async def bp_kill(workflow_id: WorkflowIdArg, ctx: Context) -> dict[str, Any]:
        return await stop_workflow(workflow_id, ctx, kill=True)

    @mcp.tool(annotations=_READ)
    async def bp_tasks(
        user_id: Annotated[int | None, Field(description="Чьи задания; не указан — пользователя вебхука", gt=0)] = None,
        include_completed: Annotated[bool, Field(description="Показывать выполненные задания")] = False,
        workflow_id: Annotated[str | None, Field(description="Только задания этого процесса")] = None,
    ) -> dict[str, Any]:
        """Задания бизнес-процессов: утверждения, ознакомления, запросы информации — с вариантами решения
        и полями, которые нужно заполнить."""
        client = get_client()
        task_filter: dict[str, Any] = {}
        if user_id:
            task_filter["USER_ID"] = user_id
        if not include_completed:
            task_filter["STATUS"] = 0
        if workflow_id:
            task_filter["WORKFLOW_ID"] = workflow_id
        response = await client.call_raw(
            "bizproc.task.list", {"SELECT": TASK_FIELDS, "FILTER": task_filter, "ORDER": {"ID": "DESC"}}
        )
        tasks = response.get("result") or []
        return {
            "tasks": [describe_task(t, client.portal_url) for t in tasks],
            "total": response.get("total", len(tasks)),
        }

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=approval.describe(
            "bp_task_complete",
            "Выполнить своё задание бизнес-процесса: утвердить, отклонить, ознакомиться или ответить на запрос.",
        ),
    )
    async def bp_task_complete(
        task_id: TaskIdArg,
        decision: Annotated[
            Literal["approve", "reject", "ok", "cancel"],
            Field(
                description=(
                    "approve/reject — для утверждения, ok — для ознакомления и запроса информации, "
                    "cancel — отказ в запросе информации с возможностью отказа. Допустимые варианты — в bp_tasks"
                )
            ),
        ],
        ctx: Context,
        comment: Annotated[str | None, Field(description="Комментарий к решению")] = None,
        fields: Annotated[
            dict[str, Any] | None, Field(description="Значения полей запроса информации по их кодам из bp_tasks")
        ] = None,
    ) -> dict[str, Any]:
        client = get_client()
        task = await fetch_task(task_id)
        described = describe_task(task, client.portal_url)
        allowed = TASK_DECISIONS.get(task.get("ACTIVITY"), {})
        if decision not in allowed:
            raise ToolError(
                f"Для задания типа «{described.get('kind')}» допустимы решения: {', '.join(allowed) or 'нет'}"
            )
        if str(task.get("STATUS")) != "0":
            raise ToolError(f"Задание #{task_id} уже не ожидает решения: {described.get('status')}")
        fields = fields or {}
        if decision == "ok":
            missing = [
                f["code"]
                for f in described.get("fields", [])
                if f.get("required") and f["code"] not in fields and f.get("default") in (None, "", [])
            ]
            if missing:
                raise ToolError(f"Не заполнены обязательные поля: {', '.join(missing)}")
        comment_rule = (described.get("comment") or {}).get("required")
        needs_comment = {"Y": True, "YA": decision == "approve", "YR": decision == "reject"}.get(comment_rule, False)
        if needs_comment and not comment:
            raise ToolError("Для этого решения нужен комментарий")

        async def run() -> dict[str, Any]:
            params: dict[str, Any] = {"TASK_ID": task_id, "STATUS": allowed[decision]}
            if comment:
                params["COMMENT"] = comment
            if fields:
                params["FIELDS"] = fields
            await client.call("bizproc.task.complete", params)
            return {"task_id": task_id, "decision": decision}

        lines = [
            f"Решение: {described['decisions'].get(decision, decision)}",
            f"Документ: {format_value(described.get('document'))}",
            f"Процесс: {format_value(described.get('template'))}",
        ]
        if comment:
            lines.append(f"Комментарий: {comment}")
        lines.extend(field_lines(fields))
        summary = build_summary(
            f"Задание бизнес-процесса #{task_id}{quoted(described.get('name'))}", client.portal_url, lines
        )
        return await approval.request(ctx, "bp_task_complete", summary, run)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
        description=approval.describe("bp_task_delegate", "Передать свои задания бизнес-процессов другому сотруднику."),
    )
    async def bp_task_delegate(
        task_ids: Annotated[list[int], Field(description="ID заданий (из bp_tasks)", min_length=1, max_length=50)],
        to_user_id: Annotated[int, Field(description="Кому передать (ID сотрудника из users_search)", gt=0)],
        ctx: Context,
        from_user_id: Annotated[
            int | None, Field(description="Чьи задания; не указан — пользователя вебхука", gt=0)
        ] = None,
    ) -> dict[str, Any]:
        client = get_client()
        tasks = await asyncio.gather(*(fetch_task(task_id) for task_id in task_ids))
        users = await client.call("user.get", {"ID": to_user_id})
        if not users:
            raise ToolError(f"Сотрудник #{to_user_id} не найден")
        target = " ".join(str(users[0][k]) for k in ("NAME", "LAST_NAME") if users[0].get(k))
        if from_user_id is None:
            from_user_id = int((await client.call("user.current"))["ID"])

        async def run() -> dict[str, Any]:
            await client.call(
                "bizproc.task.delegate",
                {"TASK_IDS": task_ids, "FROM_USER_ID": from_user_id, "TO_USER_ID": to_user_id},
            )
            return {"task_ids": task_ids, "to_user_id": to_user_id}

        lines = [f"Кому: {target} (#{to_user_id})"]
        lines += [f"Задание #{t['ID']}: {t.get('NAME')} — {t.get('DOCUMENT_NAME') or '—'}" for t in tasks]
        summary = build_summary("Передача заданий бизнес-процессов", client.portal_url, lines)
        return await approval.request(ctx, "bp_task_delegate", summary, run)
