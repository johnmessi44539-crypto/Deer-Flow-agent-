"""Exercise production scheduling, action policy and durable result handling."""

from __future__ import annotations

import asyncio
import hashlib
import json
from threading import Event as ThreadEvent
from types import SimpleNamespace

import pytest
import pytest_asyncio
from deerflow_extension_api.agent_runs import AgentRunError

from deerflow_extension_eval_center import service as service_module
from deerflow_extension_eval_center.cases import dataset_hash, load_dataset
from deerflow_extension_eval_center.service import OFFLINE_OUTPUTS, EvaluationService
from deerflow_extension_eval_center.store import ExperimentStore


@pytest_asyncio.fixture
async def service(tmp_path):
    instance = EvaluationService(str(tmp_path / "experiments.sqlite"))
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


async def _finish(service, body):
    task = service.tasks.get(body["id"])
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    return await service.get("alice", body["id"])


def _context(owner="alice", interaction="session", runs=None):
    return SimpleNamespace(principal=SimpleNamespace(user_id=owner), interaction=interaction, agent_runs=runs)


def test_offline_answers_match_the_published_reference_answers():
    dataset = load_dataset()
    assert set(OFFLINE_OUTPUTS) == {case["id"] for case in dataset["cases"]}
    for case in dataset["cases"]:
        assert json.loads(OFFLINE_OUTPUTS[case["id"]]) == case["expected"]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "   ", "k" * 129, " " * 128 + "k"])
async def test_idempotency_key_rejects_blank_or_oversize_original_value(service, key):
    with pytest.raises(AgentRunError) as denied:
        await service.begin("alice", mode="offline", idempotency_key=key)
    assert denied.value.status_code == 422
    assert (await service.list("alice"))["items"] == []


@pytest.mark.asyncio
async def test_offline_experiment_runs_three_cases_and_freezes_a_report(service):
    admitted = await service.begin("alice", mode="offline", idempotency_key="positive")
    result = await _finish(service, admitted)
    assert result["status"] == "completed"
    assert result["summary"] == {"total": 3, "passed": 3, "failed": 0, "unscored": 0}
    assert result["reference_answer_sent_to_agent"] is False
    assert result["token_usage"] is None
    assert result["cost_estimate"] is None
    for item in result["items"]:
        assert item["status"] == "completed"
        assert item["verdict"] == "pass"
        assert item["run_status"] == "offline_fixture"
        assert item["configuration_evidence"]["is_model_evaluation"] is False
        assert item["thread_id"] is None and item["run_id"] is None
        assert item["output_sha256"] == hashlib.sha256(item["output"].encode()).hexdigest()
        assert item["latency_ms"] >= 0
    report = await service.report("alice", result["id"])
    assert report["mime_type"] == "application/json"
    assert json.loads(report["content"]) == result
    # Mutating a caller's returned projection must not rewrite durable evidence.
    result["items"][0]["output"] = "caller overwrite"
    assert json.loads((await service.report("alice", admitted["id"]))["content"])["items"][0]["output"] != "caller overwrite"


