# NDDev Cline Balanced Setup

This target is managed by nddev-cline-app. Keep Cline data, settings, skills,
agents, plugins, and sessions under the explicit target.

Balanced setup rules:

- Auto-approval is disabled at launch.
- Common local inspection and validation commands may run under
  `CLINE_COMMAND_PERMISSIONS`.
- Destructive commands, auth commands, and publishing remain denied.
- Use the `nddev-builder` skill and agent for setup artifact review.
