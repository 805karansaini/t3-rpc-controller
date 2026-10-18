"""T3 command payloads, validation, and public result shapes."""

import uuid
from datetime import datetime, timezone


CODEX_PROVIDER_INSTANCE = "codex"


def require_codex_provider(instance_id: str) -> None:
    if instance_id != CODEX_PROVIDER_INSTANCE:
        raise ValueError(
            f"live task execution is restricted to provider instance {CODEX_PROVIDER_INSTANCE}"
        )


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
