# Changelog

## [0.2.3] - 2026-08-05

- Advance the same-day stable Cline CLI from `3.0.49` to `3.0.50` and
  regenerate the exact target-owned npm graph while retaining Hono `4.13.0`.
- Advance the Visual Studio Marketplace reference from `4.1.3` to `4.1.4`
  with its exact VSIX size and SHA-256.

## [0.2.2] - 2026-08-05

- Update the frozen Cline CLI graph from `3.0.48` to stable `3.0.49` and
  refresh every exact npm integrity and target-owned lockfile identity.
- Update the observed Visual Studio Marketplace reference from Cline `4.0.12`
  to stable `4.1.3` with the exact VSIX digest.
- Include Hono `4.13.0` in the regenerated graph, preserving the dependency
  security update tracked independently by pull request #5.

## [0.2.1] - 2026-08-01

- Capture and strictly resolve the caller workspace once for launch, then bind
  both the child working directory and Cline's native `--cwd` argument to it.
- Reject forwarded `--cwd`, `--cwd=...`, `-c`, and attached `-c...` overrides.

## [0.2.0]

- Adopt official-channel `npm ci` installation of the frozen public Cline CLI
  lockfile into a target-owned staging project.
- Add Node.js 20+ preflight, committed lock contract verification, and sanitized
  npm cache/config handling.
- Disable npm lifecycle scripts and bin links for the target-owned CLI install
  while preserving the committed lock and package wrapper launch path.
- Replace setup variants with one `nddev-builder` content setup and orthogonal
  `full-auto` and `safe` profiles.
- Make `full-auto` the default no-sandbox profile with native command
  permissions allowing all commands and redirects.
- Keep `safe` as the plan-first sandbox profile with command execution denied.
- Project native Cline builder skills, references, YAML agent, rules, empty MCP
  settings, and a documented `cline.plugins` AgentPlugin.
- Add explicit legacy 0.1.0 status/migrate/restore/remove handling and deny
  legacy launch.

## 0.1.0

- Add explicit-target Cline setup manager.
- Add target-bound backup, restore, remove, rollback, drift, and safety checks.
- Record Cline extension `saoudrizwan.claude-dev` `4.0.11` as unsupported for
  direct install/launch by this manager.
- Add dependency-free public contract validator and shared CI callers.
