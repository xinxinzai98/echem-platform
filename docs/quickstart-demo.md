# Reproducible synthetic demo

This walkthrough verifies the public `0.1.4` baseline without real experiment data, vendor software, network access, or instrument access.

## Requirements

- Python 3.9 or newer
- a clean checkout of the repository
- no third-party Python package

## 1. Run the bounded scanner demo

From the repository root:

```sh
python3 scripts/run_demo.py
```

The script creates a temporary SQLite database, indexes only `demo_data/`, verifies expected parser fields and counts, prints a JSON report, and deletes the temporary database when finished.

Expected summary:

```json
{
  "seen": 4,
  "imported": 4,
  "parsed": 3,
  "metadata_only": 1,
  "errors": 0
}
```

Expected records:

| File | Instrument | Technique | Status | Points |
|---|---|---|---|---:|
| `chi_cv_demo.txt` | CHI | CV | parsed | 29 |
| `chi_ocpt_demo.bin` | CHI | OCP | metadata_only | 0 |
| `corrtest_eis_demo.z60` | CorrTest | EIS | parsed | 15 |
| `corrtest_galstatic_demo.cor` | CorrTest | CP/GCD | parsed | 11 |

The script exits nonzero if any expected value differs.

## 2. Open the local workbench

```sh
python3 app.py
```

Open `http://127.0.0.1:8787`. The default configuration points to `demo_data/`.

Expected status cards:

- data files: 4
- parsed curves: 3
- integrity records: 4
- safety mode: read-only

Select the CHI CV, CorrTest EIS, or CorrTest GalStatic record to view a curve. The `.bin` placeholder should show metadata-only behavior.

Stop the server with `Ctrl+C`. Runtime SQLite files under `state/` are ignored by Git.

## 3. Verify release hygiene

```sh
python3 scripts/check_public_tree.py
python3 scripts/check_docs.py
python3 -m unittest discover -s tests -v
node --check static/app.js
```

The public screenshot was captured from this workflow:

![Read-only dashboard using synthetic demo data](images/dashboard-demo.jpg)

The screenshot is documentation, not proof of real-data compatibility or independent adoption.
