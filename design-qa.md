# EchemPlatform 数据文件夹视图设计 QA

## Comparison setup

- Source visual truth: `/Users/hive/.codex/visualizations/2026/07/24/019f9364-18cc-73f3-9fb4-3c6d2c27e792/echem-analysis-dev7-source.jpg`
- Browser-rendered implementation: `/Users/hive/.codex/visualizations/2026/07/24/019f9364-18cc-73f3-9fb4-3c6d2c27e792/echem-analysis-dev8-implementation.jpg`
- Same-input comparison: `/Users/hive/.codex/visualizations/2026/07/24/019f9364-18cc-73f3-9fb4-3c6d2c27e792/echem-analysis-dev8-comparison.png`
- Source state: `v0.3.0-dev.7` flat record list with instrument selector and monitored-directory footer.
- Implementation state: `v0.3.0-dev.8` configured-folder tree with automatic CHI/CorrTest file recognition.
- Both captures: `1425 × 1188` pixels from the same in-app browser tab and viewport override.

## Full-view comparison evidence

The implementation preserves the established desktop workbench shell, card hierarchy, status row, curve panel, sample metadata form, audit stream, typography, spacing, and teal safety palette. The left analysis rail now communicates filesystem structure instead of implying that the operator must choose a workstation. A configured root is expanded into folders and files; each file carries parser and technique badges while selection continues to drive the existing curve and metadata detail.

The former two-select toolbar is reduced to search plus test-method filtering. This creates enough vertical and horizontal room for nested paths without changing the overall two-column workbench rhythm. The monitored-directory footer is absent from the analysis page.

## Focused behavior evidence

- The configured root expands and collapses through native `details`/`summary` controls.
- Searching `chi` returns the two CHI files and updates the visible count to `2 个文件`.
- Selecting the EIS filter returns only the CorrTest EIS fixture.
- CHI and CorrTest files appear together in the same tree without a workstation selector.
- The bottom-left settings entry opens `/environment`, where the full monitored path, availability state, and read-only boundary are shown.
- File selection updates the parser label, technique, curve, SHA-256, source availability, and metadata form.
- No browser console errors were reported during the checked flow.

## Fidelity and accessibility review

- Existing visual tokens and component radii are preserved; no competing palette or new asset style was introduced.
- Folder hierarchy uses native disclosure controls and a subtle connector line, not handcrafted icons or text glyphs.
- File buttons provide `aria-current`, visible source-missing text, and high-contrast `:focus-visible` treatment.
- The interactive tree is no longer an `aria-live` region; only the compact file count announces changes.
- The curve canvas is associated with the text statistics region for a non-visual summary.
- Full data paths are reserved for the settings page; the analysis tree receives only root labels and relative paths.

## Findings

- No actionable P0, P1, or P2 visual or interaction findings remain.
- [P3] The local demo root is flat, so the screenshot shows the root disclosure plus file leaves; nested folder rendering is covered by an automated mixed-folder test and will be visible against the Windows experiment hierarchy.

## Verification checklist

- [x] No workstation selection is required.
- [x] Configured data roots render as folder/file trees.
- [x] CHI and CorrTest text exports are automatically identified and parsed.
- [x] CHI EIS `Z'` / `Z"` headers map to Nyquist axes.
- [x] Search, method filtering, folder disclosure, and file selection work.
- [x] Monitored-directory UI moved to environment settings.
- [x] Absolute source paths are not exposed by the file-tree API.
- [x] Automated suite passes.

final result: passed
