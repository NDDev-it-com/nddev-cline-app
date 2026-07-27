# Changelog

## 0.2.0

- Adopt official-channel `npm ci` installation of the frozen public Cline CLI
  lockfile into a target-owned staging project.
- Add Node.js 20+ preflight, committed lock contract verification, and sanitized
  npm cache/config handling.
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
