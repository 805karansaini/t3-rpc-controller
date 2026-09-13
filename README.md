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

Check that each machine still supports the authenticated shell, archived records, provider discovery, and other protocol surfaces required by this controller:

```bash
./t3_rpc_controller.py compatibility
./t3_rpc_controller.py compatibility --environment i7
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

Create a thread with explicit machine, project, provider, model, and safety modes:

```bash
./t3_rpc_controller.py thread create mac PROJECT_ID \
  --title "Task title" \
  --provider codex \
  --model gpt-6-astra \
  --runtime-mode approval-required \
  --interaction-mode default
```

Send a task. The result confirms the message ID and current thread state without repeating the prompt:

```bash
./t3_rpc_controller.py thread send mac THREAD_ID "Task text"
./t3_rpc_controller.py thread send mac THREAD_ID --prompt-file /path/to/prompt.md
printf '%s' "Task text" | ./t3_rpc_controller.py thread send mac THREAD_ID --prompt-file -
```

Change the provider and model for an existing thread:

```bash
./t3_rpc_controller.py thread model mac THREAD_ID --provider codex --model gpt-5.6-luna
```

The controller verifies the project, provider, and model before dispatch. It then reads the thread back and only reports success after T3 confirms the new state.

Inspect unresolved approval and user-input requests for one thread:

```bash
./t3_rpc_controller.py thread pending mac THREAD_ID
```

Interrupt the active turn, or stop the provider session completely:

```bash
./t3_rpc_controller.py thread stop mac THREAD_ID
./t3_rpc_controller.py thread stop mac THREAD_ID --session
```

The first command requires a running turn and confirms that T3 marked it interrupted. The session form confirms that the provider session reached `stopped` or `error`.

Answer one exact pending approval request:

```bash
./t3_rpc_controller.py thread approve mac THREAD_ID REQUEST_ID accept
```

Supported decisions are `accept`, `acceptForSession`, `acceptAlways`, `decline`, and `cancel`. The controller first confirms that the request is still pending on that thread, then confirms that T3 cleared it. This pre-check prevents an agent from accidentally answering an old request twice.

Answer a pending question with a JSON object keyed by T3's question IDs, or dismiss an asynchronous message-mode question:

```bash
./t3_rpc_controller.py thread answer mac THREAD_ID REQUEST_ID \
  --answers-json '{"QUESTION_ID":"Answer"}'
./t3_rpc_controller.py thread answer mac THREAD_ID REQUEST_ID \
  --answers-file /path/to/answers.json
./t3_rpc_controller.py thread dismiss-input mac THREAD_ID REQUEST_ID
```

Live task execution is intentionally limited to the `codex` provider. The controller can still discover other configured providers, but its live task examples and verification use only Codex on the Mac and i7 environments.

Rename a thread, or move it into and out of T3's archive:

```bash
./t3_rpc_controller.py thread rename mac THREAD_ID "New title"
./t3_rpc_controller.py thread archive mac THREAD_ID
./t3_rpc_controller.py thread unarchive mac THREAD_ID
```

Each command refuses an incompatible current state and confirms the new title or archive location before returning success.

Organize an active thread without deleting it:

```bash
./t3_rpc_controller.py thread pin mac THREAD_ID
./t3_rpc_controller.py thread unpin mac THREAD_ID
./t3_rpc_controller.py thread snooze mac THREAD_ID --until 2026-09-14T09:00:00+05:30
./t3_rpc_controller.py thread unsnooze mac THREAD_ID
./t3_rpc_controller.py thread settle mac THREAD_ID
./t3_rpc_controller.py thread unsettle mac THREAD_ID
```

Snooze times must be in the future and include a timezone. Every command confirms the corresponding T3 state before it reports success.

Read a focused conversation history or the filesystem diff represented by T3 checkpoints:

```bash
./t3_rpc_controller.py thread history mac THREAD_ID --turn-limit 100
./t3_rpc_controller.py thread diff mac THREAD_ID --to-turn-count 4
./t3_rpc_controller.py thread diff mac THREAD_ID --from-turn-count 1 --to-turn-count 4
```

Permanently delete a stopped thread only by repeating its exact identity:

```bash
./t3_rpc_controller.py thread delete mac THREAD_ID --confirm-thread-id THREAD_ID
```

Deletion refuses a mismatched confirmation and a thread with a running turn. It checks both the active and archived lists and reports success only after the thread has disappeared from both.

Run tests:

```bash
make check
```

The suite includes a local fake T3 endpoint. It exercises bearer authentication, WebSocket ticket exchange, the initial ping, Effect RPC headers, streamed snapshots, server failures, malformed messages, timeouts, and secret redaction without contacting the Mac or i7 T3 installations.

Run the complete read-only check against both configured machines:

```bash
make live-smoke
```

## Verified scope and limitations

- Live task work uses only the Codex provider. Provider discovery remains generic so an agent can inspect T3 configuration without running other providers.
- The Mac was used for live creation, task completion, stopping, approval, organization, history, diff, and deletion checks. The i7 passed the full read-only health, compatibility, project, thread, and provider checks.
- The installed Codex mode did not create a live pending user-question request during verification. `thread answer` and `thread dismiss-input` follow the installed T3 schema and pass automated tests, but could not be exercised against a real pending Codex question.
- This project intentionally has no graphical interface and starts no local service. Use the existing T3 Code app for visual work.

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

If a stored credential expires, run `pair` again with the same environment name; the new verified credential replaces the old one without being printed. To remove an environment, repeat its exact name:

```bash
./t3_rpc_controller.py remove-environment NAME --confirm-name NAME
```

Removal takes the environment out of active configuration and moves its local metadata and credential into a protected `.state/removed/` backup, so an accidental removal is recoverable on this machine.

All successful commands return JSON. Operational failures also return JSON on standard error with a stable category and exit status 1; invalid command usage returns category `usage` and exit status 2. Server error bodies, bearer values, WebSocket tickets, pairing values, and other credential-like fields are not included in errors.

Every thread-changing command, plus environment removal, accepts `--dry-run`. The controller still validates the named environment, project, thread, provider, model, current state, and confirmation values, but does not dispatch the change. Task text and answer contents are replaced by lengths or question IDs in the preview.

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

The executable provides environment setup, project/thread/provider/model discovery, task creation and dispatch, live monitoring, stopping, approvals, and user-input handling. Thread organization and deletion commands are the next build phase; see `ROADMAP.md` for the authoritative checklist.
