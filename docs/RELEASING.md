# Release process

Only the primary maintainer may publish an official release. A release must be built from a clean commit on `main` after CI is green.

## 1. Confirm scope and version

- Confirm `APP_VERSION` in `app.py`, README files, `CHANGELOG.md`, `CITATION.cff`, validation, and release notes match.
- Confirm the release contains only `0.1.4` behavior; do not copy development-line claims into the stable notes.
- Review the complete tracked-file list and diff.
- Confirm the working tree is clean.

## 2. Run validation

```sh
python3 -m unittest discover -s tests -v
python3 scripts/run_demo.py
python3 scripts/check_public_tree.py
python3 scripts/check_docs.py
python3 -m compileall -q app.py tests scripts
node --check static/app.js
```

Wait for the Windows and Linux GitHub Actions jobs on `main` to pass. Local output is not a substitute for green CI.

## 3. Build the fixed source artifact

```sh
python3 scripts/build_release.py
```

The command creates:

- `dist/echem-platform-v0.1.4.zip`
- `dist/echem-platform-v0.1.4.zip.sha256`

The ZIP is built from committed `HEAD` with a versioned top-level directory. Generated artifacts are ignored by Git and should be attached to the GitHub Release, not committed.

Verify the digest independently:

```sh
shasum -a 256 dist/echem-platform-v0.1.4.zip
```

Windows PowerShell:

```powershell
Get-FileHash .\dist\echem-platform-v0.1.4.zip -Algorithm SHA256
```

## 4. Tag and publish

1. Review `docs/releases/v0.1.4.md` and remove its draft warning.
2. Create an annotated tag `v0.1.4` at the validated `main` commit.
3. Push the tag without force.
4. Create a GitHub Release using the prepared notes.
5. Attach the fixed ZIP and `.sha256` file.
6. Download the attachments from GitHub and verify the hash again.
7. Confirm the README release badge and GitHub license detection are correct.

Do not publish a Windows portable claim unless the attached artifact actually contains and documents a licensed portable runtime. The `0.1.4` source artifact does not bundle Python.

## 5. After publication

- Record the release URL and final digest in the release notes or validation record.
- Open a follow-up issue for any deferred limitation.
- Never replace a published artifact under the same tag. Publish a new patch version instead.
