# nddev-cline-app Agent Instructions

This public module owns the reusable Cline CLI setup manager, public contracts,
release metadata, public documentation, and native nddev-builder content.

Do not add private harness tests, fixtures, benchmarks, memories, generated
evidence, live Cline state, provider credentials, npm credentials, editor state,
or CI pins to this repository.

Use code-owned facts instead of copying volatile values:

- manager behavior: `cli-tools/nddev_cline.py`
- public contract: `config/nddev-contract.json`
- build metadata: `build/version.json` and `build/manifest.json`
- upstream baseline: `references/cline-baseline.json`

The public runtime model is one content setup, `nddev-builder`, with orthogonal
profiles `full-auto` and `safe`. The default profile is `full-auto`.
