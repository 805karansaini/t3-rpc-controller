# T3 Native RPC Controller

A self-contained controller for reading and orchestrating multiple T3 Code servers through T3's authenticated HTTP and Effect RPC WebSocket interfaces. It does not depend on the Hermes/T3 preview session or a third-party T3 CLI wrapper.

## Location and required state

```text
/Users/karansaini/Projects/t3-rpc-controller/
├── t3_rpc_controller.py
├── test_t3_rpc_controller.py
├── requirements.txt
├── README.md
├── .gitignore
└── .state/                    # mandatory local state; never commit
    ├── environments.json      # endpoint metadata, no secrets
    └── tokens/
        ├── mac                # bearer secret, mode 0600
        └── i7                 # bearer secret, mode 0600
```

The script resolves `.state` relative to its own file, so it works regardless of the caller's current directory. `.state` is mandatory for existing connections and moves with the controller. It is excluded by `.gitignore` because it contains bearer credentials.

Expected permissions:

- `.state/`: `0700`
- `.state/tokens/`: `0700`
- `.state/environments.json`: `0600`
- Each file under `.state/tokens/`: `0600`

Never print, commit, upload, quote, log, or send token contents to an agent/chat. The `environments` command intentionally shows only non-secret endpoint metadata.

## Configured environments

- `mac`: local T3 desktop server at `http://127.0.0.1:3773`
- `i7`: remote T3 desktop server over Tailscale at `http://100.65.116.80:3773`

Each T3 server owns its own projects, threads, providers, models, terminals, and filesystem. A project with the same title on both machines is still a distinct project and must be addressed with both environment name and project ID/path.

## Protocol compatibility

The controller implements the T3 protocol required by the installed builds:

1. Authenticate HTTP requests with a dedicated bearer session.
2. Request a short-lived ticket using `POST /api/auth/websocket-ticket`.
3. Connect to `/ws?wsTicket=<ticket>`; never put the bearer in the WebSocket URL.
4. Send the initial `{"_tag":"Ping"}` message.
5. Send Effect RPC requests with `headers: []`.
6. Consume streamed `Chunk` messages until the shell snapshot arrives.

Pairing is version-adaptive:

- Current T3 builds advertising `bearer-access-token` use form-encoded OAuth token exchange at `/oauth/token`.
- Older builds fall back to `/api/auth/bootstrap/bearer`.
- The returned bearer is verified through `/api/auth/session` before it is saved.

## Requirements

- Python 3.11 or newer
- `websockets` 15–16
- Tailscale connectivity for the i7 endpoint
- Running T3 desktop/server on each target machine

Install the Python dependency if needed:

```bash
cd /Users/karansaini/Projects/t3-rpc-controller
python3 -m pip install -r requirements.txt
```

## Commands

List all projects from all configured machines concurrently:

```bash
./t3_rpc_controller.py projects
```

List one environment:

```bash
./t3_rpc_controller.py projects --environment mac
./t3_rpc_controller.py projects --environment i7
```

List configuration without exposing tokens:

```bash
./t3_rpc_controller.py environments
```

Check authentication and native RPC access on every configured machine:

```bash
./t3_rpc_controller.py health
./t3_rpc_controller.py health --environment mac
```

Multi-machine reads include successful results and an `errors` object when one target is unavailable. The command exits with status 1 if any requested target failed, while preserving data returned by healthy targets.

List recent thread summaries or narrow them to one machine, project, turn state, or pending action:

```bash
./t3_rpc_controller.py threads --limit 20
./t3_rpc_controller.py threads --environment mac --project PROJECT_ID
./t3_rpc_controller.py threads --turn-state running
./t3_rpc_controller.py threads --pending any
./t3_rpc_controller.py threads --scope archived
```

Read the latest turns and detailed state for one exact thread:

```bash
./t3_rpc_controller.py thread show mac THREAD_ID --turn-limit 20
```

Follow one thread as newline-delimited JSON. The first line summarizes the snapshot without repeating message contents, subsequent lines are ordered T3 events, and the final line explains why following stopped:

```bash
./t3_rpc_controller.py thread follow mac THREAD_ID --timeout 60
./t3_rpc_controller.py thread follow mac THREAD_ID --until-terminal --timeout 600
./t3_rpc_controller.py thread follow mac THREAD_ID --after-sequence SEQUENCE
```

Use `--timeout 0` to wait indefinitely. The follower reconnects after a dropped connection, resumes from the last event sequence, and suppresses duplicate events.

Search user and assistant messages across one or every machine:

```bash
./t3_rpc_controller.py search "text to find" --environment mac --limit 20
```

List configured provider instances, their health, capabilities, and models without exposing account identity or driver configuration:

```bash
./t3_rpc_controller.py providers
./t3_rpc_controller.py providers --environment mac --include-disabled
```

List models or narrow the result to an exact provider instance:

```bash
./t3_rpc_controller.py models --environment mac
./t3_rpc_controller.py models --environment mac --provider codex
```

Run tests:

```bash
python3 -m unittest -v test_t3_rpc_controller.py
```

## Pair or renew an environment

On the target machine, generate a fresh one-time pairing credential:

```powershell
npx t3 pair
```

On this Mac, exchange it through the controller. Pairing input is visible by user request, but the resulting long-lived bearer is not printed:

```bash
./t3_rpc_controller.py pair i7 --url http://100.65.116.80:3773
```

Do not paste pairing tokens into chat. If one is exposed, generate a new one.

To import an already-issued bearer from a protected file:

```bash
./t3_rpc_controller.py configure NAME --url http://HOST:PORT --token-file /protected/token/file
```

## Guidance for another agent

1. Work from `/Users/karansaini/Projects/t3-rpc-controller`.
2. Run `python3 -m unittest -v test_t3_rpc_controller.py` before and after protocol changes.
3. Use `./t3_rpc_controller.py environments` to discover configured targets without reading `.state/tokens`.
4. Begin with read-only `projects` queries. Do not mutate a thread until environment, project ID/path, thread ID, provider instance, model, and current thread state are explicitly verified.
5. Treat `127.0.0.1:3773` as the Mac only; it never refers to the i7.
6. Use the i7 Tailscale endpoint directly; the preview's cookies are a separate authentication context.
7. Preserve the exact native protocol fields (`wsTicket`, initial `Ping`, `headers: []`, streamed `Chunk` handling).
8. After any future mutation, read the exact target back through RPC before reporting success.
9. Never inspect or reveal bearer file contents. If authentication fails, renew pairing rather than copying secrets into logs or chat.
10. Keep `.state/` outside version control. If cloning the code to another machine, pair again instead of copying bearer credentials unless the user explicitly requires a secure credential migration.

## Current scope

The executable currently provides environment registration/pairing and project enumeration. The underlying authenticated RPC transport is ready to be extended for thread listing, thread subscription, model/provider selection, command dispatch, and monitoring. Add those operations with tests and read-before-write safeguards.
