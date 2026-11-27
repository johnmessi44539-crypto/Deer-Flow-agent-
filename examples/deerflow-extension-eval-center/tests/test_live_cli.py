"""HTTP transport tests for normal session login; no real Gateway or model."""

import json
import warnings
from urllib.parse import parse_qs

import httpx
import pytest

from deerflow_extension_eval_center import live_cli


def session_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def login_response():
    return httpx.Response(
        200,
        json={"expires_in": 60, "needs_setup": False},
        headers=[("set-cookie", "access_token=session-fixture; Path=/; HttpOnly"), ("set-cookie", "csrf_token=csrf-fixture; Path=/")],
    )


def test_normal_login_uses_form_cookies_and_double_submit_csrf():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/login/local"):
            form = parse_qs(request.content.decode())
            assert form == {"username": ["tester@example.test"], "password": ["password-fixture"], "remember_me": ["false"]}
            assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
            assert request.headers["origin"] == "http://localhost:2026"
            return login_response()
        assert "access_token=session-fixture" in request.headers["cookie"]
        assert "authorization" not in request.headers
        assert "x-deerflow-plugin-viewer" not in request.headers
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"needs_setup": False})
        assert request.url.path == "/api/plugins/local.eval-center/actions/dataset_get"
        assert request.headers["x-csrf-token"] == "csrf-fixture"
        assert json.loads(request.content) == {}
        return httpx.Response(200, json={"cases": [{"id": "case-1"}]}, headers={"set-cookie": "csrf_token=rotated-fixture; Path=/"})

    with session_client(handler) as client:
        gateway = live_cli.GatewayClient("http://localhost:2026", client=client)
        gateway.login("tester@example.test", "password-fixture")
        assert gateway.action("dataset_get", {})["cases"][0]["id"] == "case-1"
        assert client.cookies["csrf_token"] == "rotated-fixture"
    assert len(requests) == 3


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "http://localhost:2026/?x=1",
        "http://user:password@localhost:2026",
        "file:///tmp",
        "http://localhost:2026/a/../b",
        "http://localhost:2026/%2e%2e",
        "https://example.test/#fragment",
    ],
)
def test_unsafe_base_urls_are_rejected(url):
    with pytest.raises(live_cli.LiveClientError):
        live_cli.normalize_base_url(url)


def test_base_path_is_preserved_and_redirect_is_not_followed():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://foreign.example.test"})

    with session_client(handler) as client:
        gateway = live_cli.GatewayClient("https://gateway.example.test/deerflow/", client=client)
        with pytest.raises(live_cli.LiveClientError, match="redirected"):
            gateway.login("tester", "private-fixture")
    assert seen == ["https://gateway.example.test/deerflow/api/v1/auth/login/local"]


def test_missing_csrf_cookie_fails_before_plugin_request():
    called = []
    with session_client(lambda request: called.append(request)) as client:
        gateway = live_cli.GatewayClient("http://127.0.0.1:2026", client=client)
        with pytest.raises(live_cli.LiveClientError, match="CSRF cookie"):
            gateway.action("experiment_start", {"mode": "agent"})
    assert called == []


def test_authentication_errors_do_not_expose_server_body():
    with session_client(lambda request: httpx.Response(401, json={"detail": "private-response-fixture"})) as client:
        gateway = live_cli.GatewayClient("http://localhost:2026", client=client)
        with pytest.raises(live_cli.LiveClientError) as caught:
            gateway.login("tester", "password-fixture")
    assert "private-response-fixture" not in str(caught.value)
    assert "password-fixture" not in str(caught.value)


def test_setup_required_accounts_stop_before_plugin_dispatch():
    def handler(request):
        if request.url.path.endswith("/login/local"):
            return login_response()
        return httpx.Response(200, json={"needs_setup": True})

    with session_client(handler) as client:
        with pytest.raises(live_cli.LiveClientError, match="Complete account setup"):
            live_cli.GatewayClient("http://localhost:2026", client=client).login("tester", "password-fixture")


