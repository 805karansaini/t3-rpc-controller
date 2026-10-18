import argparse
import asyncio
import io
import json
import time
import unittest
from contextlib import redirect_stdout
from t3_controller.monitoring import follow_thread
from t3_controller.operations import (
    approval_response_operation,
    archive_thread_operation,
    create_thread_operation,
    delete_thread_operation,
    rename_thread_operation,
    run_thread_operation,
    send_thread_operation,
    set_thread_model_operation,
    stop_thread_operation,
    thread_state_operation,
    user_input_response_operation,
    wait_for_request_cleared,
    wait_thread_operation,
)
from t3_controller.output import UncertainDispatchError
from unittest.mock import AsyncMock, patch


def args(**values):
    return argparse.Namespace(**values)


class OperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_follow_reconnects_resumes_and_suppresses_duplicates(self):
        class Store:
            @staticmethod
            def load(environment):
                return "http://fake", "fake-token"

        class Socket:
            def __init__(self, batches, disconnect=False):
                self.batches = list(batches)
                self.disconnect = disconnect
                self.request_id = None
                self.request_payload = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def send(self, raw):
                message = json.loads(raw)
                if message.get("_tag") == "Request":
                    self.request_id = message["id"]
                    self.request_payload = message["payload"]

            async def recv(self):
                if self.batches:
                    values = self.batches.pop(0)
                    return json.dumps(
                        {"_tag": "Chunk", "requestId": self.request_id, "values": values}
                    )
                if self.disconnect:
                    raise OSError("simulated disconnect")
                raise asyncio.TimeoutError()

        snapshot = {
            "kind": "snapshot",
            "snapshot": {"thread": {"id": "t1", "messages": [], "activities": []}},
        }
        event_1 = {"kind": "event", "event": {"eventId": "e1", "sequence": 1, "type": "x"}}
        event_2 = {"kind": "event", "event": {"eventId": "e2", "sequence": 2, "type": "x"}}
        first = Socket([[snapshot, event_1]], disconnect=True)
        second = Socket([[snapshot, event_1, event_2]])
        output = io.StringIO()
        with (
            patch(
                "t3_controller.monitoring.fetch_thread_snapshot",
                new=AsyncMock(return_value=snapshot["snapshot"]),
            ),
            patch("t3_controller.monitoring.asyncio.to_thread", new=AsyncMock(return_value={"ticket": "t"})),
            patch("t3_controller.monitoring.websockets.connect", side_effect=[first, second]) as connect,
            patch("t3_controller.monitoring.asyncio.sleep", new=AsyncMock()),
            redirect_stdout(output),
        ):
            result = await follow_thread(
                Store(),
                "mac",
                "t1",
                after_sequence=None,
                timeout=10,
                max_events=2,
                retries=2,
                until_terminal=False,
                turn_limit=20,
            )
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(sum(line.get("kind") == "snapshot" for line in lines), 1)
        self.assertEqual(
            [line["event"]["eventId"] for line in lines if line.get("kind") == "event"],
            ["e1", "e2"],
        )
        self.assertEqual(result["reconnects"], 1)
        self.assertEqual(result["lastSequence"], 2)
        self.assertEqual(second.request_payload["afterSequence"], 1)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(connect.call_args_list[0].kwargs["close_timeout"], 0)

    async def test_resumed_follow_emits_changed_subscription_snapshot(self):
        class Store:
            @staticmethod
            def load(environment):
                return "http://fake", "fake-token"

        class Socket:
            request_id = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def send(self, raw):
                message = json.loads(raw)
                if message.get("_tag") == "Request":
                    self.request_id = message["id"]

            async def recv(self):
                if self.request_id:
                    request_id, self.request_id = self.request_id, None
                    return json.dumps(
                        {
                            "_tag": "Chunk",
                            "requestId": request_id,
                            "values": [
                                {
                                    "kind": "snapshot",
                                    "snapshot": {
                                        "snapshotSequence": 12,
                                        "thread": {
                                            "id": "t1",
                                            "title": "after",
                                            "messages": [],
                                            "activities": [],
                                        },
                                    },
                                }
                            ],
                        }
                    )
                raise asyncio.TimeoutError()

        before = {
            "snapshotSequence": 11,
            "thread": {
                "id": "t1",
                "title": "before",
                "messages": [],
                "activities": [],
            },
        }
        output = io.StringIO()
        with (
            patch(
                "t3_controller.monitoring.fetch_thread_snapshot",
                new=AsyncMock(return_value=before),
            ),
            patch("t3_controller.monitoring.asyncio.to_thread", new=AsyncMock(return_value={"ticket": "t"})),
            patch("t3_controller.monitoring.websockets.connect", return_value=Socket()),
            redirect_stdout(output),
        ):
            result = await follow_thread(
                Store(),
                "mac",
                "t1",
                after_sequence=10,
                timeout=1,
                max_events=None,
                retries=0,
                until_terminal=False,
                turn_limit=20,
            )
        snapshots = [
            line for line in map(json.loads, output.getvalue().splitlines())
            if line.get("kind") == "snapshot"
        ]
        self.assertEqual([row["thread"]["title"] for row in snapshots], ["before", "after"])
        self.assertEqual(result["reason"], "timeout")

    async def test_resumed_follow_stops_on_prefetched_terminal_snapshot(self):
        class Store:
            @staticmethod
            def load(environment):
                return "http://fake", "fake-token"

        terminal = {
            "thread": {
                "id": "t1",
                "messages": [],
                "activities": [],
                "latestTurn": {"state": "completed"},
                "session": {"status": "ready"},
            }
        }
        with (
            patch(
                "t3_controller.monitoring.fetch_thread_snapshot",
                new=AsyncMock(return_value=terminal),
            ),
            patch("t3_controller.monitoring.websockets.connect") as connect,
            redirect_stdout(io.StringIO()),
        ):
            result = await follow_thread(
                Store(),
                "mac",
                "t1",
                after_sequence=10,
                timeout=1,
                max_events=None,
                retries=0,
                until_terminal=True,
                turn_limit=20,
            )
        self.assertEqual(result["reason"], "terminal-state")
        connect.assert_not_called()

    async def test_follow_requires_initial_synchronization(self):
        class Store:
            @staticmethod
            def load(environment):
                return "http://fake", "fake-token"

        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def send(self, raw):
                pass

            async def recv(self):
                raise asyncio.TimeoutError()

        with (
            patch(
                "t3_controller.monitoring.fetch_thread_snapshot",
                new=AsyncMock(side_effect=asyncio.TimeoutError()),
            ),
            patch("t3_controller.monitoring.asyncio.to_thread", new=AsyncMock(return_value={"ticket": "t"})),
            patch("t3_controller.monitoring.websockets.connect", return_value=Socket()),
        ):
            with self.assertRaisesRegex(RuntimeError, "before initial synchronization"):
                await follow_thread(
                    Store(),
                    "mac",
                    "t1",
                    after_sequence=None,
                    timeout=1,
                    max_events=None,
                    retries=0,
                    until_terminal=False,
                    turn_limit=20,
                )

    async def test_follow_timeout_includes_reconnect_backoff(self):
        class Store:
            @staticmethod
            def load(environment):
                return "http://fake", "fake-token"

        snapshot = {"thread": {"id": "t1", "messages": [], "activities": []}}
        with (
            patch(
                "t3_controller.monitoring.fetch_thread_snapshot",
                new=AsyncMock(return_value=snapshot),
            ),
            patch("t3_controller.monitoring.asyncio.to_thread", new=AsyncMock(return_value={"ticket": "t"})),
            patch("t3_controller.monitoring.websockets.connect", side_effect=OSError("offline")),
            redirect_stdout(io.StringIO()),
        ):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "before initial synchronization"):
                await follow_thread(
                    Store(),
                    "mac",
                    "t1",
                    after_sequence=10,
                    timeout=0.05,
                    max_events=None,
                    retries=3,
                    until_terminal=False,
                    turn_limit=20,
                )
        self.assertLess(time.monotonic() - started, 0.3)

    async def test_create_validates_every_requested_field_on_readback(self):
        command_args = args(
            environment="mac",
            project_id="p1",
            title="Task",
            provider="codex",
            model="gpt-test",
            runtime_mode="approval-required",
            interaction_mode="plan",
            branch="main",
            worktree_path="/tmp/worktree",
            dry_run=False,
        )
        exact = {
            "id": "thread-1",
            "projectId": "p1",
            "title": "Task",
            "modelSelection": {"instanceId": "codex", "model": "gpt-test"},
            "runtimeMode": "approval-required",
            "interactionMode": "plan",
            "branch": "main",
            "worktreePath": "/tmp/worktree",
        }
        with (
            patch("t3_controller.operations.uuid.uuid4", return_value="thread-1"),
            patch("t3_controller.operations.validate_project_and_model", new=AsyncMock()),
            patch(
                "t3_controller.operations.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ),
            patch(
                "t3_controller.operations.wait_for_shell_thread",
                new=AsyncMock(return_value=exact),
            ) as wait,
        ):
            result = await create_thread_operation(object(), command_args)
        predicate = wait.await_args.args[3]
        self.assertTrue(predicate(exact))
        self.assertFalse(predicate({**exact, "title": "Wrong"}))
        self.assertFalse(predicate({**exact, "runtimeMode": "full-access"}))
        self.assertEqual(result["thread"]["projectId"], "p1")

    async def test_wait_tracks_the_submitted_message_instead_of_an_old_turn(self):
        message_id = "22222222-2222-4222-8222-222222222222"
        old = {
            "id": "t1",
            "messages": [{"id": message_id, "role": "user", "text": "Work"}],
            "activities": [
                {
                    "kind": "approval.requested",
                    "turnId": "old-turn",
                    "payload": {"requestId": "old-approval"},
                }
            ],
            "latestTurn": {"turnId": "old-turn", "state": "completed"},
        }
        running = {
            **old,
            "messages": [
                {"id": message_id, "role": "user", "text": "Work", "turnId": "target-turn"}
            ],
            "latestTurn": {"turnId": "target-turn", "state": "running"},
        }
        completed = {
            **running,
            "latestTurn": {"turnId": "target-turn", "state": "completed"},
        }
        command_args = args(
            environment="mac",
            thread_id="t1",
            message_id=message_id,
            timeout=10,
            poll_interval=0.01,
        )
        with (
            patch(
                "t3_controller.operations.require_thread_snapshot",
                new=AsyncMock(
                    side_effect=[{"thread": old}, {"thread": running}, {"thread": completed}]
                ),
            ),
            patch("t3_controller.operations.asyncio.sleep", new=AsyncMock()),
        ):
            result = await wait_thread_operation(object(), command_args)
        self.assertTrue(result["ok"])
        self.assertEqual(result["turnId"], "target-turn")

    async def test_wait_returns_pending_attention_before_terminal_state(self):
        message_id = "22222222-2222-4222-8222-222222222222"
        thread = {
            "id": "t1",
            "messages": [
                {"id": message_id, "role": "user", "text": "Work", "turnId": "target-turn"}
            ],
            "latestTurn": {"turnId": "target-turn", "state": "running"},
            "activities": [
                {
                    "kind": "approval.requested",
                    "turnId": "target-turn",
                    "payload": {"requestId": "approval-1"},
                }
            ],
        }
        command_args = args(
            environment="mac",
            thread_id="t1",
            message_id=message_id,
            timeout=10,
            poll_interval=0.01,
        )
        with patch(
            "t3_controller.operations.require_thread_snapshot",
            new=AsyncMock(return_value={"thread": thread}),
        ):
            result = await wait_thread_operation(object(), command_args)
        self.assertEqual(result["reason"], "attention-required")
        self.assertEqual(result["pending"][0]["requestId"], "approval-1")

    async def test_wait_reports_unknown_when_target_was_superseded(self):
        message_id = "22222222-2222-4222-8222-222222222222"
        thread = {
            "id": "t1",
            "messages": [
                {"id": message_id, "role": "user", "text": "Work", "turnId": "target-turn"},
                {"id": "answer", "role": "assistant", "text": "Done", "turnId": "target-turn"},
            ],
            "latestTurn": {"turnId": "newer-turn", "state": "running"},
            "activities": [],
        }
        command_args = args(
            environment="mac",
            thread_id="t1",
            message_id=message_id,
            timeout=10,
            poll_interval=0.01,
        )
        with patch(
            "t3_controller.operations.require_thread_snapshot",
            new=AsyncMock(return_value={"thread": thread}),
        ):
            result = await wait_thread_operation(object(), command_args)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "superseded")
        self.assertEqual(result["turnId"], "target-turn")
        self.assertEqual(result["turnState"], "unknown")

    async def test_wait_does_not_call_missing_latest_turn_superseded(self):
        message_id = "22222222-2222-4222-8222-222222222222"
        thread = {
            "id": "t1",
            "messages": [
                {"id": message_id, "role": "user", "text": "Work", "turnId": "target-turn"}
            ],
            "latestTurn": None,
            "activities": [],
        }
        command_args = args(
            environment="mac",
            thread_id="t1",
            message_id=message_id,
            timeout=0.01,
            poll_interval=0.01,
        )
        with patch(
            "t3_controller.operations.require_thread_snapshot",
            new=AsyncMock(return_value={"thread": thread}),
        ):
            result = await wait_thread_operation(object(), command_args)
        self.assertEqual(result["reason"], "timeout")

    async def test_wait_timeout_includes_snapshot_read(self):
        async def delayed_snapshot(*args, **kwargs):
            await asyncio.sleep(0.2)
            return {"thread": {"id": "t1", "messages": [], "activities": []}}

        command_args = args(
            environment="mac",
            thread_id="t1",
            message_id=None,
            timeout=0.05,
            poll_interval=0.01,
        )
        with patch(
            "t3_controller.operations.require_thread_snapshot",
            side_effect=delayed_snapshot,
        ):
            started = time.monotonic()
            result = await wait_thread_operation(object(), command_args)
        self.assertEqual(result["reason"], "timeout")
        self.assertLess(time.monotonic() - started, 0.15)

    async def test_confirmation_failure_preserves_generated_create_retry_ids(self):
        command_args = args(
            environment="mac",
            project_id="p1",
            title="Task",
            provider="codex",
            model="gpt-test",
            runtime_mode="approval-required",
            interaction_mode="default",
            branch=None,
            worktree_path=None,
            thread_id=None,
            command_id=None,
            dry_run=False,
        )
        with (
            patch("t3_controller.operations.validate_project_and_model", new=AsyncMock()),
            patch("t3_controller.operations.dispatch_command", new=AsyncMock(return_value={"sequence": 1})),
            patch(
                "t3_controller.operations.wait_for_shell_thread",
                new=AsyncMock(side_effect=OSError("readback failed")),
            ),
        ):
            with self.assertRaises(UncertainDispatchError) as caught:
                await create_thread_operation(object(), command_args)
        self.assertIn("threadId", caught.exception.retry)
        self.assertIn("commandId", caught.exception.retry)

    async def test_confirmation_failure_preserves_generated_send_retry_ids(self):
        thread = {
            "id": "t1",
            "modelSelection": {"instanceId": "codex", "model": "gpt-test"},
            "runtimeMode": "approval-required",
            "interactionMode": "default",
            "messages": [],
        }
        command_args = args(
            environment="mac",
            thread_id="t1",
            provider=None,
            model=None,
            runtime_mode=None,
            interaction_mode=None,
            message_id=None,
            command_id=None,
            dry_run=False,
        )
        with (
            patch(
                "t3_controller.operations.require_thread_snapshot",
                new=AsyncMock(return_value={"thread": thread}),
            ),
            patch("t3_controller.operations.dispatch_command", new=AsyncMock(return_value={"sequence": 1})),
            patch(
                "t3_controller.operations.wait_for_thread_message",
                new=AsyncMock(side_effect=OSError("readback failed")),
            ),
        ):
            with self.assertRaises(UncertainDispatchError) as caught:
                await send_thread_operation(object(), command_args, "Do work")
        self.assertIn("messageId", caught.exception.retry)
        self.assertIn("commandId", caught.exception.retry)

    async def test_run_submits_then_waits_for_the_exact_message(self):
        message_id = "22222222-2222-4222-8222-222222222222"
        command_args = args(
            environment="mac",
            thread_id="t1",
            timeout=30,
            poll_interval=0.25,
        )
        submission = {
            "ok": True,
            "commandId": "command-1",
            "messageId": message_id,
            "turnId": "turn-1",
            "reconciled": False,
        }
        outcome = {
            "ok": True,
            "operation": "thread.wait",
            "reason": "terminal-state",
            "turnState": "completed",
        }
        with (
            patch(
                "t3_controller.operations.send_thread_operation",
                new=AsyncMock(return_value=submission),
            ) as send,
            patch(
                "t3_controller.operations.wait_thread_operation",
                new=AsyncMock(return_value=outcome),
            ) as wait,
        ):
            result = await run_thread_operation(object(), command_args, "Do work")
        self.assertTrue(result["ok"])
        self.assertEqual(result["submission"]["messageId"], message_id)
        self.assertEqual(send.await_args.args[2], "Do work")
        self.assertEqual(wait.await_args.args[1].message_id, message_id)

    async def test_send_uses_codex_and_safe_runtime_default(self):
        thread = {
            "id": "t1",
            "modelSelection": {"instanceId": "codex", "model": "gpt-test"},
            "runtimeMode": None,
            "interactionMode": None,
            "messages": [],
        }
        confirmed = {
            **thread,
            "messages": [{"id": "message-1", "role": "user", "text": "Do work"}],
        }
        command_args = args(
            environment="mac",
            thread_id="t1",
            provider=None,
            model=None,
            runtime_mode=None,
            interaction_mode=None,
            dry_run=False,
        )
        with (
            patch("t3_controller.operations.uuid.uuid4", side_effect=["command-1", "message-1"]),
            patch(
                "t3_controller.operations.require_thread_snapshot",
                new=AsyncMock(side_effect=[{"thread": thread}, {"thread": confirmed}]),
            ),
            patch(
                "t3_controller.operations.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ) as dispatch,
        ):
            result = await send_thread_operation(object(), command_args, "Do work")
        command = dispatch.await_args.args[2]
        self.assertEqual(command["runtimeMode"], "approval-required")
        self.assertEqual(result["messageId"], "message-1")

        thread["modelSelection"]["instanceId"] = "claude"
        with patch(
            "t3_controller.operations.require_thread_snapshot",
            new=AsyncMock(return_value={"thread": thread}),
        ):
            with self.assertRaisesRegex(ValueError, "restricted to provider instance codex"):
                await send_thread_operation(object(), command_args, "Do work")

    async def test_send_reconciles_an_uncertain_dispatch_and_safe_retry(self):
        command_id = "11111111-1111-4111-8111-111111111111"
        message_id = "22222222-2222-4222-8222-222222222222"
        before = {
            "id": "t1",
            "modelSelection": {"instanceId": "codex", "model": "gpt-test"},
            "runtimeMode": "approval-required",
            "interactionMode": "default",
            "messages": [],
        }
        confirmed = {
            **before,
            "messages": [{"id": message_id, "role": "user", "text": "Do work"}],
        }
        command_args = args(
            environment="mac",
            thread_id="t1",
            provider=None,
            model=None,
            runtime_mode=None,
            interaction_mode=None,
            command_id=command_id,
            message_id=message_id,
            dry_run=False,
        )
        with (
            patch(
                "t3_controller.operations.require_thread_snapshot",
                new=AsyncMock(side_effect=[{"thread": before}, {"thread": confirmed}]),
            ),
            patch(
                "t3_controller.operations.dispatch_command",
                new=AsyncMock(side_effect=OSError("connection dropped after send")),
            ),
        ):
            result = await send_thread_operation(object(), command_args, "Do work")
        self.assertTrue(result["reconciled"])
        self.assertIsNone(result["receipt"])
        self.assertEqual(result["messageId"], message_id)

        with (
            patch(
                "t3_controller.operations.require_thread_snapshot",
                new=AsyncMock(return_value={"thread": confirmed}),
            ),
            patch("t3_controller.operations.dispatch_command", new=AsyncMock()) as dispatch,
        ):
            retried = await send_thread_operation(object(), command_args, "Do work")
        self.assertTrue(retried["reconciled"])
        dispatch.assert_not_awaited()

        mismatched = {
            **confirmed,
            "messages": [{"id": message_id, "role": "user", "text": "Different work"}],
        }
        with patch(
            "t3_controller.operations.require_thread_snapshot",
            new=AsyncMock(return_value={"thread": mismatched}),
        ):
            with self.assertRaisesRegex(ValueError, "does not match"):
                await send_thread_operation(object(), command_args, "Do work")

    async def test_create_reconciles_an_uncertain_dispatch(self):
        thread_id = "33333333-3333-4333-8333-333333333333"
        command_id = "44444444-4444-4444-8444-444444444444"
        command_args = args(
            environment="mac",
            project_id="p1",
            title="Task",
            provider="codex",
            model="gpt-test",
            runtime_mode="approval-required",
            interaction_mode="default",
            branch=None,
            worktree_path=None,
            thread_id=thread_id,
            command_id=command_id,
            dry_run=False,
        )
        exact = {
            "id": thread_id,
            "projectId": "p1",
            "title": "Task",
            "modelSelection": {"instanceId": "codex", "model": "gpt-test"},
            "runtimeMode": "approval-required",
            "interactionMode": "default",
            "branch": None,
            "worktreePath": None,
        }
        with (
            patch("t3_controller.operations.validate_project_and_model", new=AsyncMock()),
            patch("t3_controller.operations.find_any_shell_thread", new=AsyncMock(return_value=None)),
            patch(
                "t3_controller.operations.dispatch_command",
                new=AsyncMock(side_effect=OSError("connection dropped after create")),
            ),
            patch(
                "t3_controller.operations.wait_for_shell_thread",
                new=AsyncMock(return_value=exact),
            ),
        ):
            result = await create_thread_operation(object(), command_args)
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["thread"]["id"], thread_id)

    async def test_session_stop_requires_confirmed_terminal_session(self):
        thread = {"id": "t1", "latestTurn": {"state": "completed"}, "session": {"status": "ready"}}
        stopped = {**thread, "session": {"status": "stopped"}}
        command_args = args(environment="mac", thread_id="t1", session=True, dry_run=False)
        with (
            patch(
                "t3_controller.operations.require_thread_snapshot",
                new=AsyncMock(return_value={"thread": thread}),
            ),
            patch(
                "t3_controller.operations.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ),
            patch(
                "t3_controller.operations.wait_for_shell_thread",
                new=AsyncMock(return_value=stopped),
            ) as wait,
        ):
            await stop_thread_operation(object(), command_args)
        predicate = wait.await_args.args[3]
        self.assertFalse(predicate({"session": None}))
        self.assertTrue(predicate(stopped))

    async def test_rejected_approval_response_raises_instead_of_reporting_success(self):
        failed = {
            "thread": {
                "id": "t1",
                "activities": [
                    {"kind": "approval.requested", "payload": {"requestId": "a1"}},
                    {
                        "kind": "provider.approval.respond.failed",
                        "payload": {"requestId": "a1", "detail": "stale pending"},
                    },
                ],
            }
        }
        with patch(
            "t3_controller.operations.require_thread_snapshot",
            new=AsyncMock(return_value=failed),
        ):
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                await wait_for_request_cleared(
                    object(), "mac", "t1", "a1", "approval.requested", timeout=0
                )

    async def test_approval_operation_requires_resolution_event(self):
        pending = {
            "id": "t1",
            "modelSelection": {"instanceId": "codex", "model": "gpt-test"},
            "activities": [{"kind": "approval.requested", "payload": {"requestId": "a1"}}],
        }
        resolved = {
            "thread": {
                "id": "t1",
                "activities": [
                    *pending["activities"],
                    {"kind": "approval.resolved", "payload": {"requestId": "a1"}},
                ],
            }
        }
        command_args = args(
            environment="mac",
            thread_id="t1",
            request_id="a1",
            decision="accept",
            dry_run=False,
        )
        with (
            patch(
                "t3_controller.operations.require_thread_snapshot",
                new=AsyncMock(return_value={"thread": pending}),
            ),
            patch(
                "t3_controller.operations.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ),
            patch(
                "t3_controller.operations.wait_for_request_cleared",
                new=AsyncMock(return_value=resolved),
            ) as wait,
        ):
            result = await approval_response_operation(object(), command_args)
        self.assertTrue(result["ok"])
        self.assertEqual(wait.await_args.args[4], "approval.requested")

    async def test_user_input_operation_requires_resolution_event(self):
        pending = {
            "id": "t1",
            "modelSelection": {"instanceId": "codex", "model": "gpt-test"},
            "activities": [
                {
                    "kind": "user-input.requested",
                    "payload": {"requestId": "q1", "questions": [{"id": "color"}]},
                }
            ],
        }
        resolved = {
            "thread": {
                "id": "t1",
                "activities": [
                    *pending["activities"],
                    {"kind": "user-input.resolved", "payload": {"requestId": "q1"}},
                ],
            }
        }
        command_args = args(environment="mac", thread_id="t1", request_id="q1", dry_run=False)
        with (
            patch(
                "t3_controller.operations.require_thread_snapshot",
                new=AsyncMock(return_value={"thread": pending}),
            ),
            patch(
                "t3_controller.operations.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ),
            patch(
                "t3_controller.operations.wait_for_request_cleared",
                new=AsyncMock(return_value=resolved),
            ) as wait,
        ):
            result = await user_input_response_operation(
                object(), command_args, {"color": "Blue"}, False
            )
        self.assertEqual(result["answeredQuestionIds"], ["color"])
        self.assertEqual(wait.await_args.args[4], "user-input.requested")

    async def test_approval_and_input_refuse_non_codex_threads(self):
        approval_args = args(
            environment="mac",
            thread_id="t1",
            request_id="a1",
            decision="accept",
            dry_run=False,
        )
        non_codex = {
            "id": "t1",
            "modelSelection": {"instanceId": "opencode", "model": "model"},
            "activities": [{"kind": "approval.requested", "payload": {"requestId": "a1"}}],
        }
        with patch(
            "t3_controller.operations.require_thread_snapshot",
            new=AsyncMock(return_value={"thread": non_codex}),
        ):
            with self.assertRaisesRegex(ValueError, "restricted to provider instance codex"):
                await approval_response_operation(object(), approval_args)

        input_args = args(environment="mac", thread_id="t1", request_id="q1", dry_run=False)
        non_codex["activities"] = [
            {"kind": "user-input.requested", "payload": {"requestId": "q1"}}
        ]
        with patch(
            "t3_controller.operations.require_thread_snapshot",
            new=AsyncMock(return_value={"thread": non_codex}),
        ):
            with self.assertRaisesRegex(ValueError, "restricted to provider instance codex"):
                await user_input_response_operation(object(), input_args, {"q": "a"}, False)

    async def test_model_rename_and_archive_read_back_their_exact_state(self):
        model_args = args(
            environment="mac",
            thread_id="t1",
            provider="codex",
            model="gpt-test",
            dry_run=False,
        )
        model_thread = {
            "id": "t1",
            "modelSelection": {"instanceId": "codex", "model": "gpt-test"},
        }
        with (
            patch("t3_controller.operations.require_thread_snapshot", new=AsyncMock()),
            patch("t3_controller.operations.validate_model", new=AsyncMock()),
            patch(
                "t3_controller.operations.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ),
            patch(
                "t3_controller.operations.wait_for_shell_thread",
                new=AsyncMock(return_value=model_thread),
            ) as model_wait,
        ):
            await set_thread_model_operation(object(), model_args)
        self.assertTrue(model_wait.await_args.args[3](model_thread))
        self.assertFalse(model_wait.await_args.args[3]({"modelSelection": {"instanceId": "codex", "model": "wrong"}}))

        rename_args = args(environment="mac", thread_id="t1", title="New title", dry_run=False)
        with (
            patch("t3_controller.operations.require_thread_snapshot", new=AsyncMock()),
            patch(
                "t3_controller.operations.dispatch_command",
                new=AsyncMock(return_value={"sequence": 2}),
            ),
            patch(
                "t3_controller.operations.wait_for_shell_thread",
                new=AsyncMock(return_value={"id": "t1", "title": "New title"}),
            ) as rename_wait,
        ):
            await rename_thread_operation(object(), rename_args)
        self.assertTrue(rename_wait.await_args.args[3]({"title": "New title"}))
        self.assertFalse(rename_wait.await_args.args[3]({"title": "Old title"}))

        archive_args = args(environment="mac", thread_id="t1", dry_run=False)
        with (
            patch(
                "t3_controller.operations.find_shell_thread",
                new=AsyncMock(
                    side_effect=[
                        {"id": "t1", "archivedAt": None},
                        {"id": "t1", "archivedAt": "now"},
                    ]
                ),
            ),
            patch(
                "t3_controller.operations.dispatch_command",
                new=AsyncMock(return_value={"sequence": 3}),
            ),
        ):
            archived = await archive_thread_operation(object(), archive_args, True)
        self.assertEqual(archived["thread"]["archivedAt"], "now")

    async def test_snooze_requires_exact_requested_time(self):
        before = {"id": "t1", "snoozedUntil": None}
        wrong = {"id": "t1", "snoozedUntil": "2098-01-01T00:00:00.000Z"}
        exact = {"id": "t1", "snoozedUntil": "2099-01-01T00:00:00.000Z"}
        command_args = args(
            environment="mac",
            thread_id="t1",
            until="2099-01-01T00:00:00Z",
            dry_run=False,
        )
        with (
            patch(
                "t3_controller.operations.require_thread_snapshot",
                new=AsyncMock(
                    side_effect=[{"thread": before}, {"thread": wrong}, {"thread": exact}]
                ),
            ),
            patch(
                "t3_controller.operations.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ),
            patch("t3_controller.operations.asyncio.sleep", new=AsyncMock()),
        ):
            result = await thread_state_operation(object(), command_args, "snooze")
        self.assertEqual(result["thread"]["snoozedUntil"], exact["snoozedUntil"])

    async def test_delete_checks_identity_running_state_and_final_absence(self):
        command_args = args(
            environment="mac",
            thread_id="t1",
            confirm_thread_id="wrong",
            dry_run=False,
        )
        with self.assertRaisesRegex(ValueError, "exactly match"):
            await delete_thread_operation(object(), command_args)

        command_args.confirm_thread_id = "t1"
        with patch(
            "t3_controller.operations.find_any_shell_thread",
            new=AsyncMock(return_value={"id": "t1", "latestTurn": {"state": "running"}}),
        ):
            with self.assertRaisesRegex(ValueError, "running turn"):
                await delete_thread_operation(object(), command_args)

        with (
            patch(
                "t3_controller.operations.find_any_shell_thread",
                new=AsyncMock(side_effect=[{"id": "t1", "latestTurn": {"state": "completed"}}, None]),
            ),
            patch(
                "t3_controller.operations.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ),
        ):
            result = await delete_thread_operation(object(), command_args)
        self.assertTrue(result["deleted"])


if __name__ == "__main__":
    unittest.main()
