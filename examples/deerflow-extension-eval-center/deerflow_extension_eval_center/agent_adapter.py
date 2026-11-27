"""Execute one isolated trial through the host's public AgentRuns capability."""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from deerflow_extension_api import AgentRunError, AgentRuns

ACTIVE_STATUSES = frozenset({"pending", "running"})
WAITING_STATUSES = frozenset({"waiting_for_input", "waiting_input"})
MAX_OUTPUT_BYTES = 64 * 1024


class TrialOutputError(ValueError):
    """A deliberately public, locally authored validation message."""


def execution_identity(experiment_id: str, case_id: str, trial_index: int = 0) -> tuple[str, str, str]:
    """Return stable thread, idempotency and human-message identities."""
    if not isinstance(experiment_id, str) or not experiment_id or not isinstance(case_id, str) or not case_id:
        raise ValueError("Experiment and case IDs are required")
    if type(trial_index) is not int or trial_index < 0:
        raise ValueError("trial_index must be a non-negative integer")
    # Length-delimited identities avoid ambiguous concatenations containing ':'.
    identity = f"deerflow-eval:{len(experiment_id)}:{experiment_id}:{len(case_id)}:{case_id}:{trial_index}"
    stable_id = uuid5(NAMESPACE_URL, identity)
    return str(stable_id), f"eval:{stable_id.hex}", f"eval-human-{stable_id.hex}"


def _kind(message: Mapping[str, Any]) -> str | None:
    return message.get("type") or message.get("role")


def _extra(message: Mapping[str, Any]) -> Mapping[str, Any]:
    value = message.get("additional_kwargs")
    return value if isinstance(value, Mapping) else {}


def _pending_question(messages: list[dict[str, Any]]) -> bool:
    # Match the host's trailing unanswered tool-request semantics, without
    # importing host-private message helpers into an independent extension.
    answered = set()
    for message in messages:
        response = _extra(message).get("human_input_response")
        if isinstance(response, Mapping) and response.get("request_id"):
            answered.add(response["request_id"])
    for message in reversed(messages):
        if _kind(message) != "tool":
            return False
        artifact = message.get("artifact")
        request = artifact.get("human_input") if isinstance(artifact, Mapping) else None
        if isinstance(request, Mapping) and request.get("kind") == "human_input_request":
            request_id = request.get("request_id") or message.get("id")
            return request_id not in answered
    return False


