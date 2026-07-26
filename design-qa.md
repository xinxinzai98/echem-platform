# EchemPlatform 工作台信息架构设计 QA

## Comparison setup

- Source visual truth: `/Users/hive/Documents/Codex/2026-07-24/wo/outputs/EchemPlatform-sidebar-v0.3.0-dev.6/protocol-sidebar-1440x1200.png`
- Browser-rendered implementation: `/Users/hive/Documents/Codex/2026-07-24/wo/outputs/EchemPlatform-workspace-v0.3.0-dev.7/workspace-overview-viewport-1440x1200.png`
- Same-input comparison: `/Users/hive/Documents/Codex/2026-07-24/wo/outputs/EchemPlatform-workspace-v0.3.0-dev.7/workspace-style-comparison-2880x1200.png`
- Source state: previous shared-shell protocol page at `/protocol`.
- Implementation state: new overview page at `/`, with two instrument records, healthy local data sources, locked instrument control, and four recent activities.
- Source pixels: `1425 × 1188`; normalized to `1440 × 1200`.
- Implementation browser viewport: `1440 × 1200` CSS px at device pixel ratio `1`.
- Implementation capture: `1440 × 1200` pixels.

## Full-view comparison evidence

The new overview intentionally changes the page responsibility while preserving the selected product shell. The dark `232 px` sidebar, light canvas, teal safety palette, white cards, border radii, shadow weight, eyebrow labels, compact top bar, and desktop density remain consistent with the source. The primary content is now limited to the three requested regions: instrument activity, system indicators, and recent activity.

The comparison also confirms the requested navigation hierarchy. “工作台”, “工步设置”, and “数据分析” remain in the main navigation. “环境检测与设置” is removed from that list and appears as a compact gear utility in the bottom-left corner.

## Focused region comparison evidence

A separate crop was not required because the `2880 × 1200` same-input comparison keeps both sidebars, complete top bars, main card boundaries, status chips, typography, and the bottom-left utility dock legible at equal scale. Browser DOM checks separately confirmed the three main routes, active states, gear route, and core page headings.

## Required fidelity surfaces

- Fonts and typography: The Inter/PingFang/Microsoft YaHei stack and the existing heading, eyebrow, body, and compact UI weights are preserved. The new metric figures use the same bold optical hierarchy as the earlier status row.
- Spacing and layout rhythm: The shell width, `18 px` grid gap, `14–17 px` radii, and card padding remain aligned with the previous interface. The overview uses a two-column top region and one full-width activity region without horizontal overflow at `1440 px`.
- Colors and visual tokens: Existing ink, teal, paper, canvas, line, orange, and shadow tokens remain the source of truth. No new competing palette was introduced.
- Image quality and asset fidelity: The product mark is preserved. The new settings control uses the official Bootstrap Icons gear asset rather than a text glyph or a handcrafted icon.
- Copy and content: Page labels now match the requested mental model. Instrument activity is explicitly described as file-side observation, preventing the UI from implying that the platform has taken control of CHI or CorrTest.

## Findings

- No actionable P0, P1, or P2 findings remain.
- [P3] The overview leaves intentional whitespace below the recent-activity panel at a `1200 px`-high viewport. This keeps the operational content compact and avoids inventing extra dashboard modules.

## Comparison history

### Iteration 1

- Source and implementation were normalized to `1440 × 1200` and placed in one comparison image.
- No actionable P0, P1, or P2 visual mismatch was found after the intentional information-architecture change.
- Browser interaction evidence:
  - `/` displays only “仪器当前活动”, “系统指标”, and “最近活动”.
  - Sidebar navigation opened `/steps` and `/analysis`.
  - The bottom-left gear opened `/environment`.
  - “仅校验” on `/steps` returned the visible state `校验通过` / `通过`.
- Console evidence: no browser errors or warnings were reported across the checked routes.

## Implementation checklist

- [x] Main workspace limited to current instrument activity, system indicators, and recent activity.
- [x] “协议编辑” renamed and repositioned as “工步设置”.
- [x] Data curve and metadata tooling moved into the independent `/analysis` module.
- [x] Stage C safety content reframed as `/environment`.
- [x] Environment entry reduced to a bottom-left settings gear.
- [x] Legacy `/protocol` and `/monitor` routes retained for compatibility.
- [x] Desktop launcher now opens the overview.
- [x] Automated suite and browser interaction checks passed.

## Follow-up polish

- A later iteration can add instrument-specific live-state adapters when a verified, read-only process or file heartbeat is available.

final result: passed
