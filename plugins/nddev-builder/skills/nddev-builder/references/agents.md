# YAML Agent Workflow

Cline CLI source discovers `.yml` and `.yaml` agent files from workspace
`.cline/agents` and global `<CLINE_DIR>/agents`. nddev-cline-app projects a
single global YAML agent into `home/.cline/agents/nddev-builder.yaml`.

Checklist:

- Use a `.yaml` file, not a Markdown projection.
- Include a stable `name`.
- Keep role and boundary language specific to nddev-cline-app.
- Do not delegate private harness operations from the public agent.
- Validate that the file is regular, source-owned, and projected into the
  isolated target only.
