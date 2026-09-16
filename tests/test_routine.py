"""What unattended routines rely on: auto-approved writes, task attachments and task comments."""

import httpx
import pytest

from bitrix24_mcp_server.client import Bitrix24Client, Bitrix24Error
from bitrix24_mcp_server.server import _env_list, build_from_env

from .conftest import WEBHOOK, payload

ROUTINE_TOOLS = ("task_add_comment", "task_move_stage", "sprint_move_task")
TSV = "stage\tcount\nНовые\t5\nГотово\t7\n"


async def test_auto_approved_tools_run_without_confirmation(connect, bitrix):
    bitrix.on("tasks.task.get", {"task": {"id": "8", "title": "Отчёт"}})
    bitrix.on("v3:tasks.task.chat.message.send", {"result": True})
    bitrix.on("crm.item.get", {"item": {"id": 3, "title": "Сделка"}})
    async with connect(auto_approve=(*ROUTINE_TOOLS, "crm_update")) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        done = payload(await session.call_tool("task_add_comment", {"id": 8, "text": "[routine] готово"}))
        crm = payload(await session.call_tool("crm_update", {"entity": "deal", "id": 3, "fields": {"title": "x"}}))

    for name in ROUTINE_TOOLS:
        assert "подтверждения" not in tools[name].description, name
    for name in ("task_update", "task_delete", "bp_start", "crm_update", "crm_delete"):
        assert "подтверждения" in tools[name].description, name
    assert done["status"] == "done"
    assert done["operation"].startswith("Комментарий к задаче #8 «Отчёт»")
    assert bitrix.called("v3:tasks.task.chat.message.send") == [{"fields": {"taskId": 8, "text": "[routine] готово"}}]
    assert crm["status"] == "confirmation_required"  # CRM always asks


def test_auto_approve_setting_is_read_from_environment(monkeypatch):
    assert _env_list("MISSING_VARIABLE") == []
    monkeypatch.setenv("BITRIX24_AUTO_APPROVE", " task_add_comment, task_move_stage;sprint_move_task ")
    assert _env_list("BITRIX24_AUTO_APPROVE") == list(ROUTINE_TOOLS)
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", WEBHOOK)
    server = build_from_env()
    assert "task_add_comment, task_move_stage, sprint_move_task" not in server.instructions
    assert "sprint_move_task, task_add_comment, task_move_stage" in server.instructions


async def test_task_get_lists_attachments(connect, bitrix):
    bitrix.on(
        "tasks.task.get",
        {
            "task": {
                "id": "8",
                "title": "Отчёт",
                "ufTaskWebdavFiles": [21944],
                "tags": {"562": {"id": 562, "title": "hds-api-server"}, "564": {"id": 564, "title": "add-triggers"}},
            }
        },
    )
    bitrix.on(
        "task.item.getfiles",
        [{"ATTACHMENT_ID": 21944, "NAME": "stages.tsv", "SIZE": "15477", "DOWNLOAD_URL": bitrix.file_link(21944)}],
    )
    async with connect() as session:
        task = payload(await session.call_tool("task_get", {"id": 8}))
    assert task["files"] == [{"id": 21944, "name": "stages.tsv", "size": 15477}]
    assert task["tags"] == ["hds-api-server", "add-triggers"]
    assert "s3cr3t-token" not in str(task)
    assert bitrix.called("tasks.task.get") == [{"taskId": 8, "select": ["*", "UF_*", "TAGS"]}]


def attach(bitrix, content: bytes, **serve_options) -> None:
    bitrix.on(
        "task.item.getfiles",
        [
            {
                "ATTACHMENT_ID": 21944,
                "NAME": "stages.tsv",
                "SIZE": str(len(content)),
                "DOWNLOAD_URL": bitrix.file_link(21944),
            }
        ],
    )
    bitrix.serve_file(21944, content, **serve_options)


