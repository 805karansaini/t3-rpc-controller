# Command reference

Run `t3ctl --help` or `t3ctl thread COMMAND --help` for exact arguments.
Examples use placeholder identities; discover real values from your own server.

List all projects from all configured machines concurrently:

```bash
t3ctl projects
```

List one environment:

```bash
t3ctl projects --environment mac
t3ctl projects --environment i7
```

List configuration without exposing tokens:

```bash
t3ctl environments
```

Check authentication and native RPC access on every configured machine:

```bash
t3ctl health
t3ctl health --environment mac
```

Check the authenticated shell, archived records, provider discovery, and one thread snapshot on each machine:

```bash
t3ctl compatibility
t3ctl compatibility --environment i7
```

The result labels each capability as `verified-now`, `supported-not-checked-now`, or not applicable. Read-only compatibility checks do not claim that task dispatch or lifecycle changes were exercised during that run.

Multi-machine reads include successful results and an `errors` object when one target is unavailable. The command exits with status 1 if any requested target failed, while preserving data returned by healthy targets.

List recent thread summaries or narrow them to one machine, project, turn state, or pending action:

```bash
t3ctl threads --limit 20
t3ctl threads --environment mac --project PROJECT_ID
t3ctl threads --turn-state running
t3ctl threads --pending any
t3ctl threads --scope archived
```

Read the latest turns and detailed state for one exact thread:

```bash
t3ctl thread show mac THREAD_ID --turn-limit 20
```

Follow one thread as newline-delimited JSON. The first line summarizes the snapshot without repeating message contents, subsequent lines are ordered T3 events, and the final line explains why following stopped. If T3 exposes a changed snapshot while the stream is being established or reconnected, the controller emits that updated summary instead of silently discarding it:

```bash
t3ctl thread follow mac THREAD_ID --timeout 60
t3ctl thread follow mac THREAD_ID --until-terminal --timeout 600
t3ctl thread follow mac THREAD_ID --after-sequence SEQUENCE
```

Use `--timeout 0` to wait indefinitely. The follower reconnects after a dropped connection, resumes from the last event sequence, and suppresses duplicate events.

Search user and assistant messages across one or every machine:

```bash
t3ctl search "text to find" --environment mac --limit 20
```

List configured provider instances, their health, capabilities, and models without exposing account identity or driver configuration:

```bash
t3ctl providers
t3ctl providers --environment mac --include-disabled
```

List models or narrow the result to an exact provider instance:

```bash
t3ctl models --environment mac
t3ctl models --environment mac --provider codex
```

Create a thread with explicit machine, project, provider, model, and safety modes:

```bash
t3ctl thread create mac PROJECT_ID \
  --title "Task title" \
  --provider codex \
  --model gpt-6-astra \
  --runtime-mode approval-required \
  --interaction-mode default
```

For a creation that may need to be retried after a connection failure, generate UUIDs once and reuse them:

```bash
t3ctl thread create mac PROJECT_ID \
  --title "Task title" --provider codex --model gpt-6-astra \
  --thread-id THREAD_UUID --command-id COMMAND_UUID
```

If that thread already exists with the exact requested settings, the controller returns it with `reconciled: true` instead of creating another one. A conflicting thread ID fails without changing anything.

Send a task. The result confirms the message ID and current thread state without repeating the prompt:

```bash
t3ctl thread send mac THREAD_ID "Task text"
t3ctl thread send mac THREAD_ID --prompt-file /path/to/prompt.md
printf '%s' "Task text" | t3ctl thread send mac THREAD_ID --prompt-file -
```

For a submission that may need to be retried, reuse one message UUID and command UUID:

```bash
t3ctl thread send mac THREAD_ID "Task text" \
  --message-id MESSAGE_UUID --command-id COMMAND_UUID
```

The controller reads the thread after an uncertain dispatch. If the exact user message is present, it reports `reconciled: true`. Repeating the command with the same identifiers returns the existing message rather than submitting it again. Reusing a message ID with different text is rejected. If dispatch returns but confirmation cannot be read, the JSON error has category `uncertain_result` and includes the exact identifiers under `error.retry`; reuse those identifiers on the next attempt.

Change the provider and model for an existing thread:

```bash
t3ctl thread model mac THREAD_ID --provider codex --model gpt-5.6-luna
```

