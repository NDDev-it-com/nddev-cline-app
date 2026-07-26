---
name: nddev-builder
description: Review and maintain native Cline setup artifacts managed by nddev-cline-app.
---

# nddev-builder for Cline

Use this skill when editing or reviewing NDDev Cline setup artifacts.

Rules:

- Keep Cline state under the explicit target and `CLINE_DATA_DIR`.
- Use only current Cline native surfaces: global settings, MCP settings, rules,
  skills, agents, plugins, and CLI command permissions.
- Do not install or launch the VS Code extension from this manager.
- Do not read live provider secrets, Cline auth, VS Code state, or extension
  state.
