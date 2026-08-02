# Contributing to Echem Platform

Thank you for helping improve a small, early-stage scientific software project. Contributions should preserve the project's local-first, source-read-only, and instrument-control-free boundary.

## Before opening an issue

- Use the bug template for reproducible software defects.
- Use the parser template for a new export format or a parsing mismatch.
- Use the feature template for product ideas.
- Do not disclose a vulnerability in a public issue; follow [SECURITY.md](SECURITY.md).
- Do not upload real experimental data or vendor-proprietary material; follow [DATA_POLICY.md](DATA_POLICY.md).

## Development setup

Python 3.9 or newer is required. The `0.1.x` application has no third-party runtime dependency.

```sh
python3 -m unittest discover -s tests -v
python3 scripts/run_demo.py
python3 scripts/check_public_tree.py
python3 scripts/check_docs.py
node --check static/app.js
```

Run all checks before opening a pull request.

## Adding or changing a parser

A parser pull request should include:

1. the smallest practical synthetic fixture;
2. fixture provenance and SHA-256 in `demo_data/README.md` or a dedicated fixture README;
3. expected instrument, technique, axes, units, parse status, and point count;
4. regression tests for both the intended case and relevant failure behavior;
5. confirmation that source files remain byte-for-byte unchanged; and
6. documentation of unsupported variants and ambiguity.

Do not infer scientific meaning that is not present in the file. Prefer a visible `unparsed` or `metadata_only` result over a plausible but unsupported curve.

## Pull requests

- Keep one logical change per pull request.
- Explain the user impact and safety-boundary impact.
- Update README, validation, limitations, changelog, and tests when applicable.
- Stage files explicitly. Do not include local state, private configuration, logs, installers, or data exports.
- Keep the service loopback-only and preserve the prohibition on serial and instrument control.

By submitting a contribution, you agree that it may be distributed under the repository's [MIT License](LICENSE). You must have the right to submit both code and fixtures.
