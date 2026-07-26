# nddev-cline-app

NDDev setup-switching manager for current Cline.

This module manages Cline through an explicit, isolated target directory. It
does not read or modify live VS Code extension state, Cline authentication,
provider credentials, user caches, or global Cline state.

## Verified upstream surface

- VS Code extension: `saoudrizwan.claude-dev`
- Extension tested version: `4.0.11`
- CLI command: `cline`
- CLI npm package: `cline`
- CLI tested version: `3.0.46`
- Official source/release: <https://github.com/cline/cline/releases/tag/v4.0.11>
- Official documentation:
  <https://docs.cline.bot/getting-started/installing-cline>,
  <https://docs.cline.bot/getting-started/config>, and
  <https://docs.cline.bot/cli/cli-reference>

Cline is editor-first, but current official docs also define a CLI. This module
therefore supports target-owned CLI installation and launch only. Extension
installation and extension launch remain unsupported because those operations
would require editor-managed live state.

## Setup variants

- `safe`: plan mode, auto-approval disabled, shell commands denied.
- `balanced`: auto-approval disabled, common local validation commands allowed,
  destructive/auth/publish commands denied.
- `full-auto`: auto-approval enabled in the isolated target while destructive,
  auth, publish, and self-update commands stay denied.

All variants enable `nddev-builder` by default through Cline native skills,
agents, plugins, rules, and settings. No separate marketplace is assumed.

## Usage

Use an absolute target path:

```bash
python3 cli-tools/nddev_cline.py list --json
python3 cli-tools/nddev_cline.py plan --setup balanced --target /absolute/target --json
python3 cli-tools/nddev_cline.py install --setup balanced --target /absolute/target --json
python3 cli-tools/nddev_cline.py switch --setup safe --target /absolute/target --json
python3 cli-tools/nddev_cline.py restore --backup 0 --target /absolute/target --json
python3 cli-tools/nddev_cline.py remove --target /absolute/target --json
```

Target-owned CLI software management:

```bash
python3 cli-tools/nddev_cline.py software-status --target /absolute/target --json
python3 cli-tools/nddev_cline.py install-cli --target /absolute/target --json
python3 cli-tools/nddev_cline.py update-cli --target /absolute/target --json
```

`install-cli` runs only `bun add --global --exact --trust cline@3.0.46`
with target-owned `BUN_INSTALL_GLOBAL_DIR`, `BUN_INSTALL_BIN`,
`BUN_INSTALL_CACHE_DIR`, temp `HOME`, and temp XDG directories. The explicit
trust is limited to the official pinned `cline` package because registry
metadata declares `postinstall` and platform optional dependencies. Existing or
partial target-owned CLI software surfaces must use `update-cli` or repair;
`install-cli` only accepts an absent software surface, and current installs are
idempotent. `update-cli` on an absent target-owned CLI install is a deterministic
domain failure with no target or parent artifacts created. `software-status` is
read-only and validates the deterministic
software manifest and tree digest without executing the target binary. The
machine-owned bounds and measured baseline live in `build/manifest.json` under
`software_lifecycle.bounds`.

Setup backups live in a sibling pool marked by `NDDEV-CLINE-BACKUPS.json` and
bound to the canonical target. A preexisting collision path without that marker
is never removed or reused.

Launch forwards stdio and the child exit code:

```bash
python3 cli-tools/nddev_cline.py launch --target /absolute/target -- "review this repository"
```

The launched child receives isolated `HOME`, `USERPROFILE`, `CLINE_DATA_DIR`,
`CLINE_SANDBOX_DATA_DIR`, XDG directories, and `CLINE_COMMAND_PERMISSIONS`
values under the target. User-provided `--auto-approve`, `--data-dir`,
`--config`, `--hooks-dir`, and `--key` overrides are rejected because those
surfaces are manager-owned. Provider tokens and Cline credential environment
variables are stripped. The launch preflight lock is released before the child
process starts.

## Public validation

```bash
python3 cli-tools/validate_public_contracts.py
```

The validator is dependency-free and side-effect-free. It checks version/build
metadata, release and npm integrity baselines, exact current Cline repo/docs and
Bun docs surfaces, setup ids, command permission schemas, builder default-on
projection, Bun install policy, unsupported extension install/launch contract,
unfinished-marker absence, and shared CI caller pins.
