# NDDev Cline Full-Auto Setup

This target is managed by nddev-cline-app. Keep Cline data, settings, skills,
agents, plugins, and sessions under the explicit target.

Full-auto setup rules:

- Auto-approval is enabled only inside the isolated target.
- Destructive commands, auth commands, provider setup, and Cline self-update
  remain denied by `CLINE_COMMAND_PERMISSIONS`.
- Use the `nddev-builder` skill and agent for setup artifact review.
