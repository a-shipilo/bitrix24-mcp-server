"""CRM tools: leads, deals, contacts and companies via the universal ``crm.item.*`` API."""

import asyncio
from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .approval import ApprovalGate
from .client import Bitrix24Client
from .preview import build_summary, change_lines, field_lines, quoted

CrmEntity = Literal["lead", "deal", "contact", "company"]

ENTITY_TYPE_IDS: dict[str, int] = {"lead": 1, "deal": 2, "contact": 3, "company": 4}
_GENITIVE = {"lead": "лида", "deal": "сделки", "contact": "контакта", "company": "компании"}
_DATIVE = {"lead": "лиду", "deal": "сделке", "contact": "контакту", "company": "компании"}

PAGE_SIZE = 50

EntityArg = Annotated[CrmEntity, Field(description="Тип объекта: lead, deal, contact или company")]
IdArg = Annotated[int, Field(description="ID объекта CRM", gt=0)]
FieldsArg = Annotated[
    dict[str, Any],
    Field(
        description=(
            "Поля в camelCase, как их возвращает crm_fields, например "
            '{"title": "Поставка", "opportunity": 150000, "stageId": "NEW"}. '
            'Телефоны и e-mail: "fm": [{"typeId": "PHONE", "valueType": "WORK", "value": "+79990000000"}]'
        )
    ),
]

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def compact(record: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values: CRM objects carry dozens of unset fields."""
    return {k: v for k, v in record.items() if v is not None and v != "" and v != [] and v != {}}


def display_name(entity: str, item: dict[str, Any]) -> str:
    if entity == "contact":
        return " ".join(str(item[k]) for k in ("name", "lastName") if item.get(k))
    return str(item.get("title") or "")


def register_crm_tools(mcp: FastMCP, get_client: Callable[[], Bitrix24Client], gate: ApprovalGate) -> None:
    async def fetch_item(entity: str, item_id: int) -> dict[str, Any]:
        result = await get_client().call("crm.item.get", {"entityTypeId": ENTITY_TYPE_IDS[entity], "id": item_id})
        return result["item"]

    @mcp.tool(annotations=_READ)
    async def crm_list(
        entity: EntityArg,
        filter: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Фильтр по полям в camelCase. Префиксы: >, >=, <, <=, != или !, % (содержит), "
                    "@ (в списке). Пример: "
                    '{"stageId": "NEW", ">=opportunity": 100000, "%title": "поставка", "@assignedById": [1, 7]}'
                )
            ),
        ] = None,
        select: Annotated[
            list[str] | None,
            Field(description='Какие поля вернуть; по умолчанию ["*"] — все поля, включая телефоны и e-mail (fm)'),
        ] = None,
        order: Annotated[
            dict[str, Literal["asc", "desc", "ASC", "DESC"]] | None,
            Field(description='Сортировка, по умолчанию {"id": "desc"}'),
        ] = None,
        start: Annotated[int, Field(description="Смещение для постраничного вывода", ge=0)] = 0,
        limit: Annotated[int, Field(description="Сколько записей вернуть (1–50)", ge=1, le=PAGE_SIZE)] = 20,
    ) -> dict[str, Any]:
        """Найти лиды, сделки, контакты или компании по фильтру. Пустые поля в ответе опускаются."""
        response = await get_client().call_raw(
            "crm.item.list",
            {
                "entityTypeId": ENTITY_TYPE_IDS[entity],
                "filter": filter or {},
                "select": select or ["*"],
                "order": order or {"id": "desc"},
                "start": start,
            },
        )
        items = response["result"]["items"][:limit]
        total = response.get("total", len(items))
        next_start = start + len(items)
        return {
            "items": [compact(item) for item in items],
            "total": total,
            "next_start": next_start if next_start < total else None,
        }

    @mcp.tool(annotations=_READ)
    async def crm_get(entity: EntityArg, id: IdArg) -> dict[str, Any]:
        """Получить лид, сделку, контакт или компанию по ID со всеми заполненными полями."""
        return compact(await fetch_item(entity, id))

    @mcp.tool(annotations=_READ)
    async def crm_fields(entity: EntityArg) -> dict[str, Any]:
        """Описание полей объекта CRM: названия, типы, обязательность, варианты списков, в том числе
        пользовательские поля (ufCrm...). Используйте перед созданием или изменением."""
        result = await get_client().call("crm.item.fields", {"entityTypeId": ENTITY_TYPE_IDS[entity]})
        fields = {}
        for name, info in result["fields"].items():
            description = {"title": info.get("title"), "type": info.get("type")}
            for flag in ("isRequired", "isReadOnly", "isMultiple"):
                if info.get(flag):
                    description[flag] = True
            if info.get("statusType"):
                description["statusType"] = info["statusType"]
            if info.get("items"):
                description["items"] = [{"id": i.get("ID"), "value": i.get("VALUE")} for i in info["items"]]
            fields[name] = description
        return fields

    @mcp.tool(annotations=_READ)
    async def crm_stages(
        entity: Annotated[
            Literal["deal", "lead"], Field(description="deal — стадии сделок, lead — статусы лидов")
        ] = "deal",
        category_id: Annotated[
            int | None, Field(description="ID воронки сделок; не указан — все воронки", ge=0)
        ] = None,
    ) -> list[dict[str, Any]]:
        """Воронки и стадии сделок или статусы лидов (ID стадии нужен для фильтра и изменения stageId)."""
        client = get_client()

        async def statuses(entity_id: str) -> list[dict[str, Any]]:
            result = await client.call(
                "crm.status.list", {"filter": {"ENTITY_ID": entity_id}, "order": {"SORT": "ASC"}}
            )
            return [
                compact(
                    {
                        "id": s.get("STATUS_ID"),
                        "name": s.get("NAME"),
                        "semantics": (s.get("EXTRA") or {}).get("SEMANTICS") or s.get("SEMANTICS"),
                    }
                )
                for s in result
            ]

        if entity == "lead":
            return [{"name": "Статусы лидов", "stages": await statuses("STATUS")}]

        result = await client.call("crm.category.list", {"entityTypeId": ENTITY_TYPE_IDS["deal"]})
        categories = result["categories"]
        if category_id is not None:
            categories = [c for c in categories if int(c["id"]) == category_id]
            if not categories:
                raise ToolError(f"Воронка сделок с ID {category_id} не найдена")
        stages = await asyncio.gather(
            *(statuses("DEAL_STAGE" if int(c["id"]) == 0 else f"DEAL_STAGE_{c['id']}") for c in categories)
        )
        return [
            {"category_id": int(c["id"]), "name": c.get("name"), "is_default": c.get("isDefault"), "stages": s}
            for c, s in zip(categories, stages, strict=True)
        ]

    @mcp.tool(annotations=_READ)
    async def crm_find_by_contact_info(
        type: Annotated[Literal["PHONE", "EMAIL"], Field(description="Искать по телефону или e-mail")],
        values: Annotated[list[str], Field(description="Телефоны или e-mail (до 20)", min_length=1, max_length=20)],
        entity: Annotated[
            Literal["lead", "contact", "company"] | None,
            Field(description="Где искать; не указано — во всех"),
        ] = None,
    ) -> dict[str, list[int]]:
        """Найти ID лидов, контактов и компаний по телефону или e-mail."""
        params: dict[str, Any] = {"type": type, "values": values}
        if entity:
            params["entity_type"] = entity.upper()
        result = await get_client().call("crm.duplicate.findbycomm", params)
        return {k.lower(): [int(i) for i in ids] for k, ids in (result or {}).items()}

    @mcp.tool(annotations=_READ)
    async def crm_comments(
        entity: EntityArg,
        id: IdArg,
        start: Annotated[int, Field(description="Смещение для постраничного вывода", ge=0)] = 0,
    ) -> dict[str, Any]:
        """Комментарии из таймлайна объекта CRM."""
        response = await get_client().call_raw(
            "crm.timeline.comment.list",
            {
                "filter": {"ENTITY_ID": id, "ENTITY_TYPE": entity},
                "select": ["ID", "CREATED", "AUTHOR_ID", "COMMENT"],
                "order": {"CREATED": "DESC"},
                "start": start,
            },
        )
        return {"comments": response["result"], "total": response.get("total"), "next_start": response.get("next")}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    async def crm_create(entity: EntityArg, fields: FieldsArg, ctx: Context) -> dict[str, Any]:
        """Создать лид, сделку, контакт или компанию. Требует подтверждения пользователя."""
        if not fields:
            raise ToolError("Не переданы поля нового объекта")
        client = get_client()

        async def run() -> dict[str, Any]:
            result = await client.call("crm.item.add", {"entityTypeId": ENTITY_TYPE_IDS[entity], "fields": fields})
            return {"id": result["item"]["id"], "item": compact(result["item"])}

        summary = build_summary(f"Создание {_GENITIVE[entity]}", client.portal_url, field_lines(fields))
        return await gate.request(ctx, summary, run)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
    async def crm_update(entity: EntityArg, id: IdArg, fields: FieldsArg, ctx: Context) -> dict[str, Any]:
        """Изменить поля лида, сделки, контакта или компании (например, перевести сделку на другую
        стадию). Передавайте только изменяемые поля. Требует подтверждения пользователя."""
        if not fields:
            raise ToolError("Не переданы изменяемые поля")
        client = get_client()
        current = await fetch_item(entity, id)

        async def run() -> dict[str, Any]:
            result = await client.call(
                "crm.item.update", {"entityTypeId": ENTITY_TYPE_IDS[entity], "id": id, "fields": fields}
            )
            return {"id": id, "item": compact(result["item"])}

        summary = build_summary(
            f"Изменение {_GENITIVE[entity]} #{id}{quoted(display_name(entity, current))}",
            client.portal_url,
            change_lines(current, fields),
        )
        return await gate.request(ctx, summary, run)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
    async def crm_delete(entity: EntityArg, id: IdArg, ctx: Context) -> dict[str, Any]:
        """Удалить лид, сделку, контакт или компанию. Требует подтверждения пользователя."""
        client = get_client()
        current = await fetch_item(entity, id)

        async def run() -> dict[str, Any]:
            await client.call("crm.item.delete", {"entityTypeId": ENTITY_TYPE_IDS[entity], "id": id})
            return {"id": id, "deleted": True}

        summary = build_summary(
            f"Удаление {_GENITIVE[entity]} #{id}{quoted(display_name(entity, current))}",
            client.portal_url,
            [f"Ответственный: {current.get('assignedById', '—')}", f"Создан: {current.get('createdTime', '—')}"],
        )
        return await gate.request(ctx, summary, run)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    async def crm_add_comment(
        entity: EntityArg,
        id: IdArg,
        text: Annotated[str, Field(description="Текст комментария", min_length=1)],
        ctx: Context,
    ) -> dict[str, Any]:
        """Добавить комментарий в таймлайн лида, сделки, контакта или компании.
        Требует подтверждения пользователя."""
        client = get_client()
        current = await fetch_item(entity, id)

        async def run() -> dict[str, Any]:
            comment_id = await client.call(
                "crm.timeline.comment.add",
                {"fields": {"ENTITY_ID": id, "ENTITY_TYPE": entity, "COMMENT": text}},
            )
            return {"comment_id": comment_id}

        summary = build_summary(
            f"Комментарий к {_DATIVE[entity]} #{id}{quoted(display_name(entity, current))}",
            client.portal_url,
            [f"Текст: {text}"],
        )
        return await gate.request(ctx, summary, run)
