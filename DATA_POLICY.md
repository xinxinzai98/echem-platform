# Data policy

This policy applies to fixtures, screenshots, bug reports, pull requests, release artifacts, and any other material published through this repository.

## Data included in the repository

Files under `demo_data/` are deliberately small, synthetic fixtures created to exercise parser and user-interface behavior. They are not measurements from a real experiment, do not represent a real sample, and contain no confidential laboratory metadata.

The file `demo_data/chi_ocpt_demo.bin` is an ASCII placeholder with a `.bin` suffix. It is not a vendor binary file and is used only to verify metadata-only handling.

## Data that must not be published

Do not commit or attach:

- real or unpublished experimental data without documented authorization
- personal names, account names, workstation names, local paths, private network addresses, or sample identifiers
- instrument configuration, diagnostic logs, license files, activation data, or service records
- vendor installers, SDKs, DLLs, manuals, help files, or proprietary binary formats
- SSH keys, API tokens, passwords, certificates, or other credentials
- data whose ownership, consent, or redistribution terms are unclear

A hash is not anonymization. Publishing only a SHA-256 digest may still disclose that a specific private file exists, so hashes derived from real data require the same authorization review as the data itself.

## Contributing parser fixtures

New public fixtures should be synthetic whenever possible. A parser contribution should include:

1. a minimal fixture containing only the fields needed to reproduce the behavior;
2. a short provenance statement explaining how the fixture was generated;
3. expected instrument, technique, axes, parse status, and point count;
4. confirmation that the contributor has the right to publish the fixture; and
5. a regression test that does not require network or instrument access.

If a defect can only be reproduced with private data, keep that data outside the repository. Submit a minimal synthetic reproduction or coordinate a private review with a maintainer. Private data must not be copied into an issue, pull request, CI artifact, or AI service.

## Screenshots and demonstrations

Public screenshots and videos must use synthetic fixtures and must be reviewed for local paths, usernames, IP addresses, laboratory names, sample names, and notifications from other applications.

## Enforcement

The release workflow runs `scripts/check_public_tree.py` as a bounded guard against common accidental disclosures. That script is an additional control, not a substitute for human review. If restricted material is committed, stop publication and follow an appropriate history-remediation and credential-rotation process before continuing.
