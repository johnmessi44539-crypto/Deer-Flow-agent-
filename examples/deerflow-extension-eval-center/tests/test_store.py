"""SQLite transaction, owner isolation and exclusive-service lock contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from deerflow_extension_api.agent_runs import AgentRunError

from deerflow_extension_eval_center.cases import dataset_hash, load_dataset
from deerflow_extension_eval_center.store import ExperimentStore


@pytest.fixture
def store(tmp_path):
    instance = ExperimentStore(str(tmp_path / "experiments.sqlite"))
    instance.initialize()
    try:
        yield instance
    finally:
        instance.close()


def _body(identifier="experiment-1", status="queued"):
    return {
        "id": identifier,
        "status": status,
        "created_at": "2026-01-01T00:00:00+00:00",
        "error": None,
        "items": [],
    }


def test_relative_database_path_is_rejected():
    with pytest.raises(ValueError, match="absolute"):
        ExperimentStore("relative.sqlite")


def test_second_service_is_rejected_then_can_acquire_the_released_lock(tmp_path):
    path = str(tmp_path / "shared.sqlite")
    first = ExperimentStore(path)
    second = ExperimentStore(path)
    first.initialize()
    try:
        first.create("alice", "saved", "fingerprint", _body())
        with pytest.raises(RuntimeError, match="One eval-center service"):
            second.initialize()
    finally:
        first.close()
    try:
        second.initialize()
        assert second.get("alice", "experiment-1")["id"] == "experiment-1"
        # Closing the old, already-closed owner must not disturb the new owner.
        first.close()
        third = ExperimentStore(path)
        try:
            with pytest.raises(RuntimeError, match="One eval-center service"):
                third.initialize()
        finally:
            third.close()
    finally:
        second.close()


def test_same_owner_key_reuses_the_persisted_experiment(store):
    original, first_created = store.create("alice", "same-key", "fingerprint", _body("first"))
    replayed, replay_created = store.create("alice", "same-key", "fingerprint", _body("second"))
    assert first_created is True
    assert replay_created is False
    assert replayed["id"] == original["id"] == "first"
    assert len(store.list("alice")) == 1


def test_changed_fingerprint_conflicts_without_a_second_row(store):
    store.create("alice", "same-key", "offline", _body("first"))
    with pytest.raises(AgentRunError) as rejected:
        store.create("alice", "same-key", "agent", _body("second"))
    assert rejected.value.status_code == 409
    assert [body["id"] for body in store.list("alice")] == ["first"]


def test_idempotency_keys_are_scoped_to_owners(store):
    alice, _ = store.create("alice", "same-key", "fingerprint", _body("alice-run"))
    bob, _ = store.create("bob", "same-key", "fingerprint", _body("bob-run"))
    assert alice["id"] != bob["id"]
    assert [body["id"] for body in store.list("alice")] == ["alice-run"]
    assert [body["id"] for body in store.list("bob")] == ["bob-run"]


def test_read_and_write_do_not_cross_owner_boundaries(store):
    body, _ = store.create("alice", "owned", "fingerprint", _body())
    with pytest.raises(AgentRunError) as denied_read:
        store.get("bob", body["id"])
    assert denied_read.value.status_code == 404
    with pytest.raises(AgentRunError) as denied_write:
        store.save("bob", {**body, "status": "cancelled"})
    assert denied_write.value.status_code == 404
    assert store.get("alice", body["id"])["status"] == "queued"


def test_loaded_body_is_detached_from_the_persisted_value(store):
    store.create("alice", "owned", "fingerprint", _body())
    loaded = store.get("alice", "experiment-1")
    loaded["status"] = "forged"
    assert store.get("alice", "experiment-1")["status"] == "queued"


def test_actual_sqlite_transactions_deduplicate_concurrent_creates(store):
    count = 8
    ready = Barrier(count, timeout=10)

    def create(index):
        ready.wait()
        return store.create("alice", "concurrent-key", "fingerprint", _body(f"candidate-{index}"))

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(create, range(count)))
    assert sum(created for _, created in results) == 1
    assert len({body["id"] for body, _ in results}) == 1
    assert len(store.list("alice")) == 1


@pytest.mark.parametrize("status", ["queued", "running", "cancelling"])
def test_restart_reconciles_each_unfinished_status(store, status):
    body = _body(status=status)
    body["items"] = [{"thread_id": "thread-before-restart", "run_id": "run-before-restart"}]
    store.create("alice", "recover", "fingerprint", body)
    store.reconcile_restart()
    recovered = store.get("alice", body["id"])
    assert recovered["status"] == "needs_reconciliation"
    assert recovered["error"]
    assert recovered["items"] == body["items"]


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "needs_reconciliation"])
def test_restart_does_not_rewrite_terminal_or_already_uncertain_outcomes(store, status):
    body = _body(status=status)
    store.create("alice", "terminal", "fingerprint", body)
    store.reconcile_restart()
    assert store.get("alice", body["id"]) == body


def test_concurrent_dataset_imports_preserve_one_immutable_revision(store):
    dataset = {**load_dataset(), "id": "custom-sales"}
    ready = Barrier(8, timeout=10)

    def submit(_):
        ready.wait()
        return store.import_dataset("alice", dataset, dataset_hash(dataset))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert all(result == dataset for result in results)
    assert len(store.list_datasets("alice")) == 1
    changed = {**dataset, "name": "Changed"}
    with pytest.raises(AgentRunError) as conflict:
        store.import_dataset("alice", changed, dataset_hash(changed))
    assert conflict.value.status_code == 409
    assert store.get_dataset("alice", dataset["id"], 1) == dataset


def test_dataset_revision_and_owner_keys_survive_database_reopen(tmp_path):
    path = str(tmp_path / "datasets.sqlite")
    first = ExperimentStore(path)
    first.initialize()
    original = {**load_dataset(), "id": "custom-sales"}
    revised = {**original, "revision": 2, "name": "Revision two"}
    try:
        for dataset in (original, revised):
            first.import_dataset("alice", dataset, dataset_hash(dataset))
        first.import_dataset("bob", {**original, "name": "Bob's dataset"}, "bob-hash")
    finally:
        first.close()
    reopened = ExperimentStore(path)
    reopened.initialize()
    try:
        assert reopened.get_dataset("alice", original["id"], 1) == original
        assert reopened.get_dataset("alice", original["id"], 2) == revised
        assert reopened.get_dataset("bob", original["id"], 1)["name"] == "Bob's dataset"
        assert len(reopened.list_datasets("alice")) == 2
        assert len(reopened.list_datasets("bob")) == 1
        with pytest.raises(AgentRunError) as denied:
            reopened.get_dataset("bob", original["id"], 2)
        assert denied.value.status_code == 404
    finally:
        reopened.close()