@pytest.mark.asyncio
async def test_wrong_total_is_detected_as_one_failed_case_without_execution_failure(tmp_path):
    wrong = dict(OFFLINE_OUTPUTS)
    original = json.loads(wrong["monthly-sales-001"])
    original["grand_total"] = 999
    wrong["monthly-sales-001"] = json.dumps(original)
    service = EvaluationService(str(tmp_path / "negative.sqlite"), offline_outputs=wrong)
    await service.start()
    try:
        body = await service.begin("alice", mode="offline", idempotency_key="negative")
        result = await _finish(service, body)
        assert result["status"] == "completed"
        assert result["summary"] == {"total": 3, "passed": 2, "failed": 1, "unscored": 0}
        failed = [item for item in result["items"] if item["verdict"] == "fail"]
        assert [item["case_id"] for item in failed] == ["monthly-sales-001"]
        assert failed[0]["error"] is None
        assert any(check["name"] == "$/grand_total" and not check["passed"] for check in failed[0]["checks"])
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_concurrent_same_owner_key_admits_one_experiment_and_one_worker(service):
    ready = asyncio.Queue()
    go = asyncio.Event()
    requests = []
    # Hold the real runner at its existing concurrency boundary while admission
    # executes, so a completed worker cannot obscure duplicate task creation.
    await service._slots.acquire()

    async def begin_together():
        ready.put_nowait(True)
        await go.wait()
        return await service.begin("alice", mode="offline", idempotency_key="same-key")

    try:
        requests = [asyncio.create_task(begin_together()) for _ in range(10)]
        for _ in requests:
            await asyncio.wait_for(ready.get(), timeout=5)
        go.set()
        bodies = await asyncio.wait_for(asyncio.gather(*requests), timeout=10)
        assert len({body["id"] for body in bodies}) == 1
        assert len(service.tasks) == 1
        assert len((await service.list("alice"))["items"]) == 1
    finally:
        go.set()
        service._slots.release()
        await asyncio.gather(*requests, return_exceptions=True)
    result = await _finish(service, bodies[0])
    assert result["summary"]["passed"] == 3


@pytest.mark.asyncio
async def test_same_key_with_different_mode_is_a_conflict(service):
    await service._slots.acquire()
    try:
        first = await service.begin("alice", mode="offline", idempotency_key="mode-key")
        with pytest.raises(AgentRunError) as rejected:
            await service.begin("alice", mode="agent", idempotency_key="mode-key", runs=object())
        assert rejected.value.status_code == 409
        assert len(service.tasks) == 1
        assert len((await service.list("alice"))["items"]) == 1
    finally:
        service._slots.release()
    await _finish(service, first)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["experiment_get", "experiment_cancel", "report_export"])
