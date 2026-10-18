---
name: t3-controller
description: Use the t3ctl CLI to discover T3 Code projects and manage T3 tasks across configured servers. Applies to T3 Code tasks, not Codex desktop task management.
---

# T3 controller

Use `t3ctl` as the agent interface. In this repository, `make setup` installs `.venv/bin/t3ctl`. Use that executable if `t3ctl` is not on PATH. For use elsewhere, install this repository into a virtual environment and use its `t3ctl` executable. Use `--help` for the available commands and flags.

## Discover and submit

Run `t3ctl environments`, then `projects`, `threads`, and `models --provider codex` as needed. Select exact environment, project, thread, and model IDs from returned data. A matching title on another server is a different resource. Do not assume that a machine named `mac` or `i7` exists.

Use existing threads when appropriate. Create a thread only within the user's requested scope. Execution uses only the `codex` provider instance. Preserve requested safety modes and the CLI's approval-required default.

For an authorized task on a known thread:

```bash
t3ctl thread run ENV THREAD_ID --prompt-file /path/to/task.md --timeout 600
```

Use a prompt file or `--prompt-file -` for standard input. Keep multiline task text out of shell interpolation. `--dry-run` validates and previews a mutation without submitting it.

## Track the exact task

`thread run` submits and waits for the new message. For separate submission and monitoring, use `thread send`, retain its `messageId`, then call `thread wait ENV THREAD_ID --message-id MESSAGE_ID --timeout 600`. Waiting without that ID can observe an older completed turn.

Read the JSON result and `reason`. `attention-required` means the task needs an approval or answer. Inspect `thread pending` and present the actual request to the user when their decision is needed; do not invent answers or broaden authorization. Handle decisions already covered by the user's instructions within that scope. A timeout is not completion. `superseded` means the target turn's outcome is unknown.

For live events, use `thread follow`; it emits JSON Lines. Its final record explains why monitoring stopped. Stopping the monitor does not stop the task.

## Retry without duplicating work

For a submission that might need retrying, generate UUIDs once and pass `--message-id` and `--command-id`. For creation use `--thread-id` and `--command-id`. Preserve the original arguments with those IDs.

If an error has category `uncertain_result`, reuse the IDs in `error.retry` and the original content. Do not generate new IDs to work around an uncertain result. Inspect the exact thread if confirmation remains unavailable, and report the uncertainty before attempting unrelated changes.

## Read output and protect state

Results use `schemaVersion: 1`. Operational errors are JSON on stderr with exit status 1; invalid usage exits with status 2. Multi-server reads can return useful data and per-server errors together. Help and version output are plain text.

Use `environments` to inspect configuration. Never read or expose `.state/tokens`. Authentication failures require local pairing, not credentials in chat. `--home PATH` goes before the command and selects an explicit state directory.

Thread mutations already read back their target before reporting success. Deletion requires `--confirm-thread-id` and permanently removes the exact stopped thread; use it only when deletion is requested.
