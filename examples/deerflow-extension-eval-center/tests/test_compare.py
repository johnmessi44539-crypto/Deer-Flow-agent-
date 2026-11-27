"""Case-aligned comparisons, scope isolation and honest unknown metrics."""

from __future__ import annotations

import copy
import json

import pytest

from deerflow_extension_eval_center.compare import compare_experiments

DATASET_HASH = "a" * 64


def _experiment(identifier, items, **extra):
    return {"id": identifier, "dataset_hash": DATASET_HASH, "mode": "agent", "status": "completed", "items": items, **extra}


def _item(identity, verdict="pass", latency_ms=100, **extra):
    return {"case_id": identity, "title": identity, "verdict": verdict, "latency_ms": latency_ms, "status": "completed", **extra}


def _compare(baseline, candidate, **kwargs):
    return compare_experiments(baseline, candidate, baseline_owner="alice", candidate_owner="alice", **kwargs)


def test_comparison_aligns_by_case_id_not_list_position():
    before = _experiment("baseline", [_item("same"), _item("improved", "fail"), _item("regressed"), _item("unknown")])
    after = _experiment(
        "candidate",
        [_item("regressed", "fail", 130), _item("unknown", None), _item("same", "pass", 80), _item("improved", "pass", 90)],
    )
    result = _compare(before, after)
    assert result["compatible"] is True
    assert result["is_model_comparison"] is True
    assert result["summary"] == {"improved": 1, "regressed": 1, "unchanged": 1, "unscored": 1}
    by_id = {item["case_id"]: item for item in result["items"]}
    assert by_id["same"]["latency_delta_ms"] == -20
    assert by_id["regressed"]["latency_delta_ms"] == 30
    assert by_id["improved"]["change"] == "improved"
    assert by_id["regressed"]["change"] == "regressed"
    assert by_id["unknown"]["candidate_verdict"] is None


def test_two_failed_answers_are_unchanged_not_improved():
    result = _compare(_experiment("a", [_item("case", "fail")]), _experiment("b", [_item("case", "fail")]))
    assert result["items"][0]["change"] == "unchanged"


def test_run_success_does_not_replace_a_failed_task_verdict():
    result = _compare(
        _experiment("a", [_item("case", "pass", run_status="success")]),
        _experiment("b", [_item("case", "fail", run_status="success")]),
    )
    assert result["items"][0]["change"] == "regressed"


@pytest.mark.parametrize("field", ["error", "grader_error"])
def test_execution_or_grader_errors_are_unscored_even_if_a_stale_pass_is_present(field):
    result = _compare(
        _experiment("a", [_item("case")]),
        _experiment("b", [_item("case", **{field: "failure"})]),
    )
    assert result["summary"]["unscored"] == 1
    assert result["items"][0]["candidate_verdict"] is None


def test_normal_answer_format_failure_remains_a_failed_case():
    result = _compare(
        _experiment("a", [_item("case")]),
        _experiment("b", [_item("case", "fail", grading_error="invalid JSON")]),
    )
    assert result["items"][0]["change"] == "regressed"


def test_declared_case_order_is_stable_and_missing_trial_rows_are_unscored():
    dataset = {"cases": [{"id": "case-a"}, {"id": "case-b"}, {"id": "case-c"}]}
    result = _compare(
        _experiment("a", [_item("case-b"), _item("case-a")], dataset=dataset),
        _experiment("b", [_item("case-b")], dataset=dataset),
    )
    assert [item["case_id"] for item in result["items"]] == ["case-a", "case-b", "case-c"]
    assert result["summary"] == {"improved": 0, "regressed": 0, "unchanged": 1, "unscored": 2}
    assert result["is_model_comparison"] is False
    assert "coverage" in result["notice"]


@pytest.mark.parametrize("side", ["baseline", "candidate"])
def test_duplicate_case_ids_are_rejected_instead_of_overwriting_a_result(side):
    baseline = _experiment("a", [_item("case")])
    candidate = _experiment("b", [_item("case")])
    (baseline if side == "baseline" else candidate)["items"].append(_item("case", "fail"))
    with pytest.raises(ValueError, match="Duplicate comparison case_id"):
        _compare(baseline, candidate)


def test_duplicate_declared_dataset_case_ids_are_rejected():
    baseline = _experiment("a", [_item("case")], dataset={"cases": [{"id": "case"}, {"id": "case"}]})
    with pytest.raises(ValueError, match="Duplicate stored dataset"):
        _compare(baseline, _experiment("b", [_item("case")]))


def test_different_dataset_versions_return_an_incompatible_result_without_scores():
    result = _compare(
        _experiment("a", [_item("case")]),
        _experiment("b", [_item("case", "fail")], dataset_hash="b" * 64),
    )
    assert result["compatible"] is False
    assert result["dataset_hash"] is None
    assert "hashes differ" in result["reason"]
    assert result["items"] == []
    assert all(value == 0 for value in result["summary"].values())
    assert result["is_model_comparison"] is False