async def test_task_file_read_returns_text(connect, bitrix):
    attach(bitrix, TSV.encode())
    async with connect() as session:
        result = payload(await session.call_tool("task_file_read", {"task_id": 8, "attachment_id": 21944}))
        chunk = payload(
            await session.call_tool(
                "task_file_read", {"task_id": 8, "attachment_id": 21944, "offset": 5, "max_chars": 6}
            )
        )
    assert result["text"] == TSV
    assert result["encoding"] == "utf-8-sig"
    assert result["next_offset"] is None
    assert result["name"] == "stages.tsv"
    assert (chunk["text"], chunk["next_offset"], chunk["total_chars"]) == ("\tcount", 11, len(TSV))
    assert bitrix.downloads == ["21944", "21944"]


async def test_task_file_read_detects_windows_1251(connect, bitrix):
    attach(bitrix, TSV.encode("cp1251"))
    async with connect() as session:
        result = payload(await session.call_tool("task_file_read", {"task_id": 8, "attachment_id": 21944}))
    assert (result["text"], result["encoding"]) == (TSV, "cp1251")


async def test_task_file_read_saves_to_disk(connect, bitrix, tmp_path):
    attach(bitrix, b"\x00\x01binary")
    async with connect() as session:
        binary = await session.call_tool("task_file_read", {"task_id": 8, "attachment_id": 21944})
        saved = payload(
            await session.call_tool("task_file_read", {"task_id": 8, "attachment_id": 21944, "save_to": str(tmp_path)})
        )
        again = await session.call_tool(
            "task_file_read", {"task_id": 8, "attachment_id": 21944, "save_to": str(tmp_path / "stages.tsv")}
        )
        relative = await session.call_tool("task_file_read", {"task_id": 8, "attachment_id": 21944, "save_to": "x.tsv"})
    assert binary.isError and "save_to" in binary.content[0].text
    assert saved["saved_to"] == str(tmp_path / "stages.tsv")
    assert (tmp_path / "stages.tsv").read_bytes() == b"\x00\x01binary"
    assert "text" not in saved
    assert again.isError and "overwrite" in again.content[0].text
    assert relative.isError and "абсолютным" in relative.content[0].text


async def test_task_file_read_explains_missing_disk_scope(connect, bitrix):
    body = b'{"status":"error","errors":[{"message":"Bad permission. Could not read this file","code":0}]}'
    attach(bitrix, body, content_type="application/json; charset=UTF-8")
    async with connect() as session:
        refused = await session.call_tool("task_file_read", {"task_id": 8, "attachment_id": 21944})
        unknown = await session.call_tool("task_file_read", {"task_id": 8, "attachment_id": 1})
    assert refused.isError and "«Диск» (disk)" in refused.content[0].text
    assert unknown.isError and "Вложения: 21944 «stages.tsv»" in unknown.content[0].text


async def test_download_rejects_other_hosts_and_large_files(bitrix):
    bitrix.serve_file(5, b"x" * 100)
    client = bitrix.client()
    with pytest.raises(Bitrix24Error, match="не на портал"):
        await client.download("https://evil.example/bitrix/tools/disk/uf.php?attachedId=5", max_bytes=1000)
    with pytest.raises(Bitrix24Error, match="FILE_TOO_LARGE"):
        await client.download(bitrix.file_link(5), max_bytes=10)
    assert await client.download(bitrix.file_link(5), max_bytes=1000) == b"x" * 100


async def test_rest_v3_errors_are_parsed():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "INSUFFICIENT_SCOPE", "message": "Нет scope"}})

    client = Bitrix24Client(WEBHOOK, transport=httpx.MockTransport(handle))
    with pytest.raises(Bitrix24Error) as exc_info:
        await client.call("tasks.task.get", {}, api_v3=True)
    assert (exc_info.value.code, exc_info.value.description) == ("INSUFFICIENT_SCOPE", "Нет scope")


