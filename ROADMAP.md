# Roadmap

This roadmap describes intent, not a guarantee or delivery schedule. Safety and data provenance take priority over format count.

## `0.1.x`: maintain the public read-only baseline

- Keep the local service loopback-only.
- Improve parser diagnostics and synthetic fixture coverage.
- Maintain reproducible Windows and Linux tests.
- Publish fixed release artifacts with SHA-256 checksums.
- Gather consented, version-scoped feedback from early users.

## `0.2.x`: stronger provenance and parser boundaries

- Split parser implementations into independently tested modules.
- Record parser identity and version with each indexed file.
- Improve behavior when a source is moved or temporarily unavailable.
- Add private-fixture validation that reports no path, filename, or content.
- Document format variants by software version where evidence exists.

## `0.3.x`: offline workflow review

- Explore offline protocol validation and deterministic preview generation.
- Keep protocol work separate from instrument execution.
- Require explicit safety gates and human review for any future launch-related research.

## Out of scope for the public baseline

- remote instrument operation
- direct serial-port access
- vendor SDK reverse engineering
- automatic scientific conclusions from partially identified data
- cloud upload of laboratory data
- replacing instrument protections, physical interlocks, or local supervision

Roadmap proposals are welcome through the feature-request template. A proposed feature that changes a safety boundary must include a threat model, tests, documentation, and a separate maintainer decision.
