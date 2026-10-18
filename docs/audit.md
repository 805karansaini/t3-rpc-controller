# Refactor audit, 14 September 2026

## Scope

Keep existing T3 capabilities and the JSON command interface. Simplify the code for personal use, colleague setup, and agent access. No new server operations were added.

## Changes

- Split the 2,499-line executable into focused package modules. The original script remains a launcher; the installed `t3ctl` command calls the package directly.
- Kept task dispatch, reconciliation, approval handling, and event monitoring behavior unchanged during extraction.
- Shared shell and thread snapshot handling instead of maintaining two copies of the WebSocket subscription loop.
- Centralized environment selection and avoided duplicate reads when the same environment is supplied repeatedly.
- Reject nonfinite timeout and polling values, invalid configuration shapes, dot-only environment names, blank credentials, and URLs with embedded credentials or non-origin components.
- Resolve the default state location when called, so environment overrides do not depend on import order.
- Replace credential and configuration files through private temporary files. Preserve previous credentials if a configuration update fails, and restore a removed credential if saving the configuration fails. Removal backup names include microseconds to avoid same-second collisions.
- Encode WebSocket ticket query values correctly.
- Made `t3ctl` the documented agent interface, added a repository-local skill, shortened setup instructions, and retained the full command reference separately.
- Removed machine-specific environment names from the live smoke command.

## Verification

- Original baseline: 73 tests passed.
- Final `make check`: 84 tests passed, including local HTTP/WebSocket integration tests and new configuration failure and CLI regression checks. Compilation and dependency checks passed.
- Compared the original and refactored parser trees: every command, option, default, and choice was preserved.
- Compared extracted function bodies: controller operations, command builders, authentication, and monitoring were unchanged. Changes to CLI bodies were limited to shared environment selection, timeout validation, and resolving the default state directory when the parser is built.
- Built a wheel, installed it with its dependency into a fresh virtual environment, and ran it outside the checkout. Help, JSON output, invalid-input exit codes, and standalone state location passed. The wheel contains no local state or credentials.
- `make smoke` and skill validation passed.
- Live i7 checks passed for authentication, compatibility, projects, threads, and providers.
- The Mac endpoint refused connections at the time of this audit. Consequently, the all-server live smoke command correctly failed. No live thread mutations were performed during this refactor; those paths were checked with the automated suite.

## Remaining limits

T3 compatibility depends on the installed server build. Configuration supports one local writer at a time; the file replacements are not a database transaction or a guarantee against power loss. Internal Python import paths changed, so integrations should invoke `t3ctl`. The original script command remains supported.
