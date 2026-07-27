---
name: nddev-builder
description: Create, review, and maintain nddev-cline-app native Cline setup artifacts with progressive disclosure.
---

# nddev-builder for Cline

Use this skill when working on public nddev-cline-app artifacts: setup content,
permission profiles, Cline-native rules, skills, YAML agents, plugins, hooks
boundaries, MCP settings, runtime launch contracts, or public validation.

Start with the narrowest reference that matches the task:

- Native paths and target layout: `references/native-paths.md`
- Skill authoring and checks: `references/skills.md`
- Rules, AGENTS, and durable context: `references/rules-memory.md`
- YAML agent presets: `references/agents.md`
- Cline SDK plugins: `references/plugins.md`
- Hook adapter boundary: `references/hooks.md`
- MCP settings: `references/mcp.md`
- Runtime setup/profile behavior: `references/profiles-runtime.md`
- Public validation workflow: `references/validation.md`

Keep volatile facts code-owned. Versions, package integrity, optional package
sets, launch constants, and managed file lists live in `references/cline-baseline.json`,
`build/version.json`, `build/manifest.json`, `config/nddev-contract.json`, and
`cli-tools/nddev_cline.py`.

Do not copy live Cline auth, editor state, provider credentials, npm tokens,
runtime logs, caches, or private harness artifacts into the public module.
