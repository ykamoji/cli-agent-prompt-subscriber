"""FastAPI egress server for CLI agent conversation logs.

Receives the parsed conversation logs produced by the Claude / Antigravity
Stop hooks and pushes them into the ``logs`` collection of the target
MongoDB database. This is the hosted replacement for running ``egress.py``
locally, so hook users never need direct MongoDB access.

Authentication is per user: the CLI dashboard app creates each user in the
``users`` collection along with an ``api_key``. A POST is accepted only when
the ``X-API-Key`` header matches the stored key for the ``user_id`` in the
request body; unknown users are rejected.

Configuration is read from a ``.env`` file next to this script:

    MONGODB_URI=mongodb+srv://user:pass@cluster.example.mongodb.net/
    DB_NAME=vibe_coding

Usage:
    uv run uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from pymongo import MongoClient, UpdateOne

load_dotenv(Path(__file__).resolve().parent / ".env")

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("DB_NAME", "vibe_coding")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not MONGODB_URI:
        raise RuntimeError("MONGODB_URI is not set in the .env file")

    client = MongoClient(MONGODB_URI)
    client.admin.command("ping")  # fail fast on bad credentials/URI
    app.state.db = client[DB_NAME]
    yield
    client.close()


app = FastAPI(title="cli-agent-prompt-subscriber", lifespan=lifespan)


class EgressRequest(BaseModel):
    user_id: str = Field(min_length=1)
    user_name: str = Field(min_length=1)
    cli_agent: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    entries: list[dict]


class EgressResponse(BaseModel):
    inserted: int
    skipped: int
    total: int


def authenticate_user(db, user_id: str, x_api_key: str | None) -> None:
    """Allow the write only when the key matches the user's stored api_key.

    Users and their keys are created by the CLI dashboard app; this server
    never creates users. The same 401 is returned for unknown users and bad
    keys so callers can't probe which user_ids exist.
    """
    user = db["users"].find_one({"user_id": user_id})
    stored_key = (user or {}).get("api_key")
    if not x_api_key or not stored_key or not secrets.compare_digest(
        str(stored_key), x_api_key
    ):
        raise HTTPException(status_code=401, detail="Invalid user id or API key")


def push_logs(db, req: EgressRequest) -> EgressResponse:
    """Upsert log entries keyed by (session_id, cli_agent, entry_index).

    Existing documents keep their original fields ($setOnInsert); only the
    "completed At" timestamp is refreshed, matching the behaviour of the
    original local egress.py. Safe to re-run: clients resend whole session
    files and only new entries are inserted.
    """
    operations = []
    for idx, entry in enumerate(req.entries):
        doc = dict(entry)
        doc["cli_agent"] = req.cli_agent
        doc["session_id"] = req.session_id
        doc["entry_index"] = idx
        doc["user_id"] = req.user_id

        key = {
            "session_id": req.session_id,
            "cli_agent": req.cli_agent,
            "entry_index": idx,
        }
        payload = {k: v for k, v in doc.items() if k not in key}
        set_on_insert = {k: v for k, v in payload.items() if k != "completed At"}
        update_doc: dict = {"$setOnInsert": set_on_insert}
        if "completed At" in payload:
            update_doc["$set"] = {"completed At": payload["completed At"]}
        operations.append(UpdateOne(key, update_doc, upsert=True))

    if not operations:
        return EgressResponse(inserted=0, skipped=0, total=0)

    result = db["logs"].bulk_write(operations, ordered=False)
    total = len(operations)
    return EgressResponse(
        inserted=result.upserted_count,
        skipped=total - result.upserted_count,
        total=total,
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/egress", response_model=EgressResponse)
def egress(
    req: EgressRequest, x_api_key: str | None = Header(default=None)
) -> EgressResponse:
    db = app.state.db
    authenticate_user(db, req.user_id, x_api_key)
    return push_logs(db, req)
