import argparse
import asyncio
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

from t3_rpc_controller import (
    approval_response_operation,
    archive_thread_operation,
    create_thread_operation,
    delete_thread_operation,
    follow_thread,
    rename_thread_operation,
    send_thread_operation,
    set_thread_model_operation,
    stop_thread_operation,
    thread_state_operation,
    user_input_response_operation,
    wait_for_request_cleared,
)


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

        event_1 = {"kind": "event", "event": {"eventId": "e1", "sequence": 1, "type": "x"}}
        event_2 = {"kind": "event", "event": {"eventId": "e2", "sequence": 2, "type": "x"}}
        first = Socket([[event_1]], disconnect=True)
        second = Socket([[event_1, event_2]])
        output = io.StringIO()
        with (
            patch("t3_rpc_controller.asyncio.to_thread", new=AsyncMock(return_value={"ticket": "t"})),
            patch("t3_rpc_controller.websockets.connect", side_effect=[first, second]) as connect,
            patch("t3_rpc_controller.asyncio.sleep", new=AsyncMock()),
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
        self.assertEqual([line["event"]["eventId"] for line in lines], ["e1", "e2"])
        self.assertEqual(result["reconnects"], 1)
        self.assertEqual(result["lastSequence"], 2)
        self.assertEqual(second.request_payload["afterSequence"], 1)
        self.assertEqual(connect.call_count, 2)

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
            patch("t3_rpc_controller.uuid.uuid4", return_value="thread-1"),
            patch("t3_rpc_controller.validate_project_and_model", new=AsyncMock()),
            patch(
                "t3_rpc_controller.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ),
            patch(
                "t3_rpc_controller.wait_for_shell_thread",
                new=AsyncMock(return_value=exact),
            ) as wait,
        ):
            result = await create_thread_operation(object(), command_args)
        predicate = wait.await_args.args[3]
        self.assertTrue(predicate(exact))
        self.assertFalse(predicate({**exact, "title": "Wrong"}))
        self.assertFalse(predicate({**exact, "runtimeMode": "full-access"}))
        self.assertEqual(result["thread"]["projectId"], "p1")

    async def test_send_uses_codex_and_safe_runtime_default(self):
        thread = {
            "id": "t1",
            "modelSelection": {"instanceId": "codex", "model": "gpt-test"},
            "runtimeMode": None,
            "interactionMode": None,
            "messages": [],
        }
        confirmed = {**thread, "messages": [{"id": "message-1"}]}
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
            patch("t3_rpc_controller.uuid.uuid4", side_effect=["command-1", "message-1"]),
            patch(
                "t3_rpc_controller.require_thread_snapshot",
                new=AsyncMock(side_effect=[{"thread": thread}, {"thread": confirmed}]),
            ),
            patch(
                "t3_rpc_controller.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ) as dispatch,
        ):
            result = await send_thread_operation(object(), command_args, "Do work")
        command = dispatch.await_args.args[2]
        self.assertEqual(command["runtimeMode"], "approval-required")
        self.assertEqual(result["messageId"], "message-1")

        thread["modelSelection"]["instanceId"] = "claude"
        with patch(
            "t3_rpc_controller.require_thread_snapshot",
            new=AsyncMock(return_value={"thread": thread}),
        ):
            with self.assertRaisesRegex(ValueError, "restricted to provider instance codex"):
                await send_thread_operation(object(), command_args, "Do work")

    async def test_session_stop_requires_confirmed_terminal_session(self):
        thread = {"id": "t1", "latestTurn": {"state": "completed"}, "session": {"status": "ready"}}
        stopped = {**thread, "session": {"status": "stopped"}}
        command_args = args(environment="mac", thread_id="t1", session=True, dry_run=False)
        with (
            patch(
                "t3_rpc_controller.require_thread_snapshot",
                new=AsyncMock(return_value={"thread": thread}),
            ),
            patch(
                "t3_rpc_controller.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ),
            patch(
                "t3_rpc_controller.wait_for_shell_thread",
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
            "t3_rpc_controller.require_thread_snapshot",
            new=AsyncMock(return_value=failed),
        ):
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                await wait_for_request_cleared(
                    object(), "mac", "t1", "a1", "approval.requested", timeout=0
                )

    async def test_approval_operation_requires_resolution_event(self):
        pending = {
            "id": "t1",
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
                "t3_rpc_controller.require_thread_snapshot",
                new=AsyncMock(return_value={"thread": pending}),
            ),
            patch(
                "t3_rpc_controller.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ),
            patch(
                "t3_rpc_controller.wait_for_request_cleared",
                new=AsyncMock(return_value=resolved),
            ) as wait,
        ):
            result = await approval_response_operation(object(), command_args)
        self.assertTrue(result["ok"])
        self.assertEqual(wait.await_args.args[4], "approval.requested")

    async def test_user_input_operation_requires_resolution_event(self):
        pending = {
            "id": "t1",
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
                "t3_rpc_controller.require_thread_snapshot",
                new=AsyncMock(return_value={"thread": pending}),
            ),
            patch(
                "t3_rpc_controller.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ),
            patch(
                "t3_rpc_controller.wait_for_request_cleared",
                new=AsyncMock(return_value=resolved),
            ) as wait,
        ):
            result = await user_input_response_operation(
                object(), command_args, {"color": "Blue"}, False
            )
        self.assertEqual(result["answeredQuestionIds"], ["color"])
        self.assertEqual(wait.await_args.args[4], "user-input.requested")

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
            patch("t3_rpc_controller.require_thread_snapshot", new=AsyncMock()),
            patch("t3_rpc_controller.validate_model", new=AsyncMock()),
            patch(
                "t3_rpc_controller.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ),
            patch(
                "t3_rpc_controller.wait_for_shell_thread",
                new=AsyncMock(return_value=model_thread),
            ) as model_wait,
        ):
            await set_thread_model_operation(object(), model_args)
        self.assertTrue(model_wait.await_args.args[3](model_thread))
        self.assertFalse(model_wait.await_args.args[3]({"modelSelection": {"instanceId": "codex", "model": "wrong"}}))

        rename_args = args(environment="mac", thread_id="t1", title="New title", dry_run=False)
        with (
            patch("t3_rpc_controller.require_thread_snapshot", new=AsyncMock()),
            patch(
                "t3_rpc_controller.dispatch_command",
                new=AsyncMock(return_value={"sequence": 2}),
            ),
            patch(
                "t3_rpc_controller.wait_for_shell_thread",
                new=AsyncMock(return_value={"id": "t1", "title": "New title"}),
            ) as rename_wait,
        ):
            await rename_thread_operation(object(), rename_args)
        self.assertTrue(rename_wait.await_args.args[3]({"title": "New title"}))
        self.assertFalse(rename_wait.await_args.args[3]({"title": "Old title"}))

        archive_args = args(environment="mac", thread_id="t1", dry_run=False)
        with (
            patch(
                "t3_rpc_controller.find_shell_thread",
                new=AsyncMock(
                    side_effect=[
                        {"id": "t1", "archivedAt": None},
                        {"id": "t1", "archivedAt": "now"},
                    ]
                ),
            ),
            patch(
                "t3_rpc_controller.dispatch_command",
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
                "t3_rpc_controller.require_thread_snapshot",
                new=AsyncMock(
                    side_effect=[{"thread": before}, {"thread": wrong}, {"thread": exact}]
                ),
            ),
            patch(
                "t3_rpc_controller.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ),
            patch("t3_rpc_controller.asyncio.sleep", new=AsyncMock()),
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
            "t3_rpc_controller.find_any_shell_thread",
            new=AsyncMock(return_value={"id": "t1", "latestTurn": {"state": "running"}}),
        ):
            with self.assertRaisesRegex(ValueError, "running turn"):
                await delete_thread_operation(object(), command_args)

        with (
            patch(
                "t3_rpc_controller.find_any_shell_thread",
                new=AsyncMock(side_effect=[{"id": "t1", "latestTurn": {"state": "completed"}}, None]),
            ),
            patch(
                "t3_rpc_controller.dispatch_command",
                new=AsyncMock(return_value={"sequence": 1}),
            ),
        ):
            result = await delete_thread_operation(object(), command_args)
        self.assertTrue(result["deleted"])


if __name__ == "__main__":
    unittest.main()
