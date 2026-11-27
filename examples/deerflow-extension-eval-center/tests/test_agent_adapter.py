"""Behavioral checks at the public AgentRuns boundary; no network or model calls."""

import asyncio
import copy
from types import SimpleNamespace

import pytest
from deerflow_extension_api import AgentRunError

from deerflow_extension_eval_center.agent_adapter import HostAgentAdapter, execution_identity

CASE = {"id": "case-1", "prompt": 'Return {"value": 3}', "expected": {"private_reference": "never sent"}}


class Runs:
    def __init__(self):
        self.created = []
        self.inputs = []
        self.cancelled = []
        self.wait_calls = 0
        self.status = "success"
        self.state_next = []
        self.state_interrupts = []
        self.messages = None
        self.wait_entered = asyncio.Event()
        self.wait_release = asyncio.Event()
        self.hold_wait = False
        self.first_wait_timeout = False
        self.evidence_available = True
        self.start_error = None

    async def create_thread(self, **kwargs):
        self.created.append(kwargs)
        self.thread_id = kwargs["thread_id"]
        return self.thread_id

    async def start(self, **kwargs):
        self.inputs.append(copy.deepcopy(kwargs))
        if self.start_error:
            raise self.start_error
        return SimpleNamespace(run_id="run-1", status="pending", thread_id=self.thread_id)

    async def wait(self, **kwargs):
        self.wait_calls += 1
        if self.wait_calls == 1:
            self.wait_entered.set()
            if self.first_wait_timeout:
                raise TimeoutError
            if self.hold_wait:
                await self.wait_release.wait()
        return SimpleNamespace(run_id="run-1", status="interrupted" if self.cancelled else self.status)

    async def get_state(self, **kwargs):
        messages = self.messages
        if messages is None:
            messages = [self.inputs[-1]["input"]["messages"][0], {"type": "ai", "content": '{"value": 3}'}]
        return {"values": {"messages": copy.deepcopy(messages)}, "next": self.state_next, "interrupts": self.state_interrupts}

    async def configuration_evidence(self, **kwargs):
        if not self.evidence_available:
            raise NotImplementedError
        return {"thread_id": self.thread_id, "run_id": "run-1", "source": "host_assembly", "version": "frozen-v1"}

    async def cancel(self, **kwargs):
        self.cancelled.append(kwargs)


@pytest.mark.asyncio
async def test_success_isolated_thread_stable_identity_and_no_reference_leak():
    runs = Runs()
    notified = []

    async def on_run(thread_id, run_id):
        notified.append((thread_id, run_id))

    adapter = HostAgentAdapter(runs)
    result = await adapter.execute(experiment_id="experiment-1", case=CASE, on_run=on_run)
    thread_id, key, marker = execution_identity("experiment-1", "case-1")
    assert result["output"] == '{"value": 3}'
    assert result["run_status"] == "success" and result["error"] is None
    assert result["configuration_evidence"]["version"] == "frozen-v1"
    assert notified == [(thread_id, "run-1")]
    assert runs.inputs == [
        {
            "thread_id": thread_id,
            "input": {"messages": [{"type": "human", "id": marker, "content": CASE["prompt"]}]},
            "idempotency_key": key,
        }
    ]
    assert execution_identity("experiment-1", "case-1") == execution_identity("experiment-1", "case-1")
    assert execution_identity("experiment-1", "case-1", 1)[0] != thread_id
    assert execution_identity("experiment-2", "case-1")[0] != thread_id
    assert execution_identity("experiment-1", "case-2")[0] != thread_id


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["next", "interrupts", "tool_request", "status"])
async def test_human_input_or_approval_is_blocked_and_never_resumed(reason):
    runs = Runs()
    if reason == "next":
        runs.state_next = ["tools"]
    elif reason == "interrupts":
        runs.state_interrupts = [{"value": "approve?"}]
    elif reason == "status":
        runs.status = "waiting_for_input"
    else:
        runs.messages = [{"type": "tool", "artifact": {"human_input": {"kind": "human_input_request", "request_id": "ask-1"}}}]
    result = await HostAgentAdapter(runs).execute(experiment_id="experiment-1", case=CASE)
    assert result["blocked"] and result["output"] == ""
    assert result["error"] == "Agent requires human input or approval"


@pytest.mark.asyncio
async def test_tool_results_and_old_answers_cannot_be_scored_as_final_output():
    runs = Runs()
    marker = execution_identity("experiment-1", "case-1")[2]
    runs.messages = [
        {"type": "ai", "content": "old answer"},
        {"type": "human", "id": marker, "content": CASE["prompt"]},
        {"type": "ai", "content": "tool-call text", "tool_calls": [{"name": "read"}]},
        {"type": "tool", "content": '{"value": 3}'},
    ]
    result = await HostAgentAdapter(runs).execute(experiment_id="experiment-1", case=CASE)
    assert result["output"] == ""
    assert result["error"] == "Agent completed without a final answer"


