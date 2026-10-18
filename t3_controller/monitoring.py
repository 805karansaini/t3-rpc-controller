"""Resumable thread event streaming."""

import asyncio
import json
import time
import uuid
import websockets

from t3_controller.domain import is_terminal_thread_event, is_terminal_thread_snapshot, thread_snapshot_summary
from t3_controller.output import emit_json, safe_error_message
from t3_controller.state import EnvironmentStore
from t3_controller.transport import (
    fetch_thread_snapshot,
    http_json,
    make_request,
    websocket_url,
)
from websockets.exceptions import WebSocketException


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
    last_snapshot_summary = None
    reconnects = 0

    async def within_deadline(awaitable):
        if deadline is None:
            return await awaitable
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return await asyncio.wait_for(awaitable, timeout=remaining)

    if after_sequence is not None:
        try:
            snapshot_timeout = 30 if deadline is None else max(
                0.001, deadline - time.monotonic()
            )
            initial = await within_deadline(
                fetch_thread_snapshot(
                    http_url, token, thread_id, turn_limit, timeout=snapshot_timeout
                )
            )
        except asyncio.TimeoutError as error:
            raise RuntimeError(
                "T3 thread subscription timed out before initial synchronization"
            ) from error
        initial_thread = initial.get("thread") or {}
        if initial_thread.get("id") != thread_id:
            raise RuntimeError(f"T3 did not return the requested thread {thread_id}")
        last_snapshot_summary = thread_snapshot_summary(initial, environment)
        emit_json(last_snapshot_summary, flush=True)
        initial_snapshot_emitted = True
        if until_terminal and is_terminal_thread_snapshot(initial):
            return {
                "kind": "follow-complete",
                "reason": "terminal-state",
                "environment": environment,
                "threadId": thread_id,
                "lastSequence": last_sequence,
                "events": emitted_events,
                "reconnects": reconnects,
            }

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
            ticket_timeout = 15 if deadline is None else max(
                0.001, deadline - time.monotonic()
            )
            ticket_result = await within_deadline(
                asyncio.to_thread(
                    http_json,
                    f"{http_url}/api/auth/websocket-ticket",
                    token,
                    "POST",
                    None,
                    ticket_timeout,
                )
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
            open_timeout = 15 if deadline is None else max(0.001, deadline - time.monotonic())
            async with websockets.connect(
                websocket_url(http_url, ticket),
                open_timeout=open_timeout,
                close_timeout=0,
            ) as socket:
                await within_deadline(socket.send(json.dumps({"_tag": "Ping"})))
                await within_deadline(
                    socket.send(
                        json.dumps(make_request(request_id, "orchestration.subscribeThread", payload))
                    )
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
                            snapshot_summary = thread_snapshot_summary(
                                item["snapshot"], environment
                            )
                            if not initial_snapshot_emitted or snapshot_summary != last_snapshot_summary:
                                emit_json(snapshot_summary, flush=True)
                                initial_snapshot_emitted = True
                                last_snapshot_summary = snapshot_summary
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
                    await within_deadline(
                        socket.send(json.dumps({"_tag": "Ack", "requestId": request_id}))
                    )
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
            delay = min(2**reconnects, 5)
            if deadline is not None:
                delay = min(delay, max(0, deadline - time.monotonic()))
            await asyncio.sleep(delay)
