"""Regression tests for declarative cases and dataset identity."""

from __future__ import annotations

import copy
import json

import pytest

from deerflow_extension_eval_center.cases import MAX_CASES, MAX_PROMPT_CHARS, dataset_hash, load_dataset, validate_dataset


def test_packaged_dataset_contains_the_three_published_cases():
    dataset = load_dataset()
    assert dataset["id"] == "fixed-cases-v1"
    assert dataset["revision"] == 1
    assert [case["id"] for case in dataset["cases"]] == [
        "monthly-sales-001",
        "net-revenue-001",
        "invalid-amount-001",
    ]
    assert dataset["cases"][0]["expected"] == {"month_totals": {"2026-01": 300, "2026-02": 80}, "grand_total": 380}
    assert dataset["cases"][1]["expected"] == {"net_revenue": 230, "fully_refunded_orders": 1}
    assert dataset["cases"][2]["expected"] == {"error_code": "INVALID_AMOUNT", "invalid_row": 2}
    assert all(case["grader"] == "json_exact_v1" for case in dataset["cases"])
    # The actual answers remain separate from the task inputs.
    assert "380" not in dataset["cases"][0]["prompt"]
    assert "230" not in dataset["cases"][1]["prompt"]


def test_load_returns_independent_values_on_each_call():
    first = load_dataset()
    first["cases"][0]["expected"]["month_totals"]["2026-01"] = -1
    assert load_dataset()["cases"][0]["expected"]["month_totals"]["2026-01"] == 300


def test_validation_returns_a_detached_copy_including_nested_expected_values():
    original = load_dataset()
    validated = validate_dataset(original)
    original["cases"][0]["expected"]["month_totals"]["2026-01"] = -1
    original["cases"].pop()
    assert len(validated["cases"]) == 3
    assert validated["cases"][0]["expected"]["month_totals"]["2026-01"] == 300


def test_hash_ignores_object_key_order_and_survives_json_roundtrip():
    original = load_dataset()
    reordered = dict(reversed(list(original.items())))
    reordered["cases"] = [dict(reversed(list(case.items()))) for case in original["cases"]]
    assert dataset_hash(original) == dataset_hash(reordered)
    assert dataset_hash(original) == dataset_hash(json.loads(json.dumps(original)))
    assert len(dataset_hash(original)) == 64


@pytest.mark.parametrize("change", ["expected", "revision", "prompt", "order"])
def test_hash_changes_when_evaluation_meaning_or_order_changes(change):
    original = load_dataset()
    changed = copy.deepcopy(original)
    if change == "expected":
        changed["cases"][0]["expected"]["grand_total"] += 1
    elif change == "revision":
        changed["revision"] += 1
    elif change == "prompt":
        changed["cases"][0]["prompt"] += " "
    else:
        changed["cases"].reverse()
    assert dataset_hash(changed) != dataset_hash(original)


def test_validation_preserves_prompt_bytes_for_reproducibility():
    dataset = load_dataset()
    dataset["cases"][0]["prompt"] = " \n保留输入空白\n "
    assert validate_dataset(dataset)["cases"][0]["prompt"] == " \n保留输入空白\n "


@pytest.mark.parametrize("revision", [True, False, 0, -1, "1", 1.5, None, 2**31])
def test_invalid_dataset_revision_is_rejected(revision):
    dataset = load_dataset()
    dataset["revision"] = revision
    with pytest.raises(ValueError, match="revision"):
        validate_dataset(dataset)


@pytest.mark.parametrize("payload", [None, [], "dataset", {}, {"id": "only-id"}])
def test_missing_dataset_structure_is_rejected(payload):
    with pytest.raises(ValueError, match="dataset"):
        validate_dataset(payload)


@pytest.mark.parametrize("cases", [[], {}, None, "cases"])
def test_missing_or_non_list_cases_are_rejected(cases):
    dataset = load_dataset()
    dataset["cases"] = cases
    with pytest.raises(ValueError, match="cases"):
        validate_dataset(dataset)


def test_case_limit_is_enforced():
    dataset = load_dataset()
    dataset["cases"] = [{**dataset["cases"][0], "id": f"case-{index}"} for index in range(MAX_CASES + 1)]
    with pytest.raises(ValueError, match="cases"):
        validate_dataset(dataset)


def test_duplicate_case_id_is_rejected():
    dataset = load_dataset()
    dataset["cases"][1]["id"] = dataset["cases"][0]["id"]
    with pytest.raises(ValueError, match="duplicate"):
        validate_dataset(dataset)


@pytest.mark.parametrize("identifier", ["../case", "with space", "UPPER", "", "case_1"])
def test_invalid_case_identifiers_are_rejected(identifier):
    dataset = load_dataset()
    dataset["cases"][0]["id"] = identifier
    with pytest.raises(ValueError, match="case id"):
        validate_dataset(dataset)


@pytest.mark.parametrize("value", ["", "   ", 10, None, "x" * (MAX_PROMPT_CHARS + 1), "\ud800"])
def test_invalid_prompt_is_rejected(value):
    dataset = load_dataset()
    dataset["cases"][0]["prompt"] = value
    with pytest.raises(ValueError, match="prompt"):
        validate_dataset(dataset)


@pytest.mark.parametrize("grader", ["eval", "python", "json_exact_v2", [], None])
def test_only_registered_graders_are_accepted(grader):
    dataset = load_dataset()
    dataset["cases"][0]["grader"] = grader
    with pytest.raises(ValueError, match="grader"):
        validate_dataset(dataset)


@pytest.mark.parametrize("location", ["dataset", "case"])
def test_unknown_fields_cannot_introduce_executable_configuration(location):
    dataset = load_dataset()
    target = dataset if location == "dataset" else dataset["cases"][0]
    target["command"] = "arbitrary command"
    with pytest.raises(ValueError, match="exactly"):
        validate_dataset(dataset)


@pytest.mark.parametrize("expected", [[], None, {"amount": float("nan")}, {1: "not a JSON string key"}])
def test_invalid_reference_answers_are_rejected(expected):
    dataset = load_dataset()
    dataset["cases"][0]["expected"] = expected
    with pytest.raises(ValueError, match="expected"):
        validate_dataset(dataset)
