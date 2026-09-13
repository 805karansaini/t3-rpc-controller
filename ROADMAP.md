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
- [ ] Add environment removal and credential-renewal guidance without exposing credentials.
- [ ] Test HTTP exchange, WebSocket messages, timeouts, malformed responses, and command-line exit codes.

### 2. Add read-only discovery

- [x] List and filter threads by environment, project, status, and archived state.
- [x] Show one thread with its current task, model, provider, pending actions, and timestamps.
- [x] Search threads through T3's native search operation.
- [x] Discover provider instances and their available models.
- [x] Expose project defaults needed when creating a thread.

### 3. Add live updates

- [ ] Subscribe to one thread and print its initial state.
- [ ] Follow ordered thread events as JSON Lines for another agent to consume.
- [ ] Stop following after a terminal state or caller-supplied timeout.
- [ ] Reconnect safely and avoid emitting duplicate events.
- [ ] Report pending approvals and user-input requests prominently.

### 4. Create and run work

- [ ] Create a thread in an explicitly selected environment and project.
- [ ] Select and validate a provider instance and model.
- [ ] Send a task to an existing or newly created thread.
- [ ] Read the thread back after dispatch and return the confirmed task state.
- [ ] Support interaction and runtime modes accepted by the installed T3 build.

### 5. Control active work

- [ ] Stop or interrupt an active turn and verify it stopped.
- [ ] Answer an approval request and verify T3 accepted the response.
- [ ] Answer or dismiss a user-input request and verify the pending state cleared.
- [ ] Make retries safe so an agent cannot accidentally submit the same response twice.

### 6. Manage thread lifecycle

- [ ] Rename a thread and verify the new title.
- [ ] Archive and unarchive a thread.
- [ ] Pin, unpin, snooze, and unsnooze where supported.
- [ ] Delete a thread only with an explicit destructive flag and exact identity check.
- [ ] Add read-only diff and history retrieval where supported.

### 7. Make it dependable for autonomous agents

- [ ] Use consistent JSON envelopes, error categories, and exit codes.
- [ ] Add `--dry-run` for state-changing commands where practical.
- [ ] Redact credentials, tickets, pairing values, and sensitive server errors.
- [ ] Add compatibility diagnostics for T3 Nightly protocol changes.
- [ ] Add representative integration tests with a fake T3 server.
- [ ] Run the full checks and live read-only smoke tests on both machines.
- [ ] Update the README and handoff record with the final command reference and verified limitations.

## Commit rhythm

Changes are committed after each two or three related capabilities. Each commit must pass `make check`. Commits that touch the live protocol also require a read-only live smoke test before they are considered complete.
