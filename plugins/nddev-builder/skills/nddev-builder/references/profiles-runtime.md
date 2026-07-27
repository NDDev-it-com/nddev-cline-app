# Profiles And Runtime

Setup content and permission profiles are orthogonal.

Content setup:

- `nddev-builder`

Profiles:

- `full-auto`, default
- `safe`

Full-auto launch:

- `HOME=<target>/home`
- `--config <target>/home/.cline/data/settings`
- `--hooks-dir <target>/home/.cline/hooks`
- `--auto-approve true`
- `CLINE_COMMAND_PERMISSIONS` allows `["*"]`, denies `[]`, and allows redirects
- no `--data-dir`
- no `CLINE_DATA_DIR`
- no `CLINE_SANDBOX`
- no `--yolo`

Safe launch:

- isolated `HOME=<target>/home`
- `--plan`
- `--auto-approve false`
- `--data-dir <target>/sandbox`
- `CLINE_SANDBOX=1`
- `CLINE_COMMAND_PERMISSIONS` denies all commands and redirects

Caller-supplied posture, path, and auth override flags are manager-owned and
blocked before launch.
