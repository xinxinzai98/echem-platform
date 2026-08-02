# V0.1.4 validation record

This record is scoped to version `0.1.4`. It separates reproducible synthetic-fixture checks from maintainer-operated Windows acceptance. It does not claim independent adoption or compatibility with every real instrument export.

## Clean-checkout automated tests

Validation date: 2026-08-02

Command:

```sh
python3 -m unittest discover -s tests -v
```

Expected result: 10 tests pass.

The tests cover:

1. CHI CV text recognition and axes
2. CorrTest EIS Nyquist axes
3. CorrTest GalStatic potential-versus-time selection
4. CHI `.bin` metadata-only behavior
5. curve downsampling with preserved endpoints
6. source bytes, size, and modification time unchanged by scanning
7. sample metadata written only to the platform database
8. deferral of a recently modified file
9. rejection of a non-loopback bind address
10. resolution of relative watch roots from the configuration directory

The test suite uses temporary directories and closes SQLite connections explicitly.

## Reproducible synthetic demo

Command:

```sh
python3 scripts/run_demo.py
```

Expected result:

| Metric | Expected |
|---|---:|
| Files seen | 4 |
| Files indexed | 4 |
| Parsed curves | 3 |
| Metadata-only files | 1 |
| Read errors | 0 |

Covered fixture categories:

- CHI CV text
- CHI `.bin` metadata-only placeholder
- CorrTest EIS text
- CorrTest GalStatic / CP text

All four files are synthetic. Provenance, expected fields, and SHA-256 values are in [demo_data/README.md](demo_data/README.md).

## Release-readiness commands

The release candidate must pass all of the following from a clean checkout:

```sh
python3 -m unittest discover -s tests -v
python3 scripts/run_demo.py
python3 scripts/check_public_tree.py
python3 scripts/check_docs.py
python3 -m compileall -q app.py tests scripts
node --check static/app.js
```

Version `0.1.4` does not claim hosted CI or an operating-system/Python matrix. The primary maintainer runs these exact commands from a clean release commit, builds the fixed source ZIP, and repeats the checks from the extracted archive before publication. Command output, environment versions, the tagged commit, and the final artifact digest are retained as release evidence.

## Browser acceptance with synthetic data

- Status cards show 4 data files, 3 parsed curves, and 4 integrity records.
- CHI and CorrTest filters show the expected fixtures.
- CV, EIS, and CP/GCD method filters work.
- The EIS fixture renders as a Nyquist curve.
- The GalStatic fixture renders potential versus time.
- Metadata edits are stored in SQLite and create an audit event.
- A second scan reports no new or updated source file.
- Browser console errors: 0.

The public screenshot in `docs/images/dashboard-demo.jpg` must be regenerated only from these synthetic fixtures and reviewed according to [DATA_POLICY.md](DATA_POLICY.md).

## Maintainer-operated Windows acceptance

Original acceptance date: 2026-07-24

- Local launcher health check returned HTTP 200.
- The service listened only on `127.0.0.1:8787`.
- The acceptance flow started, checked, and stopped the platform.
- No platform process or listening port remained after acceptance.
- `serial_access=false`.
- `instrument_control=false`.

The source archive does not contain `runtime/python.exe`; launcher acceptance applies to a separately constructed maintainer portable environment.

## Not validated for `0.1.4`

- No real experimental directory was indexed as part of the `0.1.4` record.
- No serial port was accessed.
- No CHI macro was executed.
- No CorrTest SDK was installed or called.
- No independent external user or laboratory deployment was confirmed.
- No Windows installer or portable runtime is included in the source release.

Separate development-line evidence is labeled in [ADOPTION.md](ADOPTION.md) and must not be presented as `0.1.4` validation.
