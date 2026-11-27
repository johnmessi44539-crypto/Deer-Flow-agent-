"""Supervised, owner-scoped evaluation jobs; no arbitrary code execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from deerflow_extension_api.agent_runs import AgentRunError

from .agent_adapter import HostAgentAdapter, execution_identity
from .cases import dataset_hash, load_dataset, validate_dataset
from .compare import compare_experiments
from .graders import grade_output
from .store import ExperimentStore

OFFLINE_OUTPUTS = {
    "monthly-sales-001": '{"month_totals":{"2026-01":300,"2026-02":80},"grand_total":380}',
    "net-revenue-001": '{"net_revenue":230,"fully_refunded_orders":1}',
    "invalid-amount-001": '{"error_code":"INVALID_AMOUNT","invalid_row":2}',
}
logger = logging.getLogger(__name__)


async def _drain(awaitable):
    """Finish an accepted mutation before propagating caller cancellation."""
    operation = asyncio.ensure_future(awaitable)
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(operation)
            break
        except asyncio.CancelledError as exc:
            cancelled = exc
            if operation.done():
                result = operation.result()
                break
    if cancelled is not None:
        raise cancelled
    return result


async def _io(function, *args):
    return await _drain(asyncio.to_thread(function, *args))


def now() -> str:
    return datetime.now(UTC).isoformat()


def summarize(items: list[dict]) -> dict:
    return {
        "total": len(items),
        "passed": sum(item.get("verdict") == "pass" for item in items),
        "failed": sum(item.get("verdict") == "fail" for item in items),
        "unscored": sum(item.get("verdict") not in ("pass", "fail") for item in items),
    }


class EvaluationService:
    def __init__(self, storage_path: str, *, assistant_id="lead_agent", timeout_seconds=120, offline_outputs=None):
        self.store = ExperimentStore(storage_path)
        self.assistant_id = assistant_id
        self.timeout_seconds = timeout_seconds
        self.dataset = load_dataset()
        self.offline_outputs = dict(OFFLINE_OUTPUTS if offline_outputs is None else offline_outputs)
        self.tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(1)
        self._started = False
        self._stopping = False

    async def start(self, deps=None):
        if self._started:
            return
        try:
            await _io(self.store.initialize)
            await _io(self.store.reconcile_restart)
        except BaseException:
            await _io(self.store.close)
            raise
        self._started = True
        self._stopping = False

    async def stop(self):
        await _drain(self._stop())

    async def _stop(self):
        async with self._lock:
            self._stopping = True
            pending = list(self.tasks.values())
        try:
            for task in pending:
                if not task.cancelling():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            try:
                await _io(self.store.close)
            finally:
                self._started = False

    async def get(self, owner: str, identifier: str) -> dict:
        return await asyncio.to_thread(self.store.get, owner, identifier)

    async def list(self, owner: str) -> dict:
        bodies = await asyncio.to_thread(self.store.list, owner)
        fields = ("id", "mode", "offline_scenario", "status", "created_at", "updated_at", "summary", "dataset_hash")
        return {"items": [{key: body[key] for key in fields if key in body} for body in bodies]}

    async def _update(self, owner: str, identifier: str, *, item_index=None, changes=None, **fields):
        async with self._lock:
            body = await self.get(owner, identifier)
            if item_index is not None:
                body["items"][item_index].update(changes or {})
            body.update(fields)
            body["updated_at"] = now()
            body["summary"] = summarize(body["items"])
            await _io(self.store.save, owner, body)
            return body

    async def begin(
        self,
        owner: str,
        *,
        mode: str,
        idempotency_key: str,
        runs=None,
        offline_scenario="correct",
        dataset_id=None,
        dataset_revision=None,
    ) -> dict:
        if not self._started or self._stopping:
            raise AgentRunError(503, "Evaluation service is not ready")
        if not owner or len(owner) > 256:
            raise AgentRunError(403, "Authenticated owner required")
        if mode not in ("offline", "agent"):
            raise AgentRunError(422, "mode must be offline or agent")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or not 1 <= len(idempotency_key) <= 128:
            raise AgentRunError(422, "idempotency_key must contain 1-128 characters")
        if mode == "agent" and runs is None:
            raise AgentRunError(503, "Authenticated AgentRuns capability unavailable")
        if offline_scenario not in ("correct", "wrong_total") or (mode != "offline" and offline_scenario != "correct"):
            raise AgentRunError(422, "Offline scenario is supported only for offline demonstrations")
        selected_dataset = await self.get_dataset(owner, dataset_id, dataset_revision)
        if mode == "offline" and selected_dataset["id"] != self.dataset["id"]:
            raise AgentRunError(422, "Custom datasets require real Agent execution")
        fingerprint = hashlib.sha256(
            json.dumps([mode, dataset_hash(selected_dataset), self.assistant_id, offline_scenario], ensure_ascii=False).encode()
        ).hexdigest()
        identifier = str(uuid4())
        body = {
            "id": identifier,
            "mode": mode,
            "offline_scenario": offline_scenario,
            "status": "queued",
            "created_at": now(),
            "updated_at": now(),
            "dataset": selected_dataset,
            "dataset_hash": dataset_hash(selected_dataset),
            "assistant_id": self.assistant_id,
            "reference_answer_sent_to_agent": False,
            "token_usage": None,
            "cost_estimate": None,
            "error": None,
            "items": [
                {
                    "case_id": case["id"],
                    "title": case["title"],
                    "status": "queued",
                    "verdict": None,
                    "prompt": case["prompt"],
                    "expected": case["expected"],
                    "output": "",
                    "checks": [],
                    "error": None,
                    "thread_id": None,
                    "run_id": None,
                    "latency_ms": None,
                }
                for case in selected_dataset["cases"]
            ],
        }
        body["summary"] = summarize(body["items"])

        # Hold through publication of the task so a concurrent cancel can see it.
        async def publish():
            async with self._lock:
                if not self._started or self._stopping:
                    raise AgentRunError(503, "Evaluation service is stopping")
                saved, created = await _io(self.store.create, owner, idempotency_key, fingerprint, body)
                if created:
                    task = asyncio.create_task(self._execute(owner, saved["id"], runs), name=f"eval:{saved['id']}")
                    self.tasks[saved["id"]] = task
                    task.add_done_callback(lambda done, key=saved["id"]: self._observe_worker(key, done))
                return saved

        return await _drain(publish())

    def _observe_worker(self, identifier, task):
        self.tasks.pop(identifier, None)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.error("Evaluation worker cleanup failed (%s)", type(error).__name__)

    async def cancel(self, owner: str, identifier: str) -> dict:
        async def signal():
            async with self._lock:
                body = await self.get(owner, identifier)
                if body["status"] not in ("queued", "running", "cancelling"):
                    return None
                task = self.tasks.get(identifier)
                first_cancel = body["status"] != "cancelling"
                body["status"] = "cancelling"
                await _io(self.store.save, owner, body)
                if task is None:
                    body["status"] = "needs_reconciliation"
                    body["error"] = "No active local worker; inspect saved run IDs."
                    await _io(self.store.save, owner, body)
                elif first_cancel:
                    task.cancel()
                return task

        task = await _drain(signal())
        # A coroutine cancelled before its first step does not run its finally.
        if task is not None:
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            latest = await self.get(owner, identifier)
            if latest["status"] == "cancelling":
                return await self._finish_cancel(owner, identifier, runs=None)
        return await self.get(owner, identifier)

    async def _finish_cancel(self, owner: str, identifier: str, runs) -> dict:
        async with self._lock:
            return await self._finish_cancel_locked(owner, identifier, runs)

    async def _finish_cancel_locked(self, owner: str, identifier: str, runs) -> dict:
        body = await self.get(owner, identifier)
        uncertain = False
        for item in body["items"]:
            if item["status"] not in ("running", "queued"):
                continue
            if body["mode"] == "agent" and item.get("thread_id"):
                if not item.get("run_id") or runs is None:
                    uncertain = True
                else:
                    try:
                        actual = await asyncio.wait_for(runs.get(thread_id=item["thread_id"], run_id=item["run_id"]), timeout=5)
                        uncertain |= actual.status in ("pending", "running")
                    except Exception:
                        uncertain = True
            item["status"] = "cancelled"
        body["status"] = "needs_reconciliation" if uncertain else "cancelled"
        body["error"] = "Cancellation needs host reconciliation." if uncertain else None
        body["summary"] = summarize(body["items"])
        body["updated_at"] = now()
        await _io(self.store.save, owner, body)
        return body

    async def _execute(self, owner: str, identifier: str, runs):
        try:
            async with self._slots:
                body = await self._update(owner, identifier, status="running")
                for index, case in enumerate(body["dataset"]["cases"]):
                    started = monotonic()
                    await self._update(owner, identifier, item_index=index, changes={"status": "running"})
                    if body["mode"] == "offline":
                        await asyncio.sleep(0)
                        output = self.offline_outputs.get(case["id"], "")
                        if body.get("offline_scenario") == "wrong_total" and case["id"] == "monthly-sales-001":
                            altered = json.loads(output)
                            altered["grand_total"] = 999
                            output = json.dumps(altered, ensure_ascii=False)
                        result = {
                            "output": output,
                            "error": None,
                            "blocked": False,
                            "run_status": "offline_fixture",
                            "configuration_evidence": {"source": "fixed-scripted-response", "is_model_evaluation": False},
                        }
                    else:
                        planned_thread, _, _ = execution_identity(identifier, case["id"])
                        await self._update(owner, identifier, item_index=index, changes={"thread_id": planned_thread})

                        async def on_run(thread_id, run_id, item_index=index):
                            await self._update(owner, identifier, item_index=item_index, changes={"thread_id": thread_id, "run_id": run_id})

                        adapter = HostAgentAdapter(runs, timeout_seconds=self.timeout_seconds, assistant_id=self.assistant_id)
                        result = await adapter.execute(experiment_id=identifier, case=case, on_run=on_run)
                    if result.get("blocked") or result.get("error"):
                        grade = {"verdict": None, "checks": [], "grader_error": None}
                    else:
                        try:
                            grade = grade_output(result["output"], case["expected"], case["grader"])
                        except Exception as exc:
                            grade = {"verdict": None, "checks": [], "grader_error": type(exc).__name__}
                    changes = {
                        **result,
                        **grade,
                        # Keep execution errors separate from normal grading failures.
                        "error": result.get("error"),
                        "grading_error": grade.get("error"),
                        "status": "blocked" if result.get("blocked") else ("failed" if result.get("error") else "completed"),
                        "latency_ms": round((monotonic() - started) * 1000, 2),
                        "output_sha256": hashlib.sha256(result.get("output", "").encode()).hexdigest(),
                    }
                    await self._update(owner, identifier, item_index=index, changes=changes)
                    if result.get("needs_reconciliation"):
                        await self._update(owner, identifier, status="needs_reconciliation", error=result.get("error"))
                        return
                await self._update(owner, identifier, status="completed")
        except asyncio.CancelledError:
            await self._finish_cancel(owner, identifier, runs)
            raise
        except Exception as exc:
            await self._update(owner, identifier, status="failed", error=f"Evaluation infrastructure failed ({type(exc).__name__})")

    async def report(self, owner: str, identifier: str) -> dict:
        body = await self.get(owner, identifier)
        return {
            "filename": f"eval-{identifier}.json",
            "mime_type": "application/json",
            "content": json.dumps(body, ensure_ascii=False, indent=2),
        }

    async def get_dataset(self, owner, identifier=None, revision=None):
        if identifier is None and revision is None:
            return json.loads(json.dumps(self.dataset, ensure_ascii=False))
        if not isinstance(identifier, str) or not identifier or type(revision) is not int or revision < 1:
            raise AgentRunError(422, "Both dataset id and positive integer revision are required")
        if identifier == self.dataset["id"]:
            if revision != self.dataset["revision"]:
                raise AgentRunError(404, "Dataset revision not found")
            return json.loads(json.dumps(self.dataset, ensure_ascii=False))
        return await asyncio.to_thread(self.store.get_dataset, owner, identifier, revision)

    async def import_dataset(self, owner, payload):
        try:
            dataset = validate_dataset(payload)
        except ValueError as exc:
            raise AgentRunError(422, str(exc)) from None
        if dataset["id"] == self.dataset["id"]:
            raise AgentRunError(409, "The built-in dataset ID is reserved; use a custom dataset ID")
        async with self._lock:
            if not self._started or self._stopping:
                raise AgentRunError(503, "Evaluation service is stopping")
            return await _io(self.store.import_dataset, owner, dataset, dataset_hash(dataset))

    async def compare(self, owner, baseline_id, candidate_id):
        baseline, candidate = await asyncio.gather(self.get(owner, baseline_id), self.get(owner, candidate_id))
        if any(body["status"] in ("queued", "running", "cancelling") for body in (baseline, candidate)):
            raise AgentRunError(409, "Wait until both experiments finish before comparing")
        return compare_experiments(baseline, candidate, baseline_owner=owner, candidate_owner=owner)

    def handler(self, action: str):
        async def handle(payload, context):
            owner = context.principal.user_id
            if action in ("experiment_start", "experiment_cancel", "dataset_import") and context.interaction != "session":
                raise AgentRunError(403, "An authenticated browser session is required")
            allowed = {
                "dataset_get": {"id", "revision"},
                "dataset_list": set(),
                "dataset_import": {"dataset"},
                "experiment_compare": {"baseline_id", "candidate_id"},
                "experiment_start": {"mode", "idempotency_key", "offline_scenario", "dataset_id", "dataset_revision"},
                "experiment_list": set(),
                "experiment_get": {"id"},
                "experiment_cancel": {"id"},
                "report_export": {"id"},
            }
            if action not in allowed or set(payload) - allowed[action]:
                raise AgentRunError(422, "Unexpected action or fields")
            if action == "dataset_get":
                return await self.get_dataset(owner, payload.get("id"), payload.get("revision"))
            if action == "dataset_list":
                items = await asyncio.to_thread(self.store.list_datasets, owner)
                return {
                    "items": [
                        {
                            "id": self.dataset["id"],
                            "name": self.dataset["name"],
                            "revision": self.dataset["revision"],
                            "hash": dataset_hash(self.dataset),
                            "builtin": True,
                        },
                        *items,
                    ]
                }
            if action == "dataset_import":
                return await self.import_dataset(owner, payload.get("dataset"))
            if action == "experiment_compare":
                identifiers = [payload.get("baseline_id"), payload.get("candidate_id")]
                if any(not isinstance(value, str) or not 1 <= len(value) <= 64 for value in identifiers):
                    raise AgentRunError(422, "Invalid experiment ID")
                return await self.compare(owner, *identifiers)
            if action == "experiment_start":
                return await self.begin(
                    owner,
                    mode=payload.get("mode", "offline"),
                    idempotency_key=payload.get("idempotency_key", ""),
                    runs=context.agent_runs,
                    offline_scenario=payload.get("offline_scenario", "correct"),
                    dataset_id=payload.get("dataset_id"),
                    dataset_revision=payload.get("dataset_revision"),
                )
            if action == "experiment_list":
                return await self.list(owner)
            identifier = payload.get("id")
            if not isinstance(identifier, str) or not 1 <= len(identifier) <= 64:
                raise AgentRunError(422, "Invalid experiment ID")
            if action == "experiment_get":
                return await self.get(owner, identifier)
            if action == "experiment_cancel":
                return await self.cancel(owner, identifier)
            return await self.report(owner, identifier)

        return handle
