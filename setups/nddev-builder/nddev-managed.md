# NDDev Cline Managed Rules

This target is managed by nddev-cline-app.

- Keep Cline runtime state inside the explicit target.
- Treat `home/.cline/data/settings` as the native configuration directory.
- Treat `home/.cline/skills`, `home/.cline/rules`, `home/.cline/agents`, and
  `home/.cline/plugins` as the native global content directories for the
  isolated `HOME`.
- Do not copy live editor state, provider credentials, OAuth tokens, npm
  credentials, or user caches into this target.
- Do not add hook files or MCP servers unless a real source-owned adapter is
  intentionally introduced and validated.
