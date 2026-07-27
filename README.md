# nddev-cline-app

NDDev setup manager for the Cline CLI.

This module manages Cline through an explicit isolated target directory. It
does not read or modify live VS Code extension state, Cline authentication,
provider credentials, npm credentials, user caches, or global Cline state.

## Verified Upstream Surface

- CLI command: `cline`
- CLI npm package: `cline`
- CLI tested version: `3.0.46`
- Official CLI install channel: `npm install -g cline`
- Node.js preflight: 20+ required, 22 recommended
- VS Code extension metadata only: `saoudrizwan.claude-dev` `4.0.11`
- Official documentation:
  <https://docs.cline.bot/getting-started/installing-cline> and
  <https://docs.cline.bot/cli/cli-reference>

Extension installation and extension launch are unsupported because those
operations require editor-managed live state.

## Setup And Profiles

The content setup and permission profile are orthogonal.

- Setup: `nddev-builder`
- Default profile: `full-auto`
- Safe profile: `safe`

`full-auto` runs without Cline native sandboxing:

- `HOME=<target>/home`
- `--config <target>/home/.cline/data/settings`
- `--hooks-dir <target>/home/.cline/hooks`
- `--auto-approve true`
- `CLINE_COMMAND_PERMISSIONS` allows `["*"]`, denies `[]`, and allows redirects
- no `--data-dir`
- no `CLINE_DATA_DIR`
- no `CLINE_SANDBOX`
- no `--yolo`

`safe` runs plan-first with Cline sandboxing:

- isolated `HOME=<target>/home`
- `--plan`
- `--auto-approve false`
- `--data-dir <target>/sandbox`
- `CLINE_SANDBOX=1`
- `CLINE_COMMAND_PERMISSIONS` denies command execution and redirects

There is no third permission profile.

## Usage

Use an absolute target path:

```bash
python3 cli-tools/nddev_cline.py list --json
python3 cli-tools/nddev_cline.py plan --target /absolute/target --json
python3 cli-tools/nddev_cline.py install --target /absolute/target --json
python3 cli-tools/nddev_cline.py switch --profile safe --target /absolute/target --json
python3 cli-tools/nddev_cline.py migrate --profile full-auto --target /absolute/target --json
python3 cli-tools/nddev_cline.py restore --backup 0 --target /absolute/target --json
python3 cli-tools/nddev_cline.py remove --target /absolute/target --json
```

Target-owned CLI software management:

```bash
python3 cli-tools/nddev_cline.py software-status --target /absolute/target --json
python3 cli-tools/nddev_cline.py install-cli --target /absolute/target --json
python3 cli-tools/nddev_cline.py update-cli --target /absolute/target --json
```

`install-cli` uses `npm ci` from the committed public package lock in a clean
target-owned staging project with isolated cache, npmrc, global npmrc, `HOME`,
temp directory, and XDG directories. It preflights Node.js, validates the lock
digest and package-lock shape against `references/cline-baseline.json`, disables
npm lifecycle scripts and bin links, probes `cline --version` through the
package wrapper, writes a software manifest, and swaps the software surface
atomically.

`software-status` is read-only and validates the manifest and tree digest
without executing the target binary.

Launch forwards stdio and the child exit code:

```bash
python3 cli-tools/nddev_cline.py launch --target /absolute/target -- "review this repository"
```

Caller-supplied posture, path, and auth override flags are rejected before
launch. Provider tokens, npm tokens, Cline credential environment variables,
and editor state are stripped from child environments.

During launch the manager holds the exclusive target lifecycle lock until the
child Cline process completes and lock cleanup is restored. Target lifecycle
mutations, including install, switch, migrate, restore, remove, install-cli,
and update-cli, are denied while target software is running.

The launch handoff is a write-protected verified-path handoff. The manager
holds a persistent target-internal lock file with `fcntl.flock`, temporarily
removes owner-write permission from the target root and target-owned
executable/software parent chain, checks that the software manifest is current
before protection, revalidates entrypoint and package-wrapper identities plus
`O_NOFOLLOW` SHA-256 byte digests immediately before spawning the target-owned
executable, then restores modes after the child exits. This is not portable fd
execution; without a native sandbox it does not claim resistance to deliberate
same-UID `chmod` attacks.

Legacy 0.1.0 managed targets may be inspected, migrated, restored, or removed.
They are never launched.

## nddev-builder Content

The `nddev-builder` setup projects Cline-native public content into the isolated
target:

- `home/.cline/skills/nddev-builder/SKILL.md`
- `home/.cline/skills/nddev-builder/references/*.md`
- `home/.cline/agents/nddev-builder.yaml`
- `home/.cline/plugins/nddev-builder/package.json`
- `home/.cline/plugins/nddev-builder/index.js`
- `home/.cline/rules/nddev-managed.md`
- `home/.cline/data/settings/cline_mcp_settings.json`

The plugin uses documented `cline.plugins` package metadata. The MCP settings
file contains an empty `mcpServers` object because this module does not ship a
real MCP server. No marketplace, fake hook, or fake MCP server is projected.

## Public Validation

```bash
python3 cli-tools/validate_public_contracts.py
python3 -m py_compile cli-tools/nddev_cline.py cli-tools/validate_public_contracts.py
git diff --check
```

These checks are module-local and side-effect-free. Private harness tests,
benchmarks, platform proof, durable memory, and operational skills are not part
of the public module.
