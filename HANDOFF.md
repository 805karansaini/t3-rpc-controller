# T3 RPC Controller handoff

## Goal

Build a command-line controller that lets an agent operate T3 Code on the Mac and i7 machines. T3 Code remains the graphical interface. This project supplies safe discovery, task dispatch, monitoring, intervention, and thread management.

## Current status

- The controller now covers environment setup, discovery, task dispatch, monitoring, intervention, lifecycle management, history, and diffs.
- Task creation and submission preserve stable retry identifiers even when dispatch succeeds but confirmation cannot be read, and uncertain replies are reconciled against T3 state.
- `thread wait` and `thread run` track the exact submitted message and never infer success from message text. If a newer turn hides the target's final state before it is observed, they return a safe `superseded`/`unknown` result.
- The native authenticated connection and compatibility checks pass against both configured machines.
- Version control, repeatable checks, the completed roadmap, and a reviewable decision trail are in place.
- See `ROADMAP.md` for the ordered checklist and `decisions.tsv` for evidence behind completed checkpoints.

## Verified environment

- macOS controller host
- Python 3.13.13
- `websockets` 16.1.1
- T3 Code Nightly `0.0.41-nightly.20260913.1646`
- `mac` T3 server at loopback
- `i7` T3 server over Tailscale

Credential values are never recorded here. This checkout's connection state lives under `.state`, which Git ignores. Standalone installs default to the user's local state directory and can be pointed at this state with `T3_CONTROLLER_HOME` or `--home`.

## Decisions

- Build a CLI and reusable Python client, not another graphical interface or local web service.
- Keep each T3 orchestration thread as the authoritative record of its conversation and task state.
- Require environment, project, thread, provider, and model identities to be explicit before state changes.
- Read the target before a state change and read it again afterward before reporting success.
- Keep machine-readable output stable so another agent can call the controller without scraping prose.
- Commit after every two or three related capabilities, after tests pass.
- Run live tasks only through the Codex provider on the Mac and i7 environments. Creation, model changes, sending, approvals, and question responses enforce that boundary; other providers are read-only discovery data.
- Default new work to approval-required mode when no saved runtime choice exists.
- Version every machine-readable response and distinguish checks performed now from features known to be supported.

## Progress log

| Phase | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Baseline | Complete | `make check`, `make smoke`, and both-machine project query | First commit contains no local state or credentials |
| Connectivity | Complete | Live health on both machines plus local HTTP/WebSocket success, failure, malformed-message, and timeout tests | No credential values are present in tracked files or error output |
| Discovery | Complete | `threads`, `thread show`, `search`, `providers`, and `models` verified live | Safe identity selection is available for write commands |
| Monitoring | Complete | `thread follow` plus `thread pending` | Snapshot, events, timeout, resume, deduplication, and pending requests covered |
| Task control | Complete | Live create, model change, task dispatch, completion, stop, and approval response verified | User-question response is schema- and unit-verified; the current Codex mode would not emit a live question request |
| Lifecycle | Complete | Live rename, archive, pin, snooze, settle, history, diff, and guarded deletion verified | Three disposable build-test threads were removed and confirmed absent |
| Agent hardening | Complete | JSON schema versioning, bounds, all-output redaction, previews, compatibility checks, a 72-test suite, and both-machine read-only smoke | Operation tests cover creation, recoverable send, task-aware wait, model, stop, requests, organization, deletion, and monitor reconnect/resume/deduplication |
| Reliability follow-up | Complete | `make setup`, `make check`, `make smoke`, `make live-smoke`, resumed live follow, live wait, and live run preview | Version 0.2.3 preserves retry instructions, keeps stream snapshots authoritative, avoids guessing superseded outcomes, and applies total deadlines to monitoring and waiting |

## Verified limitations

- Live task execution was verified only with the Codex provider, as requested. Claude and OpenCode are not used for live work.
- Task creation, completion, stopping, and approval handling were verified live on the Mac. The i7 passed authentication, protocol compatibility, discovery, provider, and thread smoke checks without creating disposable work there.
- T3's current Codex mode did not emit a live user-question request during verification. Answer and dismiss handling match the installed T3 protocol and pass local tests, but this exact path remains unexercised against a live pending question.
- There is intentionally no graphical interface or local service; T3 Code remains the graphical interface.

## Resume instructions

1. Read `ROADMAP.md`, this file, and the last rows of `decisions.tsv`.
2. Run `git status --short --branch` and `make check`.
3. Run `./t3_rpc_controller.py compatibility` and `make live-smoke` before relying on a newer T3 Nightly build.
4. Use `./t3_rpc_controller.py environments` without opening credential files.
5. Use `--dry-run` before consequential mutations and preserve exact identity confirmations for deletion or environment removal.
6. Update this file and append a decision-log row when future protocol changes alter verified behavior.
