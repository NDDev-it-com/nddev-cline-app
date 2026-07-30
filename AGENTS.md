<!--
GENERATED FILE - DO NOT EDIT DIRECTLY
generator: gds
bundle: 0.1.0-dev
source-commit: 97e8bbaa3a0734b156b03bad704503bc46d7575b
input-digest: sha256:1ad54d086df4744f8a4c0fa8f591c5daa43c3ea369e09e0b12884dc88ad685e4
output-digest: sha256:49dd1144bf144b7195192ac0dbf0ba7fdb4ee153cc207961a4ac52300ed25fdd
edit-source:
  - .gds/repository.yaml
  - policies/base/repository-default.yaml
  - policies/owners/organization-default.yaml
  - policies/roles/public-module.yaml
  - templates/agents/repository.md.tmpl
  - templates/github-actions/go.yml.tmpl
  - templates/harnesses/claude.md.tmpl
-->
# GDS repository contract

## Scope

- Repository ID: `repo_01KYFD7H44QEF5WQTQ6K9E5GMS`.
- Roles: `module`.
- Canonical repository facts: `.gds/repository.yaml`.
- Applied bundle: `.gds/bundle.lock.yaml` (`0.1.0-dev`).
- Compiled policy: `.gds/compiled-policy.json`.

## Boundaries

- This Git repository is one independent mutation boundary.
- Preserve unrelated branches, worktrees, submodules, and dirty changes.
- Resolve cross-repository work with `gds context --json` before acting.
- Generated files are projections; change their canonical inputs and regenerate.

## Safety

- External writes require explicit approval: `true`.
- Generated projection edits: `forbidden`.
- Private parent context persistence: `forbidden`.
- Visibility contract: `public`; data classification: `public`.

## Development

- Test: `python3 -m json.tool config/nddev-contract.json`.

## Agent routing

- Start here: run `gds-orient` (or `gds context --json`) to resolve scope before
  any cross-repository work. It is the orientation entry point.
- Active skill profiles: `core, module`. Five profiles exist in total
  (`core`, `estate-admin`, `module`, `device`, `portfolio`); only the listed ones
  are active for this repository. The catalog is `skills/registry.yaml`, and each
  skill lives under `skills/canonical/<name>/SKILL.md`.
- Use on-demand skills for procedures; do not duplicate them here.
- Treat docs and memories as derived evidence, not mutation authority.

## Done

- Required verification is complete or explicitly `NOT_PROVEN`.
- Git state and every affected repository boundary are classified.
- No private data, secret, or unapproved generated drift is introduced.