def chat_page(ids: list[int], *, system: tuple[int, ...] = ()) -> dict:
    return {
        "result": {
            "chat_id": 536706,
            "messages": [
                {
                    "id": i,
                    "author_id": 0 if i in system else 11,
                    "date": f"2026-09-16T10:{i:02d}:00+03:00",
                    "text": f"[USER=5 REPLACE]Анна[/USER], сообщение {i}",
                    "params": {"FILE_ID": [900]} if i == 7 else [],
                }
                for i in ids
            ],
            "users": [{"id": 11, "name": "Александр Шипило"}],
            "files": [{"id": 900, "name": "result.csv"}],
        }
    }


async def test_task_comments_from_task_chat(connect, bitrix):
    bitrix.on("tasks.task.get", {"task": {"id": "8", "chatId": 536706}})
    pages = {None: chat_page(list(range(60, 10, -1)), system=(58,)), 11: chat_page([10, 9, 8, 7]), 7: chat_page([])}
    bitrix.on("im.dialog.messages.get", handler=lambda p: pages[p.get("LAST_ID")])
    async with connect() as session:
        latest = payload(await session.call_tool("task_comments", {"id": 8, "limit": 3}))
        everything = payload(await session.call_tool("task_comments", {"id": 8, "limit": 500, "include_system": True}))

    assert latest["source"] == "task_chat"
    assert [c["id"] for c in latest["comments"]] == [57, 59, 60]
    assert latest["comments"][0] == {
        "id": 57,
        "date": "2026-09-16T10:57:00+03:00",
        "author": "Александр Шипило",
        "author_id": 11,
        "text": "Анна, сообщение 57",
    }
    assert len(everything["comments"]) == 54
    assert everything["comments"][0]["files"] == ["result.csv"]
    assert next(c for c in everything["comments"] if c["id"] == 58)["author"] == "система"
    calls = bitrix.called("im.dialog.messages.get")
    assert calls[0] == {"DIALOG_ID": "chat536706", "LIMIT": 50}
    assert [c.get("LAST_ID") for c in calls] == [None, None, 11, 7]  # the first call needs one page only
    assert bitrix.called("tasks.task.get")[0] == {"taskId": 8, "select": ["ID", "CHAT_ID"]}


async def test_task_comments_explain_missing_im_scope(connect, bitrix):
    bitrix.on("tasks.task.get", {"task": {"id": "8", "chatId": 536706}})
    bitrix.on("im.dialog.messages.get", handler=lambda _: {"error": "insufficient_scope", "error_description": "x"})
    async with connect() as session:
        result = await session.call_tool("task_comments", {"id": 8})
    assert result.isError
    assert "«Чат и уведомления» (im)" in result.content[0].text


async def test_task_comments_on_classic_task_card(connect, bitrix):
    bitrix.on("tasks.task.get", {"task": {"id": "8"}})
    bitrix.on(
        "task.commentitem.getlist",
        [
            {"ID": "31", "AUTHOR_ID": "5", "AUTHOR_NAME": "Анна", "POST_DATE": "2", "POST_MESSAGE": "второй"},
            {
                "ID": "30",
                "AUTHOR_ID": "11",
                "AUTHOR_NAME": "Александр",
                "POST_DATE": "1",
                "POST_MESSAGE": "первый",
                "ATTACHED_OBJECTS": {"973": {"ATTACHMENT_ID": "973", "NAME": "a.png", "SIZE": "10"}},
            },
        ],
    )
    async with connect() as session:
        result = payload(await session.call_tool("task_comments", {"id": 8}))
    assert result == {
        "source": "comments",
        "comments": [
            {"id": 30, "date": "1", "author": "Александр", "author_id": 11, "text": "первый", "files": ["a.png"]},
            {"id": 31, "date": "2", "author": "Анна", "author_id": 5, "text": "второй"},
        ],
    }
    assert bitrix.called("task.commentitem.getlist") == [{"TASKID": 8, "ORDER": {"ID": "desc"}}]