async def test_another_owner_cannot_read_cancel_or_export_an_experiment(service, action):
    first = await service.begin("alice", mode="offline", idempotency_key="owned")
    await _finish(service, first)
    with pytest.raises(AgentRunError) as denied:
        await service.handler(action)({"id": first["id"]}, _context("bob"))
    assert denied.value.status_code == 404
    assert (await service.get("alice", first["id"]))["status"] == "completed"
    assert (await service.list("bob"))["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["experiment_start", "experiment_cancel"])
@pytest.mark.parametrize("interaction", ["other", "tool", None])
async def test_write_actions_require_session_provenance(service, action, interaction):
    payload = {"mode": "offline", "idempotency_key": "write"} if action == "experiment_start" else {"id": "unknown"}
    with pytest.raises(AgentRunError) as denied:
        await service.handler(action)(payload, _context(interaction=interaction))
    assert denied.value.status_code == 403
    assert (await service.list("alice"))["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["user_id", "owner", "command"])
async def test_start_action_rejects_identity_and_command_injection(service, field):
    with pytest.raises(AgentRunError) as denied:
        await service.handler("experiment_start")({"mode": "offline", "idempotency_key": "injection", field: "bob"}, _context())
    assert denied.value.status_code == 422
    assert (await service.list("alice"))["items"] == []
    assert (await service.list("bob"))["items"] == []


@pytest.mark.asyncio
async def test_cancel_action_rejects_caller_supplied_owner(service):
    first = await service.begin("alice", mode="offline", idempotency_key="existing")
    await _finish(service, first)
    with pytest.raises(AgentRunError) as denied:
        await service.handler("experiment_cancel")({"id": first["id"], "user_id": "bob"}, _context())
    assert denied.value.status_code == 422


@pytest.mark.asyncio
async def test_service_restart_marks_unfinished_work_for_reconciliation_without_launching_it(tmp_path):
    path = str(tmp_path / "restart.sqlite")
    store = ExperimentStore(path)
    store.initialize()
    try:
        for index, status in enumerate(["queued", "running", "cancelling", "completed"]):
            body = {
                "id": f"experiment-{index}",
                "created_at": "2026-01-01T00:00:00+00:00",
                "status": status,
                "error": None,
                "items": [{"thread_id": "saved-thread", "run_id": "saved-run"}],
            }
            store.create("alice", f"restart-{index}", "fingerprint", body)
    finally:
        store.close()
    restarted = EvaluationService(path)
    await restarted.start()
    try:
        for index in range(3):
            body = await restarted.get("alice", f"experiment-{index}")
            assert body["status"] == "needs_reconciliation"
            assert body["error"]
            assert body["items"][0] == {"thread_id": "saved-thread", "run_id": "saved-run"}
        assert (await restarted.get("alice", "experiment-3"))["status"] == "completed"
        assert restarted.tasks == {}
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_agent_mode_requires_the_host_capability(service):
    with pytest.raises(AgentRunError) as denied:
        await service.begin("alice", mode="agent", idempotency_key="missing-capability")
    assert denied.value.status_code == 503
    assert (await service.list("alice"))["items"] == []


@pytest.mark.asyncio
async def test_start_before_service_start_is_rejected(tmp_path):
    instance = EvaluationService(str(tmp_path / "not-started.sqlite"))
    with pytest.raises(AgentRunError) as denied:
        await instance.begin("alice", mode="offline", idempotency_key="not-started")
    assert denied.value.status_code == 503


@pytest.mark.asyncio
async def test_two_cancellers_do_not_interrupt_the_same_workers_cleanup(service, monkeypatch):
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    second_control_finished = asyncio.Event()
    never = asyncio.Event()

    class ControlledAdapter:
        def __init__(self, *args, **kwargs):
            pass

        async def execute(self, *, experiment_id, case, on_run):
            await on_run("owned-thread", "owned-run")
            entered.set()
            try:
                await never.wait()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
                cleanup_finished.set()

    class Runs:
        async def get(self, **kwargs):
            return SimpleNamespace(status="interrupted")

    class ObservedLock:
        """Observe the second cancel's decision, while retaining the real lock."""

        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.exits = 0

        async def __aenter__(self):
            return await self.wrapped.__aenter__()

        async def __aexit__(self, *args):
            result = await self.wrapped.__aexit__(*args)
            self.exits += 1
            if self.exits == 2:
                second_control_finished.set()
            return result

    monkeypatch.setattr(service_module, "HostAgentAdapter", ControlledAdapter)
    body = await service.begin("alice", mode="agent", idempotency_key="cancel-twice", runs=Runs())
    worker = service.tasks[body["id"]]
    cancellers = []
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        service._lock = ObservedLock(service._lock)
        first = asyncio.create_task(service.cancel("alice", body["id"]), name="first-canceller")
        cancellers.append(first)
        await asyncio.wait_for(cleanup_started.wait(), timeout=5)
        second = asyncio.create_task(service.cancel("alice", body["id"]), name="second-canceller")
        cancellers.append(second)
        await asyncio.wait_for(second_control_finished.wait(), timeout=5)
        # The second cancellation request has completed its admission decision;
        # the first worker is still at the explicit adapter-cleanup barrier.
        assert worker.cancelling() == 1
        assert not worker.done()
        assert not cleanup_finished.is_set()
    finally:
        release_cleanup.set()
        await asyncio.wait_for(asyncio.gather(*cancellers, return_exceptions=True), timeout=10)
    assert cleanup_finished.is_set()
    assert (await service.get("alice", body["id"]))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_worker_drains_an_accepted_sqlite_write_before_terminal_cleanup(service, monkeypatch):
    loop = asyncio.get_running_loop()
    write_entered = asyncio.Event()
    write_finished = asyncio.Event()
    cancellation_turn = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_write = ThreadEvent()
    held = False
    original_save = service.store.save
    original_finish = service._finish_cancel

    def held_save(owner, body):
        nonlocal held
        if body["status"] == "running" and not held:
            held = True
            loop.call_soon_threadsafe(write_entered.set)
            try:
                if not release_write.wait(timeout=10):
                    raise RuntimeError("test did not release the controlled SQLite write")
                return original_save(owner, body)
            finally:
                loop.call_soon_threadsafe(write_finished.set)
        return original_save(owner, body)

    async def observed_finish(*args, **kwargs):
        cleanup_started.set()
        return await original_finish(*args, **kwargs)

    monkeypatch.setattr(service.store, "save", held_save)
    monkeypatch.setattr(service, "_finish_cancel", observed_finish)
    body = await service.begin("alice", mode="offline", idempotency_key="drain-write")
    worker = service.tasks[body["id"]]
    try:
        await asyncio.wait_for(write_entered.wait(), timeout=5)
        worker.cancel()
        # FIFO callbacks provide a cancellation-delivery barrier without a
        # machine-speed-dependent sleep. The worker's wakeup is queued first.
        loop.call_soon(cancellation_turn.set)
        await asyncio.wait_for(cancellation_turn.wait(), timeout=5)
        assert worker.cancelling() == 1
        assert not cleanup_started.is_set()
        assert not write_finished.is_set()
    finally:
        release_write.set()
        await asyncio.wait_for(write_finished.wait(), timeout=10)
        await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=10)
    assert held is True
    assert cleanup_started.is_set()
    saved = await service.get("alice", body["id"])
    assert saved["status"] == "cancelled"
    assert all(item["status"] == "cancelled" for item in saved["items"])


@pytest.mark.asyncio
async def test_terminal_cleanup_serializes_with_a_second_cancellers_stale_save(service, monkeypatch):
    loop = asyncio.get_running_loop()
    adapter_entered = asyncio.Event()
    adapter_cleanup_started = asyncio.Event()
    release_adapter_cleanup = asyncio.Event()
    second_save_entered = asyncio.Event()
    second_save_finished = asyncio.Event()
    finalizer_waiting_for_lock = asyncio.Event()
    terminal_save_entered = asyncio.Event()
    release_second_save = ThreadEvent()
    never = asyncio.Event()
    original_save = service.store.save
    cancelling_saves = 0
    committed_order = []

    class ControlledAdapter:
        def __init__(self, *args, **kwargs):
            pass

        async def execute(self, *, experiment_id, case, on_run):
            await on_run("owned-thread", "owned-run")
            adapter_entered.set()
            try:
                await never.wait()
            finally:
                adapter_cleanup_started.set()
                await release_adapter_cleanup.wait()

    class Runs:
        async def get(self, **kwargs):
            return SimpleNamespace(status="interrupted")

    class ObservedLock:
        """Observe actual finalizer lock acquisition without replacing locking."""

        def __init__(self, wrapped):
            self.wrapped = wrapped

        async def __aenter__(self):
            if asyncio.current_task() is worker and second_save_entered.is_set():
                # The second cancellation signal still holds this real lock.
                assert self.wrapped.locked()
                finalizer_waiting_for_lock.set()
            return await self.wrapped.__aenter__()

        async def __aexit__(self, *args):
            return await self.wrapped.__aexit__(*args)

    def controlled_save(owner, body):
        nonlocal cancelling_saves
        if body["status"] == "cancelling":
            cancelling_saves += 1
            if cancelling_saves == 2:
                # This body was read before adapter cleanup finished. Hold its
                # actual SQLite write so it could overwrite a newer terminal
                # body if the finalizer did not share the signal's lock.
                loop.call_soon_threadsafe(second_save_entered.set)
                try:
                    if not release_second_save.wait(timeout=10):
                        raise RuntimeError("test did not release the second cancelling write")
                    result = original_save(owner, body)
                    committed_order.append("second-cancelling")
                    return result
                finally:
                    loop.call_soon_threadsafe(second_save_finished.set)
        if body["status"] == "cancelled":
            loop.call_soon_threadsafe(terminal_save_entered.set)
            result = original_save(owner, body)
            committed_order.append("terminal-cancelled")
            return result
        return original_save(owner, body)

    monkeypatch.setattr(service_module, "HostAgentAdapter", ControlledAdapter)
    monkeypatch.setattr(service.store, "save", controlled_save)
    body = await service.begin("alice", mode="agent", idempotency_key="stale-cancel-write", runs=Runs())
    worker = service.tasks[body["id"]]
    cancellers = []
    try:
        await asyncio.wait_for(adapter_entered.wait(), timeout=5)
        service._lock = ObservedLock(service._lock)
        cancellers.append(asyncio.create_task(service.cancel("alice", body["id"])))
        await asyncio.wait_for(adapter_cleanup_started.wait(), timeout=5)
        cancellers.append(asyncio.create_task(service.cancel("alice", body["id"])))
        await asyncio.wait_for(second_save_entered.wait(), timeout=5)
        release_adapter_cleanup.set()
        await asyncio.wait_for(finalizer_waiting_for_lock.wait(), timeout=5)
        assert not terminal_save_entered.is_set()
        assert not second_save_finished.is_set()
    finally:
        release_adapter_cleanup.set()
        release_second_save.set()
        if cancelling_saves >= 2:
            await asyncio.wait_for(second_save_finished.wait(), timeout=10)
        await asyncio.wait_for(asyncio.gather(*cancellers, return_exceptions=True), timeout=10)
    assert committed_order == ["second-cancelling", "terminal-cancelled"]
    assert terminal_save_entered.is_set()
    assert (await service.get("alice", body["id"]))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_stop_closes_admission_before_a_queued_publication_can_start(service):
    original_lock = service._lock
    stop_waiting = asyncio.Event()
    publication_waiting = asyncio.Event()
    controls = []
    released = False

    class ObservedLock:
        def __init__(self):
            self.attempts = 0

        async def __aenter__(self):
            self.attempts += 1
            (stop_waiting if self.attempts == 1 else publication_waiting).set()
            return await original_lock.__aenter__()

        async def __aexit__(self, *args):
            return await original_lock.__aexit__(*args)

    await original_lock.acquire()
    service._lock = ObservedLock()
    try:
        stopping = asyncio.create_task(service.stop())
        controls.append(stopping)
        await asyncio.wait_for(stop_waiting.wait(), timeout=5)
        # Shutdown has queued for the lock but has not closed admission yet.
        # The request passes the outer readiness check and queues publication
        # behind shutdown. The real lock decides this deterministic ordering.
        starting = asyncio.create_task(service.begin("alice", mode="offline", idempotency_key="stop-race"))
        controls.append(starting)
        await asyncio.wait_for(publication_waiting.wait(), timeout=5)
        original_lock.release()
        released = True
        outcomes = await asyncio.wait_for(asyncio.gather(*controls, return_exceptions=True), timeout=10)
        assert outcomes[0] is None
        assert isinstance(outcomes[1], AgentRunError)
        assert outcomes[1].status_code == 503
        assert service.tasks == {}
        assert service._started is False
        reopened = ExperimentStore(str(service.store.path))
        try:
            await asyncio.to_thread(reopened.initialize)
            assert await asyncio.to_thread(reopened.list, "alice") == []
        finally:
            await asyncio.to_thread(reopened.close)
    finally:
        if not released:
            original_lock.release()
        await asyncio.wait_for(asyncio.gather(*controls, return_exceptions=True), timeout=10)


@pytest.mark.asyncio
async def test_cancelled_start_drains_initialization_and_releases_its_database_lock(tmp_path, monkeypatch):
    instance = EvaluationService(str(tmp_path / "cancel-start.sqlite"))
    competitor = ExperimentStore(str(instance.store.path))
    loop = asyncio.get_running_loop()
    initialized = asyncio.Event()
    release_initialization = ThreadEvent()
    original_initialize = instance.store.initialize

    def held_initialize():
        original_initialize()
        loop.call_soon_threadsafe(initialized.set)
        if not release_initialization.wait(timeout=10):
            raise RuntimeError("test did not release initialization")

    monkeypatch.setattr(instance.store, "initialize", held_initialize)
    starting = asyncio.create_task(instance.start())
    try:
        await asyncio.wait_for(initialized.wait(), timeout=5)
        with pytest.raises(RuntimeError, match="One eval-center service"):
            await asyncio.to_thread(competitor.initialize)
        starting.cancel()
        release_initialization.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, timeout=10)
        assert instance._started is False
        await asyncio.to_thread(competitor.initialize)
    finally:
        release_initialization.set()
        await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), timeout=10)
        await asyncio.to_thread(competitor.close)
        await instance.stop()