@pytest.mark.asyncio
async def test_later_turn_prevents_capturing_mutable_thread_output():
    runs = Runs()
    marker = execution_identity("experiment-1", "case-1")[2]
    runs.messages = [
        {"type": "human", "id": marker},
        {"type": "ai", "content": '{"value": 3}'},
        {"type": "human", "id": "another-turn"},
        {"type": "ai", "content": "unrelated answer"},
    ]
    result = await HostAgentAdapter(runs).execute(experiment_id="experiment-1", case=CASE)
    assert result["output"] == ""
    assert result["error"] == "Thread advanced before trial output was captured"


@pytest.mark.asyncio
async def test_rekeyed_human_anchor_and_text_blocks_are_supported():
    runs = Runs()
    marker = execution_identity("experiment-1", "case-1")[2]
    runs.messages = [
        {"type": "human", "id": marker, "additional_kwargs": {"hide_from_ui": True}},
        {"type": "human", "id": marker + "__user"},
        {"type": "ai", "content": [{"type": "reasoning", "text": "private"}, {"type": "text", "text": '{"value": 3}'}]},
    ]
    result = await HostAgentAdapter(runs).execute(experiment_id="experiment-1", case=CASE)
    assert result["output"] == '{"value": 3}'


@pytest.mark.asyncio
async def test_wait_timeout_explicitly_cancels_and_observes_the_run():
    runs = Runs()
    runs.first_wait_timeout = True
    result = await HostAgentAdapter(runs).execute(experiment_id="experiment-1", case=CASE)
    assert result["error"] == "Agent trial timed out"
    assert result["run_status"] == "interrupted"
    assert not result["needs_reconciliation"]
    assert runs.cancelled == [{"thread_id": result["thread_id"], "run_id": "run-1"}]
    assert runs.wait_calls == 2


@pytest.mark.asyncio
async def test_caller_cancellation_cleans_up_known_run_and_propagates():
    runs = Runs()
    runs.hold_wait = True
    task = asyncio.create_task(HostAgentAdapter(runs).execute(experiment_id="experiment-1", case=CASE))
    try:
        await asyncio.wait_for(runs.wait_entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert len(runs.cancelled) == 1
        assert runs.wait_calls == 2
    finally:
        runs.wait_release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_uncertain_admission_is_marked_for_reconciliation():
    runs = Runs()
    runs.start_error = AgentRunError(503, "provider detail must not leak")
    result = await HostAgentAdapter(runs).execute(experiment_id="experiment-1", case=CASE)
    assert result["needs_reconciliation"]
    assert result["thread_id"] == execution_identity("experiment-1", CASE["id"])[0]
    assert result["run_id"] is None
    assert result["error"] == "Host rejected trial operation (503)"


@pytest.mark.asyncio
async def test_missing_configuration_evidence_is_explicit_without_fabrication():
    runs = Runs()
    runs.evidence_available = False
    result = await HostAgentAdapter(runs).execute(experiment_id="experiment-1", case=CASE)
    assert result["output"] == '{"value": 3}' and result["error"] is None
    assert result["configuration_evidence"] is None
    assert result["evidence_error"]


@pytest.mark.asyncio
async def test_oversized_output_is_an_error_not_a_truncated_answer():
    runs = Runs()
    marker = execution_identity("experiment-1", "case-1")[2]
    runs.messages = [{"type": "human", "id": marker}, {"type": "ai", "content": "x" * (64 * 1024 + 1)}]
    result = await HostAgentAdapter(runs).execute(experiment_id="experiment-1", case=CASE)
    assert result["output"] == ""
    assert result["error"] == "Agent answer exceeds the trial output limit"


@pytest.mark.asyncio
async def test_provider_value_error_text_is_not_exposed():
    runs = Runs()
    runs.start_error = ValueError("Authorization: provider-secret-fixture")
    result = await HostAgentAdapter(runs).execute(experiment_id="experiment-1", case=CASE)
    assert result["error"] == "Agent trial operation failed"
    assert result["needs_reconciliation"]


@pytest.mark.asyncio
async def test_failed_cancellation_observation_requires_reconciliation():
    runs = Runs()

    async def unavailable_wait(**kwargs):
        raise TimeoutError

    runs.wait = unavailable_wait
    result = await HostAgentAdapter(runs).execute(experiment_id="experiment-1", case=CASE)
    assert result["needs_reconciliation"]
    assert result["run_status"] == "needs_reconciliation"
    assert len(runs.cancelled) == 1


@pytest.mark.parametrize("timeout", [0, -1, 3601, True, float("nan"), float("inf")])
def test_invalid_timeouts_are_rejected(timeout):
    with pytest.raises(ValueError):
        HostAgentAdapter(None, timeout_seconds=timeout)
