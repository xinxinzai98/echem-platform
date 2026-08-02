# Echem Platform V0

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/xinxinzai98/echem-platform?display_name=tag)](https://github.com/xinxinzai98/echem-platform/releases)

[中文](README.md) | [English](README.en.md)

Echem Platform is a local-first, read-only workbench for indexing and visualizing CHI and CorrTest electrochemistry exports. It fingerprints every source file with SHA-256, stores research metadata in a local SQLite database, and leaves experimental files unchanged.

Current stable version: `0.1.4`

The project is newly public and early-stage. No independent external adoption is currently confirmed.

![Read-only dashboard using synthetic demo data](docs/images/dashboard-demo.jpg)

## What V0 does

- Indexes files under configured local data folders
- Recognizes common CHI text exports and CorrTest `.cor` / `.z60` text data
- Records CHI `.bin` files by metadata and SHA-256 only
- Displays two-dimensional CV, LSV, EIS, OCP, CA, and CP/GCD curves
- Stores sample identifiers, materials, electrolyte, area, tags, and notes in its own SQLite database
- Records import, update, and metadata-edit audit events
- Listens on `127.0.0.1` by default for local-browser access only

## Safety boundary

- Does not open COM3, COM4, or any serial port
- Does not execute CHI macros or call a CorrTest SDK
- Does not start, stop, or control instrument software
- Does not create, rename, overwrite, or delete files in watched folders
- Defers files that may still be changing
- Fingerprints every source file with a full SHA-256 digest
- Does not upload data or call external network services

See [SECURITY.md](SECURITY.md) for the complete boundary and vulnerability-reporting process. See [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) before using the software with research records.

## Three-minute synthetic demo

Python 3.9 or newer is required. No third-party Python package is needed.

```sh
python3 scripts/run_demo.py
```

The expected result is four indexed synthetic files: three parsed curves, one metadata-only placeholder, and zero read errors. See [docs/quickstart-demo.md](docs/quickstart-demo.md) for the full walkthrough and expected fields, and [demo_data/README.md](demo_data/README.md) for fixture provenance and checksums.

## Run locally

```sh
python3 app.py
```

Open `http://127.0.0.1:8787` in a local browser.

To scan once and exit:

```sh
python3 app.py --scan-once
```

Windows source users can run `py -3 app.py`. The included `start-windows.cmd` is intended only for a maintainer-built portable bundle containing `runtime/python.exe`; the source archive does not bundle a Python runtime.

## Configure real data folders

Edit `watch_roots` in `config.json`. Absolute paths are accepted. Relative paths are resolved from the configuration file's directory.

```json
{
  "watch_roots": [
    "D:\\Electrochemistry\\CorrTest",
    "D:\\Electrochemistry\\CHI"
  ]
}
```

Read [DATA_POLICY.md](DATA_POLICY.md) before contributing any fixture. Real experimental data, vendor installers, SDKs, help files, private configuration, and logs must not be committed to the public repository.

## Test and release checks

```sh
python3 -m unittest discover -s tests -v
python3 scripts/run_demo.py
python3 scripts/check_public_tree.py
python3 scripts/check_docs.py
node --check static/app.js
```

The project currently does not use hosted CI. Release evidence comes from running the reproducible commands above on a clean commit and repeating the same checks from the extracted release archive; it is not a claim of cross-platform CI coverage. See [VALIDATION.md](VALIDATION.md) for validation evidence and [docs/RELEASING.md](docs/RELEASING.md) for the release process.

## Project status and community

- Usage and adoption claims: [ADOPTION.md](ADOPTION.md)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)
- Changelog: [CHANGELOG.md](CHANGELOG.md)
- Roadmap: [ROADMAP.md](ROADMAP.md)
- Maintainers: [MAINTAINERS.md](MAINTAINERS.md)
- Citation metadata: [CITATION.cff](CITATION.cff)
- Third-party notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

## License

The code is released under the [MIT License](LICENSE). Fixture terms and restrictions are described in [DATA_POLICY.md](DATA_POLICY.md).
