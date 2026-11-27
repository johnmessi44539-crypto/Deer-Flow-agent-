"""Pure comparisons of two independently owner-authorized experiment snapshots.

The service must load both records through its owner-scoped store before calling
this module. Owner arguments are trusted scope metadata, never browser payloads.
No inference, file access, grading rerun or provider-cost estimation happens here.
"""

from __future__ import annotations

import math
import re
from typing import Any

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_FINISHED = frozenset({"completed", "failed", "cancelled", "needs_reconciliation"})


def _text(value: Any, fallback: str = "unknown") -> str:
    return value if isinstance(value, str) and value else fallback


def _items(experiment: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = experiment.get("items")
    if type(raw) is not list:
        raise ValueError("Experiment items must be a list")
    indexed: dict[str, dict[str, Any]] = {}
    for item in raw:
        if type(item) is not dict or type(item.get("case_id")) is not str or not item["case_id"]:
            raise ValueError("Each comparison item must have a non-empty case_id")
        identity = item["case_id"]
        if identity in indexed:
            raise ValueError(f"Duplicate comparison case_id: {identity}")
        indexed[identity] = item
    return indexed


def _verdict(item: dict[str, Any] | None) -> str | None:
    if item is None:
        return None
    if item.get("status", "completed") != "completed" or item.get("error") or item.get("grader_error"):
        return None
    verdict = item.get("verdict")
    return verdict if verdict in ("pass", "fail") else None


def _latency(item: dict[str, Any] | None) -> float | None:
    if item is None:
        return None
    value = item.get("latency_ms")
    if type(value) not in (int, float):
        return None
    try:
        return round(float(value), 2) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _case_order(*experiments: dict[str, Any]) -> list[str]:
    """Use declared dataset order, then any remaining recorded case identities."""
    ordered: dict[str, None] = {}
    for experiment in experiments:
        dataset = experiment.get("dataset")
        if type(dataset) is dict and type(dataset.get("cases")) is list:
            seen: set[str] = set()
            for case in dataset["cases"]:
                if type(case) is not dict or type(case.get("id")) is not str or not case["id"]:
                    raise ValueError("Stored dataset cases must have non-empty IDs")
                if case["id"] in seen:
                    raise ValueError(f"Duplicate stored dataset case ID: {case['id']}")
                seen.add(case["id"])
                ordered.setdefault(case["id"], None)
    for experiment in experiments:
        for identity in _items(experiment):
            ordered.setdefault(identity, None)
    return list(ordered)


def compare_experiments(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    baseline_owner: str,
    candidate_owner: str,
) -> dict[str, Any]:
    """Align cases by ID and report changes using candidate minus baseline.

    Same dataset SHA-256 is mandatory. Changed fixture scenarios remain useful
    for negative controls, but offline or mixed-mode comparisons explicitly do
    not represent model-capability evaluation. Missing grades/latencies remain
    unknown. This function deliberately computes no token or cost deltas.
    """
    if (
        type(baseline_owner) is not str
        or not baseline_owner.strip()
        or type(candidate_owner) is not str
        or baseline_owner != candidate_owner
    ):
        raise PermissionError("Both experiments must belong to the same authenticated owner")
    if type(baseline) is not dict or type(candidate) is not dict:
        raise ValueError("Comparison requires two experiment objects")
    for experiment in (baseline, candidate):
        if type(experiment.get("id")) is not str or not experiment["id"]:
            raise ValueError("Comparison requires experiment IDs")
        value = experiment.get("dataset_hash")
        if type(value) is not str or _HASH_PATTERN.fullmatch(value) is None:
            raise ValueError("Comparison requires each dataset's complete SHA-256")

    baseline_mode = _text(baseline.get("mode"))
    candidate_mode = _text(candidate.get("mode"))
    result: dict[str, Any] = {
        "baseline_id": baseline["id"],
        "candidate_id": candidate["id"],
        "dataset_hash": baseline["dataset_hash"] if baseline["dataset_hash"] == candidate["dataset_hash"] else None,
        "baseline_dataset_hash": baseline["dataset_hash"],
        "candidate_dataset_hash": candidate["dataset_hash"],
        "baseline_mode": baseline_mode,
        "candidate_mode": candidate_mode,
        "baseline_scenario": _text(baseline.get("offline_scenario", baseline.get("scenario")), "unspecified"),
        "candidate_scenario": _text(candidate.get("offline_scenario", candidate.get("scenario")), "unspecified"),
        "compatible": False,
        "reason": None,
        "is_model_comparison": False,
        "notice": "",
        "summary": {"improved": 0, "regressed": 0, "unchanged": 0, "unscored": 0},
        "items": [],
    }
    notices = []
    if baseline_mode != "agent" or candidate_mode != "agent":
        notices.append("Offline or mixed-mode results describe fixture/grader behavior, not model capability.")
    if baseline_mode != candidate_mode:
        notices.append("The experiments used different execution modes.")
    if result["baseline_scenario"] != result["candidate_scenario"]:
        notices.append("Fixture scenarios differ; this can be used as a negative-control comparison.")
    result["notice"] = " ".join(notices)
    if result["dataset_hash"] is None:
        result["reason"] = "Dataset hashes differ; select the same immutable dataset version."
        return result
    if any(experiment.get("status") not in _FINISHED for experiment in (baseline, candidate) if "status" in experiment):
        result["reason"] = "Wait until both experiments have finished before comparing them."
        return result

    baseline_items, candidate_items = _items(baseline), _items(candidate)
    case_order = _case_order(baseline, candidate)
    result["compatible"] = True
    for identity in case_order:
        before, after = baseline_items.get(identity), candidate_items.get(identity)
        before_verdict, after_verdict = _verdict(before), _verdict(after)
        if before_verdict is None or after_verdict is None:
            change = "unscored"
        elif before_verdict == after_verdict:
            change = "unchanged"
        else:
            change = "improved" if after_verdict == "pass" else "regressed"
        before_latency, after_latency = _latency(before), _latency(after)
        delta = None
        if before_latency is not None and after_latency is not None:
            difference = after_latency - before_latency
            if math.isfinite(difference):
                delta = round(difference, 2)
        result["items"].append(
            {
                "case_id": identity,
                "title": _text((after or before or {}).get("title"), identity),
                "baseline_verdict": before_verdict,
                "candidate_verdict": after_verdict,
                "change": change,
                "baseline_latency_ms": before_latency,
                "candidate_latency_ms": after_latency,
                "latency_delta_ms": delta,
            }
        )
        result["summary"][change] += 1
    complete = all(experiment.get("status", "completed") == "completed" for experiment in (baseline, candidate))
    result["is_model_comparison"] = baseline_mode == candidate_mode == "agent" and complete
    if not complete:
        notices.append("Some execution outcomes are incomplete or require reconciliation; unscored cases are not passes.")
    if set(baseline_items) != set(case_order) or set(candidate_items) != set(case_order):
        notices.append("Case coverage differs; unmatched cases remain unscored.")
        result["is_model_comparison"] = False
    result["notice"] = " ".join(notices)
    return result