The controller verifies the project, provider, and model before dispatch. It then reads the thread back and only reports success after T3 confirms the new state.

Inspect unresolved approval and user-input requests for one thread:

```bash
t3ctl thread pending mac THREAD_ID
```

Wait until a thread finishes or needs approval or input:

```bash
t3ctl thread wait mac THREAD_ID --timeout 600
t3ctl thread wait mac THREAD_ID --message-id MESSAGE_UUID --timeout 600
```

Use the `messageId` returned by `thread send` when waiting for newly submitted work. This prevents an older completed turn from being mistaken for completion of the new task. The timeout covers both polling delays and the network reads themselves. A pending approval or question returns immediately with `reason: attention-required`. A completed turn exits successfully; an interrupted, failed, timed-out, or superseded turn exits with status 1. `superseded` means a newer turn replaced the target before the controller observed its final state; T3 no longer exposes enough history to label that older result safely, so the controller reports `turnState: unknown` instead of guessing.

Submit and wait in one call when an agent does not need the live event stream:

```bash
t3ctl thread run mac THREAD_ID "Task text" --timeout 600
t3ctl thread run mac THREAD_ID --prompt-file /path/to/prompt.md --timeout 600
```

`thread run` uses the same Codex-only check, safe runtime default, retry identifiers, and exact-message tracking as `thread send` followed by `thread wait`. It returns when the task completes, fails, times out, or asks for approval or user input. Use `--dry-run` to validate the submission without starting work.

Interrupt the active turn, or stop the provider session completely:

```bash
t3ctl thread stop mac THREAD_ID
t3ctl thread stop mac THREAD_ID --session
```

The first command requires a running turn and confirms that T3 marked it interrupted. The session form confirms that the provider session reached `stopped` or `error`.

Answer one exact pending approval request:

```bash
t3ctl thread approve mac THREAD_ID REQUEST_ID accept
```

Supported decisions are `accept`, `acceptForSession`, `acceptAlways`, `decline`, and `cancel`. The controller first confirms that the request is still pending on that thread, then confirms that T3 cleared it. This pre-check prevents an agent from accidentally answering an old request twice.

Answer a pending question with a JSON object keyed by T3's question IDs, or dismiss an asynchronous message-mode question:

```bash
t3ctl thread answer mac THREAD_ID REQUEST_ID \
  --answers-json '{"QUESTION_ID":"Answer"}'
t3ctl thread answer mac THREAD_ID REQUEST_ID \
  --answers-file /path/to/answers.json
t3ctl thread dismiss-input mac THREAD_ID REQUEST_ID
```

Live task execution is limited to the `codex` provider. Thread creation, model changes, task sending, approvals, and question responses all enforce that boundary, so no command can silently start or continue work through another provider. The controller can still list other configured providers for read-only discovery.

Rename a thread, or move it into and out of T3's archive:

```bash
t3ctl thread rename mac THREAD_ID "New title"
t3ctl thread archive mac THREAD_ID
t3ctl thread unarchive mac THREAD_ID
```

Each command refuses an incompatible current state and confirms the new title or archive location before returning success.

Organize an active thread without deleting it:

```bash
t3ctl thread pin mac THREAD_ID
t3ctl thread unpin mac THREAD_ID
t3ctl thread snooze mac THREAD_ID --until 2026-09-14T09:00:00+05:30
t3ctl thread unsnooze mac THREAD_ID
t3ctl thread settle mac THREAD_ID
t3ctl thread unsettle mac THREAD_ID
```

Snooze times must be in the future and include a timezone. Every command confirms the corresponding T3 state before it reports success.

Read a focused conversation history or the filesystem diff represented by T3 checkpoints:

```bash
t3ctl thread history mac THREAD_ID --turn-limit 100
t3ctl thread diff mac THREAD_ID --to-turn-count 4
t3ctl thread diff mac THREAD_ID --from-turn-count 1 --to-turn-count 4
```

Permanently delete a stopped thread only by repeating its exact identity:

```bash
t3ctl thread delete mac THREAD_ID --confirm-thread-id THREAD_ID
```

Deletion refuses a mismatched confirmation and a thread with a running turn. It checks both the active and archived lists and reports success only after the thread has disappeared from both.


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
