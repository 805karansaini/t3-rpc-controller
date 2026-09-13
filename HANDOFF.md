# T3 RPC Controller handoff

## Goal

Build a command-line controller that lets an agent operate T3 Code on the Mac and i7 machines. T3 Code remains the graphical interface. This project supplies safe discovery, task dispatch, monitoring, intervention, and thread management.

## Current status

- The original controller can pair or configure machines and list projects.
- The native authenticated connection works against both configured machines.
- Version control, repeatable checks, and the project plan are in place.
- See `ROADMAP.md` for the ordered checklist and `decisions.tsv` for evidence behind completed checkpoints.

## Verified environment

- macOS controller host
- Python 3.13.13
- `websockets` 16.0
- T3 Code Nightly `0.0.41-nightly.20260913.1646`
- `mac` T3 server at loopback
- `i7` T3 server over Tailscale

Credential values are never recorded here. Local connection state lives under `.state`, which Git ignores.

## Decisions

- Build a CLI and reusable Python client, not another graphical interface or local web service.
- Keep each T3 orchestration thread as the authoritative record of its conversation and task state.
- Require environment, project, thread, provider, and model identities to be explicit before state changes.
- Read the target before a state change and read it again afterward before reporting success.
- Keep machine-readable output stable so another agent can call the controller without scraping prose.
- Commit after every two or three related capabilities, after tests pass.

## Progress log

| Phase | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Baseline | Complete | `make check`, `make smoke`, and both-machine project query | First commit contains no local state or credentials |
| Connectivity | Not started | | Health, partial failure, and transport tests |
| Discovery | Not started | | Threads, providers, models, search |
| Monitoring | Not started | | Thread subscription and reconnect |
| Task control | Not started | | Create, send, stop, approvals, questions |
| Lifecycle | Not started | | Rename, archive, pin, snooze, delete |
| Agent hardening | Not started | | Stable output, redaction, fake-server tests |

## Resume instructions

1. Read `ROADMAP.md`, this file, and the last rows of `decisions.tsv`.
2. Run `git status --short --branch` and `make check`.
3. Use `./t3_rpc_controller.py environments` without opening credential files.
4. Use read-only live checks before any mutation.
5. Update this file when a phase changes state and append a decision-log row for each verified checkpoint.
