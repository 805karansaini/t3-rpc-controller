import asyncio
import json
import urllib.error
import urllib.request
import uuid
import websockets
from .output import safe_error_message
from urllib.parse import urlencode, urlparse


def make_request(request_id: str, tag: str, payload: object) -> dict:
    return {
        "_tag": "Request",
        "id": request_id,
        "tag": tag,
        "payload": payload,
        "headers": [],
    }


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
    return first_shell_snapshot(messages, request_id)


def http_json(
    url: str,
    token: str | None,
    method: str = "GET",
    payload: dict | None = None,
    timeout: float = 15,
) -> dict:
    body = None if payload is None else json.dumps(payload).encode()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        parsed = urlparse(url)
        raise RuntimeError(
            f"T3 HTTP {error.code} from {parsed.scheme}://{parsed.netloc}{parsed.path}"
        ) from error


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
        raise RuntimeError(
            f"T3 HTTP {error.code} from {parsed.scheme}://{parsed.netloc}{parsed.path}"
        ) from error


def websocket_url(http_url: str, ticket: str) -> str:
    parsed = urlparse(http_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ws?{urlencode({"wsTicket": ticket})}"


async def request_ticket(http_url: str, token: str, timeout: float = 15) -> str:
    ticket_result = await asyncio.to_thread(
        http_json,
        f"{http_url}/api/auth/websocket-ticket",
        token,
        "POST",
        None,
        timeout,
    )
    if not isinstance(ticket_result, dict):
        raise RuntimeError("T3 returned an invalid WebSocket ticket response")
    ticket = ticket_result.get("ticket")
    if not isinstance(ticket, str) or not ticket:
        raise RuntimeError("T3 did not return a WebSocket ticket")
    return ticket


async def rpc_call(
    http_url: str,
    token: str,
    tag: str,
    payload: object,
    timeout: float = 30,
) -> object:
    async with asyncio.timeout(timeout):
        ticket = await request_ticket(http_url, token, min(timeout, 15))
        request_id = f"t3-controller-{uuid.uuid4()}"
        async with websockets.connect(
            websocket_url(http_url, ticket),
            open_timeout=min(timeout, 15),
            close_timeout=0,
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


async def fetch_stream_snapshot(
    http_url: str,
    token: str,
    tag: str,
    payload: dict,
    label: str,
    timeout: float = 30,
) -> dict:
    """Read the first snapshot from a T3 subscription and close the connection."""
    async with asyncio.timeout(timeout):
        ticket = await request_ticket(http_url, token, min(timeout, 15))
        request_id = f"t3-{label}-{uuid.uuid4()}"
        async with websockets.connect(
            websocket_url(http_url, ticket),
            open_timeout=min(timeout, 15),
            close_timeout=0,
        ) as socket:
            await socket.send(json.dumps({"_tag": "Ping"}))
            await socket.send(json.dumps(make_request(request_id, tag, payload)))
            while True:
                message = json.loads(await socket.recv())
                if not isinstance(message, dict):
                    raise RuntimeError(f"T3 returned an invalid {label} message")
                snapshot = first_stream_snapshot([message], request_id)
                if snapshot is not None:
                    return snapshot
                if message.get("_tag") == "Exit" and message.get("requestId") == request_id:
                    exit_value = message.get("exit", {})
                    if exit_value.get("_tag") != "Success":
                        raise RuntimeError(f"T3 RPC failed: {safe_error_message(exit_value)}")
                    raise RuntimeError(f"T3 RPC stream ended before returning a {label} snapshot")


async def fetch_shell_snapshot(http_url: str, token: str) -> dict:
    return await fetch_stream_snapshot(
        http_url, token, "orchestration.subscribeShell", {}, "shell"
    )


async def fetch_thread_snapshot(
    http_url: str,
    token: str,
    thread_id: str,
    turn_limit: int | None = None,
    timeout: float = 30,
) -> dict:
    payload: dict[str, object] = {"threadId": thread_id}
    if turn_limit is not None:
        payload["turnLimit"] = turn_limit
    return await fetch_stream_snapshot(
        http_url, token, "orchestration.subscribeThread", payload, "thread", timeout
    )
