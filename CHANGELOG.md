# Changelog

All notable changes to this project are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow semantic versioning where practical.

## [Unreleased]

### Planned

- Gather independent user feedback without publishing private laboratory or personal information.
- Expand parser coverage only through synthetic or authorized fixtures.

## [0.1.4] - 2026-08-02

### Added

- Local, loopback-only workbench for CHI and CorrTest export indexing.
- SHA-256 source fingerprints, local SQLite metadata, and audit events.
- Synthetic CHI CV, CHI metadata-only, CorrTest EIS, and CorrTest GalStatic fixtures.
- English documentation, MIT license, contribution guidance, data policy, known limitations, citation metadata, and community templates.
- Windows and Linux CI covering Python 3.9 and 3.14.
- Reproducible synthetic demo, public-tree disclosure guard, release builder, and draft release notes.

### Security

- Documented private vulnerability reporting and supported-version policy.
- Explicitly retained loopback-only, no-serial, no-instrument-control, and source-read-only boundaries.

[Unreleased]: https://github.com/xinxinzai98/echem-platform/compare/v0.1.4...HEAD
[0.1.4]: https://github.com/xinxinzai98/echem-platform/releases/tag/v0.1.4
