# Rules And Memory

Use `rules/nddev-managed.md` for target-local Cline rules. Use repository
`AGENTS.md` for public contributor-facing instructions.

Rules should be hierarchical and low entropy:

- State ownership boundaries before workflows.
- Point to manager and contract files for machine-owned facts.
- Keep auth, secrets, editor state, caches, logs, and private harness state out
  of public content.
- Do not copy volatile release versions or commit SHAs into instructions.

Private operational memory and owner-only skills belong in the root harness, not
in this public module.
