# Accessibility

DAGonWeb targets **WCAG 2.1 level AA** (EN 301 549), as required for public-sector websites by Directive (EU) 2016/2102. An accessibility statement is published at `/accessibility`; set its feedback contact, enforcement procedure and review date under **Admin → Accessibility**.

## What is implemented

| WCAG | How |
| --- | --- |
| 1.1.1 Non-text content | Decorative icons are `aria-hidden`; logos have empty `alt`; workflow charts have a text summary and a data table. |
| 1.3.1 Info and relationships | Landmarks (`nav`, `main`, `footer`), headings, table captions and `scope`, labelled form fields, `fieldset`/`legend` for groups. |
| 1.3.5 Identify input purpose | `autocomplete` on email, name and password fields. |
| 1.4.3 Contrast | Light and dark themes are checked at 4.5:1. Administrators cannot save brand or navbar colors below 4.5:1 against white text. |
| 1.4.10 Reflow | Pages work at 320–375 CSS px without horizontal scrolling. |
| 2.1.1 Keyboard | Every function works with the keyboard: palette buttons add tasks, Tab/Enter select them, arrow keys move them, the Inspector edits inputs and dependencies. The Ace editor lets keyboard users leave with Esc then Tab. |
| 2.3.3 / reduced motion | Animations and transitions stop with `prefers-reduced-motion`; the dry-run playback shows the final state at once. |
| 2.4.1 Bypass blocks | “Skip to main content” link on every page. |
| 2.4.2 Page titled | Unique titles per page. |
| 2.4.7 Focus visible | A 3 px focus outline on links, buttons, fields, task nodes and resizers. |
| 3.3.1 / 3.3.3 Error identification | Errors are text, linked to the fields with `aria-describedby`, flagged with `aria-invalid`, and summarised at the top of the form. |
| 4.1.3 Status messages | Saving status, run progress and flash messages use `role="status"` / `role="alert"` live regions. |

## Testing

- `tests/test_compliance.py` renders every page (anonymous and administrator) and checks the `lang` attribute, titles, an `h1`, the skip link, unique ids, labelled form controls, named buttons and image `alt` attributes.
- Before a release, check pages with the keyboard only, with a screen reader (NVDA, VoiceOver), at 200% zoom and at phone width, in both themes, and run an automated checker such as axe.

Known limitations are listed in the accessibility statement (drag and drop on the canvas, the Ace editor with some screen readers, third-party sign-in pages).
