"""Command-line parsing, input, and JSON output for t3ctl."""

import argparse
import asyncio
import getpass
import json
import math
import sys
from pathlib import Path

from t3_controller import __version__
from t3_controller.auth import bootstrap_bearer
from t3_controller.domain import (
    filter_thread_rows,
    merge_environment_projects,
    pending_requests_from_thread,
    provider_row,
    safe_model_row,
)
from t3_controller.monitoring import follow_thread
from t3_controller.operations import (
    approval_response_operation,
    archive_thread_operation,
    check_many_compatibility,
    check_many_environments,
    create_thread_operation,
    delete_thread_operation,
    fetch_many_providers,
    list_many_projects,
    list_many_threads,
    rename_thread_operation,
    require_thread_snapshot,
    run_thread_operation,
    safe_environment_config,
    search_many_environments,
    send_thread_operation,
    set_thread_model_operation,
    stop_thread_operation,
    thread_diff_operation,
    thread_state_operation,
    user_input_response_operation,
    wait_thread_operation,
)
from t3_controller.output import (
    OUTPUT_SCHEMA_VERSION,
    UncertainDispatchError,
    emit_json,
    error_category,
    safe_error_message,
)
from t3_controller.state import EnvironmentStore, default_home, normalize_url
from t3_controller.transport import fetch_thread_snapshot, http_json


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


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be finite and zero or greater")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return parsed


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


def selected_environments(store: EnvironmentStore, requested: list[str] | None) -> list[str]:
    names = store.names() if not requested or requested == ["all"] else requested
    if not names:
        raise RuntimeError("no environments configured")
    return list(dict.fromkeys(names))


def projects_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    names = selected_environments(store, args.environment)
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
    names = selected_environments(store, args.environment)
    rows = asyncio.run(check_many_environments(store, names))
    ok = all(row.get("authenticated") is True and row.get("rpc") is True for row in rows)
    emit_json({"ok": ok, "environments": rows}, indent=2)
    return 0 if ok else 1


def compatibility_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    names = selected_environments(store, args.environment)
    rows = asyncio.run(check_many_compatibility(store, names))
    ok = all(row.get("compatible") is True for row in rows)
    emit_json({"ok": ok, "environments": rows}, indent=2)
    return 0 if ok else 1


def threads_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    names = selected_environments(store, args.environment)
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


def thread_run_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    text = read_prompt(args.message, args.prompt_file)
    result = asyncio.run(run_thread_operation(store, args, text))
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
    names = selected_environments(store, args.environment)
    matches, errors = asyncio.run(search_many_environments(store, names, args.query, args.limit))
    matches.sort(key=lambda match: str(match.get("messageCreatedAt", "")), reverse=True)
    emit_json({"ok": not errors, "count": len(matches), "matches": matches, "errors": errors}, indent=2)
    return 0 if not errors else 1


def providers_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    names = selected_environments(store, args.environment)
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
    names = selected_environments(store, args.environment)
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
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--home", type=Path, default=default_home(), help="controller state directory")
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

    thread_run = thread_commands.add_parser(
        "run",
        help="submit one task and wait until it finishes or needs attention",
    )
    thread_run.add_argument("environment")
    thread_run.add_argument("thread_id")
    thread_run.add_argument("message", nargs="?")
    thread_run.add_argument("--prompt-file", help="read the task from a file, or use - for standard input")
    thread_run.add_argument("--provider")
    thread_run.add_argument("--model")
    thread_run.add_argument(
        "--runtime-mode",
        choices=["approval-required", "auto-accept-edits", "auto", "full-access"],
    )
    thread_run.add_argument("--interaction-mode", choices=["default", "plan"])
    thread_run.add_argument("--message-id", help="reuse this UUID after an uncertain submission")
    thread_run.add_argument("--command-id", help="reuse this UUID after an uncertain submission")
    thread_run.add_argument("--timeout", type=nonnegative_float, default=600)
    thread_run.add_argument("--poll-interval", type=positive_float, default=0.5)
    add_dry_run(thread_run)
    thread_run.set_defaults(handler=thread_run_command)

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
        details = {}
        if isinstance(error, UncertainDispatchError):
            details["retry"] = error.retry
        print(
            json.dumps(
                {
                    "schemaVersion": OUTPUT_SCHEMA_VERSION,
                    "ok": False,
                    "error": {
                        "category": error_category(error),
                        "message": safe_error_message(error),
                        **details,
                    },
                }
            ),
            file=sys.stderr,
        )
        return 1
