# DESIGN.md - RoboRun Demo / LADDER

## Context (from discovery)

- Artifact type: analytical dashboard and data tool
- Positioning: technical, utilitarian, public-proof surface
- Audience: robotics data engineers and model builders | Primary action: inspect the measured cascade, then open related video evidence
- Adjectives: measured, dense, forensic, trustworthy, sharp
- Visual word translations: measured -> every headline number comes from the snapshot; dense -> three-pane instrument layout; forensic -> selectable rungs expose decisions, signals, and reasons; trustworthy -> persisted and replayed policy states stay separate; sharp -> square edges, hard rules, and restrained status color
- Aesthetic essence (3 words): forensic cascade instrument
- Single-minded proposition: RoboRun Demo makes the route from a large egocentric corpus to a reviewable residue inspectable. Ladder is the cascade implementation, not a second public product.
- References: admire Grafana's operational density and scientific instrument panels; avoid startup landing-page structure and decorative AI-dashboard chrome
- Mode: dark | Density: dense
- Constraints: plain HTML/CSS/JS, static Vercel deployment, aggregate-only public snapshot, no invented row-level joins, WCAG 2.2 AA target

## Aesthetic

- Direction: industrial scientific console
- Defining trait: the page is one continuous work surface divided into source stages, a central visualization, and a selected-rung inspector
- Signature move: the cascade is rendered as a literal cheapest-to-terminal flow with selectable vertical stage columns and policy-aware exits

## Typography

- Display: Inter | source: local/system fallback | license: SIL OFL when web-hosted
- Body: SFMono Regular | source: platform fallback | license: platform dependent
- Mono: SFMono Regular, Roboto Mono, Cascadia Code, Consolas
- Scale: ratio 1.25 Major Third, base 14px | display 35px/1 corpus total | h1 20px/1.15 view title | h2 16px/1.25 inspector title | body 14px/1.5 explanatory copy | small 8-11px/1.4 metadata
- Weights: 400/500/700 | Measure: short operational labels rather than long-form copy | Tracking notes: uppercase metadata uses 0.08-0.14em tracking; tabular numerals are required

## Color

- Strategy: near-black field with acid chartreuse as the single active accent; amber and orange are reserved for uncertainty and failure.
- Distribution: 80 neutral / 15 status / 5 active accent
- Palette (role -> OKLCH | hex):
  - bg: oklch(0.15 0.006 145) | #090b0a
  - surface: oklch(0.17 0.009 145) | #0d100e
  - raised surface: oklch(0.19 0.011 145) | #111512
  - fg: oklch(0.95 0.012 130) | #edf0e9
  - muted: oklch(0.57 0.015 145) | #778079
  - border: oklch(0.28 0.012 145) | #252b27
  - accent: oklch(0.93 0.22 121) | #cfff43
  - accent-fg: oklch(0.15 0.006 145) | #090b0a
  - success: oklch(0.93 0.22 121) | #cfff43
  - warning: oklch(0.81 0.13 96) | #dbc45a
  - error: oklch(0.72 0.19 39) | #ff7042
- Dark mode overrides: dark is the only mode; surfaces rise through lightness and borders rather than shadows

## Spacing, radius, shadow

- Spacing base: 4px, scale: 1, 2, 3, 4, 5, 6, 8
- Radius: 0px and 50% for status dots only
- Shadow approach: defined edges; shadows are not used for surface elevation

## Layout and composition

- Grid: three-column application shell | gutters/margins: integrated into pane padding
- Spacing rhythm: 4-10px within a datum; 16-24px between analytical sections
- Signature layout move: the selected rung remains visible beside every central view, so exploration never loses context
- Density: dense | Scanning: F-pattern
- Responsive: desktop-first | breakpoints: 1180px condenses metadata; 860px becomes a stacked, scrollable work surface

## Components and states

- Button hierarchy: selected controls use filled near-black surfaces plus chartreuse indicators; secondary controls are text/edge treatments; states include hover, active, focus-visible, selected, disabled
- Inputs: the only input is a visibly labeled native signal selector
- Tables: labels left, values right, tabular numerals, light separators
- Overlays: none; related public evidence renders inline in the inspector
- Empty / loading / error: full-surface snapshot loader; explicit snapshot-error state; empty reasons state is textual
- Focus ring: high-contrast chartreuse box-shadow, never removed without replacement

## Motion

- Duration scale: instant 0ms, fast 120ms, normal 180ms
- Easing: cubic-bezier(0.2, 0.8, 0.2, 1)
- What animates: state color and opacity only | reduced-motion: no essential motion
- Signature motion: none; high-frequency analysis controls update immediately

## Iconography

- Set: custom typographic arrows and status dots | grid: 16px | stroke: not applicable | radius match: yes

## Imagery and illustration

- Mode: real product data visualization with inline public video evidence
- Rules: never substitute illustration for measured output; link evidence by dataset when the public aggregate lacks row identity
- Avoid: stock imagery, generated scenes, abstract decoration, and any suggestion that related examples are exact row joins
- Text-over-image contrast: the only video overlay is a small opaque provenance tag; the source MP4 remains directly accessible from the same evidence panel

## Dark mode

- Base bg: near-black L 0.15 | fg: off-white L 0.95 | elevation ramp: L 0.15, 0.17, 0.19
- Accent: chartreuse L 0.93 | border: lighter than each surface

## Accessibility

- Contrast: AA target on operational text and controls | Focus: visible and unobscured
- Keyboard: native buttons, links, and select are operable | Targets: 24px minimum, larger for primary navigation
- Color independence: labels and values accompany all status colors | Reduced motion: no essential animation
- Notes: charts expose values in text/title attributes and outcomes remain available as numeric rows

## Tokens (source of truth)

```css
:root {
  --font-display: Inter, "Helvetica Neue", Arial, sans-serif;
  --font-body: "SFMono-Regular", "Roboto Mono", "Cascadia Code", Consolas, monospace;
  --space: 4px;
  --radius: 0;
  --bg: oklch(0.15 0.006 145);
  --panel: oklch(0.17 0.009 145);
  --panel-2: oklch(0.19 0.011 145);
  --ink: oklch(0.95 0.012 130);
  --muted: oklch(0.57 0.015 145);
  --line: oklch(0.28 0.012 145);
  --good: oklch(0.93 0.22 121);
  --defer: oklch(0.81 0.13 96);
  --bad: oklch(0.72 0.19 39);
}
```

- Adapter: plain CSS custom properties

## Cards and surfaces

- Cards/surfaces: defined border, no shadow, zero radius, padding matched to information density | nesting: analytical regions share the work surface rather than becoming floating cards

## Slop audit

- Date: 2026-08-26 | Result: pass
- Notes: the interface is a data tool, not a marketing page; no hero, feature grid, gradient text, glassmorphism, stock art, blob radius, or decorative motion. Status meaning has text and numbers. Keyboard controls and reduced-motion-safe behavior pass the implementation gate. The related-video handoff preserves provenance by explicitly stating it is not a row-level join.

## Changelog

- 2026-08-26: Replaced the marketing page with the measured three-pane cascade explorer.
- 2026-08-26: Added public video-evidence handoff to the existing demo viewer without implying false source-row identity.
- 2026-08-26: Consolidated the public identity around `RoboRun / DEMO`; Ladder remains the underlying cascade, and the former separate viewer is now an inline evidence layer.
- 2026-08-26: Restored visible content preview with an inline public MP4, action timeline, episode stepping, and full-viewer handoff in the inspector; related examples remain explicitly non-row-joined.
