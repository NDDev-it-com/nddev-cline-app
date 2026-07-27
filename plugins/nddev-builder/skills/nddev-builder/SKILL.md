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
sets, launch constants, and managed file lists are owned by the public manager
and release package, not by this installed skill. For a target, run the public
manager `status` and `software-status` commands with `--json`. For source
ownership, follow the manager and public build/contract files in the
nddev-cline-app module checkout or release package instead of resolving module
root files from this skill directory.

Do not copy live Cline auth, editor state, provider credentials, npm tokens,
runtime logs, caches, or private harness artifacts into the public module.
