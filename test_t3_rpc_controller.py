import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from t3_rpc_controller import (
    DEFAULT_HOME,
    EnvironmentStore,
    bearer_token_from_bootstrap,
    bearer_token_from_oauth,
    first_shell_snapshot,
    make_request,
    merge_environment_projects,
    normalize_url,
    oauth_exchange_payload,
    project_rows_from_snapshot,
    read_pairing_credential,
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

    def test_rejects_unsafe_environment_names(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EnvironmentStore(Path(directory))
            with self.assertRaises(ValueError):
                store.save("../escape", "http://localhost:3773", "secret")


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
                {"id": "1", "title": "Alpha", "workspaceRoot": "/a"},
                {"id": "2", "title": "Zulu", "workspaceRoot": "/z"},
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


if __name__ == "__main__":
    unittest.main()
