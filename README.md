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

Launch forwards stdio and the child exit code:

```bash
python3 cli-tools/nddev_cline.py launch --target /absolute/target -- "review this repository"
```

The launched child receives isolated `HOME`, `USERPROFILE`, `CLINE_DATA_DIR`,
`CLINE_SANDBOX_DATA_DIR`, XDG directories, and `CLINE_COMMAND_PERMISSIONS`
values under the target. Provider tokens and Cline credential environment
variables are stripped.

## Public validation

```bash
python3 cli-tools/validate_public_contracts.py
```

The validator is dependency-free and side-effect-free. It checks version/build
metadata, release and npm integrity baselines, exact current Cline repo/docs
surfaces, setup ids, command permission schemas, builder default-on projection,
unsupported extension install/launch contract, placeholder absence, and shared
CI caller pins.
