"""Use a normal Gateway login session to inspect or run the installed plugin.

Default invocation is a read-only plugin preflight after normal login. Real
Agent execution requires --run-agent. Credentials and cookies stay in memory.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import math
import os
import sys
import time
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

NAMESPACE = "local.eval-center"
ACTIONS = frozenset({"dataset_get", "experiment_start", "experiment_get", "experiment_cancel", "report_export"})
ACTIVE = frozenset({"queued", "running", "cancelling"})
TERMINAL = frozenset({"completed", "failed", "cancelled", "needs_reconciliation"})
USERNAME_ENV = "DEERFLOW_EVAL_USERNAME"
PASSWORD_ENV = "DEERFLOW_EVAL_PASSWORD"


class LiveClientError(RuntimeError):
    """Deliberately sanitized CLI error, never containing response bodies."""


def normalize_base_url(value: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
        or "%" in parsed.path
        or "\\" in value
    ):
        raise LiveClientError("Use an HTTP(S) Gateway base URL without credentials, query, or fragment")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            raise LiveClientError("Non-loopback Gateway login requires HTTPS")
    try:
        parsed.port
    except ValueError as exc:
        raise LiveClientError("Invalid Gateway port") from exc
    return value.rstrip("/"), f"{parsed.scheme}://{parsed.netloc}"


class GatewayClient:
    def __init__(self, base_url: str, *, client: httpx.Client | None = None):
        self.base_url, self.origin = normalize_base_url(base_url)
        self.client = client if client is not None else httpx.Client(timeout=30, follow_redirects=False, trust_env=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _request(self, method: str, path: str, *, csrf: bool = False, **kwargs) -> dict[str, Any]:
        headers = {"Origin": self.origin}
        if csrf:
            try:
                token = self.client.cookies.get("csrf_token")
            except httpx.CookieConflict as exc:
                raise LiveClientError("Ambiguous CSRF cookies; start a fresh login session") from exc
            if not token:
                raise LiveClientError("Gateway has not issued a CSRF cookie; normal login is required")
            headers["X-CSRF-Token"] = token
        try:
            response = self.client.request(method, self.base_url + path, headers=headers, follow_redirects=False, **kwargs)
        except httpx.HTTPError as exc:
            raise LiveClientError("Gateway request failed; the outcome of a submitted write may be unknown") from exc
        if 300 <= response.status_code < 400:
            raise LiveClientError("Gateway redirected the request; use its final base URL")
        if response.status_code >= 400:
            messages = {
                401: "Authentication failed or the login session expired",
                403: "Gateway denied the operation; check account permissions and CSRF/session policy",
                404: "Gateway endpoint, installed plugin, or owned experiment was not found",
                409: "Gateway reported a conflict; inspect the existing operation before retrying",
                422: "Gateway rejected the request parameters",
                429: "Gateway rate limit reached; do not repeat login attempts automatically",
            }
            raise LiveClientError(messages.get(response.status_code, f"Gateway operation failed (HTTP {response.status_code})"))
        if len(response.content) > 2 * 1024 * 1024:
            raise LiveClientError("Gateway response exceeded the CLI size limit")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LiveClientError("Gateway returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise LiveClientError("Gateway returned an unexpected response shape")
        return payload

    def login(self, username: str, password: str) -> dict[str, Any]:
        if not username.strip() or not password:
            raise LiveClientError("Username and password are required")
        self._request(
            "POST",
            "/api/v1/auth/login/local",
            data={"username": username, "password": password, "remember_me": "false"},
        )
        user = self._request("GET", "/api/v1/auth/me")
        if user.get("needs_setup"):
            raise LiveClientError("Complete account setup through the normal login flow before running evaluations")
        try:
            session = self.client.cookies.get("access_token")
            csrf = self.client.cookies.get("csrf_token")
        except httpx.CookieConflict as exc:
            raise LiveClientError("Ambiguous session cookies; start a fresh login session") from exc
        if not session or not csrf:
            raise LiveClientError("Gateway did not issue normal session and CSRF cookies")
        return user

    def action(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if name not in ACTIONS or not isinstance(payload, dict):
            raise LiveClientError("Unsupported evaluation action")
        return self._request("POST", f"/api/plugins/{NAMESPACE}/actions/{name}", csrf=True, json=payload)

    def wait(
        self,
        identifier: str,
        *,
        timeout_seconds: float = 600,
        poll_interval: float = 1,
        progress=None,
        cancel_on_timeout: bool = True,
    ) -> dict[str, Any]:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or not math.isfinite(poll_interval) or poll_interval <= 0:
            raise LiveClientError("Polling timeout and interval must be positive finite numbers")
        deadline = time.monotonic() + timeout_seconds
        previous = None
        while True:
            experiment = self.action("experiment_get", {"id": identifier})
            status = experiment.get("status")
            if status not in ACTIVE | TERMINAL:
                raise LiveClientError("Plugin returned an unknown experiment status")
            signature = (status, json.dumps(experiment.get("summary", {}), sort_keys=True))
            if signature != previous and progress is not None:
                progress(experiment)
            previous = signature
            if status in TERMINAL:
                return experiment
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if not cancel_on_timeout:
                    raise LiveClientError("CLI polling timed out; the existing experiment was left unchanged")
                try:
                    cancelled = self.action("experiment_cancel", {"id": identifier})
                except LiveClientError as exc:
                    raise LiveClientError("CLI polling timed out; cancellation is unconfirmed. Inspect the saved experiment ID") from exc
                ending = cancelled.get("status")
                if ending in TERMINAL:
                    raise LiveClientError(f"CLI polling timed out; experiment is now {ending}")
                raise LiveClientError("CLI polling timed out; cancellation was requested. Inspect the saved experiment ID")
            time.sleep(min(poll_interval, remaining))


def credentials(*, environ: Mapping[str, str] | None = None, non_interactive: bool = False) -> tuple[str, str]:
    environ = os.environ if environ is None else environ
    username, password = environ.get(USERNAME_ENV), environ.get(PASSWORD_ENV)
    if non_interactive and (not username or not password):
        raise LiveClientError(f"Set {USERNAME_ENV} and {PASSWORD_ENV} for non-interactive login")
    try:
        if not username:
            username = input("DeerFlow email: ")
        if not password:
            # getpass normally warns and falls back to echoing stdin when it
            # cannot secure a TTY. Fail closed instead of exposing a password.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("DeerFlow password (not echoed): ")
    except (EOFError, getpass.GetPassWarning) as exc:
        raise LiveClientError("Use an interactive terminal or the named credential environment variables") from exc
    if not username.strip() or not password:
        raise LiveClientError("Username and password are required")
    return username.strip(), password


def _progress(experiment: dict[str, Any]) -> None:
    summary = experiment.get("summary", {})
    print(
        f"status={experiment.get('status')} passed={summary.get('passed', 0)} "
        f"failed={summary.get('failed', 0)} unscored={summary.get('unscored', 0)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:2026")
    parser.add_argument("--run-agent", action="store_true", help="Explicitly start real Agent evaluation; provider charges may apply")
    parser.add_argument("--experiment-id", help="Inspect an existing owned experiment instead of creating one")
    parser.add_argument("--cancel", action="store_true", help="Cancel the experiment selected by --experiment-id")
    parser.add_argument("--idempotency-key", help="Reuse the same key after an uncertain start response")
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--poll-interval", type=float, default=1)
    parser.add_argument("--output", type=Path, help="Save JSON report to a new file; credentials and cookies are not included")
    parser.add_argument("--show-cases", action="store_true", help="Print the installed dataset and reference answers")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args(argv)
    if args.run_agent and args.experiment_id:
        parser.error("--run-agent and --experiment-id are mutually exclusive")
    if args.cancel and not args.experiment_id:
        parser.error("--cancel requires --experiment-id")
    if args.idempotency_key and not args.run_agent:
        parser.error("--idempotency-key requires --run-agent")
    if args.output and not (args.run_agent or args.experiment_id):
        parser.error("--output requires --run-agent or --experiment-id")
    if args.idempotency_key and (not args.idempotency_key.strip() or len(args.idempotency_key) > 128):
        parser.error("--idempotency-key must contain 1-128 characters")
    if not math.isfinite(args.timeout_seconds) or not 0 < args.timeout_seconds <= 3600:
        parser.error("--timeout-seconds must be between 0 and 3600")
    if not math.isfinite(args.poll_interval) or not 0 < args.poll_interval <= 30:
        parser.error("--poll-interval must be between 0 and 30")
    gateway = None
    identifier = args.experiment_id
    started_here = False
    try:
        gateway = GatewayClient(args.base_url)
        username, password = credentials(non_interactive=args.non_interactive)
        try:
            gateway.login(username, password)
        finally:
            password = None
        dataset = gateway.action("dataset_get", {})
        cases = dataset.get("cases", [])
        if not isinstance(cases, list):
            raise LiveClientError("Plugin returned an invalid dataset")
        print(f"Normal session authenticated; installed evaluation dataset contains {len(cases)} cases.")
        if args.show_cases:
            print(json.dumps(dataset, ensure_ascii=False, indent=2, allow_nan=False))
        if args.cancel:
            experiment = gateway.action("experiment_cancel", {"id": identifier})
            _progress(experiment)
        elif args.run_agent or identifier:
            if args.run_agent:
                key = args.idempotency_key or f"live-cli-{uuid4()}"
                print(f"Real Agent evaluation requested. Idempotency key: {key}", flush=True)
                experiment = gateway.action("experiment_start", {"mode": "agent", "idempotency_key": key})
                identifier = experiment.get("id")
                if not isinstance(identifier, str) or not identifier:
                    raise LiveClientError("Plugin start response has no experiment ID; inspect history using the same idempotency key")
                started_here = True
            print(f"Experiment ID: {identifier}", flush=True)
            experiment = gateway.wait(
                identifier,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
                progress=_progress,
                cancel_on_timeout=started_here,
            )
        else:
            print("Preflight completed. Add --run-agent to start a real Agent evaluation.")
            return 0
        if args.output:
            report = gateway.action("report_export", {"id": identifier})
            content = report.get("content")
            if not isinstance(content, str):
                raise LiveClientError("Plugin report content is missing")
            # Save only report data, never HTTP headers, cookies, or login state.
            json.loads(content)
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(content)
            print(f"Report saved: {args.output}")
        summary = experiment.get("summary", {})
        if experiment.get("status") == "cancelled" and args.cancel:
            return 0
        return 0 if experiment.get("status") == "completed" and summary.get("failed", 0) == 0 and summary.get("unscored", 0) == 0 else 2
    except KeyboardInterrupt:
        if gateway is not None and identifier and started_here:
            try:
                result = gateway.action("experiment_cancel", {"id": identifier})
                print(f"Interrupted; cancellation result: {result.get('status', 'unknown')}", file=sys.stderr)
            except LiveClientError:
                print("Interrupted; cancellation is unconfirmed. Inspect the saved experiment ID.", file=sys.stderr)
        elif identifier:
            print("Interrupted; the existing experiment was left unchanged.", file=sys.stderr)
        else:
            print("Interrupted. If a start was submitted, inspect it using its idempotency key.", file=sys.stderr)
        return 130
    except (LiveClientError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, LiveClientError) else f"Local operation failed ({type(exc).__name__})"
        print(f"Evaluation CLI: {message}", file=sys.stderr)
        return 1
    finally:
        if gateway is not None:
            gateway.close()


if __name__ == "__main__":
    raise SystemExit(main())
