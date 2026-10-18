import asyncio
import http.server
import json
import threading
import unittest
from unittest.mock import AsyncMock, patch

from websockets.asyncio.server import serve

from t3_controller.transport import fetch_shell_snapshot, fetch_thread_snapshot, http_json, rpc_call


class FakeT3Server:
    def __init__(self):
        self.server = None
        self.http_server = None
        self.http_thread = None
        self.websocket_patch = None
        self.url = None
        self.ws_url = None
        self.frames = []

    async def __aenter__(self):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def send_json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path == "/api/auth/websocket-ticket":
                    if self.headers.get("Authorization") != "Bearer fake-bearer":
                        self.send_json(401, {"accessToken": "must-not-leak"})
                    else:
                        self.send_json(200, {"ticket": "fake-ticket"})
                    return
                self.send_json(404, {})

            def do_GET(self):
                if self.path == "/api/auth/session":
                    self.send_json(200, {"authenticated": True})
                elif self.path == "/fail":
                    self.send_json(500, {"credential": "must-not-leak"})
                else:
                    self.send_json(404, {})

            def log_message(self, format, *args):
                pass

        self.http_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.http_thread.start()
        self.url = f"http://127.0.0.1:{self.http_server.server_port}"
        self.server = await serve(self.handle_websocket, "127.0.0.1", 0)
        ws_port = self.server.sockets[0].getsockname()[1]
        self.ws_url = f"ws://127.0.0.1:{ws_port}"
        self.websocket_patch = patch(
            "t3_controller.transport.websocket_url",
            side_effect=lambda http_url, ticket: f"{self.ws_url}/ws?wsTicket={ticket}",
        )
        self.websocket_patch.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.websocket_patch.stop()
        self.server.close()
        await self.server.wait_closed()
        await asyncio.to_thread(self.http_server.shutdown)
        self.http_server.server_close()
        self.http_thread.join(timeout=1)

    async def handle_websocket(self, connection):
        ping = json.loads(await connection.recv())
        request = json.loads(await connection.recv())
        self.frames.append(
            {
                "path": connection.request.path,
                "ping": ping,
                "request": request,
            }
        )
        request_id = request["id"]
        tag = request["tag"]
        if tag == "test.timeout":
            await asyncio.sleep(0.2)
            return
        if tag == "test.malformed":
            await connection.send("not-json")
            return
        if tag == "test.failure":
            await connection.send(
                json.dumps(
                    {
                        "_tag": "Exit",
                        "requestId": request_id,
                        "exit": {
                            "_tag": "Failure",
                            "accessToken": "must-not-leak",
                        },
                    }
                )
            )
            return
        if tag == "orchestration.subscribeShell":
            await connection.send(
                json.dumps(
                    {
                        "_tag": "Chunk",
                        "requestId": request_id,
                        "values": [
                            {
                                "kind": "snapshot",
                                "snapshot": {"projects": [], "threads": []},
                            }
                        ],
                    }
                )
            )
            return
        await connection.send(
            json.dumps(
                {
                    "_tag": "Exit",
                    "requestId": request_id,
                    "exit": {"_tag": "Success", "value": {"pong": True}},
                }
            )
        )


class TransportIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_thread_snapshot_disables_websocket_close_wait(self):
        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def send(self, raw):
                pass

            async def recv(self):
                await asyncio.sleep(1)

        with (
            patch(
                "t3_controller.transport.request_ticket",
                new=AsyncMock(return_value="ticket"),
            ),
            patch("t3_controller.transport.websockets.connect", return_value=Socket()) as connect,
        ):
            with self.assertRaises(asyncio.TimeoutError):
                await fetch_thread_snapshot(
                    "http://fake", "token", "thread-1", timeout=0.01
                )
        self.assertEqual(connect.call_args.kwargs["close_timeout"], 0)

    async def test_authenticated_rpc_uses_ticket_ping_and_headers(self):
        async with FakeT3Server() as fake:
            result = await rpc_call(fake.url, "fake-bearer", "server.probe", {})
            self.assertEqual(result, {"pong": True})
            frame = fake.frames[0]
            self.assertEqual(frame["path"], "/ws?wsTicket=fake-ticket")
            self.assertEqual(frame["ping"], {"_tag": "Ping"})
            self.assertEqual(frame["request"]["headers"], [])

    async def test_streamed_shell_snapshot_is_decoded(self):
        async with FakeT3Server() as fake:
            snapshot = await fetch_shell_snapshot(fake.url, "fake-bearer")
            self.assertEqual(snapshot, {"projects": [], "threads": []})

    async def test_malformed_failure_and_timeout_are_bounded_and_redacted(self):
        async with FakeT3Server() as fake:
            with self.assertRaises(json.JSONDecodeError):
                await rpc_call(fake.url, "fake-bearer", "test.malformed", {})
            with self.assertRaisesRegex(RuntimeError, "REDACTED") as failure:
                await rpc_call(fake.url, "fake-bearer", "test.failure", {})
            self.assertNotIn("must-not-leak", str(failure.exception))
            with self.assertRaises(asyncio.TimeoutError):
                await rpc_call(fake.url, "fake-bearer", "test.timeout", {}, timeout=0.05)

    async def test_http_error_body_is_not_exposed(self):
        async with FakeT3Server() as fake:
            with self.assertRaisesRegex(RuntimeError, "T3 HTTP 500") as raised:
                await asyncio.to_thread(http_json, f"{fake.url}/fail", None)
            self.assertNotIn("must-not-leak", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
