# Hook Boundary

Cline source supports hook files with event names such as `TaskStart`,
`TaskResume`, `TaskCancel`, `TaskComplete`, `TaskError`, `PreToolUse`,
`PostToolUse`, `UserPromptSubmit`, `PreCompact`, and `SessionShutdown`.

Supported extensions include no extension, `.sh`, `.bash`, `.zsh`, `.js`,
`.mjs`, `.cjs`, `.ts`, `.mts`, `.cts`, `.py`, and `.ps1`.

nddev-cline-app does not ship hook files in Phase A. The manager passes an
isolated `--hooks-dir`; future hook adapters must be real source-owned regular
files, bounded, deterministic, and validated before projection.
