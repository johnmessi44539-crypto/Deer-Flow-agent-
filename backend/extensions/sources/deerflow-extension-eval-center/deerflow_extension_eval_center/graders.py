"""Deterministic graders for persisted Agent answers.

Only data is evaluated: no case-supplied Python, expressions or shell commands
are executed. Invalid model answers fail the case; invalid grading configuration
raises ValueError so the runner can report a separate grader error.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal, DecimalException
from typing import Any

SUPPORTED_GRADERS = frozenset({"json_exact_v1"})
MAX_OUTPUT_BYTES = 64 * 1024
MAX_JSON_DEPTH = 20
MAX_JSON_NODES = 5000


def validate_expected(expected: Any) -> dict[str, Any]:
    """Return a detached, bounded JSON object or reject invalid configuration."""
    if type(expected) is not dict:
        raise ValueError("expected must be a JSON object")
    count = 0

    def visit(value: Any, depth: int) -> None:
        nonlocal count
        count += 1
        if count > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError("expected exceeds the JSON complexity limit")
        if type(value) is dict:
            for key, child in value.items():
                if type(key) is not str:
                    raise ValueError("expected object keys must be strings")
                visit(child, depth + 1)
        elif type(value) is list:
            for child in value:
                visit(child, depth + 1)
        elif type(value) is float:
            if not math.isfinite(value):
                raise ValueError("expected numbers must be finite")
        elif value is not None and type(value) not in (str, int, bool):
            raise ValueError("expected must contain JSON values only")

    visit(expected, 0)
    try:
        encoded = json.dumps(expected, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("expected must contain valid UTF-8 JSON") from exc
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ValueError("expected exceeds the JSON size limit")
    return json.loads(encoded)


def _object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def _report_value(value: Any) -> Any:
    """Keep checks JSON serializable without losing high-precision diagnostics."""
    if isinstance(value, Decimal):
        converted = float(value)
        if math.isfinite(converted) and Decimal(str(converted)) == value:
            return converted
        return str(value)
    if type(value) is dict:
        return {key: _report_value(child) for key, child in value.items()}
    if type(value) is list:
        return [_report_value(child) for child in value]
    return value


def _check(name: str, passed: bool, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "name": name,
        "passed": passed,
        "expected": _report_value(expected),
        "actual": _report_value(actual),
    }


def _compare(expected: Any, actual: Any, path: str, checks: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        checks.append(_check(path, False, "bounded JSON depth", "depth limit exceeded"))
        return
    if type(expected) is dict:
        if type(actual) is not dict:
            checks.append(_check(path, False, "object", type(actual).__name__))
            return
        checks.append(_check(f"{path} keys", set(expected) == set(actual), sorted(expected), sorted(actual)))
        for key, expected_value in expected.items():
            if key in actual:
                escaped_key = key.replace("~", "~0").replace("/", "~1")
                _compare(expected_value, actual[key], f"{path}/{escaped_key}", checks, depth + 1)
        return
    if type(expected) is list:
        if type(actual) is not list:
            checks.append(_check(path, False, "array", type(actual).__name__))
            return
        checks.append(_check(f"{path} length", len(expected) == len(actual), len(expected), len(actual)))
        for index, (expected_value, actual_value) in enumerate(zip(expected, actual, strict=False)):
            _compare(expected_value, actual_value, f"{path}/{index}", checks, depth + 1)
        return
    if type(expected) is bool or type(actual) is bool:
        equal = type(expected) is type(actual) and expected == actual
    elif type(expected) in (int, float) and type(actual) in (int, float, Decimal):
        equal = Decimal(str(expected)) == Decimal(str(actual))
    else:
        equal = type(expected) is type(actual) and expected == actual
    checks.append(_check(path, equal, expected, actual))


def grade_output(output: str, expected: dict[str, Any], grader: str = "json_exact_v1") -> dict[str, Any]:
    """Grade one complete JSON object; formatting and object-key order may vary.

    Integer and equivalent decimal representations compare numerically. Booleans
    are never numbers. Markdown fences, prose, duplicate keys and JSON extensions
    such as NaN are rejected instead of being repaired before grading.
    """
    if type(grader) is not str or grader not in SUPPORTED_GRADERS:
        raise ValueError(f"unsupported grader: {grader}")
    expected = validate_expected(expected)
    error: str | None = None
    if type(output) is not str:
        error = "answer must be a string containing one JSON object"
    else:
        try:
            if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
                error = "answer exceeds the JSON size limit"
            else:
                actual = json.loads(output, parse_float=Decimal, parse_constant=_reject_constant, object_pairs_hook=_object_from_pairs)
                if type(actual) is not dict:
                    error = "answer must be one JSON object"
        except (ValueError, UnicodeError, RecursionError, DecimalException):
            error = "answer is not a valid single JSON object (duplicate keys and non-finite constants are forbidden)"
    if error is not None:
        return {
            "verdict": "fail",
            "checks": [_check("valid_json_object", False, "one valid JSON object", error)],
            "error": error,
        }
    checks = [_check("valid_json_object", True, "one valid JSON object", "object")]
    _compare(expected, actual, "$", checks)
    return {
        "verdict": "pass" if all(check["passed"] for check in checks) else "fail",
        "checks": checks,
        "error": None,
    }
