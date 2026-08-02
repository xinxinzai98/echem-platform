# Known limitations

These limits apply to the `0.1.x` release line.

## File and parser coverage

- CHI text parsing is heuristic and covers only common exported two-column tables.
- CHI `.bin` files are metadata-only. The project does not decode, edit, or convert proprietary binary content.
- CorrTest support covers text-form `.cor` and `.z60` examples that expose readable table headers. It does not load a CorrTest SDK.
- A recognized extension does not guarantee that a specific firmware or software version is supported.
- The workbench previews two-dimensional curves. It does not perform equivalent-circuit fitting, formal Rct determination, iR correction, reference-electrode conversion, or publication-grade electrochemical analysis in `0.1.x`.
- The `0.1.x` scanner does not reject every file symlink that resolves outside a watched root. Do not place untrusted symlinks in a watched directory; stricter containment is required before treating such directories as adversarial input.
- Parsers run in the main local process rather than an operating-system sandbox.

## Runtime and interface

- The local HTTP service has no user authentication and is therefore restricted to loopback addresses.
- The `0.1.x` service does not provide a general-purpose authentication, CSRF-token, or multi-tenant authorization layer. Do not expose it through a proxy or port-forwarding rule.
- `0.1.x` is designed for one local operator, not concurrent multi-user access.
- The default maximum source-file size is 50 MiB and the displayed curve may be downsampled while preserving endpoints.
- The source release requires Python 3.9 or newer. It does not include a Windows installer or bundled Python runtime.
- Windows launcher scripts expect a separately built `runtime/python.exe` portable bundle.

## Safety and research use

- The project is not an instrument controller, interlock, emergency-stop system, laboratory information management system, backup system, or regulatory record system.
- A successful parse means only that a table was recognized. It does not prove scientific validity, correct units, correct electrode area, or suitability for a research conclusion.
- SHA-256 fingerprints support integrity checks but do not replace backups, access control, retention policies, or signed provenance records.
- Maintainer-operated validation is not evidence of independent laboratory adoption.

Report a defect through the appropriate issue template. Report security problems according to [SECURITY.md](SECURITY.md).