def _custom_dataset(revision=1):
    dataset = load_dataset()
    dataset.update(id="custom-regression", name="Custom deterministic case", revision=revision)
    dataset["cases"] = [
        {
            "id": "custom-value",
            "title": "Custom value",
            "prompt": 'Return only {"value":3}',
            "expected": {"value": 3},
            "grader": "json_exact_v1",
        }
    ]
    return dataset


@pytest.mark.asyncio
async def test_dataset_import_same_revision_and_hash_is_idempotent(service):
    dataset = _custom_dataset()
    first = await service.handler("dataset_import")({"dataset": dataset}, _context())
    replay = await service.handler("dataset_import")({"dataset": dataset}, _context())
    assert first == replay == dataset
    listing = await service.handler("dataset_list")({}, _context())
    imported = [item for item in listing["items"] if item["id"] == dataset["id"]]
    assert len(imported) == 1
    assert imported[0]["hash"] == dataset_hash(dataset)


@pytest.mark.asyncio
async def test_dataset_revision_is_immutable_and_new_revision_preserves_the_old_one(service):
    original = _custom_dataset()
    await service.handler("dataset_import")({"dataset": original}, _context())
    changed = _custom_dataset()
    changed["cases"][0]["expected"]["value"] = 99
    with pytest.raises(AgentRunError) as conflict:
        await service.handler("dataset_import")({"dataset": changed}, _context())
    assert conflict.value.status_code == 409
    changed["revision"] = 2
    await service.handler("dataset_import")({"dataset": changed}, _context())
    old = await service.handler("dataset_get")({"id": original["id"], "revision": 1}, _context())
    new = await service.handler("dataset_get")({"id": original["id"], "revision": 2}, _context())
    assert old == original
    assert new == changed
    assert dataset_hash(old) != dataset_hash(new)
    listing = await service.handler("dataset_list")({}, _context())
    assert [item["revision"] for item in listing["items"] if item["id"] == original["id"]] == [2, 1]


