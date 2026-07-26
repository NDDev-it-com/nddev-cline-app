# Security

Report security issues privately to the repository owner.

This module manages only explicit target directories. It must not read live
Cline authentication, provider credentials, VS Code extension state, runtime
caches, or global Cline state outside the target supplied with `--target`.
