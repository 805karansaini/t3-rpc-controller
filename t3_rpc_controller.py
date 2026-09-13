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


async def list_projects(store: EnvironmentStore, name: str) -> list[dict]:
    url, token = store.load(name)
    snapshot = await fetch_shell_snapshot(url, token)
    return project_rows_from_snapshot(snapshot)


async def list_many_projects(store: EnvironmentStore, names: list[str]) -> dict[str, list[dict]]:
    results = await asyncio.gather(*(list_projects(store, name) for name in names), return_exceptions=True)
    output: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            errors[name] = str(result)
        else:
            output[name] = result
    if errors:
        successful = {name: len(projects) for name, projects in output.items()}
        raise RuntimeError(json.dumps({"errors": errors, "successful_counts": successful}))
    return output


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
    projects_by_environment = asyncio.run(list_many_projects(store, names))
    result = {
        "counts": {name: len(projects) for name, projects in projects_by_environment.items()},
        "total": sum(len(projects) for projects in projects_by_environment.values()),
        "projects": merge_environment_projects(projects_by_environment),
    }
    print(json.dumps(result, indent=2))
    return 0


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
    environments.set_defaults(handler=lambda store, args: print(json.dumps(store.load_config(), indent=2)) or 0)

    projects = subparsers.add_parser("projects", help="list T3 projects over native RPC")
    projects.add_argument("--environment", action="append")
    projects.set_defaults(handler=projects_command)
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
