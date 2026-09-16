import pytest
from mcp.server.fastmcp.exceptions import ToolError

from bitrix24_mcp_server.approval import ApprovalGate


class Counter:
    def __init__(self) -> None:
        self.runs = 0

    async def __call__(self) -> dict:
        self.runs += 1
        return {"ok": True}


async def test_token_mode_defers_until_confirmed():
    gate, run = ApprovalGate("token"), Counter()
    pending = await gate.request(None, "Удаление сделки #1", run)
    assert pending["status"] == "confirmation_required"
    assert pending["preview"] == "Удаление сделки #1"
    assert run.runs == 0

    done = await gate.confirm(pending["confirmation_id"])
    assert done == {"status": "done", "operation": "Удаление сделки #1", "result": {"ok": True}}
    assert run.runs == 1


async def test_confirmation_id_is_single_use():
    gate, run = ApprovalGate("token"), Counter()
    pending = await gate.request(None, "x", run)
    await gate.confirm(pending["confirmation_id"])
    with pytest.raises(ToolError):
        await gate.confirm(pending["confirmation_id"])
    assert run.runs == 1


async def test_unknown_confirmation_id_is_rejected():
    with pytest.raises(ToolError):
        await ApprovalGate("token").confirm("deadbeef")


async def test_cancelled_operation_cannot_be_confirmed():
    gate, run = ApprovalGate("token"), Counter()
    pending = await gate.request(None, "x", run)
    assert gate.cancel(pending["confirmation_id"])["status"] == "cancelled"
    assert gate.cancel(pending["confirmation_id"])["status"] == "not_found"
    with pytest.raises(ToolError):
        await gate.confirm(pending["confirmation_id"])
    assert run.runs == 0


async def test_pending_operations_expire():
    now = [1000.0]
    gate, run = ApprovalGate("token", ttl_seconds=60, clock=lambda: now[0]), Counter()
    pending = await gate.request(None, "x", run)
    now[0] += 61
    with pytest.raises(ToolError):
        await gate.confirm(pending["confirmation_id"])
    assert run.runs == 0


async def test_oldest_pending_operation_is_evicted_when_full():
    gate = ApprovalGate("token", max_pending=2)
    first = await gate.request(None, "1", Counter())
    await gate.request(None, "2", Counter())
    await gate.request(None, "3", Counter())
    with pytest.raises(ToolError):
        await gate.confirm(first["confirmation_id"])


async def test_auto_mode_without_elicitation_support_uses_tokens():
    pending = await ApprovalGate("auto").request(None, "x", Counter())
    assert pending["status"] == "confirmation_required"


async def test_elicitation_mode_requires_client_support():
    run = Counter()
    with pytest.raises(ToolError, match="elicitation"):
        await ApprovalGate("elicitation").request(None, "x", run)
    assert run.runs == 0


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        ApprovalGate("never")  # type: ignore[arg-type]
