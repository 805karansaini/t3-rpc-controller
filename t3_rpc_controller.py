#!/usr/bin/env python3
import argparse
import asyncio
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlparse

import websockets


ENV_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
DEFAULT_HOME = Path(__file__).resolve().parent / ".state"


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


def normalize_url(url: str) -> str:
    value = url.strip()
    if value.startswith("@url:") or "`" in value:
        raise ValueError("use a plain URL, for example http://100.65.116.80:3773 (without @url: or backticks)")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("T3 URL must be an http(s) origin")
    return f"{parsed.scheme}://{parsed.netloc}"


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
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"T3 HTTP {error.code} from {url}: {detail}") from error


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
    ticket_result = await asyncio.to_thread(
        http_json,
        f"{http_url}/api/auth/websocket-ticket",
        token,
        "POST",
    )
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
            raw_message = await asyncio.wait_for(socket.recv(), timeout=timeout)
            message = json.loads(raw_message)
            if message.get("_tag") != "Exit" or message.get("requestId") != request_id:
                continue
            exit_value = message.get("exit", {})
            if exit_value.get("_tag") == "Success":
                return exit_value.get("value")
            raise RuntimeError(f"T3 RPC {tag} failed: {json.dumps(exit_value)}")


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
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"T3 HTTP {error.code} from {url}: {detail}") from error


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
    ticket_result = await asyncio.to_thread(
        http_json,
        f"{http_url}/api/auth/websocket-ticket",
        token,
        "POST",
    )
    ticket = ticket_result.get("ticket")
    if not isinstance(ticket, str) or not ticket:
        raise RuntimeError("T3 did not return a WebSocket ticket")

    request_id = f"hermes-projects-{uuid.uuid4()}"
    messages: list[dict] = []
    async with websockets.connect(websocket_url(http_url, ticket), open_timeout=15) as socket:
        await socket.send(json.dumps({"_tag": "Ping"}))
        await socket.send(json.dumps(make_request(request_id, "orchestration.subscribeShell", {})))
        while True:
            raw_message = await asyncio.wait_for(socket.recv(), timeout=30)
            message = json.loads(raw_message)
            messages.append(message)
            snapshot = first_shell_snapshot(messages[-1:], request_id)
            if snapshot is not None:
                return snapshot
            if message.get("_tag") == "Exit" and message.get("requestId") == request_id:
                exit_value = message.get("exit", {})
                if exit_value.get("_tag") != "Success":
                    raise RuntimeError(f"T3 RPC failed: {json.dumps(exit_value)}")
                raise RuntimeError("T3 RPC stream ended before returning a shell snapshot")


async def fetch_thread_snapshot(
    http_url: str,
    token: str,
    thread_id: str,
    turn_limit: int | None = None,
) -> dict:
    ticket_result = await asyncio.to_thread(
        http_json,
        f"{http_url}/api/auth/websocket-ticket",
        token,
        "POST",
    )
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
            raw_message = await asyncio.wait_for(socket.recv(), timeout=30)
            message = json.loads(raw_message)
            snapshot = first_stream_snapshot([message], request_id)
            if snapshot is not None:
                return snapshot
            if message.get("_tag") == "Exit" and message.get("requestId") == request_id:
                exit_value = message.get("exit", {})
                if exit_value.get("_tag") != "Success":
                    raise RuntimeError(f"T3 RPC failed: {json.dumps(exit_value)}")
                raise RuntimeError("T3 RPC stream ended before returning a thread snapshot")


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
            errors[name] = str(result)
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
            errors[name] = str(result)
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
            errors[name] = str(result)
        else:
            output[name] = result
    return output, errors


