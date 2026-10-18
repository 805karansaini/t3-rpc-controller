"""T3 reads and verified thread mutations."""

import argparse
import asyncio
import time
import uuid

from t3_controller.domain import (
    build_approval_response_command,
    build_thread_create_command,
    build_thread_lifecycle_command,
    build_thread_model_command,
    build_thread_rename_command,
    build_thread_stop_command,
    build_turn_start_command,
    build_user_input_dismiss_command,
    build_user_input_response_command,
    dry_run_result,
    model_selection,
    normalize_future_datetime,
    pending_requests_from_thread,
    project_rows_from_snapshot,
    request_response_outcome,
    require_codex_provider,
    thread_row,
    validate_uuid,
)
from t3_controller.output import UncertainDispatchError, safe_error_message
from t3_controller.state import EnvironmentStore
from t3_controller.transport import (
    fetch_shell_snapshot,
    fetch_thread_snapshot,
    http_json,
    rpc_call,
)


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
    timeout: float = 30,
) -> dict:
    url, token = store.load(environment)
    snapshot = await fetch_thread_snapshot(
        url, token, thread_id, turn_limit, timeout=timeout
    )
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
        raise UncertainDispatchError(
            "T3 thread creation may have succeeded, but confirmation failed",
            {"threadId": thread_id, "commandId": command["commandId"]},
        ) from (dispatch_error or confirmation_error)
    return {
        "ok": True,
        "operation": "thread.create",
        "commandId": command["commandId"],
        "receipt": receipt,
        "reconciled": dispatch_error is not None,
        "thread": thread_row(confirmed, args.environment),
    }


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
        raise UncertainDispatchError(
            "T3 task submission may have succeeded, but confirmation failed",
            {"messageId": message_id, "commandId": command["commandId"]},
        ) from (dispatch_error or confirmation_error)
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
    last_thread = None

    def timeout_result() -> dict:
        result = {
            "ok": False,
            "operation": "thread.wait",
            "reason": "timeout",
            "environment": args.environment,
            "threadId": args.thread_id,
            "messageId": target_message_id,
            "turnId": target_turn_id,
        }
        if last_thread is not None:
            result["thread"] = thread_row(last_thread, args.environment)
        return result

    while True:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return timeout_result()
        try:
            if remaining is None:
                snapshot = await require_thread_snapshot(
                    store, args.environment, args.thread_id, 100
                )
            else:
                async with asyncio.timeout(remaining):
                    snapshot = await require_thread_snapshot(
                        store,
                        args.environment,
                        args.thread_id,
                        100,
                        timeout=remaining,
                    )
        except asyncio.TimeoutError:
            return timeout_result()
        thread = snapshot["thread"]
        last_thread = thread
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

        pending = pending_requests_from_thread(thread)
        if target_message_id:
            pending = [
                request
                for request in pending
                if target_turn_id is not None and request.get("turnId") == target_turn_id
            ]
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

        latest_turn = thread.get("latestTurn") or {}
        latest_turn_id = latest_turn.get("turnId")
        latest_state = latest_turn.get("state")
        matches_target = target_message_id is None or (
            target_turn_id is not None and latest_turn_id == target_turn_id
        )
        target_state = latest_state if matches_target else None
        if target_state in {"completed", "interrupted", "error"}:
            ok = target_state == "completed"
            return {
                "ok": ok,
                "operation": "thread.wait",
                "reason": "terminal-state",
                "environment": args.environment,
                "threadId": args.thread_id,
                "messageId": target_message_id,
                "turnId": target_turn_id or latest_turn_id,
                "turnState": target_state,
                "thread": thread_row(thread, args.environment),
            }

        if (
            target_message_id
            and target_turn_id
            and isinstance(latest_turn_id, str)
            and latest_turn_id != target_turn_id
        ):
            return {
                "ok": False,
                "operation": "thread.wait",
                "reason": "superseded",
                "environment": args.environment,
                "threadId": args.thread_id,
                "messageId": target_message_id,
                "turnId": target_turn_id,
                "turnState": "unknown",
                "thread": thread_row(thread, args.environment),
            }

        if deadline is not None and time.monotonic() >= deadline:
            return timeout_result()
        sleep_for = args.poll_interval
        if deadline is not None:
            sleep_for = min(sleep_for, max(0, deadline - time.monotonic()))
        await asyncio.sleep(sleep_for)


async def run_thread_operation(
    store: EnvironmentStore,
    args: argparse.Namespace,
    text: str,
) -> dict:
    submission = await send_thread_operation(store, args, text)
    if submission.get("dryRun") is True:
        return {
            "ok": True,
            "dryRun": True,
            "operation": "thread.run",
            "submission": submission,
            "wait": {"planned": True, "timeout": args.timeout},
        }
    wait_args = argparse.Namespace(
        environment=args.environment,
        thread_id=args.thread_id,
        message_id=submission["messageId"],
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )
    outcome = await wait_thread_operation(store, wait_args)
    return {
        "ok": outcome.get("ok") is True,
        "operation": "thread.run",
        "submission": {
            "commandId": submission["commandId"],
            "messageId": submission["messageId"],
            "turnId": submission.get("turnId"),
            "reconciled": submission.get("reconciled", False),
        },
        "outcome": outcome,
    }


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
