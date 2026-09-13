# T3 RPC Controller roadmap

The controller is an agent-facing command-line tool for operating existing T3 Code installations. T3 Code remains the graphical interface and the owner of projects, threads, provider sessions, terminals, and files.

## Completion criteria

The work is complete when an agent can use documented commands to identify a machine and project, inspect available providers and models, create and manage a thread, send a task, follow ordered updates, stop work, answer approval and user-input requests, and verify every state-changing action. The controller must not expose credentials and must recover cleanly from a dropped connection or an unavailable machine.

## Chronological build plan

### 0. Establish a safe baseline

- [x] Initialize a Git repository on the `main` branch.
- [x] Keep `.state`, credentials, virtual environments, caches, and local output out of Git.
- [x] Add repeatable `make` commands for setup, tests, checks, and live smoke tests.
- [x] Add this roadmap, a brief handoff document, and an append-only decision log.
- [x] Make the first verified baseline commit.

### 1. Harden configuration and connectivity

- [x] Add a `health` command that checks authentication and RPC access per environment.
- [x] Return useful partial results when one of several environments is unavailable.
- [x] Ensure commands construct safe output instead of printing stored configuration directly.
- [x] Add environment removal and credential-renewal guidance without exposing credentials.
- [x] Test HTTP exchange, WebSocket messages, timeouts, malformed responses, and command-line exit codes.

### 2. Add read-only discovery

- [x] List and filter threads by environment, project, status, and archived state.
- [x] Show one thread with its current task, model, provider, pending actions, and timestamps.
- [x] Search threads through T3's native search operation.
- [x] Discover provider instances and their available models.
- [x] Expose project defaults needed when creating a thread.

### 3. Add live updates

- [x] Subscribe to one thread and print its initial state.
- [x] Follow ordered thread events as JSON Lines for another agent to consume.
- [x] Stop following after a terminal state or caller-supplied timeout.
- [x] Reconnect safely and avoid emitting duplicate events.
- [x] Report pending approvals and user-input requests prominently.

### 4. Create and run work

- [x] Create a thread in an explicitly selected environment and project.
- [x] Select and validate a provider instance and model.
- [x] Send a task to an existing or newly created thread.
- [x] Read the thread back after dispatch and return the confirmed task state.
- [x] Support interaction and runtime modes accepted by the installed T3 build.

### 5. Control active work

- [x] Stop or interrupt an active turn and verify it stopped.
- [x] Answer an approval request and verify T3 accepted the response.
- [x] Answer or dismiss a user-input request and verify the pending state cleared.
- [x] Make retries safe so an agent cannot accidentally submit the same response twice.

### 6. Manage thread lifecycle

- [x] Rename a thread and verify the new title.
- [x] Archive and unarchive a thread.
- [x] Pin, unpin, snooze, and unsnooze where supported.
- [x] Settle and unsettle threads for T3's active-work workflow.
- [x] Delete a thread only with an explicit destructive flag and exact identity check.
- [x] Add read-only diff and history retrieval where supported.

### 7. Make it dependable for autonomous agents

- [x] Use consistent JSON envelopes, error categories, and exit codes.
- [x] Add `--dry-run` for state-changing commands where practical.
- [x] Redact credentials, tickets, pairing values, and sensitive server errors.
- [x] Add compatibility diagnostics for T3 Nightly protocol changes.
- [x] Add representative integration tests with a fake T3 server.
- [x] Run the full checks and live read-only smoke tests on both machines.
- [x] Update the README and handoff record with the final command reference and verified limitations.

### 8. Reliability follow-up

- [x] Keep unexpected network and protocol failures in the JSON error contract.
- [x] Bound requests by total elapsed time and require initial monitor synchronization.
- [x] Emit one initial snapshot across monitor reconnects.
- [x] Add stable retry identifiers and reconcile uncertain task creation or submission.
- [x] Add a task-aware wait command that stops for completion, failure, timeout, approval, or input.
- [x] Add a combined submit-and-wait command for the normal agent workflow.
- [x] Version every JSON record and distinguish live compatibility checks from declared support.
- [x] Pin the tested dependency and run setup, tests, and smoke checks through the project environment.
- [x] Separate state, output, and network code from the controller workflow.
- [x] Preserve retry identifiers across successful dispatches whose confirmation read fails.
- [x] Recognize an exact completed turn even after a newer turn becomes current.
- [x] Keep standalone installation state outside `site-packages`.
- [x] Make resumed monitoring start with a snapshot and include setup and backoff in its deadline.
- [x] Preserve changed stream snapshots, stop immediately for resumed terminal state, and bound connection cleanup.
- [x] Return an explicit unknown result when a newer turn hides an older target's final state.

## Commit rhythm

Changes are committed after each two or three related capabilities. Each commit must pass `make check`. Commits that touch the live protocol also require a read-only live smoke test before they are considered complete.
