"""Single-service SQLite persistence. Blocking calls belong on worker threads."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from deerflow_extension_api.agent_runs import AgentRunError


class ExperimentStore:
    def __init__(self, path: str):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("storage_path must be absolute")
        self._lock_file = None

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("One eval-center service per database is supported") from exc
        self._lock_file = handle
        try:
            with self.connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS experiments ("
                    "id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL, "
                    "fingerprint TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL, "
                    "UNIQUE(owner, request_key))"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS datasets (owner TEXT NOT NULL, id TEXT NOT NULL, "
                    "revision INTEGER NOT NULL, hash TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(owner,id,revision))"
                )
        except BaseException:
            self.close()
            raise

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def close(self) -> None:
        handle, self._lock_file = self._lock_file, None
        if handle is not None:
            handle.close()

    def create(self, owner: str, key: str, fingerprint: str, body: dict) -> tuple[dict, bool]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT fingerprint,body FROM experiments WHERE owner=? AND request_key=?", (owner, key)).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise AgentRunError(409, "Idempotency key reused with different evaluation settings")
                return json.loads(existing["body"]), False
            count = db.execute("SELECT COUNT(*) FROM experiments WHERE owner=?", (owner,)).fetchone()[0]
            if count >= 200:
                raise AgentRunError(409, "Local prototype limit: 200 experiments per owner")
            db.execute(
                "INSERT INTO experiments VALUES (?,?,?,?,?,?)",
                (body["id"], owner, key, fingerprint, body["created_at"], json.dumps(body, ensure_ascii=False)),
            )
        return body, True

    def get(self, owner: str, identifier: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT body FROM experiments WHERE id=? AND owner=?", (identifier, owner)).fetchone()
        if row is None:
            raise AgentRunError(404, "Experiment not found")
        return json.loads(row["body"])

    def save(self, owner: str, body: dict) -> None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE experiments SET body=? WHERE id=? AND owner=?",
                (json.dumps(body, ensure_ascii=False), body["id"], owner),
            ).rowcount
        if changed != 1:
            raise AgentRunError(404, "Experiment not found")

    def list(self, owner: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT body FROM experiments WHERE owner=? ORDER BY created_at DESC,id DESC LIMIT 50", (owner,)).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def reconcile_restart(self) -> None:
        with self.connect() as db:
            rows = db.execute("SELECT id,body FROM experiments").fetchall()
            for row in rows:
                body: dict[str, Any] = json.loads(row["body"])
                if body["status"] not in ("queued", "running", "cancelling"):
                    continue
                body["status"] = "needs_reconciliation"
                body["error"] = "Service restarted; inspect saved run IDs before starting another experiment."
                db.execute("UPDATE experiments SET body=? WHERE id=?", (json.dumps(body, ensure_ascii=False), row["id"]))

    def import_dataset(self, owner: str, dataset: dict, content_hash: str) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT hash,body FROM datasets WHERE owner=? AND id=? AND revision=?", (owner, dataset["id"], dataset["revision"])
            ).fetchone()
            if old is not None:
                if old["hash"] != content_hash:
                    raise AgentRunError(409, "Dataset revision is immutable; increase revision for changed content")
                return json.loads(old["body"])
            if db.execute("SELECT COUNT(*) FROM datasets WHERE owner=?", (owner,)).fetchone()[0] >= 100:
                raise AgentRunError(409, "Local prototype limit: 100 dataset revisions per owner")
            db.execute(
                "INSERT INTO datasets VALUES (?,?,?,?,?)",
                (owner, dataset["id"], dataset["revision"], content_hash, json.dumps(dataset, ensure_ascii=False)),
            )
        return dataset

    def get_dataset(self, owner: str, identifier: str, revision: int) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT body FROM datasets WHERE owner=? AND id=? AND revision=?", (owner, identifier, revision)).fetchone()
        if row is None:
            raise AgentRunError(404, "Dataset revision not found")
        return json.loads(row["body"])

    def list_datasets(self, owner: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id,revision,hash,body FROM datasets WHERE owner=? ORDER BY id,revision DESC", (owner,)).fetchall()
        return [
            {"id": row["id"], "name": json.loads(row["body"])["name"], "revision": row["revision"], "hash": row["hash"], "builtin": False}
            for row in rows
        ]