@pytest.mark.asyncio
async def test_custom_datasets_are_owner_scoped_even_when_ids_and_revisions_match(service):
    alice = _custom_dataset()
    await service.handler("dataset_import")({"dataset": alice}, _context("alice"))
    with pytest.raises(AgentRunError) as missing:
        await service.handler("dataset_get")({"id": alice["id"], "revision": 1}, _context("bob"))
    assert missing.value.status_code == 404
    listing = await service.handler("dataset_list")({}, _context("bob"))
    assert not any(item["id"] == alice["id"] for item in listing["items"])
    bob = _custom_dataset()
    bob["cases"][0]["expected"]["value"] = 88
    await service.handler("dataset_import")({"dataset": bob}, _context("bob"))
    assert await service.get_dataset("alice", alice["id"], 1) == alice
    assert await service.get_dataset("bob", bob["id"], 1) == bob


@pytest.mark.asyncio
async def test_dataset_import_requires_a_session_and_rejects_owner_injection(service):
    dataset = _custom_dataset()
    with pytest.raises(AgentRunError) as denied:
        await service.handler("dataset_import")({"dataset": dataset}, _context(interaction="other"))
    assert denied.value.status_code == 403
    with pytest.raises(AgentRunError) as injected:
        await service.handler("dataset_import")({"dataset": dataset, "user_id": "bob"}, _context())
    assert injected.value.status_code == 422
    assert not any(item["id"] == dataset["id"] for item in (await service.handler("dataset_list")({}, _context()))["items"])


