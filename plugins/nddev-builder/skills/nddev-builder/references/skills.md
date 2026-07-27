# Skill Workflow

Create skills as directories containing `SKILL.md`. Keep the entry skill short
and route to focused references. Put stable workflows in references; point to
code-owned files for versions, package pins, exact managed path lists, and
launch constants.

Checklist:

- The YAML frontmatter has `name` and `description`.
- The description states when to use the skill.
- The body uses progressive disclosure instead of long duplicated contracts.
- References are regular files, bounded, and shipped under the skill directory.
- Public skills do not mention private harness-only commands as executable
  requirements.
