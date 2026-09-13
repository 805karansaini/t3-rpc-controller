#!/usr/bin/env python3
import argparse
import asyncio
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

import websockets
from websockets.exceptions import WebSocketException


ENV_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
DEFAULT_HOME = Path(__file__).resolve().parent / ".state"
SENSITIVE_KEY_RE = re.compile(r"token|ticket|authorization|cookie|credential|secret", re.I)
CODEX_PROVIDER_INSTANCE = "codex"
OUTPUT_SCHEMA_VERSION = 1


class EnvironmentStore:
    def __init__(self, home: Path = DEFAULT_HOME):
        self.home = Path(home)
        self.config_path = self.home / "environments.json"
        self.token_dir = self.home / "tokens"

    def save(self, name: str, url: str, token: str) -> None:
        if not ENV_NAME_RE.fullmatch(name):
            raise ValueError("environment name must match [A-Za-z0-9._-]+")
        normalized_url = normalize_url(url)
        self.home.mkdir(parents=True, exist_ok=True)
        self.token_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        os.chmod(self.token_dir, 0o700)
        config = self.load_config()
        config[name] = {"url": normalized_url}
        temp_path = self.config_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(config, indent=2) + "\n")
        os.chmod(temp_path, 0o600)
        temp_path.replace(self.config_path)
        token_path = self.token_dir / name
        token_path.write_text(token.strip())
        os.chmod(token_path, 0o600)

    def load_config(self) -> dict:
        if not self.config_path.exists():
            return {}
        return json.loads(self.config_path.read_text())

    def load(self, name: str) -> tuple[str, str]:
        if not ENV_NAME_RE.fullmatch(name):
            raise ValueError("invalid environment name")
        config = self.load_config()
        if name not in config:
            raise KeyError(f"unknown environment: {name}")
        token_path = self.token_dir / name
        if not token_path.exists():
            raise FileNotFoundError(f"missing token for environment: {name}")
        return config[name]["url"], token_path.read_text().strip()

    def names(self) -> list[str]:
        return sorted(self.load_config())

    def remove(self, name: str) -> Path:
        if not ENV_NAME_RE.fullmatch(name):
            raise ValueError("invalid environment name")
        config = self.load_config()
        if name not in config:
            raise KeyError(f"unknown environment: {name}")
        removed_root = self.home / "removed"
        removed_root.mkdir(parents=True, exist_ok=True)
        os.chmod(removed_root, 0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = removed_root / f"{name}-{stamp}"
        backup.mkdir(mode=0o700)
        metadata_path = backup / "environment.json"
        metadata_path.write_text(json.dumps({name: {"url": config[name].get("url")}}, indent=2) + "\n")
        os.chmod(metadata_path, 0o600)
        token_path = self.token_dir / name
        if token_path.exists():
            token_path.replace(backup / "token")
        del config[name]
        temp_path = self.config_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(config, indent=2) + "\n")
        os.chmod(temp_path, 0o600)
        temp_path.replace(self.config_path)
        return backup