@pytest.mark.asyncio
async def test_custom_dataset_cannot_silently_reuse_builtin_offline_answers(service):
    dataset = _custom_dataset()
    await service.handler("dataset_import")({"dataset": dataset}, _context())
    with pytest.raises(AgentRunError) as rejected:
        await service.handler("experiment_start")(
            {"mode": "offline", "idempotency_key": "custom-offline", "dataset_id": dataset["id"], "dataset_revision": 1},
            _context(),
        )
    assert rejected.value.status_code == 422
    assert (await service.list("alice"))["items"] == []


@pytest.mark.asyncio
async def test_custom_agent_experiment_freezes_the_selected_dataset_version(service):
    class Runs:
        def __init__(self):
            self.inputs = []
            self.messages = {}

        async def create_thread(self, **kwargs):
            return kwargs["thread_id"]

        async def start(self, **kwargs):
            self.inputs.append(kwargs)
            self.messages[kwargs["thread_id"]] = kwargs["input"]["messages"]
            return SimpleNamespace(run_id="run-1", status="pending")

        async def wait(self, **kwargs):
            return SimpleNamespace(run_id=kwargs["run_id"], status="success")

        async def get_state(self, **kwargs):
            return {
                "values": {"messages": [*self.messages[kwargs["thread_id"]], {"type": "ai", "content": '{"value":3}'}]},
                "next": [],
                "interrupts": [],
            }

        async def configuration_evidence(self, **kwargs):
            return {"source": "host_assembly", **kwargs}

    original = _custom_dataset()
    await service.handler("dataset_import")({"dataset": original}, _context())
    runs = Runs()
    await service._slots.acquire()
    try:
        body = await service.handler("experiment_start")(
            {"mode": "agent", "idempotency_key": "custom-agent", "dataset_id": original["id"], "dataset_revision": 1},
            _context(runs=runs),
        )
        changed = _custom_dataset(revision=2)
        changed["cases"][0]["prompt"] = 'Return only {"value":999}'
        changed["cases"][0]["expected"] = {"value": 999}
        await service.handler("dataset_import")({"dataset": changed}, _context())
    finally:
        service._slots.release()
    result = await _finish(service, body)
    assert result["dataset"] == original
    assert result["dataset_hash"] == dataset_hash(original)
    assert result["summary"] == {"total": 1, "passed": 1, "failed": 0, "unscored": 0}
    assert result["items"][0]["expected"] == {"value": 3}
    assert len(runs.inputs) == 1
    human = runs.inputs[0]["input"]["messages"][0]
    assert human["content"] == original["cases"][0]["prompt"]
    assert set(human) == {"type", "id", "content"}


