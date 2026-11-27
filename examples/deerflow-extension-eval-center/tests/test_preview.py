"""Drive the local HTTP preview through its real offline evaluation lifecycle."""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient

from deerflow_extension_eval_center.preview import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "preview.sqlite"))
    with TestClient(app, base_url="http://127.0.0.1:2027") as instance:
        yield instance


def _headers(client):
    response = client.get("/")
    assert response.status_code == 200
    token = re.search(r'const csrf = "([A-Za-z0-9_-]+)";', response.text).group(1)
    return {"X-Eval-Preview-CSRF": token, "Origin": "http://127.0.0.1:2027"}


def test_page_serves_same_ui_assets_and_explicit_offline_notice(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "本机离线预览" in response.text
    assert "mountEvaluation" in response.text and "/assets/index.mjs" in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert client.get("/health").json() == {"ready": True, "mode": "offline-preview"}
    assert "mountEvaluation" in client.get("/assets/index.mjs").text
    assert client.get("/assets/styles.css").status_code == 200
    assert client.get("/assets/service.py").status_code == 404
    assert client.get("/assets/%2e%2e%2fservice.py").status_code == 404
    assert client.get("/docs").status_code == 404


@pytest.mark.parametrize("token", ["", "incorrect-token"])
def test_action_rejects_missing_or_invalid_csrf(client, token):
    response = client.post("/actions/dataset_get", json={}, headers={"X-Eval-Preview-CSRF": token})
    assert response.status_code == 403


@pytest.mark.parametrize("host", ["example.com", "localhost:2028", "127.0.0.1.evil:2027"])
def test_non_preview_host_is_denied(client, host):
    assert client.get("/", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost:2027"])
def test_cross_origin_request_is_denied_even_with_valid_token(client, origin):
    headers = {**_headers(client), "Origin": origin}
    assert client.post("/actions/dataset_get", json={}, headers=headers).status_code == 403


def test_offline_action_runs_three_cases_and_exports_real_report(client):
    headers = _headers(client)
    dataset = client.post("/actions/dataset_get", json={}, headers=headers).json()
    assert len(dataset["cases"]) == 3
    response = client.post("/actions/experiment_start", json={"mode": "offline", "idempotency_key": "preview-first"}, headers=headers)
    assert response.status_code == 200, response.text
    identifier = response.json()["id"]

    async def finish():
        task = client.app.state.evaluation_service.tasks.get(identifier)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)

    client.portal.call(finish)
    response = client.post("/actions/experiment_get", json={"id": identifier}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["mode"] == "offline"
    assert body["summary"] == {"total": 3, "passed": 3, "failed": 0, "unscored": 0}
    assert all(item["run_id"] is None for item in body["items"])
    listing = client.post("/actions/experiment_list", json={}, headers=headers).json()
    assert [item["id"] for item in listing["items"]] == [identifier]
    report = client.post("/actions/report_export", json={"id": identifier}, headers=headers).json()
    assert json.loads(report["content"]) == body
    assert report["mime_type"] == "application/json"


def test_real_agent_is_denied_without_creating_an_experiment(client):
    headers = _headers(client)
    response = client.post("/actions/experiment_start", json={"mode": "agent", "idempotency_key": "no-real-agent"}, headers=headers)
    assert response.status_code == 403
    assert client.post("/actions/experiment_list", json={}, headers=headers).json()["items"] == []


def test_unknown_action_and_invalid_request_bodies_are_rejected(client):
    headers = _headers(client)
    assert client.post("/actions/execute_shell", json={}, headers=headers).status_code == 404
    assert client.post("/actions/dataset_get", json=[], headers=headers).status_code == 422
    assert client.post("/actions/dataset_get", content="{}", headers=headers).status_code == 415
    headers = {**headers, "Content-Type": "application/json"}
    assert client.post("/actions/dataset_get", content="{", headers=headers).status_code == 422
    assert client.post("/actions/dataset_get", content=" " * (256 * 1024 + 1), headers=headers).status_code == 413


def test_csrf_token_is_unique_to_app_lifetime(client, tmp_path):
    stale_headers = _headers(client)
    app = create_app(str(tmp_path / "other-preview.sqlite"))
    with TestClient(app, base_url="http://127.0.0.1:2027") as other:
        assert other.post("/actions/dataset_get", json={}, headers=stale_headers).status_code == 403


def test_localhost_host_and_matching_origin_are_supported(client):
    headers = {**_headers(client), "Host": "localhost:2027", "Origin": "http://localhost:2027"}
    assert client.post("/actions/dataset_get", json={}, headers=headers).status_code == 200


def test_negative_control_and_comparison_through_real_http_actions(client):
    headers = _headers(client)

    def run(scenario):
        response = client.post(
            "/actions/experiment_start",
            headers=headers,
            json={
                "mode": "offline",
                "offline_scenario": scenario,
                "idempotency_key": scenario,
            },
        )
        assert response.status_code == 200, response.text
        identifier = response.json()["id"]

        async def finish():
            task = client.app.state.evaluation_service.tasks.get(identifier)
            if task is not None:
                await asyncio.wait_for(asyncio.shield(task), 10)

        client.portal.call(finish)
        return identifier

    baseline, candidate = run("correct"), run("wrong_total")
    response = client.post(
        "/actions/experiment_compare",
        headers=headers,
        json={
            "baseline_id": baseline,
            "candidate_id": candidate,
        },
    )
    assert response.status_code == 200, response.text
    comparison = response.json()
    assert comparison["compatible"] is True
    assert comparison["is_model_comparison"] is False
    assert comparison["summary"] == {"improved": 0, "regressed": 1, "unchanged": 2, "unscored": 0}
    assert [item["case_id"] for item in comparison["items"] if item["change"] == "regressed"] == ["monthly-sales-001"]
    listing = client.post("/actions/experiment_list", headers=headers, json={}).json()["items"]
    assert all("items" not in body and "dataset" not in body for body in listing)


def test_dataset_import_and_version_lookup_through_http(client):
    headers = _headers(client)
    dataset = client.post("/actions/dataset_get", headers=headers, json={}).json()
    dataset.update(id="my-first-eval", name="My first evaluation")
    assert client.post("/actions/dataset_import", headers=headers, json={"dataset": dataset}).status_code == 200
    dataset["name"] = "Conflicting revision"
    assert client.post("/actions/dataset_import", headers=headers, json={"dataset": dataset}).status_code == 409
    response = client.post("/actions/dataset_get", headers=headers, json={"id": dataset["id"], "revision": 1})
    assert response.status_code == 200
    assert response.json()["name"] == "My first evaluation"
    listing = client.post("/actions/dataset_list", headers=headers, json={}).json()["items"]
    assert len(listing) == 2
    assert {item["id"] for item in listing} == {"fixed-cases-v1", "my-first-eval"}
    assert (
        client.post(
            "/actions/experiment_start",
            headers=headers,
            json={
                "mode": "offline",
                "idempotency_key": "no-scripted-answer",
                "dataset_id": dataset["id"],
                "dataset_revision": 1,
            },
        ).status_code
        == 422
    )