async def list_many_projects(
    store: EnvironmentStore,
    names: list[str],
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    results = await asyncio.gather(*(list_projects(store, name) for name in names), return_exceptions=True)
    output: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            errors[name] = str(result)
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
        row["error"] = str(error)
    return row


async def check_many_environments(store: EnvironmentStore, names: list[str]) -> list[dict]:
    return list(await asyncio.gather(*(check_environment(store, name) for name in names)))


def configure_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    if args.token_file:
        token = Path(args.token_file).expanduser().read_text().strip()
    else:
        token = getpass.getpass("T3 bearer token (input hidden): ").strip()
    if not token:
        raise ValueError("bearer token cannot be empty")
    store.save(args.name, args.url, token)
    print(json.dumps({"configured": args.name, "url": normalize_url(args.url)}))
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
    print(json.dumps({"paired": args.name, "url": url, "authenticated": True}))
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
    print(json.dumps(result, indent=2))
    return 0 if not errors else 1


def health_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    names = store.names() if not args.environment or args.environment == ["all"] else args.environment
    if not names:
        raise RuntimeError("no environments configured")
    rows = asyncio.run(check_many_environments(store, names))
    ok = all(row.get("authenticated") is True and row.get("rpc") is True for row in rows)
    print(json.dumps({"ok": ok, "environments": rows}, indent=2))
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
    print(json.dumps(result, indent=2))
    return 0 if not errors else 1


def thread_show_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    url, token = store.load(args.environment)
    snapshot = asyncio.run(fetch_thread_snapshot(url, token, args.thread_id, args.turn_limit))
    print(json.dumps({"ok": True, "environment": args.environment, **snapshot}, indent=2))
    return 0


def search_command(store: EnvironmentStore, args: argparse.Namespace) -> int:
    names = store.names() if not args.environment or args.environment == ["all"] else args.environment
    if not names:
        raise RuntimeError("no environments configured")
    matches, errors = asyncio.run(search_many_environments(store, names, args.query, args.limit))
    matches.sort(key=lambda match: str(match.get("messageCreatedAt", "")), reverse=True)
    print(json.dumps({"ok": not errors, "count": len(matches), "matches": matches, "errors": errors}, indent=2))
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
    print(json.dumps({"ok": not errors, "count": len(rows), "providers": rows, "errors": errors}, indent=2))
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
    print(json.dumps({"ok": not errors, "count": len(rows), "models": rows, "errors": errors}, indent=2))
    return 0 if not errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Native T3 HTTP/WebSocket RPC controller")
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME, help="controller state directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser("configure", help="save a named T3 bearer environment")
    configure.add_argument("name")
    configure.add_argument("--url", required=True)
    configure.add_argument("--token-file", help="read token from a file; otherwise prompt securely")
    configure.set_defaults(handler=configure_command)

    pair = subparsers.add_parser("pair", help="exchange a one-time pairing token for a bearer session")
    pair.add_argument("name")
    pair.add_argument("--url", required=True)
    pair.set_defaults(handler=pair_command)

    environments = subparsers.add_parser("environments", help="list configured environments")
    environments.set_defaults(
        handler=lambda store, args: print(json.dumps(safe_environment_config(store), indent=2)) or 0
    )

    health = subparsers.add_parser("health", help="check authentication and RPC access")
    health.add_argument("--environment", action="append")
    health.set_defaults(handler=health_command)

    projects = subparsers.add_parser("projects", help="list T3 projects over native RPC")
    projects.add_argument("--environment", action="append")
    projects.set_defaults(handler=projects_command)

    threads = subparsers.add_parser("threads", help="list and filter T3 threads")
    threads.add_argument("--environment", action="append")
    threads.add_argument("--project", help="only return threads in this exact project ID")
    threads.add_argument("--scope", choices=["active", "archived", "all"], default="active")
    threads.add_argument("--turn-state", choices=["running", "completed", "interrupted", "error"])
    threads.add_argument("--pending", choices=["approvals", "input", "any"])
    threads.add_argument("--limit", type=int)
    threads.set_defaults(handler=threads_command)

    thread = subparsers.add_parser("thread", help="inspect or control one exact T3 thread")
    thread_commands = thread.add_subparsers(dest="thread_command", required=True)
    thread_show = thread_commands.add_parser("show", help="show detailed thread state and history")
    thread_show.add_argument("environment")
    thread_show.add_argument("thread_id")
    thread_show.add_argument("--turn-limit", type=int, default=20)
    thread_show.set_defaults(handler=thread_show_command)

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
    except (OSError, ValueError, KeyError, RuntimeError, asyncio.TimeoutError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