@pytest.mark.asyncio
async def test_wrong_total_scenario_is_handler_accessible_and_part_of_idempotency(service):
    payload = {"mode": "offline", "idempotency_key": "negative-handler", "offline_scenario": "wrong_total"}
    body = await service.handler("experiment_start")(payload, _context())
    result = await _finish(service, body)
    assert result["offline_scenario"] == "wrong_total"
    assert result["summary"] == {"total": 3, "passed": 2, "failed": 1, "unscored": 0}
    assert result["items"][0]["case_id"] == "monthly-sales-001"
    assert result["items"][0]["verdict"] == "fail"
    replay = await service.handler("experiment_start")(payload, _context())
    assert replay["id"] == body["id"]
    with pytest.raises(AgentRunError) as conflict:
        await service.handler("experiment_start")({**payload, "offline_scenario": "correct"}, _context())
    assert conflict.value.status_code == 409
    assert len((await service.list("alice"))["items"]) == 1


@pytest.mark.asyncio
async def test_comparison_handler_blocks_other_owners_experiments(service):
    alice = await service.begin("alice", mode="offline", idempotency_key="alice")
    await _finish(service, alice)
    bob = await service.begin("bob", mode="offline", idempotency_key="bob")
    task = service.tasks.get(bob["id"])
    if task is not None:
        await asyncio.wait_for(task, timeout=10)
    with pytest.raises(AgentRunError) as denied:
        await service.handler("experiment_compare")({"baseline_id": alice["id"], "candidate_id": bob["id"]}, _context())
    assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_comparison_handler_rejects_an_in_progress_experiment(service):
    baseline = await service.begin("alice", mode="offline", idempotency_key="done")
    await _finish(service, baseline)
    await service._slots.acquire()
    try:
        queued = await service.begin("alice", mode="offline", idempotency_key="queued")
        with pytest.raises(AgentRunError) as rejected:
            await service.handler("experiment_compare")({"baseline_id": baseline["id"], "candidate_id": queued["id"]}, _context())
        assert rejected.value.status_code == 409
    finally:
        service._slots.release()
    await _finish(service, queued)


