import json
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

from t3_rpc_controller import (
    DEFAULT_HOME,
    EnvironmentStore,
    bearer_token_from_bootstrap,
    bearer_token_from_oauth,
    build_thread_create_command,
    build_thread_lifecycle_command,
    build_thread_model_command,
    build_thread_rename_command,
    build_thread_stop_command,
    build_turn_start_command,
    build_approval_response_command,
    build_user_input_dismiss_command,
    build_user_input_response_command,
    build_parser,
    dry_run_result,
    emit_json,
    filter_thread_rows,
    first_shell_snapshot,
    first_stream_snapshot,
    make_request,
    merge_environment_projects,
    normalize_url,
    normalize_future_datetime,
    oauth_exchange_payload,
    provider_row,
    project_rows_from_snapshot,
    read_pairing_credential,
    safe_environment_config,
    safe_model_row,
    is_terminal_thread_event,
    is_terminal_thread_snapshot,
    thread_snapshot_summary,
    model_selection,
    pending_requests_from_thread,
    request_response_outcome,
    require_codex_provider,
    read_prompt,
    read_json_object,
    redact_sensitive,
    require_pending_request,
    list_many_projects,
)


class EnvironmentStoreTests(unittest.TestCase):
    def test_default_state_is_self_contained_beside_controller(self):
        self.assertEqual(DEFAULT_HOME, Path(__file__).resolve().parent / ".state")

    def test_pairing_credential_uses_visible_input(self):
        with patch("builtins.input", return_value="  one-time-token  ") as visible_input:
            self.assertEqual(read_pairing_credential(), "one-time-token")
        visible_input.assert_called_once_with("One-time T3 pairing token: ")

    def test_rejects_hermes_url_annotation_as_cli_input(self):
        with self.assertRaisesRegex(ValueError, "plain URL"):
            normalize_url("@url:`http://100.65.116.80:3773`")

    def test_saves_token_separately_with_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EnvironmentStore(Path(directory))
            store.save("i7", "http://100.65.116.80:3773", "secret-token")

            config = json.loads((Path(directory) / "environments.json").read_text())
            self.assertEqual(config["i7"]["url"], "http://100.65.116.80:3773")
            self.assertNotIn("token", config["i7"])

            token_path = Path(directory) / "tokens" / "i7"
            self.assertEqual(token_path.read_text(), "secret-token")
            self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)

    def test_environment_removal_is_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EnvironmentStore(Path(directory) / "state")
            store.save("test", "http://127.0.0.1:3773", "private-token")
            backup = store.remove("test")
            self.assertNotIn("test", store.names())
            self.assertFalse((store.token_dir / "test").exists())
            self.assertEqual((backup / "token").read_text(), "private-token")
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)

    def test_rejects_unsafe_environment_names(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EnvironmentStore(Path(directory))
            with self.assertRaises(ValueError):
                store.save("../escape", "http://localhost:3773", "secret")

    def test_safe_environment_config_only_returns_urls(self):
        store = EnvironmentStore(Path("/unused"))
        with patch.object(
            store,
            "load_config",
            return_value={"mac": {"url": "http://localhost:3773", "token": "secret"}},
        ):
            self.assertEqual(
                safe_environment_config(store),
                {"mac": {"url": "http://localhost:3773"}},
            )


class RpcProtocolTests(unittest.TestCase):
    def test_builds_current_t3_oauth_exchange(self):
        self.assertEqual(
            oauth_exchange_payload("pair-code"),
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": "pair-code",
                "subject_token_type": "urn:t3:params:oauth:token-type:environment-bootstrap",
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "client_label": "hermes-agent",
                "client_device_type": "bot",
                "client_os": "macos",
            },
        )

    def test_requires_bearer_access_token_in_oauth_response(self):
        self.assertEqual(
            bearer_token_from_oauth({"token_type": "Bearer", "access_token": "access"}),
            "access",
        )
        with self.assertRaises(RuntimeError):
            bearer_token_from_oauth({"token_type": "DPoP", "access_token": "access"})

    def test_requires_session_token_in_bootstrap_response(self):
        self.assertEqual(
            bearer_token_from_bootstrap({"authenticated": True, "sessionToken": "bearer"}),
            "bearer",
        )
        with self.assertRaises(RuntimeError):
            bearer_token_from_bootstrap({"authenticated": True})

    def test_request_contains_current_t3_headers_field(self):
        self.assertEqual(
            make_request("req-1", "orchestration.subscribeShell", {}),
            {
                "_tag": "Request",
                "id": "req-1",
                "tag": "orchestration.subscribeShell",
                "payload": {},
                "headers": [],
            },
        )

    def test_project_rows_are_sorted_by_title(self):
        snapshot = {
            "projects": [
                {"id": "2", "title": "Zulu", "workspaceRoot": "/z"},
                {"id": "1", "title": "Alpha", "workspaceRoot": "/a"},
            ]
        }
        self.assertEqual(
            project_rows_from_snapshot(snapshot),
            [
                {
                    "id": "1",
                    "title": "Alpha",
                    "workspaceRoot": "/a",
                    "defaultModelSelection": None,
                    "defaultThreadEnvMode": None,
                    "repositoryIdentity": None,
                    "autoPull": None,
                    "scripts": [],
                },
                {
                    "id": "2",
                    "title": "Zulu",
                    "workspaceRoot": "/z",
                    "defaultModelSelection": None,
                    "defaultThreadEnvMode": None,
                    "repositoryIdentity": None,
                    "autoPull": None,
                    "scripts": [],
                },
            ],
        )

    def test_extracts_shell_snapshot_from_stream_chunks(self):
        messages = [
            {"_tag": "Pong"},
            {
                "_tag": "Chunk",
                "requestId": "req-1",
                "values": [
                    {"kind": "event", "event": {"type": "ignored"}},
                    {"kind": "snapshot", "snapshot": {"projects": [{"id": "p1"}]}},
                ],
            },
        ]
        self.assertEqual(first_shell_snapshot(messages, "req-1"), {"projects": [{"id": "p1"}]})

    def test_extracts_thread_snapshot_from_stream_chunks(self):
        message = {
            "_tag": "Chunk",
            "requestId": "req-2",
            "values": [{"kind": "snapshot", "snapshot": {"thread": {"id": "t1"}}}],
        }
        self.assertEqual(first_stream_snapshot([message], "req-2"), {"thread": {"id": "t1"}})

    def test_filters_threads_by_project_turn_state_and_pending_action(self):
        rows = [
            {
                "id": "t1",
                "projectId": "p1",
                "latestTurn": {"state": "running"},
                "hasPendingApprovals": True,
                "hasPendingUserInput": False,
                "updatedAt": "2026-09-13T10:00:00Z",
            },
            {
                "id": "t2",
                "projectId": "p2",
                "latestTurn": {"state": "completed"},
                "hasPendingApprovals": False,
                "hasPendingUserInput": False,
                "updatedAt": "2026-09-13T11:00:00Z",
            },
        ]
        self.assertEqual(
            [row["id"] for row in filter_thread_rows(rows, "p1", "running", "any")],
            ["t1"],
        )

    def test_merges_projects_without_deduplicating_across_machines(self):
        merged = merge_environment_projects(
            {
                "mac": [{"id": "same", "title": "Repo", "workspaceRoot": "/mac/repo"}],
                "i7": [{"id": "same", "title": "Repo", "workspaceRoot": "C:\\repo"}],
            }
        )
        self.assertEqual(
            merged,
            [
                {"environment": "i7", "id": "same", "title": "Repo", "workspaceRoot": "C:\\repo"},
                {"environment": "mac", "id": "same", "title": "Repo", "workspaceRoot": "/mac/repo"},
            ],
        )

    def test_provider_output_omits_auth_identity_and_driver_configuration(self):
        row = provider_row(
            {
                "instanceId": "codex_work",
                "driver": "codex",
                "enabled": True,
                "installed": True,
                "status": "ready",
                "auth": {"status": "authenticated", "email": "private@example.com"},
                "config": {"token": "secret"},
                "models": [],
            },
            "mac",
        )
        self.assertEqual(row["authStatus"], "authenticated")
        self.assertNotIn("auth", row)
        self.assertNotIn("config", row)

    def test_model_output_keeps_selection_metadata(self):
        row = safe_model_row(
            {
                "slug": "gpt-test",
                "name": "GPT Test",
                "isDefault": True,
                "isCustom": False,
                "capabilities": {"optionDescriptors": []},
            }
        )
        self.assertEqual(row["slug"], "gpt-test")
        self.assertTrue(row["isDefault"])
        self.assertEqual(row["capabilities"], {"optionDescriptors": []})

    def test_thread_snapshot_summary_does_not_repeat_message_contents(self):
        summary = thread_snapshot_summary(
            {
                "snapshotSequence": 42,
                "thread": {
                    "id": "t1",
                    "messages": [{"text": "private prompt"}],
                    "activities": [],
                    "checkpoints": [],
                    "proposedPlans": [],
                },
            },
            "mac",
        )
        self.assertEqual(summary["messageCount"], 1)
        self.assertNotIn("messages", summary["thread"])

    def test_terminal_event_requires_a_settled_session_status(self):
        self.assertTrue(
            is_terminal_thread_event(
                {
                    "kind": "event",
                    "event": {
                        "type": "thread.session-set",
                        "payload": {"session": {"status": "ready"}},
                    },
                }
            )
        )
        self.assertFalse(
            is_terminal_thread_event(
                {
                    "kind": "event",
                    "event": {
                        "type": "thread.session-set",
                        "payload": {"session": {"status": "running"}},
                    },
                }
            )
        )

    def test_terminal_snapshot_requires_no_running_turn(self):
        self.assertTrue(
            is_terminal_thread_snapshot(
                {"thread": {"latestTurn": {"state": "completed"}, "session": {"status": "ready"}}}
            )
        )
        self.assertFalse(
            is_terminal_thread_snapshot(
                {"thread": {"latestTurn": {"state": "running"}, "session": {"status": "ready"}}}
            )
        )

    def test_builds_thread_create_with_explicit_routing(self):
        command = build_thread_create_command(
            thread_id="t1",
            project_id="p1",
            title="Controller test",
            selection=model_selection("codex_work", "gpt-test"),
            runtime_mode="approval-required",
            interaction_mode="plan",
            branch=None,
            worktree_path=None,
        )
        self.assertEqual(command["type"], "thread.create")
        self.assertEqual(command["modelSelection"], {"instanceId": "codex_work", "model": "gpt-test"})
        self.assertEqual(command["projectId"], "p1")

    def test_builds_turn_start_without_echoing_requirements_into_output_helpers(self):
        command = build_turn_start_command(
            thread_id="t1",
            text="Do the work",
            runtime_mode="full-access",
            interaction_mode="default",
        )
        self.assertEqual(command["message"]["text"], "Do the work")
        self.assertEqual(command["message"]["role"], "user")
        self.assertEqual(command["message"]["attachments"], [])

    def test_builds_model_update_and_reads_prompt_files(self):
        command = build_thread_model_command("t1", model_selection("codex", "gpt-test"))
        self.assertEqual(command["type"], "thread.meta.update")
        self.assertEqual(command["modelSelection"]["model"], "gpt-test")
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt.txt"
            prompt.write_text("Task from file")
            self.assertEqual(read_prompt(None, str(prompt)), "Task from file")

    def test_builds_rename_and_archive_commands(self):
        rename = build_thread_rename_command("t1", "New title")
        self.assertEqual(rename["type"], "thread.meta.update")
        self.assertEqual(rename["title"], "New title")
        archive = build_thread_lifecycle_command("t1", "archive")
        self.assertEqual(archive["type"], "thread.archive")
        self.assertEqual(archive["threadId"], "t1")
        delete = build_thread_lifecycle_command("t1", "delete")
        self.assertEqual(delete["type"], "thread.delete")

    def test_normalizes_future_snooze_time_and_rejects_naive_time(self):
        self.assertEqual(
            normalize_future_datetime("2099-01-01T05:30:00+05:30"),
            "2099-01-01T00:00:00.000Z",
        )
        with self.assertRaisesRegex(ValueError, "timezone"):
            normalize_future_datetime("2099-01-01T00:00:00")

    def test_redacts_sensitive_keys_and_transport_values(self):
        redacted = redact_sensitive(
            {
                "accessToken": "private",
                "message": "Bearer abc123 at ws://host/ws?wsTicket=ticket123",
            }
        )
        self.assertEqual(redacted["accessToken"], "[REDACTED]")
        self.assertNotIn("abc123", redacted["message"])
        self.assertNotIn("ticket123", redacted["message"])

    def test_normal_json_output_redacts_nested_thread_content(self):
        output = io.StringIO()
        with redirect_stdout(output):
            emit_json(
                {
                    "messages": [{"text": "Bearer private-value"}],
                    "activities": [{"payload": {"credential": "private-value"}}],
                }
            )
        rendered = output.getvalue()
        self.assertNotIn("private-value", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_dry_run_hides_task_and_answer_contents(self):
        command = build_turn_start_command(
            thread_id="t1",
            text="private task text",
            runtime_mode="approval-required",
            interaction_mode="default",
        )
        preview = dry_run_result("mac", command)
        self.assertEqual(preview["planned"]["messageCharacters"], 17)
        self.assertNotIn("private task text", json.dumps(preview))
        answer_preview = dry_run_result(
            "mac",
            build_user_input_response_command("t1", "q1", {"color": "private answer"}),
        )
        self.assertEqual(answer_preview["planned"]["answerQuestionIds"], ["color"])
        self.assertNotIn("private answer", json.dumps(answer_preview))

    def test_argument_errors_are_json_and_reject_negative_limits(self):
        parser = build_parser()
        with patch("sys.stderr") as stderr, self.assertRaises(SystemExit) as raised:
            parser.parse_args(["threads", "--limit", "-1"])
        self.assertEqual(raised.exception.code, 2)
        output = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertEqual(json.loads(output)["error"]["category"], "usage")

    def test_builds_turn_interrupt_and_session_stop_commands(self):
        interrupt = build_thread_stop_command("t1", "turn-1", False)
        self.assertEqual(interrupt["type"], "thread.turn.interrupt")
        self.assertEqual(interrupt["turnId"], "turn-1")
        session_stop = build_thread_stop_command("t1", None, True)
        self.assertEqual(session_stop["type"], "thread.session.stop")
        self.assertNotIn("turnId", session_stop)

    def test_pending_requests_keep_only_unresolved_items(self):
        thread = {
            "activities": [
                {
                    "kind": "approval.requested",
                    "summary": "Run command",
                    "turnId": "turn-1",
                    "createdAt": "2026-09-13T10:00:00Z",
                    "payload": {"requestId": "a1", "requestType": "command_execution"},
                },
                {
                    "kind": "user-input.requested",
                    "summary": "Choose",
                    "turnId": "turn-1",
                    "createdAt": "2026-09-13T10:01:00Z",
                    "payload": {"requestId": "q1", "questions": [{"id": "q"}]},
                },
                {
                    "kind": "approval.resolved",
                    "summary": "Approved",
                    "payload": {"requestId": "a1"},
                },
            ]
        }
        pending = pending_requests_from_thread(thread)
        self.assertEqual([item["requestId"] for item in pending], ["q1"])

    def test_stale_failed_response_is_not_pending_or_success(self):
        thread = {
            "activities": [
                {"kind": "approval.requested", "payload": {"requestId": "a1"}},
                {
                    "kind": "provider.approval.respond.failed",
                    "payload": {"requestId": "a1", "detail": "stale pending"},
                },
            ]
        }
        self.assertEqual(pending_requests_from_thread(thread), [])
        self.assertEqual(request_response_outcome(thread, "a1", "approval.requested"), "failed")
        with self.assertRaisesRegex(ValueError, "not pending"):
            require_pending_request(thread, "a1", "approval.requested")

    def test_live_task_provider_is_restricted_to_codex(self):
        require_codex_provider("codex")
        with self.assertRaisesRegex(ValueError, "restricted to provider instance codex"):
            require_codex_provider("claude")

    def test_builds_approval_and_user_input_responses(self):
        approval = build_approval_response_command("t1", "a1", "accept")
        self.assertEqual(approval["type"], "thread.approval.respond")
        self.assertEqual(approval["decision"], "accept")
        answer = build_user_input_response_command("t1", "q1", {"question": "Blue"})
        self.assertEqual(answer["answers"], {"question": "Blue"})
        dismiss = build_user_input_dismiss_command("t1", "q1")
        self.assertEqual(dismiss["type"], "thread.user-input.dismiss")

    def test_reads_json_answers_and_rejects_non_objects(self):
        self.assertEqual(read_json_object('{"q1":"Blue"}', None), {"q1": "Blue"})
        with self.assertRaisesRegex(ValueError, "JSON object"):
            read_json_object('["Blue"]', None)

    def test_requires_exact_pending_request_kind(self):
        thread = {
            "id": "t1",
            "activities": [
                {
                    "kind": "approval.requested",
                    "summary": "Run",
                    "payload": {"requestId": "a1"},
                }
            ],
        }
        self.assertEqual(require_pending_request(thread, "a1", "approval.requested")["requestId"], "a1")
        with self.assertRaisesRegex(ValueError, "not user-input.requested"):
            require_pending_request(thread, "a1", "user-input.requested")


class MultiEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_listing_preserves_successes_when_one_environment_fails(self):
        async def fake_list(_store, name):
            if name == "i7":
                raise RuntimeError("offline")
            return [{"id": "p1", "title": "Repo", "workspaceRoot": "/repo"}]

        with patch("t3_rpc_controller.list_projects", new=AsyncMock(side_effect=fake_list)):
            projects, errors = await list_many_projects(EnvironmentStore(Path("/unused")), ["mac", "i7"])

        self.assertEqual(projects["mac"][0]["id"], "p1")
        self.assertEqual(errors, {"i7": "offline"})


if __name__ == "__main__":
    unittest.main()
