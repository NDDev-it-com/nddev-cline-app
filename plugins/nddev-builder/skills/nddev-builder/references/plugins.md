# Plugin Workflow

Cline documents native plugins through a package `cline.plugins` field. The
public module ships `home/.cline/plugins/nddev-builder/package.json` and
`home/.cline/plugins/nddev-builder/index.js`.

Checklist:

- Use `package.json` with `cline.plugins`.
- Export a valid `AgentPlugin` object from `index.js`.
- Keep tool adapters deterministic, local, bounded, and read-only.
- Do not emulate a marketplace.
- Do not add fake external services, fake hooks, fake MCP servers, or auth
  flows.
- Keep optional `@cline/sdk` peer dependency metadata optional.