def test_poll_timeout_requests_explicit_cancellation(monkeypatch):
    calls = []

    def handler(request):
        action = request.url.path.rsplit("/", 1)[-1]
        calls.append(action)
        return httpx.Response(200, json={"id": "experiment-1", "status": "cancelled" if action == "experiment_cancel" else "running"})

    times = iter([0, 10])
    monkeypatch.setattr(live_cli.time, "monotonic", lambda: next(times))
    with session_client(handler) as client:
        client.cookies.set("csrf_token", "csrf-fixture")
        gateway = live_cli.GatewayClient("http://localhost:2026", client=client)
        with pytest.raises(live_cli.LiveClientError, match="now cancelled"):
            gateway.wait("experiment-1", timeout_seconds=1)
    assert calls == ["experiment_get", "experiment_cancel"]


def test_credentials_use_named_environment_and_never_prompt_when_provided(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected username prompt"))
    monkeypatch.setattr(live_cli.getpass, "getpass", lambda prompt: pytest.fail("unexpected password prompt"))
    assert live_cli.credentials(environ={live_cli.USERNAME_ENV: "tester", live_cli.PASSWORD_ENV: "password-fixture"}) == (
        "tester",
        "password-fixture",
    )
    with pytest.raises(live_cli.LiveClientError, match="non-interactive"):
        live_cli.credentials(environ={}, non_interactive=True)


def test_credentials_refuse_getpass_echo_fallback(monkeypatch):
    def insecure_getpass(prompt):
        warnings.warn("Cannot control echo", live_cli.getpass.GetPassWarning)
        pytest.fail("Password fallback must not be reached")

    monkeypatch.setattr(live_cli.getpass, "getpass", insecure_getpass)
    with pytest.raises(live_cli.LiveClientError, match="interactive terminal"):
        live_cli.credentials(environ={live_cli.USERNAME_ENV: "tester"})


def test_inspection_timeout_does_not_cancel_existing_experiment(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"id": "experiment-1", "status": "running"})

    times = iter([0, 10])
    monkeypatch.setattr(live_cli.time, "monotonic", lambda: next(times))
    with session_client(handler) as client:
        client.cookies.set("csrf_token", "csrf-fixture")
        gateway = live_cli.GatewayClient("http://localhost:2026", client=client)
        with pytest.raises(live_cli.LiveClientError, match="left unchanged"):
            gateway.wait("experiment-1", timeout_seconds=1, cancel_on_timeout=False)
    assert calls == ["experiment_get"]


def test_default_main_preflight_never_starts_real_model(monkeypatch, capsys):
    actions = []

    class FakeGateway:
        def __init__(self, base_url):
            pass

        def login(self, username, password):
            assert password == "password-fixture"

        def action(self, name, payload):
            actions.append(name)
            return {"cases": [{"id": "case-1"}]}

        def close(self):
            pass

    monkeypatch.setattr(live_cli, "GatewayClient", FakeGateway)
    monkeypatch.setattr(live_cli, "credentials", lambda **kwargs: ("tester", "password-fixture"))
    assert live_cli.main([]) == 0
    assert actions == ["dataset_get"]
    output = capsys.readouterr()
    assert "password-fixture" not in output.out + output.err
    assert "--run-agent" in output.out


def test_explicit_run_uses_idempotency_key_and_saves_only_report(monkeypatch, tmp_path, capsys):
    actions = []
    experiment = {"id": "experiment-1", "status": "completed", "summary": {"passed": 3, "failed": 0, "unscored": 0}}

    class FakeGateway:
        def __init__(self, base_url):
            pass

        def login(self, username, password):
            assert password == "password-fixture"

        def action(self, name, payload):
            actions.append((name, payload))
            if name == "dataset_get":
                return {"cases": [{"id": "case-1"}]}
            if name == "report_export":
                return {"content": json.dumps(experiment)}
            return experiment

        def wait(self, identifier, **kwargs):
            return experiment

        def close(self):
            pass

    report = tmp_path / "report.json"
    monkeypatch.setattr(live_cli, "GatewayClient", FakeGateway)
    monkeypatch.setattr(live_cli, "credentials", lambda **kwargs: ("tester", "password-fixture"))
    assert live_cli.main(["--run-agent", "--idempotency-key", "stable-key", "--output", str(report)]) == 0
    assert ("experiment_start", {"mode": "agent", "idempotency_key": "stable-key"}) in actions
    assert json.loads(report.read_text()) == experiment
    output = capsys.readouterr()
    assert "password-fixture" not in output.out + output.err + report.read_text()
