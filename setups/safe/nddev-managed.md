# NDDev Cline Safe Setup

This target is managed by nddev-cline-app. Keep Cline data, settings, skills,
agents, plugins, and sessions under the explicit target.

Safe setup rules:

- Use plan mode and require approval for all actions.
- Shell execution is denied by `CLINE_COMMAND_PERMISSIONS`.
- Use the `nddev-builder` skill and agent for setup artifact review.
- Do not read live provider secrets, VS Code state, extension state, or auth
  outside the target.