def _answer(messages: list[dict[str, Any]], message_id: str, run_id: str) -> str:
    marker = next(
        (
            i
            for i, message in enumerate(messages)
            if _kind(message) in {"human", "user"}
            and not _extra(message).get("hide_from_ui")
            and (message.get("id") in {message_id, f"{message_id}__user"} or _extra(message).get("run_id") == run_id)
        ),
        None,
    )
    if marker is None:
        raise TrialOutputError("Cannot associate thread output with this trial")
    tail = messages[marker + 1 :]
    if any(_kind(message) in {"human", "user"} for message in tail):
        raise TrialOutputError("Thread advanced before trial output was captured")
    answers = [
        message
        for message in tail
        if _kind(message) in {"ai", "assistant"}
        and not message.get("tool_calls")
        and not _extra(message).get("tool_calls")
        and not _extra(message).get("hide_from_ui")
    ]
    if not answers:
        raise TrialOutputError("Agent completed without a final answer")
    content = answers[-1].get("content")
    if isinstance(content, list):
        content = "".join(
            block["text"]
            for block in content
            if isinstance(block, Mapping) and block.get("type") in {"text", "output_text"} and isinstance(block.get("text"), str)
        )
    if not isinstance(content, str) or not content.strip():
        raise TrialOutputError("Agent completed without a text answer")
    if len(content.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise TrialOutputError("Agent answer exceeds the trial output limit")
    return content


class HostAgentAdapter:
    def __init__(self, runs: AgentRuns, *, timeout_seconds: float = 120, assistant_id: str = "lead_agent"):
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3600
        ):
            raise ValueError("timeout_seconds must be between 0 and 3600")
        if not isinstance(assistant_id, str) or not assistant_id:
            raise ValueError("assistant_id is required")
        self.runs = runs
        self.timeout_seconds = timeout_seconds
        self.assistant_id = assistant_id

    async def _stop(self, thread_id: str, run_id: str) -> tuple[str, bool]:
        """Cancellation acknowledgement alone does not establish termination."""
        try:
            await asyncio.wait_for(self.runs.cancel(thread_id=thread_id, run_id=run_id), timeout=5)
        except Exception:
            # A run can complete concurrently with cancellation, so observe
            # its status even if the cancellation request was rejected.
            pass
        try:
            final = await asyncio.wait_for(self.runs.wait(thread_id=thread_id, run_id=run_id, timeout=5), timeout=6)
            status = final.status
            return status, status in ACTIVE_STATUSES
        except Exception:
            return "needs_reconciliation", True

    async def execute(
        self,
        *,
        experiment_id: str,
        case: dict[str, Any],
        trial_index: int = 0,
        on_run: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        thread_id, request_key, message_id = execution_identity(experiment_id, case.get("id"), trial_index)
        prompt = case.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Case prompt is required")
        result = {
            "thread_id": thread_id,
            "run_id": None,
            "output": "",
            "configuration_evidence": None,
            "evidence_error": None,
            "error": None,
            "blocked": False,
            "run_status": "pending",
            "needs_reconciliation": False,
        }
        phase = "creating_thread"
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self.runs.create_thread(assistant_id=self.assistant_id, thread_id=thread_id, metadata={"title": f"Eval {case['id']}"})
                phase = "admitting"
                # Expected answers are deliberately excluded from model input.
                run = await self.runs.start(
                    thread_id=thread_id,
                    input={"messages": [{"type": "human", "id": message_id, "content": prompt}]},
                    idempotency_key=request_key,
                )
                result.update(run_id=run.run_id, run_status=run.status)
                phase = "observing"
                if on_run is not None:
                    await on_run(thread_id, run.run_id)
                final = await self.runs.wait(thread_id=thread_id, run_id=run.run_id, timeout=self.timeout_seconds)
                result["run_status"] = final.status
                if final.status in ACTIVE_STATUSES:
                    raise TimeoutError("Host wait returned an active run")
                state = await self.runs.get_state(thread_id=thread_id)
                values = state.get("values")
                messages = values.get("messages", []) if isinstance(values, Mapping) else []
                if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
                    raise TrialOutputError("Host returned invalid thread messages")
                if final.status in WAITING_STATUSES or state.get("next") or state.get("interrupts") or _pending_question(messages):
                    result.update(blocked=True, error="Agent requires human input or approval")
                    return result
                if final.status != "success":
                    result["error"] = "Agent run did not succeed"
                    return result
                result["output"] = _answer(messages, message_id, run.run_id)

            # A missing capability must not be replaced by mutable configuration.
            # The caller can grade this answer but must display evidence_error.
            try:
                proof = await asyncio.wait_for(self.runs.configuration_evidence(thread_id=thread_id, run_id=run.run_id), timeout=5)
                if (
                    not isinstance(proof, dict)
                    or proof.get("thread_id") != thread_id
                    or proof.get("run_id") != run.run_id
                    or proof.get("source") != "host_assembly"
                ):
                    raise ValueError("Invalid host configuration evidence")
                result["configuration_evidence"] = copy.deepcopy(proof)
            except Exception:
                result["evidence_error"] = "Host configuration evidence is unavailable or does not match this trial"
            return result
        except asyncio.CancelledError:
            if result["run_id"]:
                await self._stop(thread_id, result["run_id"])
            raise
        except TimeoutError:
            result["error"] = "Agent trial timed out"
            if result["run_id"]:
                status, uncertain = await self._stop(thread_id, result["run_id"])
                result.update(run_status=status, needs_reconciliation=uncertain)
            else:
                result.update(run_status="needs_reconciliation", needs_reconciliation=True)
            return result
        except AgentRunError as exc:
            result["error"] = f"Host rejected trial operation ({exc.status_code})"
            if phase == "admitting" and exc.status_code not in {400, 403, 404, 409, 412, 422}:
                result.update(run_status="needs_reconciliation", needs_reconciliation=True)
            elif phase == "observing" and result["run_status"] in ACTIVE_STATUSES:
                status, uncertain = await self._stop(thread_id, result["run_id"])
                result.update(run_status=status, needs_reconciliation=uncertain)
            else:
                result["run_status"] = "error"
            return result
        except Exception as exc:
            result["error"] = str(exc) if isinstance(exc, TrialOutputError) else "Agent trial operation failed"
            if phase in {"creating_thread", "admitting"}:
                result.update(run_status="needs_reconciliation", needs_reconciliation=True)
            elif result["run_status"] in ACTIVE_STATUSES:
                status, uncertain = await self._stop(thread_id, result["run_id"])
                result.update(run_status=status, needs_reconciliation=uncertain)
            return result
