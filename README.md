# T3 RPC controller

A Python CLI for reading and controlling T3 Code servers. Use `t3ctl` from a terminal or an agent. Results are JSON, and T3 Code remains the graphical interface.

The existing commands cover environment setup, discovery, task submission, monitoring, approvals, conversation history, and thread management. Task execution is restricted to the `codex` provider instance. Other providers are available for read-only discovery.

## Setup

Requires Python 3.11 or newer and a running T3 Code server.

From this repository:

```bash
make setup
source .venv/bin/activate
t3ctl --help
```

For a standalone installation, run `python -m pip install /path/to/t3-rpc-controller` inside your preferred virtual environment. Installation provides the same `t3ctl` command. The original `python t3_rpc_controller.py` launcher still works from the checkout. Python modules under `t3_controller` are internal; use the CLI as the stable agent interface.

Each colleague should pair with their own T3 server. Generate a one-time pairing credential on the target machine with `npx t3 pair`, then enter it locally:

```bash
t3ctl pair local --url http://127.0.0.1:3773
t3ctl health --environment local
t3ctl projects --environment local
t3ctl models --environment local --provider codex
```

`pair` prompts visibly for the one-time credential and verifies the resulting bearer before saving it. To import an existing bearer, use `t3ctl configure NAME --url URL --token-file /protected/token/file`.

## Agent workflow

Use the existing CLI with the repository skill at [.agents/skills/t3-controller/SKILL.md](.agents/skills/t3-controller/SKILL.md). The skill explains discovery, exact task tracking, retries, and when to return pending questions or approvals to the user. It adds no server capabilities and does not install anything globally.

Discover the environment and project first. Create a thread only when the requested task needs one, using a model returned by discovery:

```bash
t3ctl environments
t3ctl projects --environment local
t3ctl thread create local PROJECT_ID --title "Task title" --provider codex --model MODEL_ID
t3ctl thread run local THREAD_ID --prompt-file /path/to/task.md --timeout 600
```

`thread run` submits and waits for that exact message. It returns when the task finishes, fails, times out, or needs attention. `--dry-run` validates a mutation without dispatching it. Use `thread send` and `thread wait --message-id MESSAGE_ID` separately when you need to do other work while waiting.

See the [command reference](docs/commands.md) for all existing commands, including streaming, approvals, deletion, and retry examples.

## Output and retries

- Successful results use JSON with `schemaVersion: 1` on standard output. `thread follow` streams one JSON object per line.
- Operational errors return JSON on standard error and exit with status 1. Invalid CLI usage exits with status 2. Help and version output are plain text.
- Multi-environment reads preserve successful results alongside an `errors` object and exit with status 1 if any requested server failed.
- `thread wait` and `thread run` return structured results. Inspect `reason`, not just the exit code. `attention-required` means a pending approval or question needs handling. `superseded` means the old turn's outcome cannot be determined.
- For retryable creates and sends, generate UUIDs once and preserve them. After `uncertain_result`, reuse the exact IDs from `error.retry` and the original task content. New IDs can create duplicate work.

## Local state

A Git checkout uses its own `.state` directory regardless of the caller's working directory. Standalone installations use `~/.local/state/t3-rpc-controller`. Override either with `T3_CONTROLLER_HOME` or a global option before the command:

```bash
t3ctl --home /path/to/controller-state environments
```

State contains `environments.json` and a separate `tokens/NAME` file for each server. Directories are private to the owner and credential files have mode `0600`. Writes replace complete files. Configuration changes are intended for one local writer at a time, not concurrent administration.

Do not share `.state` or copy credentials into prompts, logs, or version control. `environments` shows only endpoint metadata. URLs must be plain HTTP or HTTPS origins, without embedded credentials, paths, queries, or fragments. Remote endpoints require network access from the controller host, such as a connected Tailscale network.

To remove configuration with a recoverable local backup:

```bash
t3ctl remove-environment NAME --confirm-name NAME
```

## Development and verification

```bash
make check       # compile, check dependencies, run all tests
make smoke       # check the CLI and local configuration
make live-smoke  # read-only checks against configured servers
```

Tests include a local fake HTTP and WebSocket server, so the normal suite needs no live T3 installation. It checks authentication, protocol messages, errors, timeouts, reconciliation, pending requests, secret redaction, and local configuration failure cases.

See the [refactor audit](docs/audit.md) for changes, verification results, and remaining limits.

The package separates responsibilities:

| File | Purpose |
| --- | --- |
| `cli.py` | Arguments, file input, command handlers, exit codes |
| `operations.py` | Server reads and mutations with readback checks |
| `domain.py` | Payload builders, validation, and result shapes |
| `monitoring.py` | Event streaming, reconnecting, and resuming |
| `auth.py` | Pairing protocol compatibility |
| `transport.py` | HTTP and WebSocket communication |
| `state.py` | Local configuration and credentials |
| `output.py` | JSON output, errors, and credential redaction |

T3's protocol can change between builds. Run `t3ctl compatibility` against your installation before relying on it. A read-only compatibility result does not prove live mutations work. Earlier implementation checks and limitations are recorded in `HANDOFF.md` and `ROADMAP.md`; they describe the builds tested at that time.
