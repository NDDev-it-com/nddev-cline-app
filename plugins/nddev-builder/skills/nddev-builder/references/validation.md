# Public Validation

Run public module checks from the module root:

```bash
python3 cli-tools/validate_public_contracts.py
python3 -m py_compile cli-tools/nddev_cline.py cli-tools/validate_public_contracts.py
git diff --check
```

Those checks are module-local and side-effect-free. They do not install Cline,
touch live Cline state, run private harness lanes, start CI, push, or tag.

Private lifecycle tests, fixtures, platform proof, benchmarks, durable memory,
and operational ownership skills are root-harness concerns.
