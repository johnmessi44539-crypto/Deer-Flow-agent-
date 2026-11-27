"""Validation and immutable identities for the first fixed evaluation dataset."""

from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files
from typing import Any

from .graders import SUPPORTED_GRADERS, validate_expected

MAX_CASES = 20
MAX_PROMPT_CHARS = 12000
_ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_DATASET_FIELDS = {"id", "name", "revision", "cases"}
_CASE_FIELDS = {"id", "title", "prompt", "expected", "grader"}


def _text(value: Any, name: str, maximum: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"{name} must contain valid UTF-8 text") from exc
    return value


def _identifier(value: Any, name: str) -> str:
    value = _text(value, name, 80)
    if _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must contain lowercase words separated by hyphens")
    return value


def validate_dataset(payload: Any) -> dict[str, Any]:
    """Validate declarative data and return a deep copy suitable for persistence.

    Cases declare an allowlisted grader name, never executable code. Unknown
    fields are rejected so command/configuration fields cannot silently acquire
    behavior in a later runner. Prompt bytes are preserved for reproducibility.
    """
    if type(payload) is not dict or set(payload) != _DATASET_FIELDS:
        raise ValueError("dataset must contain exactly id, name, revision and cases")
    dataset_id = _identifier(payload["id"], "dataset id")
    name = _text(payload["name"], "dataset name", 120)
    revision = payload["revision"]
    if type(revision) is not int or not 1 <= revision <= 2**31 - 1:
        raise ValueError("dataset revision must be a positive integer")
    raw_cases = payload["cases"]
    if type(raw_cases) is not list or not 1 <= len(raw_cases) <= MAX_CASES:
        raise ValueError(f"dataset cases must contain between 1 and {MAX_CASES} entries")
    cases: list[dict[str, Any]] = []
    identities: set[str] = set()
    for raw_case in raw_cases:
        if type(raw_case) is not dict or set(raw_case) != _CASE_FIELDS:
            raise ValueError("case must contain exactly id, title, prompt, expected and grader")
        case_id = _identifier(raw_case["id"], "case id")
        if case_id in identities:
            raise ValueError(f"duplicate case id: {case_id}")
        identities.add(case_id)
        grader = raw_case["grader"]
        if type(grader) is not str or grader not in SUPPORTED_GRADERS:
            raise ValueError("case grader must be an allowlisted grader")
        cases.append(
            {
                "id": case_id,
                "title": _text(raw_case["title"], "case title", 120),
                "prompt": _text(raw_case["prompt"], "case prompt", MAX_PROMPT_CHARS),
                "expected": validate_expected(raw_case["expected"]),
                "grader": grader,
            }
        )
    return {"id": dataset_id, "name": name, "revision": revision, "cases": cases}


def load_dataset() -> dict[str, Any]:
    """Read the packaged synthetic fixtures; no network or user data is read."""
    resource = files("deerflow_extension_eval_center").joinpath("data", "fixed-cases-v1.json")
    return validate_dataset(json.loads(resource.read_text(encoding="utf-8")))


def dataset_hash(dataset: dict[str, Any]) -> str:
    """Hash validated data, including expected answers, in canonical key order."""
    validated = validate_dataset(dataset)
    encoded = json.dumps(validated, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