def normalize_url(url: str) -> str:
    value = url.strip()
    if value.startswith("@url:") or "`" in value:
        raise ValueError("use a plain URL, for example http://100.65.116.80:3773 (without @url: or backticks)")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("T3 URL must be an http(s) origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def redact_sensitive(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if SENSITIVE_KEY_RE.search(str(key)) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
        return re.sub(r"(?i)([?&]wsTicket=)[^&\s\"']+", r"\1[REDACTED]", value)
    return value


def safe_error_message(error: object) -> str:
    if isinstance(error, (dict, list)):
        return json.dumps(redact_sensitive(error), separators=(",", ":"))
    return str(redact_sensitive(str(error)))


def error_category(error: BaseException) -> str:
    if isinstance(error, (ValueError, KeyError)):
        return "invalid_request"
    if isinstance(error, FileNotFoundError):
        return "configuration"
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(error, (OSError, WebSocketException)):
        return "connection"
    return "remote"


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be one or greater")
    return parsed


def emit_json(value: object, *, indent: int | None = None, flush: bool = False) -> None:
    if isinstance(value, dict) and "schemaVersion" not in value:
        value = {"schemaVersion": OUTPUT_SCHEMA_VERSION, **value}
    print(json.dumps(redact_sensitive(value), indent=indent), flush=flush)


def require_codex_provider(instance_id: str) -> None:
    if instance_id != CODEX_PROVIDER_INSTANCE:
        raise ValueError(
            f"live task execution is restricted to provider instance {CODEX_PROVIDER_INSTANCE}"
        )


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def make_request(request_id: str, tag: str, payload: object) -> dict:
    return {
        "_tag": "Request",
        "id": request_id,
        "tag": tag,
        "payload": payload,
        "headers": [],
    }


def project_rows_from_snapshot(snapshot: dict) -> list[dict]:
    rows = [
        {
            "id": project.get("id"),
            "title": project.get("title"),
            "workspaceRoot": project.get("workspaceRoot"),
            "defaultModelSelection": project.get("defaultModelSelection"),
            "defaultThreadEnvMode": project.get("defaultThreadEnvMode"),
            "repositoryIdentity": project.get("repositoryIdentity"),
            "autoPull": project.get("autoPull"),
            "scripts": project.get("scripts", []),
        }
        for project in snapshot.get("projects", [])
    ]
    return sorted(rows, key=lambda row: (str(row.get("title", "")).lower(), str(row.get("id", ""))))


def first_shell_snapshot(messages: list[dict], request_id: str) -> dict | None:
    for message in messages:
        if message.get("_tag") != "Chunk" or message.get("requestId") != request_id:
            continue
        for value in message.get("values", []):
            if isinstance(value, dict) and value.get("kind") == "snapshot":
                snapshot = value.get("snapshot")
                if isinstance(snapshot, dict):
                    return snapshot
    return None


def first_stream_snapshot(messages: list[dict], request_id: str) -> dict | None:
    for message in messages:
        if message.get("_tag") != "Chunk" or message.get("requestId") != request_id:
            continue
        for value in message.get("values", []):
            if isinstance(value, dict) and value.get("kind") == "snapshot":
                snapshot = value.get("snapshot")
                if isinstance(snapshot, dict):
                    return snapshot
    return None


def thread_snapshot_summary(snapshot: dict, environment: str) -> dict:
    thread = snapshot.get("thread", {})
    return {
        "kind": "snapshot",
        "environment": environment,
        "snapshotSequence": snapshot.get("snapshotSequence"),
        "page": snapshot.get("page"),
        "thread": thread_row(thread, environment),
        "messageCount": len(thread.get("messages", [])),
        "activityCount": len(thread.get("activities", [])),
        "checkpointCount": len(thread.get("checkpoints", [])),
        "proposedPlanCount": len(thread.get("proposedPlans", [])),
    }


def is_terminal_thread_event(item: dict) -> bool:
    if item.get("kind") != "event":
        return False
    event = item.get("event", {})
    if event.get("type") != "thread.session-set":
        return False
    status = (event.get("payload", {}).get("session") or {}).get("status")
    return status in {"ready", "stopped", "interrupted", "error"}


def is_terminal_thread_snapshot(snapshot: dict) -> bool:
    thread = snapshot.get("thread", {})
    latest_state = (thread.get("latestTurn") or {}).get("state")
    session_status = (thread.get("session") or {}).get("status")
    return latest_state != "running" and session_status in {
        "ready",
        "stopped",
        "interrupted",
        "error",
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def model_selection(instance_id: str, model: str) -> dict:
    return {"instanceId": instance_id, "model": model}


def validate_uuid(value: str | None, option: str) -> str | None:
    if value is None:
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise ValueError(f"{option} must be a UUID") from error
    if str(parsed) != value.lower():
        raise ValueError(f"{option} must use canonical UUID form")
    return value.lower()


def build_thread_create_command(
    *,
    thread_id: str,
    project_id: str,
    title: str,
    selection: dict,
    runtime_mode: str,
    interaction_mode: str,
    branch: str | None,
    worktree_path: str | None,
    command_id: str | None = None,
) -> dict:
    return {
        "type": "thread.create",
        "commandId": command_id or str(uuid.uuid4()),
        "threadId": thread_id,
        "projectId": project_id,
        "title": title,
        "modelSelection": selection,
        "runtimeMode": runtime_mode,
        "interactionMode": interaction_mode,
        "branch": branch,
        "worktreePath": worktree_path,
        "createdAt": utc_now(),
    }


def build_turn_start_command(
    *,
    thread_id: str,
    text: str,
    runtime_mode: str,
    interaction_mode: str,
    selection: dict | None = None,
    command_id: str | None = None,
    message_id: str | None = None,
) -> dict:
    command = {
        "type": "thread.turn.start",
        "commandId": command_id or str(uuid.uuid4()),
        "threadId": thread_id,
        "message": {
            "messageId": message_id or str(uuid.uuid4()),
            "role": "user",
            "text": text,
            "attachments": [],
        },
        "runtimeMode": runtime_mode,
        "interactionMode": interaction_mode,
        "createdAt": utc_now(),
    }
    if selection is not None:
        command["modelSelection"] = selection
    return command


def build_thread_model_command(thread_id: str, selection: dict) -> dict:
    return {
        "type": "thread.meta.update",
        "commandId": str(uuid.uuid4()),
        "threadId": thread_id,
        "modelSelection": selection,
    }


def build_thread_rename_command(thread_id: str, title: str) -> dict:
    return {
        "type": "thread.meta.update",
        "commandId": str(uuid.uuid4()),
        "threadId": thread_id,
        "title": title,
    }


def build_thread_lifecycle_command(thread_id: str, action: str, **fields: object) -> dict:
    return {
        "type": f"thread.{action}",
        "commandId": str(uuid.uuid4()),
        "threadId": thread_id,
        **fields,
    }


def normalize_future_datetime(value: str) -> str:
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("snooze time must be an ISO 8601 date and time with a timezone") from error
    if parsed.tzinfo is None:
        raise ValueError("snooze time must include a timezone")
    parsed_utc = parsed.astimezone(timezone.utc)
    if parsed_utc <= datetime.now(timezone.utc):
        raise ValueError("snooze time must be in the future")
    return parsed_utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_thread_stop_command(thread_id: str, turn_id: str | None, stop_session: bool) -> dict:
    command = {
        "type": "thread.session.stop" if stop_session else "thread.turn.interrupt",
        "commandId": str(uuid.uuid4()),
        "threadId": thread_id,
        "createdAt": utc_now(),
    }
    if not stop_session and turn_id:
        command["turnId"] = turn_id
    return command


def pending_requests_from_thread(thread: dict) -> list[dict]:
    pending: dict[str, dict] = {}
    for activity in thread.get("activities", []):
        if not isinstance(activity, dict) or not isinstance(activity.get("payload"), dict):
            continue
        payload = activity["payload"]
        request_id = payload.get("requestId")
        if not isinstance(request_id, str):
            continue
        kind = activity.get("kind")
        if kind in {"approval.requested", "user-input.requested"}:
            pending[request_id] = {
                "requestId": request_id,
                "kind": kind,
                "turnId": activity.get("turnId"),
                "summary": activity.get("summary"),
                "createdAt": activity.get("createdAt"),
                "detail": payload.get("detail"),
                "requestType": payload.get("requestType"),
                "options": payload.get("options", []),
                "questions": payload.get("questions", []),
                "responseMode": payload.get("responseMode"),
            }
        elif kind in {"approval.resolved", "user-input.resolved"}:
            pending.pop(request_id, None)
        elif kind in {
            "provider.approval.respond.failed",
            "provider.user-input.respond.failed",
        }:
            detail = str(payload.get("detail", "")).lower()
            if "stale pending" in detail or "unknown pending" in detail:
                pending.pop(request_id, None)
    return sorted(pending.values(), key=lambda item: str(item.get("createdAt", "")))


def request_response_outcome(thread: dict, request_id: str, requested_kind: str) -> str:
    resolved_kind = (
        "approval.resolved" if requested_kind == "approval.requested" else "user-input.resolved"
    )
    failed_kind = (
        "provider.approval.respond.failed"
        if requested_kind == "approval.requested"
        else "provider.user-input.respond.failed"
    )
    for activity in reversed(thread.get("activities", [])):
        if not isinstance(activity, dict) or not isinstance(activity.get("payload"), dict):
            continue
        if activity["payload"].get("requestId") != request_id:
            continue
        if activity.get("kind") == resolved_kind:
            return "resolved"
        if activity.get("kind") == failed_kind:
            return "failed"
        if activity.get("kind") == requested_kind:
            return "pending"
    return "unknown"


def build_approval_response_command(
    thread_id: str,
    request_id: str,
    decision: str,
) -> dict:
    return {
        "type": "thread.approval.respond",
        "commandId": str(uuid.uuid4()),
        "threadId": thread_id,
        "requestId": request_id,
        "decision": decision,
        "createdAt": utc_now(),
    }


def build_user_input_response_command(
    thread_id: str,
    request_id: str,
    answers: dict,
) -> dict:
    return {
        "type": "thread.user-input.respond",
        "commandId": str(uuid.uuid4()),
        "threadId": thread_id,
        "requestId": request_id,
        "answers": answers,
        "createdAt": utc_now(),
    }


def build_user_input_dismiss_command(thread_id: str, request_id: str) -> dict:
    return {
        "type": "thread.user-input.dismiss",
        "commandId": str(uuid.uuid4()),
        "threadId": thread_id,
        "requestId": request_id,
        "createdAt": utc_now(),
    }


def dry_run_result(environment: str, command: dict) -> dict:
    visible_fields = (
        "threadId",
        "projectId",
        "title",
        "modelSelection",
        "runtimeMode",
        "interactionMode",
        "branch",
        "worktreePath",
        "requestId",
        "decision",
        "snoozedUntil",
        "reason",
    )
    planned = {key: command[key] for key in visible_fields if key in command}
    message = command.get("message")
    if isinstance(message, dict):
        planned["messageId"] = message.get("messageId")
        planned["messageCharacters"] = len(str(message.get("text", "")))
    answers = command.get("answers")
    if isinstance(answers, dict):
        planned["answerQuestionIds"] = sorted(answers)
    return {
        "ok": True,
        "dryRun": True,
        "environment": environment,
        "operation": command.get("type"),
        "planned": planned,
    }


def thread_row(thread: dict, environment: str) -> dict:
    return {
        "environment": environment,
        "id": thread.get("id"),
        "projectId": thread.get("projectId"),
        "title": thread.get("title"),
        "modelSelection": thread.get("modelSelection"),
        "runtimeMode": thread.get("runtimeMode"),
        "interactionMode": thread.get("interactionMode"),
        "latestTurn": thread.get("latestTurn"),
        "session": thread.get("session"),
        "backgroundLiveness": thread.get("backgroundLiveness"),
        "planProgress": thread.get("planProgress"),
        "hasPendingApprovals": bool(thread.get("hasPendingApprovals")),
        "hasPendingUserInput": bool(thread.get("hasPendingUserInput")),
        "hasActionableProposedPlan": bool(thread.get("hasActionableProposedPlan")),
        "archivedAt": thread.get("archivedAt"),
        "settledAt": thread.get("settledAt"),
        "snoozedUntil": thread.get("snoozedUntil"),
        "pinnedAt": thread.get("pinnedAt"),
        "createdAt": thread.get("createdAt"),
        "updatedAt": thread.get("updatedAt"),
    }


def filter_thread_rows(
    rows: list[dict],
    project_id: str | None = None,
    turn_state: str | None = None,
    pending: str | None = None,
) -> list[dict]:
    filtered = rows
    if project_id:
        filtered = [row for row in filtered if row.get("projectId") == project_id]
    if turn_state:
        filtered = [
            row
            for row in filtered
            if (row.get("latestTurn") or {}).get("state") == turn_state
        ]
    if pending == "approvals":
        filtered = [row for row in filtered if row.get("hasPendingApprovals")]
    elif pending == "input":
        filtered = [row for row in filtered if row.get("hasPendingUserInput")]
    elif pending == "any":
        filtered = [
            row
            for row in filtered
            if row.get("hasPendingApprovals") or row.get("hasPendingUserInput")
        ]
    return sorted(
        filtered,
        key=lambda row: (str(row.get("updatedAt", "")), str(row.get("id", ""))),
        reverse=True,
    )


def merge_environment_projects(projects_by_environment: dict[str, list[dict]]) -> list[dict]:
    merged = [
        {"environment": environment, **project}
        for environment, projects in projects_by_environment.items()
        for project in projects
    ]
    return sorted(
        merged,
        key=lambda row: (
            str(row.get("environment", "")).lower(),
            str(row.get("title", "")).lower(),
            str(row.get("id", "")),
        ),
    )


def http_json(url: str, token: str | None, method: str = "GET", payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        parsed = urlparse(url)
        raise RuntimeError(f"T3 HTTP {error.code} from {parsed.scheme}://{parsed.netloc}{parsed.path}") from error


def websocket_url(http_url: str, ticket: str) -> str:
    parsed = urlparse(http_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ws?wsTicket={ticket}"


async def rpc_call(
    http_url: str,
    token: str,
    tag: str,
    payload: object,
    timeout: float = 30,
) -> object:
    async with asyncio.timeout(timeout):
        ticket_result = await asyncio.to_thread(
            http_json,
            f"{http_url}/api/auth/websocket-ticket",
            token,
            "POST",
        )
        if not isinstance(ticket_result, dict):
            raise RuntimeError("T3 returned an invalid WebSocket ticket response")
        ticket = ticket_result.get("ticket")
        if not isinstance(ticket, str) or not ticket:
            raise RuntimeError("T3 did not return a WebSocket ticket")

        request_id = f"t3-controller-{uuid.uuid4()}"
        async with websockets.connect(
            websocket_url(http_url, ticket),
            open_timeout=min(timeout, 15),
        ) as socket:
            await socket.send(json.dumps({"_tag": "Ping"}))
            await socket.send(json.dumps(make_request(request_id, tag, payload)))
            while True:
                raw_message = await socket.recv()
                message = json.loads(raw_message)
                if not isinstance(message, dict):
                    raise RuntimeError("T3 returned an invalid RPC message")
                if message.get("_tag") != "Exit" or message.get("requestId") != request_id:
                    continue
                exit_value = message.get("exit", {})
                if exit_value.get("_tag") == "Success":
                    return exit_value.get("value")
                raise RuntimeError(f"T3 RPC {tag} failed: {safe_error_message(exit_value)}")


def http_form(url: str, payload: dict[str, str]) -> dict:
    request = urllib.request.Request(
        url,
        data=urlencode(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        parsed = urlparse(url)
        raise RuntimeError(f"T3 HTTP {error.code} from {parsed.scheme}://{parsed.netloc}{parsed.path}") from error


def oauth_exchange_payload(credential: str) -> dict[str, str]:
    return {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": credential,
        "subject_token_type": "urn:t3:params:oauth:token-type:environment-bootstrap",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "client_label": "hermes-agent",
        "client_device_type": "bot",
        "client_os": "macos",
    }


def bearer_token_from_oauth(result: dict) -> str:
    token = result.get("access_token")
    if result.get("token_type") != "Bearer" or not isinstance(token, str) or not token.strip():
        raise RuntimeError("T3 OAuth exchange did not return a bearer access token")
    return token.strip()


def bearer_token_from_bootstrap(result: dict) -> str:
    token = result.get("sessionToken")
    if result.get("authenticated") is not True or not isinstance(token, str) or not token.strip():
        raise RuntimeError("T3 pairing response did not include an authenticated bearer session")
    return token.strip()


def bootstrap_bearer(http_url: str, credential: str) -> str:
    session_state = http_json(f"{http_url}/api/auth/session", None)
    session_methods = session_state.get("auth", {}).get("sessionMethods", [])
    if "bearer-access-token" in session_methods:
        result = http_form(f"{http_url}/oauth/token", oauth_exchange_payload(credential))
        return bearer_token_from_oauth(result)

    result = http_json(
        f"{http_url}/api/auth/bootstrap/bearer",
        None,
        "POST",
        {"credential": credential},
    )
    return bearer_token_from_bootstrap(result)


async def fetch_shell_snapshot(http_url: str, token: str) -> dict:
    async with asyncio.timeout(30):
        ticket_result = await asyncio.to_thread(
            http_json,
            f"{http_url}/api/auth/websocket-ticket",
            token,
            "POST",
        )
        if not isinstance(ticket_result, dict):
            raise RuntimeError("T3 returned an invalid WebSocket ticket response")
        ticket = ticket_result.get("ticket")
        if not isinstance(ticket, str) or not ticket:
            raise RuntimeError("T3 did not return a WebSocket ticket")

        request_id = f"hermes-projects-{uuid.uuid4()}"
        async with websockets.connect(websocket_url(http_url, ticket), open_timeout=15) as socket:
            await socket.send(json.dumps({"_tag": "Ping"}))
            await socket.send(json.dumps(make_request(request_id, "orchestration.subscribeShell", {})))
            while True:
                raw_message = await socket.recv()
                message = json.loads(raw_message)
                if not isinstance(message, dict):
                    raise RuntimeError("T3 returned an invalid shell message")
                snapshot = first_shell_snapshot([message], request_id)
                if snapshot is not None:
                    return snapshot
                if message.get("_tag") == "Exit" and message.get("requestId") == request_id:
                    exit_value = message.get("exit", {})
                    if exit_value.get("_tag") != "Success":
                        raise RuntimeError(f"T3 RPC failed: {safe_error_message(exit_value)}")
                    raise RuntimeError("T3 RPC stream ended before returning a shell snapshot")


async def fetch_thread_snapshot(
    http_url: str,
    token: str,
    thread_id: str,
    turn_limit: int | None = None,
) -> dict:
    async with asyncio.timeout(30):
        ticket_result = await asyncio.to_thread(
            http_json,
            f"{http_url}/api/auth/websocket-ticket",
            token,
            "POST",
        )
        if not isinstance(ticket_result, dict):
            raise RuntimeError("T3 returned an invalid WebSocket ticket response")
        ticket = ticket_result.get("ticket")
        if not isinstance(ticket, str) or not ticket:
            raise RuntimeError("T3 did not return a WebSocket ticket")

        request_id = f"t3-thread-{uuid.uuid4()}"
        payload: dict[str, object] = {"threadId": thread_id}
        if turn_limit is not None:
            payload["turnLimit"] = turn_limit
        async with websockets.connect(websocket_url(http_url, ticket), open_timeout=15) as socket:
            await socket.send(json.dumps({"_tag": "Ping"}))
            await socket.send(
                json.dumps(make_request(request_id, "orchestration.subscribeThread", payload))
            )
            while True:
                raw_message = await socket.recv()
                message = json.loads(raw_message)
                if not isinstance(message, dict):
                    raise RuntimeError("T3 returned an invalid thread message")
                snapshot = first_stream_snapshot([message], request_id)
                if snapshot is not None:
                    return snapshot
                if message.get("_tag") == "Exit" and message.get("requestId") == request_id:
                    exit_value = message.get("exit", {})
                    if exit_value.get("_tag") != "Success":
                        raise RuntimeError(f"T3 RPC failed: {safe_error_message(exit_value)}")
                    raise RuntimeError("T3 RPC stream ended before returning a thread snapshot")


async def follow_thread(
    store: EnvironmentStore,
    environment: str,
    thread_id: str,
    *,
    after_sequence: int | None,
    timeout: float,
    max_events: int | None,
    retries: int,
    until_terminal: bool,
    turn_limit: int,
) -> dict:
    http_url, token = store.load(environment)
    deadline = None if timeout == 0 else time.monotonic() + timeout
    last_sequence = after_sequence
    emitted_event_ids: set[str] = set()
    emitted_events = 0
    synchronized = False
    initial_snapshot_emitted = False
    reconnects = 0

    while True:
        if deadline is not None and time.monotonic() >= deadline:
            if not synchronized:
                raise RuntimeError("T3 thread subscription timed out before initial synchronization")
            return {
                "kind": "follow-complete",
                "reason": "timeout",
                "environment": environment,
                "threadId": thread_id,
                "lastSequence": last_sequence,
                "events": emitted_events,
                "reconnects": reconnects,
            }
        try:
            ticket_result = await asyncio.to_thread(
                http_json,
                f"{http_url}/api/auth/websocket-ticket",
                token,
                "POST",
            )
            if not isinstance(ticket_result, dict):
                raise RuntimeError("T3 returned an invalid WebSocket ticket response")
            ticket = ticket_result.get("ticket")
            if not isinstance(ticket, str) or not ticket:
                raise RuntimeError("T3 did not return a WebSocket ticket")
            request_id = f"t3-follow-{uuid.uuid4()}"
            payload: dict[str, object] = {
                "threadId": thread_id,
                "requestCompletionMarker": True,
                "turnLimit": turn_limit,
            }
            if last_sequence is not None:
                payload["afterSequence"] = last_sequence
            async with websockets.connect(websocket_url(http_url, ticket), open_timeout=15) as socket:
                await socket.send(json.dumps({"_tag": "Ping"}))
                await socket.send(
                    json.dumps(make_request(request_id, "orchestration.subscribeThread", payload))
                )
                while True:
                    remaining = None if deadline is None else max(0.001, deadline - time.monotonic())
                    raw_message = await asyncio.wait_for(socket.recv(), timeout=remaining)
                    message = json.loads(raw_message)
                    if message.get("_tag") == "Exit" and message.get("requestId") == request_id:
                        exit_value = message.get("exit", {})
                        if exit_value.get("_tag") == "Success":
                            if not synchronized:
                                raise RuntimeError(
                                    "T3 thread subscription ended before initial synchronization"
                                )
                            return {
                                "kind": "follow-complete",
                                "reason": "stream-ended",
                                "environment": environment,
                                "threadId": thread_id,
                                "lastSequence": last_sequence,
                                "events": emitted_events,
                                "reconnects": reconnects,
                            }
                        raise RuntimeError(
                            f"T3 thread subscription failed: {safe_error_message(exit_value)}"
                        )
                    if message.get("_tag") != "Chunk" or message.get("requestId") != request_id:
                        continue
                    for item in message.get("values", []):
                        if not isinstance(item, dict):
                            continue
                        if item.get("kind") == "snapshot" and isinstance(item.get("snapshot"), dict):
                            snapshot_thread = item["snapshot"].get("thread") or {}
                            if snapshot_thread.get("id") != thread_id:
                                raise RuntimeError(
                                    f"T3 did not return the requested thread {thread_id}"
                                )
                            synchronized = True
                            if not initial_snapshot_emitted:
                                emit_json(
                                    thread_snapshot_summary(item["snapshot"], environment),
                                    flush=True,
                                )
                                initial_snapshot_emitted = True
                            if until_terminal and is_terminal_thread_snapshot(item["snapshot"]):
                                return {
                                    "kind": "follow-complete",
                                    "reason": "terminal-state",
                                    "environment": environment,
                                    "threadId": thread_id,
                                    "lastSequence": last_sequence,
                                    "events": emitted_events,
                                    "reconnects": reconnects,
                                }
                            continue
                        if item.get("kind") == "synchronized":
                            synchronized = True
                            emit_json(
                                {
                                    "kind": "synchronized",
                                    "environment": environment,
                                    "threadId": thread_id,
                                    "lastSequence": last_sequence,
                                },
                                flush=True,
                            )
                            continue
                        if item.get("kind") != "event" or not isinstance(item.get("event"), dict):
                            continue
                        event = item["event"]
                        event_id = event.get("eventId")
                        sequence = event.get("sequence")
                        if isinstance(event_id, str) and event_id in emitted_event_ids:
                            continue
                        if isinstance(sequence, int) and last_sequence is not None and sequence <= last_sequence:
                            continue
                        if isinstance(event_id, str):
                            emitted_event_ids.add(event_id)
                        if isinstance(sequence, int):
                            last_sequence = sequence
                        emitted_events += 1
                        output_item = {"environment": environment, "threadId": thread_id, **item}
                        emit_json(output_item, flush=True)
                        if max_events is not None and emitted_events >= max_events:
                            return {
                                "kind": "follow-complete",
                                "reason": "max-events",
                                "environment": environment,
                                "threadId": thread_id,
                                "lastSequence": last_sequence,
                                "events": emitted_events,
                                "reconnects": reconnects,
                            }
                        if until_terminal and synchronized and is_terminal_thread_event(item):
                            return {
                                "kind": "follow-complete",
                                "reason": "terminal-state",
                                "environment": environment,
                                "threadId": thread_id,
                                "lastSequence": last_sequence,
                                "events": emitted_events,
                                "reconnects": reconnects,
                            }
                    await socket.send(json.dumps({"_tag": "Ack", "requestId": request_id}))
        except asyncio.TimeoutError:
            if not synchronized:
                raise RuntimeError("T3 thread subscription timed out before initial synchronization")
            return {
                "kind": "follow-complete",
                "reason": "timeout",
                "environment": environment,
                "threadId": thread_id,
                "lastSequence": last_sequence,
                "events": emitted_events,
                "reconnects": reconnects,
            }
        except (OSError, WebSocketException) as error:
            if reconnects >= retries:
                raise RuntimeError(
                    f"T3 thread subscription disconnected after {reconnects} reconnects: {error}"
                ) from error
            reconnects += 1
            await asyncio.sleep(min(2**reconnects, 5))


async def fetch_archived_shell_snapshot(http_url: str, token: str) -> dict:
    result = await rpc_call(http_url, token, "orchestration.getArchivedShellSnapshot", {})
    if not isinstance(result, dict):
        raise RuntimeError("T3 returned an invalid archived shell snapshot")
    return result


async def list_projects(store: EnvironmentStore, name: str) -> list[dict]:
    url, token = store.load(name)
    snapshot = await fetch_shell_snapshot(url, token)
    return project_rows_from_snapshot(snapshot)


async def list_threads(store: EnvironmentStore, name: str, scope: str) -> list[dict]:
    url, token = store.load(name)
    snapshots = []
    if scope in {"active", "all"}:
        snapshots.append(await fetch_shell_snapshot(url, token))
    if scope in {"archived", "all"}:
        snapshots.append(await fetch_archived_shell_snapshot(url, token))
    by_id: dict[str, dict] = {}
    for snapshot in snapshots:
        for thread in snapshot.get("threads", []):
            if isinstance(thread, dict) and isinstance(thread.get("id"), str):
                by_id[thread["id"]] = thread_row(thread, name)
    return list(by_id.values())


async def list_many_threads(
    store: EnvironmentStore,
    names: list[str],
    scope: str,
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    results = await asyncio.gather(
        *(list_threads(store, name, scope) for name in names),
        return_exceptions=True,
    )
    output: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            errors[name] = safe_error_message(result)
        else:
            output[name] = result
    return output, errors


async def search_environment(store: EnvironmentStore, name: str, query: str, limit: int) -> list[dict]:
    url, token = store.load(name)
    result = await rpc_call(
        url,
        token,
        "orchestration.searchThreads",
        {"query": query, "limit": limit},
    )
    if not isinstance(result, dict) or not isinstance(result.get("matches"), list):
        raise RuntimeError("T3 returned an invalid thread search result")
    return [{"environment": name, **match} for match in result["matches"]]


async def search_many_environments(
    store: EnvironmentStore,
    names: list[str],
    query: str,
    limit: int,
) -> tuple[list[dict], dict[str, str]]:
    results = await asyncio.gather(
        *(search_environment(store, name, query, limit) for name in names),
        return_exceptions=True,
    )
    matches: list[dict] = []
    errors: dict[str, str] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            errors[name] = safe_error_message(result)
        else:
            matches.extend(result)
    return matches, errors


def provider_row(provider: dict, environment: str, include_models: bool = True) -> dict:
    row = {
        "environment": environment,
        "instanceId": provider.get("instanceId"),
        "driver": provider.get("driver"),
        "displayName": provider.get("displayName"),
        "enabled": provider.get("enabled"),
        "installed": provider.get("installed"),
        "availability": provider.get("availability", "available"),
        "status": provider.get("status"),
        "authStatus": (provider.get("auth") or {}).get("status"),
        "version": provider.get("version"),
        "message": provider.get("message"),
        "requiresNewThreadForModelChange": provider.get("requiresNewThreadForModelChange", False),
        "supportsConversationRollback": provider.get("supportsConversationRollback", False),
        "reportsContextWindow": provider.get("reportsContextWindow", False),
        "modelCount": len(provider.get("models", [])),
    }
    if include_models:
        row["models"] = [safe_model_row(model) for model in provider.get("models", [])]
    return row


def safe_model_row(model: dict) -> dict:
    return {
        "slug": model.get("slug"),
        "name": model.get("name"),
        "shortName": model.get("shortName"),
        "subProvider": model.get("subProvider"),
        "aliases": model.get("aliases", []),
        "isDefault": bool(model.get("isDefault")),
        "isCustom": bool(model.get("isCustom")),
        "isLegacy": bool(model.get("isLegacy")),
        "capabilities": model.get("capabilities"),
    }


async def fetch_providers(store: EnvironmentStore, name: str) -> list[dict]:
    url, token = store.load(name)
    config = await rpc_call(url, token, "server.getConfig", {})
    if not isinstance(config, dict) or not isinstance(config.get("providers"), list):
        raise RuntimeError("T3 returned an invalid server configuration")
    return [provider for provider in config["providers"] if isinstance(provider, dict)]


async def fetch_many_providers(
    store: EnvironmentStore,
    names: list[str],
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    results = await asyncio.gather(
        *(fetch_providers(store, name) for name in names),
        return_exceptions=True,
    )
    output: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            errors[name] = safe_error_message(result)
        else:
            output[name] = result
    return output, errors


async def validate_project_and_model(
    store: EnvironmentStore,
    environment: str,
    project_id: str,
    instance_id: str,
    model_slug: str,
) -> tuple[dict, dict, dict]:
    require_codex_provider(instance_id)
    url, token = store.load(environment)
    shell, providers = await asyncio.gather(
        fetch_shell_snapshot(url, token),
        fetch_providers(store, environment),
    )
    project = next((item for item in shell.get("projects", []) if item.get("id") == project_id), None)
    if project is None:
        raise ValueError(f"project {project_id} does not exist in environment {environment}")
    provider = next((item for item in providers if item.get("instanceId") == instance_id), None)
    if provider is None:
        raise ValueError(f"provider instance {instance_id} does not exist in environment {environment}")
    if provider.get("availability", "available") != "available":
        raise ValueError(f"provider instance {instance_id} is unavailable")
    if provider.get("enabled") is not True or provider.get("installed") is not True:
        raise ValueError(f"provider instance {instance_id} is not enabled and installed")
    if provider.get("status") not in {"ready", "warning"}:
        raise ValueError(f"provider instance {instance_id} is not ready")
    model = next((item for item in provider.get("models", []) if item.get("slug") == model_slug), None)
    if model is None:
        raise ValueError(f"model {model_slug} is not available from provider instance {instance_id}")
    return project, provider, model


async def validate_model(
    store: EnvironmentStore,
    environment: str,
    instance_id: str,
    model_slug: str,
) -> tuple[dict, dict]:
    require_codex_provider(instance_id)
    providers = await fetch_providers(store, environment)
    provider = next((item for item in providers if item.get("instanceId") == instance_id), None)
    if provider is None:
        raise ValueError(f"provider instance {instance_id} does not exist in environment {environment}")
    if provider.get("availability", "available") != "available":
        raise ValueError(f"provider instance {instance_id} is unavailable")
    if provider.get("enabled") is not True or provider.get("installed") is not True:
        raise ValueError(f"provider instance {instance_id} is not enabled and installed")
    if provider.get("status") not in {"ready", "warning"}:
        raise ValueError(f"provider instance {instance_id} is not ready")
    model = next((item for item in provider.get("models", []) if item.get("slug") == model_slug), None)
    if model is None:
        raise ValueError(f"model {model_slug} is not available from provider instance {instance_id}")
    return provider, model


async def dispatch_command(store: EnvironmentStore, environment: str, command: dict) -> dict:
    url, token = store.load(environment)
    result = await rpc_call(url, token, "orchestration.dispatchCommand", command)
    if not isinstance(result, dict) or not isinstance(result.get("sequence"), int):
        raise RuntimeError("T3 returned an invalid command receipt")
    return result


async def wait_for_shell_thread(
    store: EnvironmentStore,
    environment: str,
    thread_id: str,
    predicate,
    timeout: float = 10,
) -> dict:
    url, token = store.load(environment)
    deadline = time.monotonic() + timeout
    while True:
        snapshot = await fetch_shell_snapshot(url, token)
        thread = next((item for item in snapshot.get("threads", []) if item.get("id") == thread_id), None)
        if thread is not None and predicate(thread):
            return thread
        if time.monotonic() >= deadline:
            raise RuntimeError(f"T3 did not confirm the requested state for thread {thread_id}")
        await asyncio.sleep(0.2)


async def find_shell_thread(
    store: EnvironmentStore,
    environment: str,
    thread_id: str,
    archived: bool,
) -> dict | None:
    url, token = store.load(environment)
    snapshot = (
        await fetch_archived_shell_snapshot(url, token)
        if archived
        else await fetch_shell_snapshot(url, token)
    )
    return next((item for item in snapshot.get("threads", []) if item.get("id") == thread_id), None)


async def find_any_shell_thread(
    store: EnvironmentStore,
    environment: str,
    thread_id: str,
) -> dict | None:
    active = await find_shell_thread(store, environment, thread_id, archived=False)
    if active is not None:
        return active
    return await find_shell_thread(store, environment, thread_id, archived=True)


async def require_thread_snapshot(
    store: EnvironmentStore,
    environment: str,
    thread_id: str,
    turn_limit: int = 5,
) -> dict:
    url, token = store.load(environment)
    snapshot = await fetch_thread_snapshot(url, token, thread_id, turn_limit)
    thread = snapshot.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        raise RuntimeError(f"T3 did not return the requested thread {thread_id}")
    if thread.get("deletedAt") is not None:
        raise ValueError(f"thread {thread_id} is deleted")
    return snapshot


def thread_matches_create_command(thread: dict, command: dict) -> bool:
    return (
        thread.get("projectId") == command["projectId"]
        and thread.get("title") == command["title"]
        and thread.get("modelSelection") == command["modelSelection"]
        and thread.get("runtimeMode") == command["runtimeMode"]
        and thread.get("interactionMode") == command["interactionMode"]
        and thread.get("branch") == command["branch"]
        and thread.get("worktreePath") == command["worktreePath"]
    )


async def wait_for_thread_message(
    store: EnvironmentStore,
    environment: str,
    thread_id: str,
    message_id: str,
    text: str,
    timeout: float = 10,
) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        snapshot = await require_thread_snapshot(store, environment, thread_id, 20)
        message = next(
            (
                message
                for message in snapshot["thread"].get("messages", [])
                if message.get("id") == message_id
            ),
            None,
        )
        if message is not None:
            if message.get("role") != "user" or message.get("text") != text:
                raise ValueError(
                    f"message {message_id} already exists but does not match the submitted task"
                )
            return snapshot
        if time.monotonic() >= deadline:
            raise RuntimeError("T3 did not confirm the submitted user message")
        await asyncio.sleep(0.2)


async def create_thread_operation(store: EnvironmentStore, args: argparse.Namespace) -> dict:
    await validate_project_and_model(
        store,
        args.environment,
        args.project_id,
        args.provider,
        args.model,
    )
    supplied_thread_id = validate_uuid(getattr(args, "thread_id", None), "--thread-id")
    command_id = validate_uuid(getattr(args, "command_id", None), "--command-id")
    thread_id = supplied_thread_id or str(uuid.uuid4())
    command = build_thread_create_command(
        thread_id=thread_id,
        project_id=args.project_id,
        title=args.title,
        selection=model_selection(args.provider, args.model),
        runtime_mode=args.runtime_mode,
        interaction_mode=args.interaction_mode,
        branch=args.branch,
        worktree_path=args.worktree_path,
        command_id=command_id,
    )
    if supplied_thread_id:
        existing = await find_any_shell_thread(store, args.environment, thread_id)
        if existing is not None:
            if not thread_matches_create_command(existing, command):
                raise ValueError(
                    f"thread {thread_id} already exists but does not match the requested configuration"
                )
            return {
                "ok": True,
                "operation": "thread.create",
                "commandId": command["commandId"],
                "receipt": None,
                "reconciled": True,
                "thread": thread_row(existing, args.environment),
            }
    if args.dry_run:
        return dry_run_result(args.environment, command)
    dispatch_error = None
    try:
        receipt = await dispatch_command(store, args.environment, command)
    except Exception as error:
        dispatch_error = error
        receipt = None
    try:
        confirmed = await wait_for_shell_thread(
            store,
            args.environment,
            thread_id,
            lambda thread: thread_matches_create_command(thread, command),
        )
    except Exception as confirmation_error:
        if dispatch_error is not None:
            raise RuntimeError(
                f"T3 thread creation result is unknown; retry with --thread-id {thread_id} "
                f"and --command-id {command['commandId']}"
            ) from dispatch_error
        raise confirmation_error
    return {
        "ok": True,
        "operation": "thread.create",
        "commandId": command["commandId"],
        "receipt": receipt,
        "reconciled": dispatch_error is not None,
        "thread": thread_row(confirmed, args.environment),
    }


def read_prompt(message: str | None, prompt_file: str | None) -> str:
    if prompt_file and message is not None:
        raise ValueError("provide either a message argument or --prompt-file, not both")
    if prompt_file:
        text = sys.stdin.read() if prompt_file == "-" else Path(prompt_file).expanduser().read_text()
    elif message is not None:
        text = message
    else:
        raise ValueError("provide a message argument or --prompt-file")
    if not text.strip():
        raise ValueError("task message cannot be empty")
    if len(text) > 120_000:
        raise ValueError("task message exceeds T3's 120000 character limit")
    return text


async def send_thread_operation(store: EnvironmentStore, args: argparse.Namespace, text: str) -> dict:
    before = await require_thread_snapshot(store, args.environment, args.thread_id, 20)
    thread = before["thread"]
    current_selection = thread.get("modelSelection") or {}
    require_codex_provider(args.provider or current_selection.get("instanceId", ""))
    selection = None
    if bool(args.provider) != bool(args.model):
        raise ValueError("--provider and --model must be supplied together")
    if args.provider and args.model:
        provider, _ = await validate_model(store, args.environment, args.provider, args.model)
        current = current_selection
        if (
            provider.get("requiresNewThreadForModelChange") is True
            and current.get("model") != args.model
        ):
            raise ValueError(f"provider {args.provider} requires a new thread to change models")
        selection = model_selection(args.provider, args.model)
    command_id = validate_uuid(getattr(args, "command_id", None), "--command-id")
    supplied_message_id = validate_uuid(getattr(args, "message_id", None), "--message-id")
    command = build_turn_start_command(
        thread_id=args.thread_id,
        text=text,
        runtime_mode=args.runtime_mode or thread.get("runtimeMode") or "approval-required",
        interaction_mode=args.interaction_mode or thread.get("interactionMode") or "default",
        selection=selection,
        command_id=command_id,
        message_id=supplied_message_id,
    )
    message_id = command["message"]["messageId"]
    existing_message = next(
        (message for message in thread.get("messages", []) if message.get("id") == message_id),
        None,
    )
    if supplied_message_id and existing_message is not None:
        if existing_message.get("role") != "user" or existing_message.get("text") != text:
            raise ValueError(
                f"message {message_id} already exists but does not match the submitted task"
            )
        return {
            "ok": True,
            "operation": "thread.turn.start",
            "commandId": command["commandId"],
            "messageId": message_id,
            "turnId": existing_message.get("turnId"),
            "receipt": None,
            "reconciled": True,
            "thread": thread_row(thread, args.environment),
        }
    if args.dry_run:
        return dry_run_result(args.environment, command)
    dispatch_error = None
    try:
        receipt = await dispatch_command(store, args.environment, command)
    except Exception as error:
        dispatch_error = error
        receipt = None
    try:
        confirmed = await wait_for_thread_message(
            store,
            args.environment,
            args.thread_id,
            message_id,
            text,
        )
    except Exception as confirmation_error:
        if dispatch_error is not None:
            raise RuntimeError(
                f"T3 task submission result is unknown; retry with --message-id {message_id} "
                f"and --command-id {command['commandId']}"
            ) from dispatch_error
        raise confirmation_error
    confirmed_message = next(
        message
        for message in confirmed["thread"].get("messages", [])
        if message.get("id") == message_id
    )
    return {
        "ok": True,
        "operation": "thread.turn.start",
        "commandId": command["commandId"],
        "messageId": message_id,
        "turnId": confirmed_message.get("turnId"),
        "receipt": receipt,
        "reconciled": dispatch_error is not None,
        "thread": thread_row(confirmed["thread"], args.environment),
    }


async def set_thread_model_operation(store: EnvironmentStore, args: argparse.Namespace) -> dict:
    await require_thread_snapshot(store, args.environment, args.thread_id)
    await validate_model(store, args.environment, args.provider, args.model)
    selection = model_selection(args.provider, args.model)
    command = build_thread_model_command(args.thread_id, selection)
    if args.dry_run:
        return dry_run_result(args.environment, command)
    receipt = await dispatch_command(store, args.environment, command)
    confirmed = await wait_for_shell_thread(
        store,
        args.environment,
        args.thread_id,
        lambda thread: thread.get("modelSelection") == selection,
    )
    return {
        "ok": True,
        "operation": "thread.meta.update",
        "commandId": command["commandId"],
        "receipt": receipt,
        "thread": thread_row(confirmed, args.environment),
    }


async def rename_thread_operation(store: EnvironmentStore, args: argparse.Namespace) -> dict:
    await require_thread_snapshot(store, args.environment, args.thread_id)
    title = args.title.strip()
    if not title:
        raise ValueError("thread title cannot be empty")
    command = build_thread_rename_command(args.thread_id, title)
    if args.dry_run:
        return dry_run_result(args.environment, command)
    receipt = await dispatch_command(store, args.environment, command)
    confirmed = await wait_for_shell_thread(
        store,
        args.environment,
        args.thread_id,
        lambda thread: thread.get("title") == title,
    )
    return {
        "ok": True,
        "operation": command["type"],
        "commandId": command["commandId"],
        "receipt": receipt,
        "thread": thread_row(confirmed, args.environment),
    }


async def archive_thread_operation(
    store: EnvironmentStore,
    args: argparse.Namespace,
    archive: bool,
) -> dict:
    before = await find_shell_thread(store, args.environment, args.thread_id, archived=not archive)
    if before is None:
        opposite = await find_shell_thread(store, args.environment, args.thread_id, archived=archive)
        if opposite is not None:
            state = "already archived" if archive else "not archived"
            raise ValueError(f"thread {args.thread_id} is {state}")
        raise ValueError(f"thread {args.thread_id} does not exist in environment {args.environment}")
    command = build_thread_lifecycle_command(
        args.thread_id,
        "archive" if archive else "unarchive",
    )
    if args.dry_run:
        return dry_run_result(args.environment, command)
    receipt = await dispatch_command(store, args.environment, command)
    deadline = time.monotonic() + 10
    while True:
        confirmed = await find_shell_thread(store, args.environment, args.thread_id, archived=archive)
        if confirmed is not None:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"T3 did not confirm archive state for thread {args.thread_id}")
        await asyncio.sleep(0.2)
    return {
        "ok": True,
        "operation": command["type"],
        "commandId": command["commandId"],
        "receipt": receipt,
        "thread": thread_row(confirmed, args.environment),
    }


async def thread_state_operation(
    store: EnvironmentStore,
    args: argparse.Namespace,
    action: str,
) -> dict:
    before = await require_thread_snapshot(store, args.environment, args.thread_id)
    thread = before["thread"]
    state_fields = {
        "pin": ("pinnedAt", True),
        "unpin": ("pinnedAt", False),
        "snooze": ("snoozedUntil", True),
        "unsnooze": ("snoozedUntil", False),
        "settle": ("settledAt", True),
        "unsettle": ("settledAt", False),
    }
    field, should_be_set = state_fields[action]
    is_set = thread.get(field) is not None
    if is_set == should_be_set:
        state = "already set" if should_be_set else "already clear"
        raise ValueError(f"thread {args.thread_id} has {field} {state}")
    extra: dict[str, object] = {}
    if action == "snooze":
        extra["snoozedUntil"] = normalize_future_datetime(args.until)
    elif action in {"unsnooze", "unsettle"}:
        extra["reason"] = "user"
    command = build_thread_lifecycle_command(args.thread_id, action, **extra)
    if args.dry_run:
        return dry_run_result(args.environment, command)
    receipt = await dispatch_command(store, args.environment, command)
    deadline = time.monotonic() + 10
    while True:
        confirmed = await require_thread_snapshot(store, args.environment, args.thread_id)
        confirmed_value = confirmed["thread"].get(field)
        matches = (
            confirmed_value == extra.get("snoozedUntil")
            if action == "snooze"
            else (confirmed_value is not None) == should_be_set
        )
        if matches:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"T3 did not confirm {field} for thread {args.thread_id}")
        await asyncio.sleep(0.2)
    return {
        "ok": True,
        "operation": command["type"],
        "commandId": command["commandId"],
        "receipt": receipt,
        "thread": thread_row(confirmed["thread"], args.environment),
    }


async def delete_thread_operation(store: EnvironmentStore, args: argparse.Namespace) -> dict:
    if args.confirm_thread_id != args.thread_id:
        raise ValueError("--confirm-thread-id must exactly match the thread ID")
    before = await find_any_shell_thread(store, args.environment, args.thread_id)
    if before is None:
        raise ValueError(f"thread {args.thread_id} does not exist in environment {args.environment}")
    if (before.get("latestTurn") or {}).get("state") == "running":
        raise ValueError("refusing to delete a thread with a running turn; stop it first")
    command = build_thread_lifecycle_command(args.thread_id, "delete")
    if args.dry_run:
        return dry_run_result(args.environment, command)
    receipt = await dispatch_command(store, args.environment, command)
    deadline = time.monotonic() + 10
    while True:
        if await find_any_shell_thread(store, args.environment, args.thread_id) is None:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"T3 did not confirm deletion of thread {args.thread_id}")
        await asyncio.sleep(0.2)
    return {
        "ok": True,
        "operation": command["type"],
        "commandId": command["commandId"],
        "receipt": receipt,
        "environment": args.environment,
        "threadId": args.thread_id,
        "deleted": True,
    }


async def thread_diff_operation(store: EnvironmentStore, args: argparse.Namespace) -> dict:
    await require_thread_snapshot(store, args.environment, args.thread_id, args.to_turn_count)
    url, token = store.load(args.environment)
    payload: dict[str, object] = {
        "threadId": args.thread_id,
        "toTurnCount": args.to_turn_count,
        "ignoreWhitespace": args.ignore_whitespace,
    }
    if args.from_turn_count is None:
        method = "orchestration.getFullThreadDiff"
    else:
        if args.from_turn_count > args.to_turn_count:
            raise ValueError("--from-turn-count cannot exceed --to-turn-count")
        payload["fromTurnCount"] = args.from_turn_count
        method = "orchestration.getTurnDiff"
    result = await rpc_call(url, token, method, payload)
    if not isinstance(result, dict) or not isinstance(result.get("diff"), str):
        raise RuntimeError("T3 returned an invalid thread diff")
    return {"ok": True, "environment": args.environment, "method": method, **result}


async def stop_thread_operation(store: EnvironmentStore, args: argparse.Namespace) -> dict:
    before = await require_thread_snapshot(store, args.environment, args.thread_id)
    thread = before["thread"]
    turn_id = (thread.get("latestTurn") or {}).get("turnId")
    if not args.session and (thread.get("latestTurn") or {}).get("state") != "running":
        raise ValueError(f"thread {args.thread_id} has no running turn to interrupt")
    command = build_thread_stop_command(args.thread_id, turn_id, args.session)
    if args.dry_run:
        return dry_run_result(args.environment, command)
    receipt = await dispatch_command(store, args.environment, command)
    if args.session:
        predicate = lambda current: (current.get("session") or {}).get("status") in {
            "stopped",
            "error",
        }
    else:
        predicate = lambda current: (current.get("latestTurn") or {}).get("state") != "running"
    confirmed = await wait_for_shell_thread(
        store,
        args.environment,
        args.thread_id,
        predicate,
    )
    return {
        "ok": True,
        "operation": command["type"],
        "commandId": command["commandId"],
        "receipt": receipt,
        "thread": thread_row(confirmed, args.environment),
    }


def require_pending_request(thread: dict, request_id: str, kind: str) -> dict:
    request = next(
        (item for item in pending_requests_from_thread(thread) if item.get("requestId") == request_id),
        None,
    )
    if request is None:
        raise ValueError(f"request {request_id} is not pending on thread {thread.get('id')}")
    if request.get("kind") != kind:
        raise ValueError(f"request {request_id} is {request.get('kind')}, not {kind}")
    return request


async def wait_for_request_cleared(
    store: EnvironmentStore,
    environment: str,
    thread_id: str,
    request_id: str,
    requested_kind: str,
    timeout: float = 10,
) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        snapshot = await require_thread_snapshot(store, environment, thread_id, 20)
        outcome = request_response_outcome(snapshot["thread"], request_id, requested_kind)
        if outcome == "resolved":
            return snapshot
        if outcome == "failed":
            raise RuntimeError(f"T3 rejected the response for request {request_id}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"T3 did not confirm resolution of request {request_id}")
        await asyncio.sleep(0.2)


async def wait_thread_operation(store: EnvironmentStore, args: argparse.Namespace) -> dict:
    target_message_id = validate_uuid(getattr(args, "message_id", None), "--message-id")
    deadline = None if args.timeout == 0 else time.monotonic() + args.timeout
    target_turn_id = None
    while True:
        snapshot = await require_thread_snapshot(store, args.environment, args.thread_id, 100)
        thread = snapshot["thread"]
        pending = pending_requests_from_thread(thread)
        if pending:
            return {
                "ok": True,
                "operation": "thread.wait",
                "reason": "attention-required",
                "environment": args.environment,
                "threadId": args.thread_id,
                "pending": pending,
                "thread": thread_row(thread, args.environment),
            }

        if target_message_id:
            message = next(
                (
                    message
                    for message in thread.get("messages", [])
                    if message.get("id") == target_message_id
                ),
                None,
            )
            if message is None:
                raise ValueError(
                    f"message {target_message_id} does not exist on thread {args.thread_id}"
                )
            if message.get("role") != "user":
                raise ValueError(f"message {target_message_id} is not a user task")
            if isinstance(message.get("turnId"), str):
                target_turn_id = message["turnId"]

        latest_turn = thread.get("latestTurn") or {}
        latest_turn_id = latest_turn.get("turnId")
        latest_state = latest_turn.get("state")
        matches_target = target_message_id is None or (
            target_turn_id is not None and latest_turn_id == target_turn_id
        )
        if matches_target and latest_state and latest_state != "running":
            ok = latest_state == "completed"
            return {
                "ok": ok,
                "operation": "thread.wait",
                "reason": "terminal-state",
                "environment": args.environment,
                "threadId": args.thread_id,
                "messageId": target_message_id,
                "turnId": latest_turn_id,
                "turnState": latest_state,
                "thread": thread_row(thread, args.environment),
            }

        if deadline is not None and time.monotonic() >= deadline:
            return {
                "ok": False,
                "operation": "thread.wait",
                "reason": "timeout",
                "environment": args.environment,
                "threadId": args.thread_id,
                "messageId": target_message_id,
                "turnId": target_turn_id,
                "thread": thread_row(thread, args.environment),
            }
        await asyncio.sleep(args.poll_interval)


async def approval_response_operation(store: EnvironmentStore, args: argparse.Namespace) -> dict:
    before = await require_thread_snapshot(store, args.environment, args.thread_id, 20)
    require_codex_provider((before["thread"].get("modelSelection") or {}).get("instanceId", ""))
    require_pending_request(before["thread"], args.request_id, "approval.requested")
    command = build_approval_response_command(args.thread_id, args.request_id, args.decision)
    if args.dry_run:
        return dry_run_result(args.environment, command)
    receipt = await dispatch_command(store, args.environment, command)
    confirmed = await wait_for_request_cleared(
        store,
        args.environment,
        args.thread_id,
        args.request_id,
        "approval.requested",
    )
    return {
        "ok": True,
        "operation": command["type"],
        "commandId": command["commandId"],
        "requestId": args.request_id,
        "decision": args.decision,
        "receipt": receipt,
        "thread": thread_row(confirmed["thread"], args.environment),
    }


def read_json_object(value: str | None, file_path: str | None) -> dict:
    if value is not None and file_path is not None:
        raise ValueError("provide either --answers-json or --answers-file, not both")
    if file_path:
        raw = sys.stdin.read() if file_path == "-" else Path(file_path).expanduser().read_text()
    elif value is not None:
        raw = value
    else:
        raise ValueError("provide --answers-json or --answers-file")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"answers must be valid JSON: {error.msg}") from error
    if not isinstance(decoded, dict):
        raise ValueError("answers must be a JSON object keyed by question ID")
    return decoded


async def user_input_response_operation(
    store: EnvironmentStore,
    args: argparse.Namespace,
    answers: dict | None,
    dismiss: bool,
) -> dict:
    before = await require_thread_snapshot(store, args.environment, args.thread_id, 20)
    require_codex_provider((before["thread"].get("modelSelection") or {}).get("instanceId", ""))
    request = require_pending_request(before["thread"], args.request_id, "user-input.requested")
    if dismiss:
        if request.get("responseMode") != "message":
            raise ValueError("only asynchronous message-mode questions can be dismissed")
        command = build_user_input_dismiss_command(args.thread_id, args.request_id)
    else:
        command = build_user_input_response_command(args.thread_id, args.request_id, answers or {})
    if args.dry_run:
        return dry_run_result(args.environment, command)
    receipt = await dispatch_command(store, args.environment, command)
    confirmed = await wait_for_request_cleared(
        store,
        args.environment,
        args.thread_id,
        args.request_id,
        "user-input.requested",
    )
    result = {
        "ok": True,
        "operation": command["type"],
        "commandId": command["commandId"],
        "requestId": args.request_id,
        "receipt": receipt,
        "thread": thread_row(confirmed["thread"], args.environment),
    }
    if not dismiss:
        result["answeredQuestionIds"] = sorted((answers or {}).keys())
    return result


async def list_many_projects(
    store: EnvironmentStore,
    names: list[str],
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    results = await asyncio.gather(*(list_projects(store, name) for name in names), return_exceptions=True)
    output: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            errors[name] = safe_error_message(result)
        else:
            output[name] = result
    return output, errors


def safe_environment_config(store: EnvironmentStore) -> dict[str, dict[str, object]]:
    safe: dict[str, dict[str, object]] = {}
    for name, entry in store.load_config().items():
        url = entry.get("url") if isinstance(entry, dict) else None
        safe[name] = {"url": url}
    return safe


async def check_environment(store: EnvironmentStore, name: str) -> dict:
    row: dict[str, object] = {
        "environment": name,
        "url": None,
        "authenticated": False,
        "rpc": False,
    }
    try:
        url, token = store.load(name)
        row["url"] = url
        session = await asyncio.to_thread(http_json, f"{url}/api/auth/session", token)
        if session.get("authenticated") is not True:
            raise RuntimeError("stored T3 session is not authenticated")
        row["authenticated"] = True
        await rpc_call(url, token, "server.probe", {})
        row["rpc"] = True
    except Exception as error:
        row["error"] = safe_error_message(error)
    return row


async def check_many_environments(store: EnvironmentStore, names: list[str]) -> list[dict]:
    return list(await asyncio.gather(*(check_environment(store, name) for name in names)))


async def check_compatibility(store: EnvironmentStore, name: str) -> dict:
    row: dict[str, object] = {
        "environment": name,
        "compatible": False,
        "checks": {
            "authentication": False,
            "serverProbe": False,
            "activeShell": False,
            "archivedShell": False,
            "providerConfig": False,
        },
    }
    try:
        url, token = store.load(name)
        row["url"] = url
        session = await asyncio.to_thread(http_json, f"{url}/api/auth/session", token)
        if session.get("authenticated") is not True:
            raise RuntimeError("stored T3 session is not authenticated")
        row["checks"]["authentication"] = True
        await rpc_call(url, token, "server.probe", {})
        row["checks"]["serverProbe"] = True
        active = await fetch_shell_snapshot(url, token)
        if not isinstance(active.get("threads"), list) or not isinstance(active.get("projects"), list):
            raise RuntimeError("T3 active shell snapshot has an incompatible shape")
        row["checks"]["activeShell"] = True
        active_threads = active.get("threads", [])
        thread_snapshot_checked = False
        if active_threads:
            thread_id = active_threads[0].get("id")
            if isinstance(thread_id, str) and thread_id:
                thread_snapshot = await fetch_thread_snapshot(url, token, thread_id, 1)
                if (thread_snapshot.get("thread") or {}).get("id") != thread_id:
                    raise RuntimeError("T3 thread snapshot has an incompatible shape")
                thread_snapshot_checked = True
        archived = await fetch_archived_shell_snapshot(url, token)
        if not isinstance(archived.get("threads"), list):
            raise RuntimeError("T3 archived shell snapshot has an incompatible shape")
        row["checks"]["archivedShell"] = True
        config = await rpc_call(url, token, "server.getConfig", {})
        if not isinstance(config, dict) or not isinstance(config.get("providers"), list):
            raise RuntimeError("T3 provider configuration has an incompatible shape")
        row["checks"]["providerConfig"] = True
        row["authMethods"] = list((session.get("auth") or {}).get("sessionMethods", []))
        row["providerInstances"] = len(config["providers"])
        row["compatible"] = True
        row["capabilities"] = {
            "projectAndThreadDiscovery": "verified-now",
            "providerAndModelDiscovery": "verified-now",
            "threadSnapshot": (
                "verified-now" if thread_snapshot_checked else "not-applicable-no-active-thread"
            ),
            "threadEventStreaming": "supported-not-checked-now",
            "taskDispatch": "supported-not-checked-now",
            "approvalsAndInput": "supported-not-checked-now",
            "threadLifecycle": "supported-not-checked-now",
            "historyAndDiffs": "supported-not-checked-now",
        }
    except Exception as error:
        row["error"] = safe_error_message(error)
    return row


async def check_many_compatibility(store: EnvironmentStore, names: list[str]) -> list[dict]:
    return list(await asyncio.gather(*(check_compatibility(store, name) for name in names)))


def configure_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    if args.token_file:
        token = Path(args.token_file).expanduser().read_text().strip()
    else:
        token = getpass.getpass("T3 bearer token (input hidden): ").strip()
    if not token:
        raise ValueError("bearer token cannot be empty")
    store.save(args.name, args.url, token)
    emit_json({"ok": True, "configured": args.name, "url": normalize_url(args.url)})
    return 0


def read_pairing_credential() -> str:
    return input("One-time T3 pairing token: ").strip()


def pair_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    url = normalize_url(args.url)
    credential = read_pairing_credential()
    if not credential:
        raise ValueError("pairing token cannot be empty")
    token = bootstrap_bearer(url, credential)
    session = http_json(f"{url}/api/auth/session", token)
    if session.get("authenticated") is not True:
        raise RuntimeError("issued T3 bearer session failed verification")
    store.save(args.name, url, token)
    emit_json({"ok": True, "paired": args.name, "url": url, "authenticated": True})
    return 0


def remove_environment_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    if args.confirm_name != args.name:
        raise ValueError("--confirm-name must exactly match the environment name")
    if args.name not in store.names():
        raise KeyError(f"unknown environment: {args.name}")
    if args.dry_run:
        emit_json(
            {
                "ok": True,
                "dryRun": True,
                "operation": "environment.remove",
                "planned": {"environment": args.name, "recoverableBackup": True},
            },
            indent=2,
        )
        return 0
    backup = store.remove(args.name)
    emit_json(
        {
            "ok": True,
            "removed": args.name,
            "recoverableBackup": str(backup),
        },
        indent=2,
    )
    return 0


def projects_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    names = store.names() if not args.environment or args.environment == ["all"] else args.environment
    if not names:
        raise RuntimeError("no environments configured")
    projects_by_environment, errors = asyncio.run(list_many_projects(store, names))
    result = {
        "ok": not errors,
        "counts": {name: len(projects) for name, projects in projects_by_environment.items()},
        "total": sum(len(projects) for projects in projects_by_environment.values()),
        "projects": merge_environment_projects(projects_by_environment),
        "errors": errors,
    }
    emit_json(result, indent=2)
    return 0 if not errors else 1


def health_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    names = store.names() if not args.environment or args.environment == ["all"] else args.environment
    if not names:
        raise RuntimeError("no environments configured")
    rows = asyncio.run(check_many_environments(store, names))
    ok = all(row.get("authenticated") is True and row.get("rpc") is True for row in rows)
    emit_json({"ok": ok, "environments": rows}, indent=2)
    return 0 if ok else 1


def compatibility_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    names = store.names() if not args.environment or args.environment == ["all"] else args.environment
    if not names:
        raise RuntimeError("no environments configured")
    rows = asyncio.run(check_many_compatibility(store, names))
    ok = all(row.get("compatible") is True for row in rows)
    emit_json({"ok": ok, "environments": rows}, indent=2)
    return 0 if ok else 1


def threads_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    names = store.names() if not args.environment or args.environment == ["all"] else args.environment
    if not names:
        raise RuntimeError("no environments configured")
    threads_by_environment, errors = asyncio.run(list_many_threads(store, names, args.scope))
    rows = [row for environment_rows in threads_by_environment.values() for row in environment_rows]
    rows = filter_thread_rows(rows, args.project, args.turn_state, args.pending)
    if args.limit is not None:
        rows = rows[: args.limit]
    result = {
        "ok": not errors,
        "count": len(rows),
        "threads": rows,
        "errors": errors,
    }
    emit_json(result, indent=2)
    return 0 if not errors else 1


def thread_show_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    url, token = store.load(args.environment)
    snapshot = asyncio.run(fetch_thread_snapshot(url, token, args.thread_id, args.turn_limit))
    emit_json({"ok": True, "environment": args.environment, **snapshot}, indent=2)
    return 0


def thread_follow_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    result = asyncio.run(
        follow_thread(
            store,
            args.environment,
            args.thread_id,
            after_sequence=args.after_sequence,
            timeout=args.timeout,
            max_events=args.max_events,
            retries=args.retries,
            until_terminal=args.until_terminal,
            turn_limit=args.turn_limit,
        )
    )
    emit_json(result, flush=True)
    return 0


def thread_create_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    result = asyncio.run(create_thread_operation(store, args))
    emit_json(result, indent=2)
    return 0


def thread_send_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    text = read_prompt(args.message, args.prompt_file)
    result = asyncio.run(send_thread_operation(store, args, text))
    emit_json(result, indent=2)
    return 0


def thread_model_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    result = asyncio.run(set_thread_model_operation(store, args))
    emit_json(result, indent=2)
    return 0


def thread_rename_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    result = asyncio.run(rename_thread_operation(store, args))
    emit_json(result, indent=2)
    return 0


def thread_archive_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    result = asyncio.run(archive_thread_operation(store, args, True))
    emit_json(result, indent=2)
    return 0


def thread_unarchive_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    result = asyncio.run(archive_thread_operation(store, args, False))
    emit_json(result, indent=2)
    return 0


def thread_state_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    result = asyncio.run(thread_state_operation(store, args, args.thread_command))
    emit_json(result, indent=2)
    return 0


def thread_delete_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    result = asyncio.run(delete_thread_operation(store, args))
    emit_json(result, indent=2)
    return 0


def thread_history_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    snapshot = asyncio.run(
        require_thread_snapshot(store, args.environment, args.thread_id, args.turn_limit)
    )
    thread = snapshot["thread"]
    emit_json(
        {
            "ok": True,
            "environment": args.environment,
            "threadId": args.thread_id,
            "messages": thread.get("messages", []),
            "activities": thread.get("activities", []),
            "checkpoints": thread.get("checkpoints", []),
            "proposedPlans": thread.get("proposedPlans", []),
        },
        indent=2,
    )
    return 0


def thread_diff_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    result = asyncio.run(thread_diff_operation(store, args))
    emit_json(result, indent=2)
    return 0


def thread_pending_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    snapshot = asyncio.run(
        require_thread_snapshot(store, args.environment, args.thread_id, args.turn_limit)
    )
    pending = pending_requests_from_thread(snapshot["thread"])
    emit_json(
        {
            "ok": True,
            "environment": args.environment,
            "threadId": args.thread_id,
            "count": len(pending),
            "pending": pending,
        },
        indent=2,
    )
    return 0


def thread_wait_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    result = asyncio.run(wait_thread_operation(store, args))
    emit_json(result, indent=2)
    return 0 if result.get("ok") is True else 1


def thread_stop_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    result = asyncio.run(stop_thread_operation(store, args))
    emit_json(result, indent=2)
    return 0


def thread_approve_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    result = asyncio.run(approval_response_operation(store, args))
    emit_json(result, indent=2)
    return 0


def thread_answer_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    answers = read_json_object(args.answers_json, args.answers_file)
    result = asyncio.run(user_input_response_operation(store, args, answers, False))
    emit_json(result, indent=2)
    return 0


def thread_dismiss_input_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    result = asyncio.run(user_input_response_operation(store, args, None, True))
    emit_json(result, indent=2)
    return 0


def search_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    names = store.names() if not args.environment or args.environment == ["all"] else args.environment
    if not names:
        raise RuntimeError("no environments configured")
    matches, errors = asyncio.run(search_many_environments(store, names, args.query, args.limit))
    matches.sort(key=lambda match: str(match.get("messageCreatedAt", "")), reverse=True)
    emit_json({"ok": not errors, "count": len(matches), "matches": matches, "errors": errors}, indent=2)
    return 0 if not errors else 1


def providers_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    names = store.names() if not args.environment or args.environment == ["all"] else args.environment
    if not names:
        raise RuntimeError("no environments configured")
    providers_by_environment, errors = asyncio.run(fetch_many_providers(store, names))
    rows = [
        provider_row(provider, environment)
        for environment, providers in providers_by_environment.items()
        for provider in providers
    ]
    if not args.include_disabled:
        rows = [row for row in rows if row.get("enabled") is True]
    rows.sort(key=lambda row: (str(row.get("environment")), str(row.get("instanceId"))))
    emit_json({"ok": not errors, "count": len(rows), "providers": rows, "errors": errors}, indent=2)
    return 0 if not errors else 1


def models_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    names = store.names() if not args.environment or args.environment == ["all"] else args.environment
    if not names:
        raise RuntimeError("no environments configured")
    providers_by_environment, errors = asyncio.run(fetch_many_providers(store, names))
    rows = []
    for environment, providers in providers_by_environment.items():
        for provider in providers:
            if args.provider and provider.get("instanceId") != args.provider:
                continue
            if not args.include_disabled and provider.get("enabled") is not True:
                continue
            for model in provider.get("models", []):
                rows.append(
                    {
                        "environment": environment,
                        "instanceId": provider.get("instanceId"),
                        "driver": provider.get("driver"),
                        "providerStatus": provider.get("status"),
                        **safe_model_row(model),
                    }
                )
    rows.sort(
        key=lambda row: (
            str(row.get("environment")),
            str(row.get("instanceId")),
            not bool(row.get("isDefault")),
            str(row.get("name")),
        )
    )
    emit_json({"ok": not errors, "count": len(rows), "models": rows, "errors": errors}, indent=2)
    return 0 if not errors else 1


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(
            json.dumps(
                {
                    "schemaVersion": OUTPUT_SCHEMA_VERSION,
                    "ok": False,
                    "error": {"category": "usage", "message": safe_error_message(message)},
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="Native T3 HTTP/WebSocket RPC controller")
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME, help="controller state directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_dry_run(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--dry-run", action="store_true", help="validate and preview without changing T3")

    configure = subparsers.add_parser("configure", help="save a named T3 bearer environment")
    configure.add_argument("name")
    configure.add_argument("--url", required=True)
    configure.add_argument("--token-file", help="read token from a file; otherwise prompt securely")
    configure.set_defaults(handler=configure_command)

    pair = subparsers.add_parser("pair", help="exchange a one-time pairing token for a bearer session")
    pair.add_argument("name")
    pair.add_argument("--url", required=True)
    pair.set_defaults(handler=pair_command)

    remove_environment = subparsers.add_parser("remove-environment", help="remove and locally back up one environment")
    remove_environment.add_argument("name")
    remove_environment.add_argument("--confirm-name", required=True, help="must exactly repeat NAME")
    add_dry_run(remove_environment)
    remove_environment.set_defaults(handler=remove_environment_command)

    environments = subparsers.add_parser("environments", help="list configured environments")
    environments.set_defaults(
        handler=lambda store, args: emit_json(
            {
                "ok": True,
                "count": len(store.names()),
                "environments": safe_environment_config(store),
            },
            indent=2,
        )
        or 0
    )

    health = subparsers.add_parser("health", help="check authentication and RPC access")
    health.add_argument("--environment", action="append")
    health.set_defaults(handler=health_command)

    compatibility = subparsers.add_parser("compatibility", help="check the T3 protocol features required by this controller")
    compatibility.add_argument("--environment", action="append")
    compatibility.set_defaults(handler=compatibility_command)

    projects = subparsers.add_parser("projects", help="list T3 projects over native RPC")
    projects.add_argument("--environment", action="append")
    projects.set_defaults(handler=projects_command)

    threads = subparsers.add_parser("threads", help="list and filter T3 threads")
    threads.add_argument("--environment", action="append")
    threads.add_argument("--project", help="only return threads in this exact project ID")
    threads.add_argument("--scope", choices=["active", "archived", "all"], default="active")
    threads.add_argument("--turn-state", choices=["running", "completed", "interrupted", "error"])
    threads.add_argument("--pending", choices=["approvals", "input", "any"])
    threads.add_argument("--limit", type=positive_int)
    threads.set_defaults(handler=threads_command)

    thread = subparsers.add_parser("thread", help="inspect or control one exact T3 thread")
    thread_commands = thread.add_subparsers(dest="thread_command", required=True)
    thread_show = thread_commands.add_parser("show", help="show detailed thread state and history")
    thread_show.add_argument("environment")
    thread_show.add_argument("thread_id")
    thread_show.add_argument("--turn-limit", type=positive_int, default=20)
    thread_show.set_defaults(handler=thread_show_command)

    thread_follow = thread_commands.add_parser("follow", help="stream ordered thread updates as JSON Lines")
    thread_follow.add_argument("environment")
    thread_follow.add_argument("thread_id")
    thread_follow.add_argument("--after-sequence", type=nonnegative_int)
    thread_follow.add_argument("--timeout", type=nonnegative_float, default=60)
    thread_follow.add_argument("--max-events", type=positive_int)
    thread_follow.add_argument("--retries", type=nonnegative_int, default=3)
    thread_follow.add_argument("--until-terminal", action="store_true")
    thread_follow.add_argument("--turn-limit", type=positive_int, default=20)
    thread_follow.set_defaults(handler=thread_follow_command)

    thread_create = thread_commands.add_parser("create", help="create a thread with an explicit provider and model")
    thread_create.add_argument("environment")
    thread_create.add_argument("project_id")
    thread_create.add_argument("--title", required=True)
    thread_create.add_argument("--provider", required=True)
    thread_create.add_argument("--model", required=True)
    thread_create.add_argument(
        "--runtime-mode",
        choices=["approval-required", "auto-accept-edits", "auto", "full-access"],
        default="approval-required",
    )
    thread_create.add_argument("--interaction-mode", choices=["default", "plan"], default="default")
    thread_create.add_argument("--branch")
    thread_create.add_argument("--worktree-path")
    thread_create.add_argument(
        "--thread-id",
        help="reuse this UUID to reconcile an uncertain earlier creation",
    )
    thread_create.add_argument(
        "--command-id",
        help="reuse this UUID with --thread-id after an uncertain earlier creation",
    )
    add_dry_run(thread_create)
    thread_create.set_defaults(handler=thread_create_command)

    thread_send = thread_commands.add_parser("send", help="send a task to an existing thread")
    thread_send.add_argument("environment")
    thread_send.add_argument("thread_id")
    thread_send.add_argument("message", nargs="?")
    thread_send.add_argument("--prompt-file", help="read the task from a file, or use - for standard input")
    thread_send.add_argument("--provider")
    thread_send.add_argument("--model")
    thread_send.add_argument(
        "--runtime-mode",
        choices=["approval-required", "auto-accept-edits", "auto", "full-access"],
    )
    thread_send.add_argument("--interaction-mode", choices=["default", "plan"])
    thread_send.add_argument(
        "--message-id",
        help="reuse this UUID to reconcile an uncertain earlier submission",
    )
    thread_send.add_argument(
        "--command-id",
        help="reuse this UUID with --message-id after an uncertain earlier submission",
    )
    add_dry_run(thread_send)
    thread_send.set_defaults(handler=thread_send_command)

    thread_model = thread_commands.add_parser("model", help="change the provider and model for a thread")
    thread_model.add_argument("environment")
    thread_model.add_argument("thread_id")
    thread_model.add_argument("--provider", required=True)
    thread_model.add_argument("--model", required=True)
    add_dry_run(thread_model)
    thread_model.set_defaults(handler=thread_model_command)

    thread_rename = thread_commands.add_parser("rename", help="rename a thread and verify its title")
    thread_rename.add_argument("environment")
    thread_rename.add_argument("thread_id")
    thread_rename.add_argument("title")
    add_dry_run(thread_rename)
    thread_rename.set_defaults(handler=thread_rename_command)

    thread_archive = thread_commands.add_parser("archive", help="archive a thread")
    thread_archive.add_argument("environment")
    thread_archive.add_argument("thread_id")
    add_dry_run(thread_archive)
    thread_archive.set_defaults(handler=thread_archive_command)

    thread_unarchive = thread_commands.add_parser("unarchive", help="restore an archived thread")
    thread_unarchive.add_argument("environment")
    thread_unarchive.add_argument("thread_id")
    add_dry_run(thread_unarchive)
    thread_unarchive.set_defaults(handler=thread_unarchive_command)

    for action, help_text in (
        ("pin", "pin a thread"),
        ("unpin", "remove a thread from the pinned group"),
        ("unsnooze", "wake a snoozed thread now"),
        ("settle", "move a thread to the settled shelf"),
        ("unsettle", "return a settled thread to the active list"),
    ):
        state_parser = thread_commands.add_parser(action, help=help_text)
        state_parser.add_argument("environment")
        state_parser.add_argument("thread_id")
        add_dry_run(state_parser)
        state_parser.set_defaults(handler=thread_state_command)

    thread_snooze = thread_commands.add_parser("snooze", help="snooze a thread until a future time")
    thread_snooze.add_argument("environment")
    thread_snooze.add_argument("thread_id")
    thread_snooze.add_argument("--until", required=True, help="ISO 8601 date and time with timezone")
    add_dry_run(thread_snooze)
    thread_snooze.set_defaults(handler=thread_state_command)

    thread_delete = thread_commands.add_parser("delete", help="permanently delete one stopped thread")
    thread_delete.add_argument("environment")
    thread_delete.add_argument("thread_id")
    thread_delete.add_argument(
        "--confirm-thread-id",
        required=True,
        help="must exactly repeat THREAD_ID",
    )
    add_dry_run(thread_delete)
    thread_delete.set_defaults(handler=thread_delete_command)

    thread_history = thread_commands.add_parser("history", help="read messages, activities, checkpoints, and plans")
    thread_history.add_argument("environment")
    thread_history.add_argument("thread_id")
    thread_history.add_argument("--turn-limit", type=positive_int, default=100)
    thread_history.set_defaults(handler=thread_history_command)

    thread_diff = thread_commands.add_parser("diff", help="read the filesystem diff through a turn count")
    thread_diff.add_argument("environment")
    thread_diff.add_argument("thread_id")
    thread_diff.add_argument("--to-turn-count", type=nonnegative_int, required=True)
    thread_diff.add_argument("--from-turn-count", type=nonnegative_int)
    thread_diff.add_argument("--ignore-whitespace", action="store_true")
    thread_diff.set_defaults(handler=thread_diff_command)

    thread_pending = thread_commands.add_parser("pending", help="list unresolved approvals and questions")
    thread_pending.add_argument("environment")
    thread_pending.add_argument("thread_id")
    thread_pending.add_argument("--turn-limit", type=positive_int, default=20)
    thread_pending.set_defaults(handler=thread_pending_command)

    thread_wait = thread_commands.add_parser(
        "wait",
        help="wait until a thread finishes or needs approval or input",
    )
    thread_wait.add_argument("environment")
    thread_wait.add_argument("thread_id")
    thread_wait.add_argument(
        "--message-id",
        help="wait for the exact user task created by thread send",
    )
    thread_wait.add_argument("--timeout", type=nonnegative_float, default=600)
    thread_wait.add_argument("--poll-interval", type=positive_float, default=0.5)
    thread_wait.set_defaults(handler=thread_wait_command)

    thread_stop = thread_commands.add_parser("stop", help="interrupt a running turn or stop its provider session")
    thread_stop.add_argument("environment")
    thread_stop.add_argument("thread_id")
    thread_stop.add_argument("--session", action="store_true", help="stop the provider session instead of only the active turn")
    add_dry_run(thread_stop)
    thread_stop.set_defaults(handler=thread_stop_command)

    thread_approve = thread_commands.add_parser("approve", help="answer one exact pending approval")
    thread_approve.add_argument("environment")
    thread_approve.add_argument("thread_id")
    thread_approve.add_argument("request_id")
    thread_approve.add_argument(
        "decision",
        choices=["accept", "acceptForSession", "acceptAlways", "decline", "cancel"],
    )
    add_dry_run(thread_approve)
    thread_approve.set_defaults(handler=thread_approve_command)

    thread_answer = thread_commands.add_parser("answer", help="answer one exact pending user-input request")
    thread_answer.add_argument("environment")
    thread_answer.add_argument("thread_id")
    thread_answer.add_argument("request_id")
    thread_answer.add_argument("--answers-json")
    thread_answer.add_argument("--answers-file", help="read a JSON object from a file, or use - for standard input")
    add_dry_run(thread_answer)
    thread_answer.set_defaults(handler=thread_answer_command)

    thread_dismiss = thread_commands.add_parser("dismiss-input", help="dismiss an asynchronous message-mode question")
    thread_dismiss.add_argument("environment")
    thread_dismiss.add_argument("thread_id")
    thread_dismiss.add_argument("request_id")
    add_dry_run(thread_dismiss)
    thread_dismiss.set_defaults(handler=thread_dismiss_input_command)

    search = subparsers.add_parser("search", help="search user and assistant thread messages")
    search.add_argument("query")
    search.add_argument("--environment", action="append")
    search.add_argument("--limit", type=int, default=20, choices=range(1, 51), metavar="1..50")
    search.set_defaults(handler=search_command)

    providers = subparsers.add_parser("providers", help="list provider instances and capabilities")
    providers.add_argument("--environment", action="append")
    providers.add_argument("--include-disabled", action="store_true")
    providers.set_defaults(handler=providers_command)

    models = subparsers.add_parser("models", help="list models available from provider instances")
    models.add_argument("--environment", action="append")
    models.add_argument("--provider", help="only return models for this provider instance ID")
    models.add_argument("--include-disabled", action="store_true")
    models.set_defaults(handler=models_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = EnvironmentStore(args.home)
    try:
        return args.handler(store, args)
    except Exception as error:
        print(
            json.dumps(
                {
                    "schemaVersion": OUTPUT_SCHEMA_VERSION,
                    "ok": False,
                    "error": {
                        "category": error_category(error),
                        "message": safe_error_message(error),
                    },
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
