# EchemPlatform 侧边栏工作台设计 QA

## Comparison setup

- Source visual truth: `/Users/hive/Documents/Codex/2026-07-24/wo/outputs/Windows_StageB_协议页面_20260724.png`
- Normalized source: `/Users/hive/Documents/Codex/2026-07-24/wo/outputs/EchemPlatform-sidebar-v0.3.0-dev.6/protocol-reference-normalized-1425x1188.png`
- Browser-rendered implementation: `/Users/hive/Documents/Codex/2026-07-24/wo/outputs/EchemPlatform-sidebar-v0.3.0-dev.6/protocol-sidebar-1440x1200.png`
- Same-input comparison: `/Users/hive/Documents/Codex/2026-07-24/wo/outputs/EchemPlatform-sidebar-v0.3.0-dev.6/protocol-before-after-normalized.jpg`
- Route and state: `/protocol`, public example draft, initial unvalidated state.
- Source pixels: `1440 × 1200`; normalized by top-left crop to `1425 × 1188`.
- Implementation browser viewport: `1440 × 1200` CSS px at device pixel ratio `1`.
- Implementation capture: `1425 × 1188` pixels. The in-app browser capture omits its scrollbar/chrome edge; comparison therefore uses the equally cropped source.

## Full-view comparison evidence

The implementation preserves the source page’s light canvas, teal/orange safety palette, four-metric row, two-column editor/review structure, card radii, shadow weight, copy hierarchy, form density, and initial Dry-run state. The intentional structural change is a persistent dark sidebar that gives the three existing routes one stable workbench frame. The former top navigation is removed to avoid duplicated navigation.

The sidebar occupies `232 px`; the content grid remains readable at both the `1440 px` Windows acceptance width and the in-app browser’s normal `1280 px` desktop width. At `1280 px`, measured document scroll width was `1265 px`, so persistent controls are not hidden by horizontal overflow.

## Focused region comparison evidence

No separate crop was required. The normalized full-view comparison keeps the complete sidebar, page heading, metric cards, protocol metadata form, review actions, safety card, validation card, and first continuous-step controls legible in one image. Browser checks separately confirmed all three sidebar destinations and their active states.

## Required fidelity surfaces

- Fonts and typography: Existing Inter/PingFang/Microsoft YaHei stack, weights, headings, eyebrow labels, form sizes, and line heights are preserved. Sidebar type uses the same family and a compact but readable three-level hierarchy.
- Spacing and layout rhythm: Existing 14–24 px card rhythm and 17–18 px radii remain intact. Sidebar padding, 62 px navigation rows, and sticky full-height frame create a stable desktop shell without crowding the editor.
- Colors and visual tokens: Existing `--ink`, teal, paper, canvas, line, orange, and shadow tokens remain the source of truth. The dark sidebar derives from the existing ink family; selected navigation uses the existing teal accent.
- Image quality and asset fidelity: The interface contains no new raster imagery. The existing product mark is reused without replacing or approximating product imagery.
- Copy and content: Existing experiment, protocol, safety, and validation copy is preserved. New copy is limited to functional navigation labels, Windows test-environment context, and the persistent safety-state summary.

## Findings

- No actionable P0, P1, or P2 findings remain.
- [P3] English eyebrow labels coexist with Chinese navigation labels. This matches the existing interface convention and is acceptable for this iteration.

## Comparison history

### Iteration 1

- Earlier finding: [P2] The first sidebar width and `1360 px` minimum shell caused horizontal overflow in the in-app browser’s normal `1280 px` desktop viewport, clipping the top-right scan action.
- Fix made: Reduced the sidebar from `248 px` to `232 px`, reduced the desktop minimum shell width to `1180 px`, and tightened the protocol editor grid minimums.
- Post-fix evidence: At a `1280 px` browser width, `scrollWidth=1265`, the complete scan action is visible, and all three pages render without horizontal overflow.

### Iteration 2

- Post-fix visual comparison: The normalized `1425 × 1188` before/after image shows the sidebar addition without loss of the source page’s primary hierarchy, form readability, safety cards, or first-step controls.
- Browser interaction evidence: Navigated `/` → `/protocol` → `/monitor`; each route displayed exactly one active sidebar item. Protocol “仅校验” completed with the visible result `通过`.
- Console evidence: No browser errors or warnings were reported on the checked protocol and monitor states.

## Implementation checklist

- [x] Shared sidebar shell on data, protocol, and safety-gate pages.
- [x] Real route navigation with one active state per page.
- [x] Persistent local-only and control-lock context.
- [x] Stage C sidebar state follows actual control capabilities.
- [x] Desktop widths checked at 1280 and 1440.
- [x] Complete automated regression suite passed.
- [x] Primary navigation and protocol validation tested in the browser.

## Follow-up polish

- A future iteration may replace `Workspace` and `Safety status` with Chinese labels if the product language is standardized as fully Chinese.

final result: passed
