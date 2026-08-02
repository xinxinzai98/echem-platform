# Usage and adoption evidence

This file separates public adoption from maintainer-operated validation. Counts are scoped to a version or commit and must not be generalized to later releases.

## Public adoption

Echem Platform is newly public and early-stage. As of 2026-08-02, no independent external user, laboratory deployment, or downstream project has been confirmed with permission to publish. Stars, forks, downloads, issues, and pull requests should be read directly from GitHub rather than copied into a static claim here.

## `0.1.4` public baseline

- Four synthetic fixtures are included: three parsed curves and one metadata-only placeholder.
- Ten automated tests pass in a clean checkout.
- A maintainer-operated Windows acceptance check confirmed loopback-only HTTP access and disabled serial and instrument-control flags.
- The `0.1.4` validation did not index a real experimental data directory.

See [VALIDATION.md](VALIDATION.md) for the version-scoped record.

## Separate development-line validation

Public commit [`b626c4c`](https://github.com/xinxinzai98/echem-platform/commit/b626c4c) on branch `codex/v0.3-desktop-launcher` was validated by the maintainer on one authorized Windows workstation as `0.3.0-dev.11`:

- 135 files were indexed in a read-only scan;
- classified files included 69 CHI and 61 CorrTest exports;
- 93 files were parsed and 34 were retained as metadata-only;
- 120 automated tests passed locally and in the Windows candidate deployment;
- no analysis record was saved and source files were not modified; and
- `instrument_control`, `serial_access`, and launch availability remained disabled.

The detailed record is maintained in the development branch's [`VALIDATION.md`](https://github.com/xinxinzai98/echem-platform/blob/codex/v0.3-desktop-launcher/VALIDATION.md). This is maintainer-operated compatibility validation, not independent adoption and not evidence that `0.1.4` supports every validated development-line behavior.

## Claim policy

- Do not buy stars, downloads, testimonials, or other activity.
- Do not describe internal testing as broad adoption.
- Do not identify a person, laboratory, workstation, or institution without written authorization.
- Record feedback with the tested version, operating system, data category, and permission status.
- Prefer aggregate counts and synthetic reproductions over publishing file names or research content.