@pytest.mark.parametrize("value", [None, "", "hash", True, "g" * 64])
def test_missing_or_invalid_hash_cannot_accidentally_make_two_experiments_compatible(value):
    with pytest.raises(ValueError, match="SHA-256"):
        _compare(_experiment("a", [], dataset_hash=value), _experiment("b", [], dataset_hash=value))


@pytest.mark.parametrize("owner", ["bob", "", None, 0])
def test_different_or_invalid_owner_scopes_are_rejected(owner):
    with pytest.raises(PermissionError, match="same authenticated owner"):
        compare_experiments(_experiment("a", []), _experiment("b", []), baseline_owner="alice", candidate_owner=owner)


def test_body_owner_fields_do_not_override_the_services_trusted_scope():
    with pytest.raises(PermissionError):
        compare_experiments(
            _experiment("a", [], owner="alice"),
            _experiment("b", [], owner="alice"),
            baseline_owner="alice",
            candidate_owner="bob",
        )


def test_changed_offline_fixture_scenarios_are_comparable_but_not_model_evaluation():
    result = _compare(
        _experiment("a", [_item("case")], mode="offline", scenario="correct"),
        _experiment("b", [_item("case", "fail")], mode="offline", scenario="wrong_total"),
    )
    assert result["compatible"] is True
    assert result["summary"]["regressed"] == 1
    assert result["baseline_scenario"] == "correct"
    assert result["candidate_scenario"] == "wrong_total"
    assert result["is_model_comparison"] is False
    assert "not model capability" in result["notice"]
    assert "negative-control" in result["notice"]


def test_mixed_modes_have_an_explicit_notice():
    result = _compare(_experiment("a", [_item("case")], mode="offline"), _experiment("b", [_item("case")]))
    assert result["compatible"] is True
    assert result["is_model_comparison"] is False
    assert "different execution modes" in result["notice"]


def test_persisted_offline_scenario_field_is_used_for_negative_control_labels():
    result = _compare(
        _experiment("a", [_item("case")], mode="offline", offline_scenario="correct"),
        _experiment("b", [_item("case", "fail")], mode="offline", offline_scenario="wrong_total"),
    )
    assert result["baseline_scenario"] == "correct"
    assert result["candidate_scenario"] == "wrong_total"
    assert "negative-control" in result["notice"]


@pytest.mark.parametrize("status", ["queued", "running", "cancelling"])
def test_in_progress_experiments_are_not_compared_as_final_results(status):
    result = _compare(_experiment("a", [_item("case")]), _experiment("b", [_item("case")], status=status))
    assert result["compatible"] is False
    assert result["items"] == []
    assert "finished" in result["reason"]


@pytest.mark.parametrize("status", ["failed", "cancelled", "needs_reconciliation"])
def test_terminal_uncertain_outcomes_can_be_inspected_but_are_labelled_incomplete(status):
    result = _compare(_experiment("a", [_item("case")]), _experiment("b", [_item("case", None)], status=status))
    assert result["compatible"] is True
    assert result["is_model_comparison"] is False
    assert result["summary"]["unscored"] == 1
    assert "incomplete" in result["notice"]


def test_minimal_pre_authorized_samples_may_omit_experiment_and_item_status():
    before = {"id": "a", "dataset_hash": DATASET_HASH, "items": [{"case_id": "case", "verdict": "fail"}]}
    after = {"id": "b", "dataset_hash": DATASET_HASH, "items": [{"case_id": "case", "verdict": "pass"}]}
    assert _compare(before, after)["summary"]["improved"] == 1


@pytest.mark.parametrize("latency", [None, True, "100", -1, float("nan"), float("inf"), 10**1000])
def test_missing_or_invalid_latency_stays_unknown(latency):
    result = _compare(_experiment("a", [_item("case")]), _experiment("b", [_item("case", latency_ms=latency)]))
    item = result["items"][0]
    assert item["candidate_latency_ms"] is None
    assert item["latency_delta_ms"] is None
    json.dumps(result, allow_nan=False)


def test_comparison_does_not_mutate_snapshots_or_fabricate_token_cost_metrics():
    before = _experiment("a", [_item("case", latency_ms=10.12)], token_usage=None, cost_estimate=None)
    after = _experiment("b", [_item("case", latency_ms=8.01)], token_usage=None, cost_estimate=None)
    saved = copy.deepcopy((before, after))
    result = _compare(before, after)
    assert (before, after) == saved
    assert result["items"][0]["latency_delta_ms"] == -2.11
    assert "token_usage_delta" not in result
    assert "cost_estimate_delta" not in result
    json.dumps(result, allow_nan=False)