@pytest.mark.asyncio
async def test_handler_compares_positive_and_negative_control_without_claiming_model_quality(service):
    baseline = await service.begin("alice", mode="offline", idempotency_key="comparison-good")
    await _finish(service, baseline)
    candidate = await service.begin("alice", mode="offline", idempotency_key="comparison-bad", offline_scenario="wrong_total")
    await _finish(service, candidate)
    result = await service.handler("experiment_compare")({"baseline_id": baseline["id"], "candidate_id": candidate["id"]}, _context())
    assert result["compatible"] is True
    assert result["summary"] == {"improved": 0, "regressed": 1, "unchanged": 2, "unscored": 0}
    assert result["baseline_scenario"] == "correct"
    assert result["candidate_scenario"] == "wrong_total"
    assert result["is_model_comparison"] is False


@pytest.mark.asyncio
async def test_stop_waits_for_an_accepted_dataset_import_and_rejects_later_imports(service, monkeypatch):
    loop = asyncio.get_running_loop()
    import_entered = asyncio.Event()
    stop_waiting = asyncio.Event()
    release_import = ThreadEvent()
    original_lock = service._lock
    original_import = service.store.import_dataset
    competitor = ExperimentStore(str(service.store.path))
    dataset = _custom_dataset()
    operations = []

    class ObservedLock:
        def __init__(self):
            self.attempts = 0

        async def __aenter__(self):
            self.attempts += 1
            if self.attempts == 2:
                stop_waiting.set()
            return await original_lock.__aenter__()

        async def __aexit__(self, *args):
            return await original_lock.__aexit__(*args)

    def held_import(owner, body, content_hash):
        loop.call_soon_threadsafe(import_entered.set)
        if not release_import.wait(timeout=10):
            raise RuntimeError("test did not release the accepted dataset import")
        return original_import(owner, body, content_hash)

    service._lock = ObservedLock()
    monkeypatch.setattr(service.store, "import_dataset", held_import)
    try:
        importing = asyncio.create_task(service.handler("dataset_import")({"dataset": dataset}, _context()))
        operations.append(importing)
        await asyncio.wait_for(import_entered.wait(), timeout=5)
        stopping = asyncio.create_task(service.stop())
        operations.append(stopping)
        await asyncio.wait_for(stop_waiting.wait(), timeout=5)
        assert not importing.done()
        assert not stopping.done()
        assert service._started is True
        # The accepted import is still using this service's lifecycle. Another
        # instance must not acquire its database while the write is outstanding.
        with pytest.raises(RuntimeError, match="One eval-center service"):
            await asyncio.to_thread(competitor.initialize)
        release_import.set()
        saved, stopped = await asyncio.wait_for(asyncio.gather(*operations), timeout=10)
        assert saved == dataset
        assert stopped is None
        assert service._started is False
        await asyncio.to_thread(competitor.initialize)
        assert await asyncio.to_thread(competitor.get_dataset, "alice", dataset["id"], 1) == dataset
        with pytest.raises(AgentRunError) as rejected:
            await service.handler("dataset_import")({"dataset": _custom_dataset(revision=2)}, _context())
        assert rejected.value.status_code == 503
        records = await asyncio.to_thread(competitor.list_datasets, "alice")
        assert [(item["id"], item["revision"]) for item in records] == [(dataset["id"], 1)]
    finally:
        release_import.set()
        await asyncio.wait_for(asyncio.gather(*operations, return_exceptions=True), timeout=10)
        await asyncio.to_thread(competitor.close)
