# Native Cline Paths

Use the isolated target as the only writable runtime boundary.

Full-auto uses `HOME=<target>/home`, so native Cline global state resolves under
`<target>/home/.cline`.

Safe also uses isolated `HOME=<target>/home`, but adds `--data-dir
<target>/sandbox` and `CLINE_SANDBOX=1`.

Managed target paths:

- `home/.cline/data/settings/global-settings.json`
- `home/.cline/data/settings/cline_mcp_settings.json`
- `home/.cline/rules/nddev-managed.md`
- `home/.cline/skills/nddev-builder/SKILL.md`
- `home/.cline/skills/nddev-builder/references/*.md`
- `home/.cline/agents/nddev-builder.yaml`
- `home/.cline/plugins/nddev-builder/package.json`
- `home/.cline/plugins/nddev-builder/index.js`

The manager owns those paths only inside the explicit target. Legacy 0.1.0
managed paths are readable only for status, migrate, restore, and remove.
