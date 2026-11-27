"""Behavioral checks for the actual deterministic grading implementation."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from deerflow_extension_eval_center.cases import load_dataset
from deerflow_extension_eval_center.graders import MAX_JSON_DEPTH, MAX_OUTPUT_BYTES, grade_output


@pytest.mark.parametrize("index", [0, 1, 2])
def test_each_published_reference_answer_passes(index):
    case = load_dataset()["cases"][index]
    result = grade_output(json.dumps(case["expected"], ensure_ascii=False), case["expected"], case["grader"])
    assert result["verdict"] == "pass"
    assert result["error"] is None
    assert all(check["passed"] for check in result["checks"])


def test_key_order_whitespace_and_equivalent_decimal_numbers_are_accepted():
    expected = {"month_totals": {"2026-01": 300, "2026-02": 80}, "grand_total": 380}
    answer = ' \n{ "grand_total": 380.0, "month_totals": {"2026-02": 8e1, "2026-01": 300} }\n'
    assert grade_output(answer, expected)["verdict"] == "pass"


@pytest.mark.parametrize(
    "answer",
    [
        '{"grand_total":379,"month_totals":{"2026-01":300,"2026-02":80}}',
        '{"grand_total":380,"month_totals":{"2026-01":301,"2026-02":80}}',
        '{"grand_total":"380","month_totals":{"2026-01":300,"2026-02":80}}',
        '{"grand_total":380}',
        '{"grand_total":380,"month_totals":{"2026-01":300,"2026-02":80},"extra":true}',
        '{"grand_total":380,"month_totals":{"2026-01":300,"2026-02":80,"2026-03":0}}',
    ],
)
def test_incorrect_answers_fail_specific_checks(answer):
    expected = load_dataset()["cases"][0]["expected"]
    result = grade_output(answer, expected)
    assert result["verdict"] == "fail"
    assert result["error"] is None
    assert any(not check["passed"] for check in result["checks"])


@pytest.mark.parametrize(
    "answer",
    [
        "",
        "the answer is 1",
        '```json\n{"amount":1}\n```',
        'Here is the result: {"amount":1}',
        '{"amount":1} thanks',
        '{"amount":1}{"amount":1}',
        "[]",
        "null",
        "true",
        '"object"',
        '{"amount":1,}',
        '{"amount":NaN}',
        '{"amount":Infinity}',
        '{"amount":-Infinity}',
        '{"amount":0,"amount":1}',
        '{"amount":{"nested":0,"nested":1}}',
        '{"amount":1e99999999999999999999999999999999999}',
        "\ud800",
        None,
        {"amount": 1},
    ],
)
def test_non_json_or_ambiguous_model_answers_fail_without_raising(answer):
    result = grade_output(answer, {"amount": 1})
    assert result["verdict"] == "fail"
    assert result["error"]
    assert result["checks"][0]["name"] == "valid_json_object"
    assert result["checks"][0]["passed"] is False


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ('{"value":true}', {"value": 1}),
        ('{"value":false}', {"value": 0}),
        ('{"value":1}', {"value": True}),
        ('{"value":0.0}', {"value": False}),
        ('{"value":null}', {"value": False}),
    ],
)
def test_booleans_are_not_treated_as_numbers(answer, expected):
    assert grade_output(answer, expected)["verdict"] == "fail"


def test_high_precision_decimal_is_not_rounded_into_a_false_pass():
    result = grade_output('{"amount":100.000000000000000000000000001}', {"amount": 100})
    assert result["verdict"] == "fail"
    assert result["checks"][-1]["actual"] == "100.000000000000000000000000001"
    json.dumps(result, allow_nan=False)


def test_large_valid_json_exponent_is_a_wrong_answer_and_report_stays_json_safe():
    result = grade_output('{"amount":1e9999}', {"amount": 1})
    assert result["verdict"] == "fail"
    assert result["error"] is None
    json.dumps(result, allow_nan=False)


def test_nested_objects_arrays_null_and_strings_are_compared_structurally():
    expected = {"records": [{"id": "A", "value": None}, {"id": "B", "value": True}]}
    assert grade_output(json.dumps(expected), expected)["verdict"] == "pass"
    wrong = {"records": [{"id": "B", "value": True}, {"id": "A", "value": None}]}
    assert grade_output(json.dumps(wrong), expected)["verdict"] == "fail"


@pytest.mark.parametrize("answer", ['{"items":[]}', '{"items":[1,2]}', '{"items":{}}'])
def test_array_length_and_type_must_match(answer):
    assert grade_output(answer, {"items": [1]})["verdict"] == "fail"


def test_check_paths_use_escaped_json_pointer_segments():
    result = grade_output('{"a/b~c":2}', {"a/b~c": 1})
    assert result["checks"][-1]["name"] == "$/a~1b~0c"


def test_oversize_answer_is_rejected_before_parsing():
    result = grade_output('{"amount":"' + "x" * MAX_OUTPUT_BYTES + '"}', {"amount": 1})
    assert result["verdict"] == "fail"
    assert "size limit" in result["error"]


@pytest.mark.parametrize("grader", ["python", "unknown", None, []])
def test_unknown_grader_is_a_configuration_error(grader):
    with pytest.raises(ValueError, match="unsupported grader"):
        grade_output("{}", {}, grader)


@pytest.mark.parametrize(
    "expected", [None, [], {"x": float("nan")}, {"x": float("inf")}, {"x": Decimal("1.2")}, {1: "bad key"}, {"x": object()}]
)
def test_invalid_reference_is_a_configuration_error(expected):
    with pytest.raises(ValueError, match="expected"):
        grade_output("{}", expected)


def test_deep_reference_is_rejected_instead_of_exhausting_recursion():
    expected = {}
    node = expected
    for _ in range(MAX_JSON_DEPTH + 1):
        node["nested"] = {}
        node = node["nested"]
    with pytest.raises(ValueError, match="complexity"):
        grade_output("{}", expected)


def test_grading_never_mutates_the_expected_answer():
    expected = {"items": [{"amount": 1}]}
    before = json.dumps(expected, sort_keys=True)
    grade_output('{"items":[{"amount":2}]}', expected)
    assert json.dumps(expected, sort_keys=True) == before
