# Odile — brand sheet

A quiet, classical identity for a product whose promise is that nothing is seen.
A single word set very large on a pool of paper, with a seventeenth-century
painting filling the rest of the screen. No accent colour, no ornament, nothing
that moves.

This sheet is the build contract. Every value below is copyable: a hex, a size
in pixels or rem, a weight, a duration, a measured contrast ratio. Where a rule
does not apply, the sheet says so and why. `docs/brand/REVIEW.md` is the
five-test review; `tools/brand_lint.py` enforces the palette, the surface rules
and the sheet's completeness.

## Contents

| Section | Answers |
|---|---|
| The name | Where the identity comes from, and the six rules it enforces |
| Name and mark | Wordmark, the website icon, clear space, minimum size, lockups |
| Palette | Every hex, its use, and every measured contrast pair |
| Accessibility tokens | Role tokens, focus, target size, measure, zoom, semantics |
| Typography | Every role, size, weight, line height, tracking and fallback |
| Spacing and layout | The 4 px scale, containers, breakpoints, rhythm |
| Component states | Button, input, textarea, checkbox, link, skip link, focus, disabled, error, empty, loading |
| Iconography | The glyph set, the stroke, and the no-icon-family rule |
| Motion | Durations, easing, what moves, reduced motion |
| Art direction | The Ruysch, its grade, its crop, the veil, product artwork |
| The landing page | The four elements and the confirmation, with values |
| Voice | Rules, five before/after pairs, formats, lexicon |
| Email | Invite, ready and brief, with subject lines and structure |
| Video | Title, caption and end cards at 1440 and 390 |
| Product surfaces | What each surface carries and what it never carries |
| Interaction | One primary, destructive actions, overlays, keyboard |
| Brand review | The procedure and the verdict format |
| Change log | Every edit to this sheet |
| Do / Don't | Pairs, not adjectives |

## The name

Odile of Alsace was given a house on a hill about 690 and kept it until she
died. The gift is documented; the blindness, the restored sight and the spring
are legend, written down three centuries later. The brand is built on the gift
and lit by the legend, and never presents one as the other.

Her name carries both readings. The old German *ōt* is "heritage, possession" —
what is yours by right. The tenth-century life glosses her baptismal name
*Sol Dei*, the sun of God. The machine is both: yours, and the thing you see by.

Say it *oh-DEEL* (French). Never *oh-dyle*, never *ODL* in copy, never
"Odile.ai".

The story is the source, not the surface. One painted saint is in the system:
the website icon is a detail of Saint Ottilia's head from Lucas Cranach the
Elder's *Saints Christina and Ottilia* (Katharinenaltar, Dresden), and it lives
only in browser chrome, never on a page. No other saint is depicted on any
surface: no crozier, no Latin, no feast-day promotion, and no blindness used as
a metaphor. The ring is the O of the name and the wall in plan; the icon is her
face. The painting is the lit room; every other surface is a page.

These rules sit beside the palette, and are enforced like it:

- **A house, not a platform.** The agent keeps the house; the owner holds the
  only sign-in. Never "platform", "instance", "our servers" or "we'll set it up
  for you".
- **The seal is the key.** *Key* means the boot key in the measured command line
  — a public key with no private half — and the disk key sealed to the measured
  boot. The owner's credential is the sign-in; the way back is the recovery code
  and the recovery file. A setup token, an API token or a session is a token.
- **A boundary, not a shield.** Say where another's authority stops. Never
  "fortress", "vault", "impenetrable" or "unhackable".
- **Kept.** *Kept* is the word for durability; a backup is a copy, not a vault.
- **Readable is the patronage.** Contrast is measured against the composited
  surface, every public page gets a screen-reader read-through, and colour never
  carries meaning alone.
- **The agent says "I".** Never "we", never an emoji, never performed warmth.
  Silence is its default; it speaks when it has something to report or is asked.

The full note — sources, the split between documented, estimated and legend,
and the lexicon: `docs/brand/ST-ODILE.md`.

## Name and mark

### Wordmark

- **Text:** `Odile`, title case, one word. Never `ODILE`, `odile`, `Odile.`,
  `Odile™` or a typographic ligature.
- **Face:** Cormorant Garamond 500, roman (not italic).
- **Size:** `clamp(5rem, 7.1vw, 6.7rem)` on the landing — 80.0 px at 390 and
  102.24 px at 1440. The floor is 5rem, not 4.6rem; the old floor rendered
  73.6 px and contradicted this sheet. On a product page the wordmark is the
  header mark, not the page's voice: `1.5rem` (24 px) on the document and form
  pages and `2rem` (32 px) on the report pages, the first-visit pages and the
  demo sign-in — the pairing is §Typography, *Two section-title roles*.
- **Line height:** .88. **Tracking:** −.03em. **Colour:** `--ink`. One line.
- **Clear space:** 0.5 × the rendered type size on all four sides — 12 px at
  24, 16 px at 32, 40 px at 390 and 51 px at 1440 on the landing — measured to
  other elements, not to the viewport edge (the page inset may be smaller).
  Nothing enters it: no rule, image edge, caption, button or second line. The
  shipped pages hold it, measured from the wordmark's box to the h1's box: the
  24 px mark clears by 12.0 px at 390 and 20.0 px at 1440 on claim, ceremony,
  register and forgot, 21.7 / 31.7 px on privacy, faq and terms, and 24.0 px
  at both widths on account; the 32 px mark clears by 26.3 / 34.3 px on
  `/verify` and `/verify-public`, 20.0 px at 390, 320 and 1440 on `/roadmap`
  and `/status`, 16.6 px on `/first-visit` and 37.6 px on
  `/first-visit-preview` at both widths. Claim, ceremony, register and forgot
  sit exactly at the 12 px floor
  at 390, and every other page clears its floor at both widths except the one
  named here. The tenant `/verify` page (`templates/verify.html`) is that
  exception: its header carries the wordmark alone, the box sits 12.0 px above
  the h1 at ≤560 — 4 px under the 32 px floor — and the 28 px header that
  closes the gap changes bytes embedded in the signed installer, so it ships
  with the release that re-embeds the page (t_fc412bd9). (The inked text sits
  further from the h1 — 20.1 / 28.1 px on claim, 29.8 / 39.8 px on privacy,
  33.5 / 41.5 px on `/verify` — because the wordmark's line box is taller than
  its glyphs; the box is the figure this rule measures.)
- **Minimum size:** 24 px on a page — the product header. The landing's floor
  is 80 px, and the video card's 390 rendition is 27.6 px, which is above the
  minimum and needs no exception (§Video). Nothing on a page sets the wordmark
  below 24 px.
- **Position:** top of the 520 px marketing column, 22 px above the
  description. It is the loudest thing on the page and the only thing at that
  size.

### The website icon

- **Source:** `web/icon-source.jpg` — a 475×475 crop of Lucas Cranach the
  Elder, *Saints Christina and Ottilia*, 1506, reverse of the right wing of
  the Katharinenaltar, Dresden (Galerie Alte Meister), public domain, from the
  2210×4231 Wikimedia Commons reproduction. `web/icon-source.txt` records the
  URL, both hashes and the box; `web/icon-source-supplied.jpg` is the 480×480
  JPEG Savio supplied on 2026-11-13, byte-identical. The right-hand figure is
  Saint Ottilia — Odile — the name the house carries.
- **One rendition, every size.** The icon is the painting itself, warmed to the
  sheet's grade and rendered once at 512 px, then downsampled with LANCZOS to
  every size — 16, 32, 48, 180, 192 and 512. There is no second drawing and no
  per-size pass: the 16 px tab entry is the 512 px master downsampled, and
  `tests/test_web_icon.py` asserts exactly that. Savio, 2026-11-13: a separate
  16 px sign was tried and retired (Change log). The grade, measured on the
  master: mean HSV saturation 0.34 and hue 41° (the source crop is 0.20);
  R−B 12 / 29 / 56 across the luminance bands (deepest, 10–25 %, above the
  25th percentile); top-decile luminance 208. At 16 px the face averages 172
  luminance against 59 in the corners, the skin keeps a mean R−B of 64, and no
  eye-band cell falls below 140 — the eyes are a shadow about 20 levels under
  the cheek, not a black socket. The reduction gives up the craquelure, the
  halo's rays and the veil's folds; it keeps the warm face, the pale wimple and
  the dark ground. `docs/brand/ICON-PROOF.png` is the at-size proof.
- **Files:** `favicon.ico` (16/32/48, one rendition), `icon-32.png`,
  `icon-192.png` and `icon-512.png` (the same rendition), `apple-touch-icon.png`
  (180: the head on an opaque paper mount, 12 px of paper on every side).
  Served first-party with `cache-control: public, max-age=31536000, immutable`.
  On a tenant machine the same five files are carried inside the signed
  installer, written to the machine's own icon root and answered there by its
  page server at `/favicon.ico`, `/icon-32.png`, `/icon-192.png`,
  `/icon-512.png` and `/apple-touch-icon.png` — the paths `/verify` and the
  first-visit page link — with the same types and the same immutable cache, so
  neither page asks HQ for its tab. The public demo host is the one place where
  the three files its sign-in page links (`favicon.ico`, `icon-32.png`,
  `apple-touch-icon.png`, the same bytes) sit beside that page in
  `/srv/odile-demo` and the demo block answers those three paths itself with
  one static handle and the same cache; `/icon-192.png` and `/icon-512.png`
  stay the page server's for the Webapp shell. `tools/icon_build.py`
  regenerates every one from the source.
- **Use:** the browser icon only. It appears in `rel=icon` and
  `rel=apple-touch-icon` links, never as an `<img>`, never in a page body,
  never as a `data:` favicon, never in an email or a video card. The tab is
  Odile's on every surface this sheet paints: the pages under `web/`, a
  tenant's `/verify` and first-visit page, the demo host's own sign-in page,
  and the Hermes Webapp the owner lands on after signing in. The gateway's
  server-rendered `GET /login` is the one screen that still shows the vendor's
  tab icon, because v0.23.0 exposes no seam for it
  (`docs/runbooks/install.md`, *What is not themed*). The Website icon is the
  Webapp's tab too: the installer copies `odile-favicon.ico` and
  `odile-apple-touch-icon.png` into the built bundle and points the shell's
  three tab links at them, so the vendor's Nous mark is no longer what the tab
  shows. The vendor's `apple-touch-icon.png` stays in the bundle as a vendor
  file, and the About hero (`Powered by Hermes`) is the remaining vendor
  credit. Savio, 2026-11-13: "Could the favicon also apply on the Hermes agent
  web interface page after logging in (e.g. https://demo.odile.fyi/ logged
  in)? Currently it shows the Hermes favicon." The credit sits in the HTML
  comment beside the link on every page: "Lucas Cranach the Elder, Saints
  Christina and Ottilia (Katharinenaltar, Dresden). Public domain. Source:
  Wikimedia Commons."
- **Clear space:** the 16/32/48 tab canvas is edge-to-edge; the 180 touch
  icon carries a 12 px paper mount. Do not crop closer than the chin.
- **Minimum size:** 16 px. Never recoloured, outlined, rounded, given a
  second ring or animated; never flipped, never cropped to the eyes, never
  placed on a gradient.

### Lockups

- **There are none.** No wordmark + ring lockup, no monogram, no icon family,
  no `Odile — <tagline>`, no "by Odile" endorsement, no partner lockup.
- The only approved arrangement *as a unit* is the video end card: wordmark,
  claim and two addresses at fixed positions (§Video). That is a card layout,
  not a lockup. The landing page's wordmark above its description is not a
  lockup either; the description follows 22 px below and stands on its own.
- There are no logo files. No SVG wordmark, no PNG wordmark, no outlined
  wordmark: the wordmark is live text in Cormorant every time. The website
  icon is a raster set, not a vector mark.

## Palette

| Token | Hex | Use |
|---|---|---|
| `--paper` | `#EADFC4` | The light pool, the button text. Old paper: deeper and yellower than ivory, never `#FFF`. |
| `--ink` | `#1C1813` | Wordmark, field text, button fill. |
| `--soft` | `#4A4136` | Description, confirmation sub-line, secondary text. |
| `--oxblood` | `#6E2A2A` | The second ink: rules, numerals, small-caps labels, the plate's ground rule. The same value as `--error`; never a fill, never a button, never a status colour on its own. |
| `--edge` | `rgba(28,24,19,.55)` | The field underline and control boundaries. Composited `#797263` on paper, 3.60:1 — reads as an edge, not a rule. |
| `--ink-hover` | `#2A241C` | Hover for the one filled button and any ink surface. Never `#000`. |
| `--backdrop-base` | `#0B0806` | The warm black behind the painting only. Never text. |

There is no accent colour. Oxblood is a second ink, not an accent: darker than
`--soft`, never a fill, and no state is carried by it alone. Contrast comes
from ink on paper, and from the paper against the painting. The website icon is
a warm-graded painting, not a UI surface: its colours are artwork tones,
measured on the master in §Name and mark, and they never leave the icon.

### Product extension

The Webapp, claim, verify, status and demo surfaces use the same paper/ink core
plus neutral steps for cards, rules and machine text. No new hue; the only
saturated colour is oxblood, and it is either an error or a rule.

| Token | Value | Use |
|---|---|---|
| `--card` | `#F2E9D4` | A raised paper panel; one step lighter than the page, 1.10:1 against paper. Pair it with `--line`; never rely on the fill alone as a boundary. |
| `--paper-light` | `#F6EFDF` | The lightest paper; one highlight, never a second background. |
| `--panel` | `#DDD0AE` | A recessed sand panel: credentials, the report. One step darker than the page, 1.16:1. Pair it with `--line`. |
| `--line` | `rgba(28,24,19,.18)` | A structural rule, composited `#C5BBA4` on paper (1.44:1). Never a border on every box, never text. |
| `--line-soft` | `rgba(28,24,19,.10)` | A division inside a panel, composited `#D5CBB2` on paper (1.22:1). Never text. |
| `--placeholder` | `#5D5244` | Italic field placeholder on the marketing veil; 5.75:1 on paper, 5.52:1 on the 1440 veil, 5.06:1 on the darkest rendered field pixel (`#DCD2B8`, 390), 6.31:1 on `--card`. |
| `--placeholder-on-paper` | `#5D5244` | Opaque paper fields only. The same value as `--placeholder`: the old `#776B5A` measured 3.93:1 on `#EADFC4` and was retired. |
| `--error` | `#6E2A2A` | Error text and the invalid underline only; 7.82:1 on paper, 8.57:1 on card. |
| `--term` | `#17130F` | Terminal field; warm black, never `#000`. |
| `--term-edge` | `#2A241D` | Terminal border; one step off the field. |
| `--term-ink` | `#EFE7D8` | Terminal body text; 15.04:1 on `--term`. |
| `--term-dim` | `#A79C8B` | Terminal secondary text; 6.84:1 on `--term`. |
| `--term-line` | `rgba(239,231,216,.16)` | Terminal rule. Never text. |
| `--term-command` | `#FFF8EA` | The command a user copies; not pure white; 17.48:1 on `--term`. |
| `--term-ok` | `#D8CBB5` | A confirmed line in the terminal; 11.55:1 on `--term`. |
| `--term-prompt` | `#8F8677` | The shell prompt; 5.14:1 on `--term`. |

### Legacy surfaces

One surface is not on odile.fyi and has not migrated: the demo template
(`demo/local/template.html`). It keeps the old paper until its own card lands,
paints the v0 values in its own `:root`, and the lint needs those values
stated. Never use them on `web/` or `docs/brand/` surfaces.

The prototype (`prototype/userflow.html`) was the second surface. The one-root
cut of 2026-11-14 (`24ee323`) left `prototype/` out of the public tree, so the
surface is gone, not un-migrated: no commit reachable from `main` carries
`prototype/userflow.html`, and `git ls-tree HEAD prototype` is empty. It is a
record in this sheet, not a build target — there is no path on this tree to
open. The status template (`templates/status.html`) left this list on
2026-11-13, when ODL-467 published it at `/status`: it paints `--paper` and
`--card`, and the two rows it carries are the deck's.

| Token | Value | Use |
|---|---|---|
| `--paper-v0` | `#F6F0E4` | The old paper; demo template only. |
| `--card-v0` | `#FBF6EA` | The old raised panel; demo template only. |
| `--error-v0` | `#8F3418` | The old error red; demo template only. |
| `--placeholder-v0` | `#776B5A` | The old opaque-field placeholder; demo template only. |

### Contrast pairs

Measured with the WCAG 2.x relative-luminance formula in sRGB, not eyeballed.
A pair is a foreground on a composited background: where a veil or a panel
changes the surface, the ratio is measured against that surface.

| Pair (foreground on background) | Ratio | Minimum | Use |
|---|---|---|---|
| `--ink` on `--paper` | 13.33:1 | 4.5 | Wordmark, headings, body |
| `--ink` on `--card` | 14.61:1 | 4.5 | Body on a raised panel |
| `--ink` on `--panel` | 11.52:1 | 4.5 | Body in a recessed panel |
| `--soft` on `--paper` | 7.55:1 | 4.5 | Description, secondary text |
| `--soft` on `--card` | 8.27:1 | 4.5 | Product secondary text |
| `--soft` on `--panel` | 6.53:1 | 4.5 | Secondary text in a panel |
| `--oxblood` on `--paper` | 7.82:1 | 4.5 | Rules and numerals; the text beside them is ink |
| `--oxblood` on `--card` | 8.57:1 | 4.5 | Labels on a raised panel |
| `--oxblood` on `--panel` | 6.76:1 | 4.5 | Labels in a recessed panel |
| `--placeholder` `#5D5244` on `--paper` | 5.75:1 | 4.5 | Marketing field |
| `--placeholder` `#5D5244` on `#DDD3B9` | 5.11:1 | 4.5 | Worst measured field (1440, backdrop blocked) |
| `--placeholder-on-paper` on `--paper` | 5.75:1 | 4.5 | Opaque product field |
| `--placeholder-on-paper` on `--card` | 6.31:1 | 4.5 | Opaque card field |
| `--ink` on `#E5DBC0` | 12.80:1 | 4.5 | Type on the 1440 veil |
| `--soft` on `#E5DBC0` | 7.25:1 | 4.5 | Description on the 1440 veil |
| `--placeholder` `#5D5244` on `#E5DBC0` | 5.52:1 | 4.5 | Field on the 1440 veil |
| `--ink` on `#E6D8BA` | 12.52:1 | 4.5 | Type on the 390 veil |
| `--soft` on `#E6D8BA` | 7.09:1 | 4.5 | Description on the 390 veil |
| `--error` on `#E6D8BA` | 7.35:1 | 4.5 | Error on the 390 veil |
| `--ink` on worst measured veil `#D5CBB3` | 10.95:1 | 4.5 | Wordmark, 390, rendered pixel |
| `--soft` on worst measured veil `#D7CDB4` | 6.32:1 | 4.5 | Footnote, 1440, rendered pixel |
| `--error` on worst measured field `#DCD2B9` | 6.89:1 | 4.5 | Error under the field, 1440, rendered pixel |
| `--error` on `#DDD3B9` | 6.95:1 | 4.5 | Error on the worst field |
| `--placeholder` `#5D5244` on worst measured field `#DCD2B8` | 5.06:1 | 4.5 | Field, 390, rendered pixel |
| `--edge` composite `#7C766A` on `--card` | 3.73:1 | 3.0 | Input underline on a card |
| `--edge` composite `#797263` on `--paper` | 3.60:1 | 3.0 | Input underline, control boundary |
| `--edge` composite `#736B59` on `--panel` | 3.45:1 | 3.0 | Control boundary in a panel |
| `--error` on `--paper` | 7.82:1 | 4.5 | Error text |
| `--error` on `--card` | 8.57:1 | 4.5 | Error text on a panel |
| `--paper` on `--ink` | 13.33:1 | 4.5 | Primary button label |
| `--paper` on `--ink-hover` | 11.59:1 | 4.5 | Primary button hover |
| `--term-ink` on `--term` | 15.04:1 | 4.5 | Terminal body |
| `--term-dim` on `--term` | 6.84:1 | 4.5 | Terminal secondary |
| `--term-prompt` on `--term` | 5.14:1 | 4.5 | Prompt |
| `--term-command` on `--term` | 17.48:1 | 4.5 | Command |
| `--term-ok` on `--term` | 11.55:1 | 4.5 | Confirmed line |
| `--line` composite on `--paper` | 1.44:1 | not text | Structural only; never carries meaning |
| `--line-soft` composite on `--paper` | 1.22:1 | not text | Division only; never carries meaning |
| `--card` on `--paper` | 1.10:1 | not text | Raised panel fill; pair with `--line` |
| `--panel` on `--paper` | 1.16:1 | not text | Recessed panel fill; pair with `--line` |

Disabled controls are exempt under WCAG 1.4.3: `--ink` at 38 % over `--paper`
computes 2.30:1 with paper text. The reason must be visible within 8 px of the
control; opacity alone never communicates "disabled".

**Measure the composited surface.** A token that passes on paper can fail on a
veil: the old `#776B5A` placeholder measured 4.59:1 on the ivory paper and
4.11:1 on the 390 veil, and on `#EADFC4` it falls to 3.93:1 — which is why the
placeholder is now `#5D5244`. A contrast number is only valid for the surface
named beside it.

## Accessibility tokens

Use roles, not raw colours. Every role below resolves to a token in §Palette;
the contrast pair is the one measured for that surface.

| Role | Token or value | Rule |
|---|---|---|
| Text, primary | `--ink` on `--paper` / `--card` | ≥4.5:1; measured 13.33 / 14.61 |
| Text, secondary | `--soft` on `--paper` / `--card` | ≥4.5:1; measured 7.55 / 8.27 |
| Text, placeholder | `--placeholder` on the veil; `--placeholder-on-paper` on opaque paper | Only on the named surface; measured 5.52 on the 1440 veil, 5.41 on the 390 veil, worst 5.06 on a rendered field |
| Text, error | `--error` on `--paper` / `--card` | ≥4.5:1; measured 7.82 / 8.57; text and underline only |
| Surface, page | `--paper` | Never `#FFF`; one page per screen |
| Surface, raised | `--card` | One step lighter; pair with `--line` |
| Surface, highlight | `--paper-light` | One highlight per screen, never a second background |
| Boundary, structural | `--line` / `--line-soft` | Decoration only; never the only signal |
| Boundary, control | `--edge` | ≥3:1 against the adjacent surface; 3.60 on paper; the shipped input measures 3.42 at 1440 and 3.32 at 390 |
| Focus ring | 2 px solid `--ink`, offset 3 px; on `--term`, `--term-ink` | ≥3:1 against both sides; 13.33:1 on paper, 14.61:1 on card, 12.80:1 on the 1440 veil, 12.52:1 on the 390 veil; never `outline: none` without this replacement |
| Target size | 44×44 px minimum, 8 px minimum gap | WCAG 2.2 2.5.8; inline prose links are the documented exception |
| Body text | 18 px / 1.6 on public pages; 16 px minimum for notes; Webapp chat 16 px / 1.6 | Large text is 24 px regular or 18.66 px bold for the 3:1 rule |
| Measure | 36ch marketing; 60–72ch product; 66ch email; 1100 px video | A wider measure is a new decision |
| Motion | 120 / 180 / 240 ms; 0 under `prefers-reduced-motion: reduce` | §Motion |
| Zoom | 200 % text, 320 px reflow, no horizontal scroll | Test at 320×640 and a 32 px root |
| Keyboard | Every control reachable in DOM order; focus visible; overlays trap and restore; a skip link is the first focusable element when a page has more than one landmark | No hover-only action, no positive `tabindex` |

Semantics are part of the token set: one `<h1>` per screen and no heading
level skipped; `<button>` for actions, `<a href>` for navigation; a real
`<label>` for every field (visually hidden is allowed, placeholder is not a
label); `aria-invalid` and `aria-describedby` on an invalid field; `role="alert"`
on the message that appears; `lang` on the document; a page `<title>` that is
the wordmark or the screen name plus the wordmark. Pseudo-elements are
measured too: `::placeholder`, `::selection`, `::marker` and `:focus-visible`
each get a computed-style read in every review. `::selection` is `--paper` on
`--ink`; inside a terminal it is `--term-ink` on `--term` (15.04:1), because the
page pair paints `--ink` on `--term` (1.05:1) there and a copied fingerprint
would show no highlight; `::marker` inherits the body colour and gets no custom
glyph. Under
`forced-colors: active`, keep the 2 px outline and let the system colours
replace the tokens. Dark mode is not in alpha.0: `prefers-color-scheme` does not
switch the palette; the terminal is the only dark surface.

## Typography

Two type roles and two machine roles. One face never does both jobs:

- **Display — Cormorant Garamond** (SIL OFL 1.1), a variable face 300–700
  with roman and italic, subset to the characters the page uses and embedded
  as woff2. It is the brand voice at display size: the wordmark, h1/h2/h3 and
  the demo sign-in's h1. It never sets a sentence at reading size. No font host
  is ever contacted.
- **Text — the platform's text serif**, `"Iowan Old Style", "Palatino Linotype",
  Palatino, Georgia, "DejaVu Serif", serif`, at 18 px / 400 / 1.6 for public
  prose and 16 px for notes. It is a system stack: no file, no request, no
  swap. The resolved face is the platform's own — Iowan Old Style on macOS,
  Palatino Linotype or Georgia on Windows, DejaVu Serif on Linux (measured on
  this box). A line break may move between platforms; the measure band is the
  contract, not the pixel width.
- **Interface — the Webapp bundle's own UI sans stack** (OpenDyslexic first,
  then the platform sans): `"OpenDyslexic", "Segoe WPC", "Segoe UI",
  -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
  system-ui, sans-serif, …`. It sets the
  sidebar, labels, buttons, composer and chat prose at the vendor's sizes:
  13–14 px labels, 17 px composer, 16 px / 1.6 chat with a 60–72ch measure.
  The Webapp's `--dt-font-sans` resolves to this stack; Cormorant is reached
  only by `p.wordmark` and the skin's headings.
- **Machine — the system mono stack**, unchanged: `ui-monospace, SFMono-Regular,
  Menlo, Consolas, "Liberation Mono", monospace`.

```css
--display: "Cormorant Garamond", "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
--text:    "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, "DejaVu Serif", serif;
--ui:      "OpenDyslexic", "Segoe WPC", "Segoe UI", -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", system-ui, sans-serif;
--mono:    ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
```

Every `font-family` declaration names the full stack; the semantic role
(`--display`, `--text`, `--ui`) is copied, never a bare `serif` or `sans-serif`
alone. If the embedded display face fails, its fallback stack carries the
headings at the same sizes and measures. The text face has no embedded file,
so it cannot swap and does not move the first paint.

| Role | Face | Size | Weight | Line height | Tracking | Colour | Measure |
|---|---|---|---|---|---|---|---|
| Wordmark | Display | `clamp(5rem, 7.1vw, 6.7rem)` — 80.0 / 102.24 px | 500 | .88 | −.03em | `--ink` | one line |
| Product header wordmark | Display | `1.5rem` — 24 px on the document pages; `2rem` — 32 px on the report pages, the first-visit pages and the demo sign-in | 500 | .88 | −.03em | `--ink` | one line |
| Marketing description | Text | 18 px | 400 | 1.6 | 0 | `--soft` | 36ch |
| Marketing field text | Text | 16.8 px | 400 | 1.4 | 0 | `--ink` | field width |
| Marketing placeholder | Text italic | same | 400 | 1.4 | 0 | `--placeholder` | field width |
| Primary button | Text | 10 px caps | 600 | 1 | .28em | `--paper` on `--ink` | one line |
| Confirmation line | Text italic | 19.52 px | 400 | 1.4 | 0 | `--ink` | 30ch |
| Confirmation sub-line | Text italic | 16 px | 400 | 1.6 | 0 | `--soft` | 36ch |
| Confirmation address | Text | 16 px | 400 | 1.5 | 0 | `--ink` | 36ch, wrap anywhere |
| Marketing error | Text italic | 16 px | 400 | 1.5 | 0 | `--error` | field width |
| Page title (h1) | Display | `clamp(2.5rem, 5vw, 4.1rem)` — 40 / 65.6 px | 500 | 1.02 | −.025em | `--ink` | 18ch |
| Section title (h2) — report pages | Display | `clamp(1.9rem, 3vw, 2.6rem)` — 30.4 / 41.6 px | 500 | 1.04 | −.02em | `--ink` | 48ch |
| Section title (h2) — document pages | Display | `1.34rem` — 21.44 px | 600 | 1.2 | 0 | `--ink` | 48ch |
| Subsection (h3) | Display | `1.45rem` — 23.2 px | 500 | 1.1 | −.01em | `--ink` | 52ch |
| Product body | Text | 18 px | 400 | 1.6 | 0 | `--ink` | 60–72ch |
| Product help | Text | 16 px | 400 | 1.6 | 0 | `--soft` | 60ch |
| Empty / loading line | Text | 17 px | 400 | 1.6 | 0 | `--ink` | 60ch |
| Notice / callout | Text | 16 px | 400 | 1.6 | 0 | `--soft`; strong `--ink` | 60ch |
| Status lede | Text | 18 px | 400 | 1.6 | 0 | `--soft` | 66ch, `text-wrap:balance` |
| Status figure (mono) | Mono | `clamp(2rem, 4.5vw, 3.1rem)` — 32 / 49.6 px | 400 | 1 | −.02em | `--ink` | one measured figure per screen; tabular figures |
| Table label (mono caps) | Mono | `.72rem` — 11.52 px column; `.78rem` — 12.48 px row | 600 | 1.3 / 1.4 | .12em | `--soft` | one line, uppercase |
| Label (caps) | Text | 10 px caps | 600 | 1 | .24em | `--soft` | one line |
| Micro-label (caps) | Text | 9 px caps | 600 | 1 | .20em | `--soft` | one line |
| Mono body | Mono | 12.5 px | 400 | 1.55 | 0 | `--ink` or `--term-ink` | 72ch, no wrap inside a token |
| Mono value | Mono | 12.5 px | 400 | 1.5 | 0 | `--ink` | right-aligned, `overflow-wrap:anywhere` |
| Terminal command | Mono | 12.5 px | 400 | 1.55 | 0 | `--term-command` | 72ch |
| Email heading (HTML only) | Text (system serif) | 24 px | 500 | 1.1 | −.02em | `--ink` | 48ch |
| Email body | Text (system serif) | 17 px | 400 | 1.55 | 0 | `--ink` | 66ch |
| Email footer | Text (system serif) | 12.5 px | 400 | 1.5 | 0 | `--soft` | 66ch |
| Video wordmark | Display | 102 px at 1440; 27.6 px at 390 | 500 | .88 | −.03em | `--ink` | one line |
| Video sentence | Display | 64 px at 1440; 17.3 px at 390 | 500 | 1.15 | 0 | `--soft` | ≤1100 px |
| Video address | Mono | 52 px at 1440; 14.1 px at 390 | 400 | 1.5 | 0 | `--ink` | one line each |
| Webapp UI label | UI | 13–14 px | 400 | 1.5 | 0 | `--ink` / `--soft` | one line |
| Webapp composer | UI | 17 px | 400 | 1.55 | 0 | `--ink` | field width |
| Webapp chat prose | UI | 16 px | 400 | 1.6 | 0 | `--ink` | 60–72ch |

Rules that are not in the table:

- **Capitals are always letterspaced.** Primary buttons .28em; compact
  controls .12–.24em depending on the row; lowercase is never tracked, except
  the display faces' optical negative tracking (wordmark −.03em, h1 −.025em,
  the report h2 −.02em, h3 −.01em). The document h2 and every form field are
  untracked: no `input`, `textarea` or `select` rule declares `letter-spacing`,
  and `tools/brand_lint.py` fails on one.
- **Two section-title roles, and each travels with the wordmark.** A paper page
  takes one shell, never a mix. The **document** shell — a 24 px wordmark and
  the `1.34rem`/600 h2 — is the default for a page read as a document or
  filled as a form (claim, ceremony, faq, privacy, terms, register, forgot,
  account, and the `/signin` form `app.py` renders, which declares the same
  24 px mark and `1.34rem`/600 h2 in its own shell):
  a section is separated from 18 px prose by weight, not by size, because the
  step is only 1.19×, and that is why the weight is 600 rather than the h1's
  500. The **report** shell — a 32 px wordmark and the
  `clamp(1.9rem,3vw,2.6rem)`/500 h2 — is for the pages whose sections are
  blocks of machine values (`/verify`, `/verify-public`, the verify page
  template, `/status`, `/roadmap`), the two first-visit pages and the demo
  sign-in: the step is 2.3× at 1440, so 500 carries it. `web/first-visit.html`
  takes it because it is embedded beside the verify page in the signed
  installer; `web/first-visit-preview.html` is its read-only snapshot, which
  `app.py` serves at `/first-visit-preview` — the demo host calls it the
  retired preview and does not link it — and it carries the same shell because
  it renders that screen. At 390 the
  wordmark is 1.12× its h2 on the document pages and 1.05× on the report
  pages; at 1440 the report h2 overtakes the wordmark (41.6 vs 32). The page
  title is the page's voice and the wordmark is the masthead in both. A new
  paper page takes the document shell unless it is the report family; the lint
  holds every page under `web/` and `templates/` to the pairing — it reads
  `.brand` as the rule's last simple selector wherever the rule is scoped, and
  it holds an h2 written in px to the same 1.2 rem floor.
- **No faux bold, no outline type, no gradient text, no all-caps sentence.**
  The 9 px micro-label is a label, never a paragraph.
- **The display face is never used for a sentence at reading size.** If the
  text is a paragraph, a note, a label, a field or a button, it is the text
  face — or the interface face on the Webapp. The video card's 390 rendition
  is the same 64 px display line at 27.1 %: a picture, not a reading-size page.
- **Minimums:** body text 18 px on a public page (16 px for notes); Webapp
  chat prose 16 px; UI labels 13 px; mono 12 px; labels 9 px. Prose never
  falls below 14 px on a 390-wide card.
- **Numbers are old-style in prose and tabular in mono.** Machine values
  (hashes, fingerprints, IPs, versions, measurements) are always mono; prose
  numbers are the text face. A short machine value — a SHA-256 fingerprint, an
  IP, a version, a commit hash — never wraps mid-token: wrap the row, not the
  token. A value longer than its panel wraps inside its own field with
  `overflow-wrap:anywhere` and is never set below 12 px to fit: the 96-hex
  SEV-SNP measurement is 795 px at the 13.76 px mono and cannot fit the 674 px
  `/verify` panel at 1440 or the 316 px one at 390, so it wraps there and the
  page never widens.
- **The description is held to 36ch.** At 18 px that is about 325–415 px
  depending on the platform face, and three lines at 1440; product prose is
  60–72ch. A measure outside those bands is a new decision, not a tweak.
- **An embedded text face is the exception, not the rule.** Embed one only if
  the platform stack fails the wrap/measure test on two platforms; then it is
  one OFL text face on every page, and the signed pages are re-cut in the
  release card.

## Spacing and layout

### The scale

A 4 px base. Use `4 / 8 / 12 / 16 / 24 / 32 / 48 / 64` px (tokens
`--space-1` … `--space-16`). No arbitrary margin.

| Step | Token | Use |
|---|---|---|
| 4 | `--space-1` | Icon-to-text, hairline gaps |
| 8 | `--space-2` | Label to field (new work; the landing page keeps 6), error to field, inline groups |
| 12 | `--space-3` | Compact padding, stacked metadata |
| 16 | `--space-4` | Form gap, panel padding, paragraph gap |
| 24 | `--space-6` | Panel padding, section gap inside a card |
| 32 | `--space-8` | Block gap, compact grid gap |
| 48 | `--space-12` | Section gap on product pages, two-column grid gap |
| 64 | `--space-16` | Page section gap, large empty space |

Three optical exceptions exist in the shipped landing page and are frozen
there: 6 px from a label to its field (4 + 2), 22 px from the wordmark to the
description (20 + 2) and 34 px from the description to the action (32 + 2). New
work uses 8, 24 and 32; the three exceptions may be rounded in the next landing
edit without re-measuring.

### Containers and breakpoints

- **Marketing column:** 520 px, inset `clamp(22px, 6vw, 76px)` from the left
  on wide screens and 22 px at ≤820. Optically centred vertically at ≥821;
  top-aligned at ≤820 with `padding-top: clamp(28px, 7vh, 54px)`.
- **Product page:** one column, max 760 px; the stage max 1180 px. Wide
  two-column grids use a 48 px gap (the shipped prototype measures 54/50/40;
  round to 48 in new work). Page inset 18 px at ≤560 and 24 px above (the
  prototype's 26 px rounds to 24).
- **Breakpoints:** 560 px (small-screen padding; report tables stack), 700 px
  (compact product; the prototype rail), 820 px (marketing wash and top
  alignment), 900 px (demo workspace rail), 980 px (two-column product grid).
  Five are enough; a sixth is a new decision.
- **Measure:** 36ch marketing, 60–72ch product, 66ch email, 1100 px video.
- **Above the fold:** at 390×844 the primary action is visible without
  scrolling. Marketing carries at most three blocks before its one action; the
  claim ceremony carries at most seven, and a fourth pre-action block is a
  decision to move it after the action or cut it. The trust moment — no one at
  Odile holds the disk key, the seal is the key, and there is no reboot wait —
  sits above the fold on the landing and claim screens. The support-grant
  sentence is the grant's own row: ODL-217 proved the grant and ODL-474 landed
  the A10 row, so a tenant's `/verify` names a live grant by its id, expiry and
  key fingerprint rather than showing a bare FAIL, and a machine with no grant
  says `no grant`.
- **Overflow:** no horizontal overflow at 320, 390, 720 or 1440 px in any
  state. A 249-character email scrolls inside its field and a 248-character
  confirmation address wraps in its line; the page never widens. Fields and
  panels get `min-width: 0` and `overflow-wrap: anywhere` before they get a
  scrollbar.
- **Direction and language:** alpha.0 ships English only. There is no RTL layout
  and no second language. The embedded Cormorant subset carries the display
  characters; the text face is the platform's own, so a new script is a new
  subset for the display face, not a new face. The 4 px scale
  and the containers are direction-neutral: an RTL build is a layout decision,
  not a token rewrite.

## Component states

States are designed, not implied. Each row gives the exact value a builder
copies.

| Component | State | Values |
|---|---|---|
| Primary button | Default | 44 px min-height; padding 0 24 px; 1 px `--ink` border; `--ink` fill; label `--paper`, 10 px caps, 600, .28em; radius 0 |
| Primary button | Width | 340 px at ≥561 on a single-column form; full width at ≤560. The landing's 340 px block at the product page's own inset breakpoint: 820 px is the marketing wash and top alignment (§Containers and breakpoints), not a form rule, and at 561–820 the claim column is 513–672 px, where full width would be a bar |
| Primary button | Hover | Fill and border `--ink-hover` `#2A241C`; no transform, no shadow |
| Primary button | Active | Hover values; no movement; the label does not shift |
| Primary button | Focus-visible | 2 px `--ink` outline, 3 px offset (13.33:1 on paper) |
| Primary button | Disabled | Opacity .38; `cursor: not-allowed`; no hover; the reason is visible within 8 px, never opacity alone |
| Primary button | Busy | Height stays 44 px; label stays; `aria-busy="true"`; one 2 px paper rule inset along the bottom edge travels once in 1.2 s linear; reduced motion shows it static at 50 % |
| Secondary button | Default | Transparent; 1 px `--edge` border; `--ink` label; same height, padding and type as the primary |
| Secondary button | Hover | `rgba(28,24,19,.05)` fill; border `--ink`; no movement |
| Secondary button | Focus / disabled | Focus ring as primary; disabled opacity .38 with a visible reason |
| Quiet / link button | Default | No box; 10 px caps, 600, .20em, `--ink`; underline 1 px `--edge`, offset .22em; 44 px min-height |
| Quiet / link button | Hover | Underline `--ink`; no colour change |
| Skip link | Default | `.skip{position:absolute;left:-9999px;top:0}`: the link leaves the layout at rest and is still the first focusable element — `<a class="skip" href="#main">Skip to content</a>` before `<main id="main">`, because the page has more than one landmark (§Accessibility tokens, Keyboard); no positive `tabindex`; it is never a second primary action |
| Skip link | Focus-visible | `left:24px` (`18px` at ≤560); `top:12px`; `z-index:10`; 44 px min-height; padding 12 px 16 px; `--ink` fill, `--paper` label at 10 px caps / 600 / `.20em` / line-height 1.2 in the `--text` face (the link role, not the `.28em` filled-control role; the generated `/roadmap` page is the one named exception — `ui-monospace`, matching that page's own mono micro-labels), `text-decoration:none`; ring 2 px `--ink` at 3 px offset (13.33:1 on paper). `:focus-visible` only — never `:focus` alone, so a pointer click does not paint the bar. Measured at 1440: 170.7 × 44 at (24, 12); at 390 and 320: the same 170.7 × 44 at (18, 12), overlapping the 66 × 52 wordmark across its 66 px width — accepted, the bar is visible only while focused, sits above it at `z-index:10` and `elementFromPoint` returns the link itself |
| Text input | Default | 44 px min-height; transparent; bottom border 1 px `--edge`; padding 9 px 2 px 11 px; text 1.05rem/1.4 `--ink`; radius 0 |
| Text input | Placeholder | Italic; `--placeholder` on the veil; `--placeholder-on-paper` on opaque paper; opacity 1; measured per §Contrast pairs |
| Text input | Focus | 2 px `--ink` outline, 3 px offset; bottom border `--ink`; the outline is never removed |
| Text input | Filled | Same as default; the label does not disappear |
| Text input | Error | Bottom border `--error`; message 8 px below the field, `.94rem` italic `--error`; `aria-invalid="true"`; `aria-describedby` points at the message; `role="alert"` |
| Text input | Disabled | Opacity .38; the reason is visible within 8 px; no hover |
| Text input | Long value | The field scrolls horizontally inside itself; the page does not widen; 249 characters was the measured worst case |
| Textarea | Default / focus | 84 px min-height; 1 px `--edge` border; `--card` fill; padding 12 px 14 px; mono 12.5 px/1.55; focus ring as input |
| Textarea | Error | Border `--error`; message and ARIA as input |
| Checkbox | Default | 18×18 px visual in a label of at least 44 px height; 1 px `--edge`; `--paper` fill; radius 0 |
| Checkbox | Checked | 1 px `--ink`; a 10×10 px `--ink` square, centred; no native white or black |
| Checkbox | Focus | 2 px `--ink` ring on the label, 3 px offset |
| Link | Default | `--ink`; underline 1 px; `text-underline-offset: 2px`; visited stays `--ink`; never colour alone |
| Link | Hover | Underline thickens to 2 px; colour stays `--ink` |
| Link | Focus-visible | 2 px `--ink` outline, 3 px offset |
| Focus (global) | Any control | 2 px solid `--ink`, 3 px offset; on `--term`, 2 px `--term-ink`; target ≥3:1 against both sides; visible on keyboard only (`:focus-visible`) while inputs also show it on `:focus` |
| Disabled (global) | Any control | Opacity .38; `cursor: not-allowed`; no hover or active state; a visible reason within 8 px; WCAG 1.4.3 exempts the text, not the explanation |
| Error (global) | Invalid field | `--error` text and underline only; never a red fill, never a red primary button; iconless, because the message is the signal |
| Empty | Empty state | One sentence naming what is missing (`No session yet.` in the terminal; the claim screen's `No key yet.` is retired with the key step and re-cut in t_13cacab8), one action, nothing else. Optional contained artwork per §Art direction. Never a bare "No data" |
| Loading | Page | Paper field, wordmark, one status line in the empty/loading role (`Loading…` is enough; the line says what is happening, never a claim); no spinner, skeleton or shimmer. A wait longer than 400 ms gets the 2 px progress rule; reduced motion shows it static. Never a blank white frame |
| Loading | Region | A 2 px `--line` rule that fills once in 1.2 s linear; `aria-busy="true"` on the region; no layout shift |
| Success | Confirmation | The form is removed, not hidden; `role="status"`; no toast, no confetti, no second action |
| Notice / callout | Info | 2 px `--edge` left rule, 14 px padding-left, `.98rem` `--soft`, strong `--ink`; no fill, no shadow |

## Iconography

- **There is no icon family.** No icon set, no line-icon library, no emoji, no
  flag, no eye, no lock, no shield, no key icon. The website icon (§Name and
  mark) is a painted head, not an icon, and it appears in browser chrome only.
- **Functional glyphs, when a glyph is unavoidable:** 1 px `--ink` stroke on a
  16, 20 or 24 px box, square caps, no fill, no two-tone, optically centred at
  20 px and aligned to the text baseline + 2 px. The default box is 20 px.
- **The permitted glyph set is text:** `→` (U+2192) for a forward action,
  `←` for back, `·` (U+00B7) for a separator, `—` (U+2014) for a dash, `×`
  (U+00D7) for close, `$` for a shell prompt. Set them in the surrounding
  face, not as images.
- **No icon-only control.** A glyph button carries a visible label or a
  minimum 44×44 px target plus `aria-label`; the label is preferred.
- **No decorative glyphs.** A rule separates; a glyph is an action. No stars,
  sparkles, checks, dots or arrows used as ornament.
- **Website icon exception:** the raster set in §Name and mark. It is the only
  image outside a page's one artwork, and it is never drawn as a glyph.

## Motion

Motion explains a state change; it never entertains. These are the only
durations:

| Token | Duration | Use |
|---|---|---|
| `--motion-fast` | 120 ms | Colour and opacity on a control |
| `--motion-base` | 180 ms | Hover and background (the shipped button uses `.18s`) |
| `--motion-panel` | 240 ms | Panel, drawer or overlay entering; 250 ms is the shipped prototype drawer and may be rounded to 240 |
| `--motion-progress` | 1200 ms, linear, once | The loading rule; never loops |

- **Easing:** `cubic-bezier(.2, 0, 0, 1)` for enter/exit; `linear` for the
  progress rule. No spring, no bounce, no elastic, no `ease-in-out` by default.
- **Nothing translates further than 8 px.** A panel may translate ≤8 px; text
  never slides, scales or fades word by word. No parallax, no marquee, no
  carousel, no auto-playing video, no looping animation. The one exception is
  the loading progress rule, which fills its own track once — a fill, not a
  translation.
- **Maximum 300 ms for a transition.** A transition longer than 300 ms is a new
  decision. The 1.2 s progress rule is a fill, not a transition, and is the
  only timing above 300 ms.
- **Reduced motion:** under `prefers-reduced-motion: reduce`, set
  `transition: none` and `animation: none` on every element, `scroll-behavior:
  auto`, and render the progress rule static at 50 %. The state still changes;
  only the tween is removed. This is tested in every visual review, including
  the drawer and the terminal transcript.
- **Video is exempt from `prefers-reduced-motion`** because the viewer controls
  playback, but the cut itself uses hard cuts and holds, never an animated
  transition (§Video).

## Art direction

Classical painting, modern restraint. The painting is a room the type sits in,
not wallpaper. One artwork per page, and one painted head in the browser tab:
two paintings in the system, never on the same surface. Public domain,
uncredited in the interface except where the licence requires it; the icon's
credit sits in the HTML comment beside its link.

### The Ruysch backdrop

- **Artwork:** Rachel Ruysch, *Still-Life with Flowers*, c. 1717 — public
  domain. Provenance lives in an HTML comment on the landing page. No second
  image on the page, no collage, no carousel, no frame, no gold border, no
  vignette.
- **File:** `web/bg-ruysch.webp`, 1400×875, WebP quality 68, 96,726 bytes
  (97 KB). Served `cache-control: public, max-age=31536000, immutable`. One
  request, first-party. The painting is decorative: no alt text; the credit is
  in the HTML comment and in `/about` if that page is built.
- **Grade:** warm the highlights, lower the saturation slightly, keep the blacks
  warm. Measured targets for the shipped file: mean HSV saturation 0.414; the
  top decile mean is (216,198,166); every luminance band above the 10th
  percentile has R > B (the 10–25 % band is R−B 1.8; the 25–100 % bands are
  R−B 26–76); the deepest decile stays neutral within 1 level (9,9,8). Do not
  add a filter that makes the painting look new, and do not brighten the
  deepest decile.
- **Crop at ≥821 px:** `center / cover`; at 1440×900 the whole plate is shown
  (scale 1.0286; 1400×875 → 1440×900) with nothing cropped. Keep it that way.
- **Crop at ≤820 px:** `center 42% / cover` — x 50 %, y 42 %. The rendered
  image is 1350×844 and at x 50 % the source window is x 498–902 — the middle
  29 % of the plate; 71.1 % of the width is cropped and the height is whole.
  The 42 % is vertical: it keeps the bouquet's lower half in view, and it never
  moves the crop horizontally. The bouquet must remain visible at the bottom;
  never crop it out, never flip the image, never move the position left of
  centre.
- **Crop between 821 and 1439 px:** the same `center / cover`. At 1024×1366
  the scale is 1.561 and the source window is x 372–1028 — the middle 47 % of
  the width, height whole. Keep the position at centre; the bouquet stays in
  the right half of the frame at every width above 820.
- **Veil, ≥821 px:** one radial gradient,
  `radial-gradient(78% 96% at 4% 46%, #EADFC4 0%, rgba(234,223,196,.985) 26%, rgba(234,223,196,.88) 43%, rgba(234,223,196,.50) 58%, rgba(234,223,196,.12) 70%, rgba(234,223,196,0) 78%)`.
  At 1440 the type sits on `#E5DBC0`, 4–10 RGB levels darker than paper.
- **Veil, ≤820 px:** one linear wash,
  `linear-gradient(180deg, rgba(234,223,196,.90) 0%, rgba(234,223,196,.95) 22%, rgba(234,223,196,.93) 62%, rgba(234,223,196,.62) 84%, rgba(234,223,196,.30) 100%)`.
  At 390 the type sits on `#E6D8BA`, 4–10 RGB levels darker than paper.
- **The veil is the contract.** Never put body text directly on the painting.
  Paper behind every word, the bouquet visible at the edge. Contrast is
  measured against the composited surface, never against `--paper`.
- **The only gradient is the paper light.** No decorative gradient, no mesh, no
  glow, no gradient text.

### The website icon

The browser icon is the second painting, and the only one allowed outside a
page: Lucas Cranach the Elder, *Saints Christina and Ottilia* (Katharinenaltar,
Dresden, 1506), cut to Saint Ottilia's head. It never appears on a page; the
Ruysch never appears in the tab. The file set, the one rendition and the
credit are §Name and mark.

### Product surfaces

Product surfaces are paper, not gallery. The Webapp, claim, verify,
status, demo and email pages use the paper field and ink type. One contained
artwork is allowed in an empty state or onboarding: longest side ≤320 px at 390
and ≤480 px at 1440, at least 24 px from text, never behind controls, tables or
terminal output, no frame, no shadow, no caption.

The hosted demo sign-in carries one **plate
band** instead: a detail crop of the same Ruysch file above the landing line,
full column width, `clamp(120px,14vw,140px)` (120 px at 390, 140 px at 1440 and
84 px below 360 px, where the fold needs the room), `object-position: center
50%`, decorative (`alt=""`), no frame, no caption, never behind text, controls,
tables or terminal output. HQ's page that answers `demo.odile.fyi/verify` when
the name falls back to HQ (`web/verify-public.html`, the demo role's copy of the
retired `web/verify.html`) keeps the verify band it carried: the same crop above
the first section, `clamp(120px,18vw,180px)` (a flat 120 px at ≤560),
`object-position: center 70%`, measured 180 × 712 px at 1440 and 120 × 354 px at
390. The sign-in is the one band carried **inline**, as a `data:image/webp` URI and
not a second file: the demo host serves that page from one file behind
`handle /login*` and has no static route for `web/bg-ruysch.webp`, so a sibling
image would need a Caddy route and a restart. It is the same 96,726-byte file,
not a re-cut, so the grade targets above hold byte for byte. A machine's
`/verify` is paper only — a tenant's and the demo tenant's alike: the page
program (`verify/verify.py`, `templates/verify.html`) carries no static route
for the painting, and the machine's own report page is a document before it is a
room. Measured on the live demo tenant, 2026-11-18: `demo.odile.fyi/verify`
200, 43,159 B, 0 `<img>`. No other product surface
carries the painting, and no painting is used behind product controls. On phones
a note keeps its rule and loses its fill, so the only filled object on the
screen is the primary action.

No stock photography, no AI-generated art, no second painting on a page, no
decorative gradients. The only gradient is the paper light that carries the
painting.

## The landing page

The page is one 520 px column: wordmark, description, field, button. No rail,
no card, no rule.

### Backdrop

Rachel Ruysch, full-bleed, with the paper veil from §Art direction. At 1440 the
whole plate shows; at 390 the vertical wash carries the type and the bouquet
emerges at the bottom. The `<body>` background is `--backdrop-base`, with the
image `center / cover no-repeat fixed`; at ≤820 the attachment becomes
`scroll` and the position `center 42%` (x 50 %, y 42 %).

### Wordmark

80.0 px Cormorant 500 in `--ink` at 390; 102.24 px at 1440. Top of the column,
22 px above the description. Line height .88, tracking −.03em.

### Description

18 px text serif 400 in `--soft`, 1.6 line height, 36ch measure (about
325–415 px depending on the platform face), left-aligned. It is a subset of the
approved sentences in `docs/CLAIMS.md`; no new trust sentence may be introduced
here.

### Email field and button

- No box: a full-width line of ink on a 1 px underline in `--edge`; placeholder
  italic in `--placeholder` `#5D5244` (5.75:1 on paper, 5.52:1 on the 1440 veil,
  5.06:1 on the darkest rendered field at 390); field min-height 44 px; text
  16.8 px/1.4. The shipped page pads `8px 2px 11px`; the component standard is
  9 px top padding and the 44 px min-height holds either way. New work uses 9 px.
- The button is a solid `--ink` block, 340 px wide at ≥821 and full width at
  ≤820, 44 px tall, paper-coloured caps at .28em, padding 15 px 24 px 14 px
  26 px. It is the only filled object on the page.
- On an error the underline turns `--error` and the message sits directly
  beneath the field, in the same red, at `.94rem` italic. The field gets
  `aria-invalid="true"` and `aria-describedby`; the message has `role="alert"`.
- The label is visually hidden but real: `<label class="vh" for="email">Email
  address</label>`. The placeholder is not the label.
- A honeypot field sits outside the tab order (`tabindex="-1"`,
  `aria-hidden="true"`).

### Confirmation

The form is removed, not hidden: no field, no button. Three quiet lines take
its place — "You're on the list." (19.52 px italic), "Stay tuned for your
invite." (14.4 px italic `--soft`), then the address in roman 14.08 px. There is
no remove link: an address stays on the list until an unsubscribe flow exists.
A signed-up visitor is never asked to sign up again and is never handed a
destructive action from the confirmation.

### Document

`<title>` is `Odile` on the landing and `You're on the list — Odile` on the
confirmation. `robots: index` on the landing, `noindex` on the confirmation.
The meta description and `og:description` carry the same approved sentence as
the visible lede. No script, no cookie, no third-party request.

## Voice

Declarative, unhurried, no exclamation marks, no numbers that were not
measured. Three short sentences, then an address. The agent says *I*; the
company says *Odile* or *we* only where the company is the actor. The abbess
test: would an abbess say this to the household? If it is chummy, salesy,
apologetic or excited, no.

### Five before/after pairs from shipped surfaces

These are transcriptions, not inventions, quoted as rendered text (HTML tags
omitted). The Before column quotes retired copy at the commit that shipped it
(or its parent, where the named commit is the fix); the After column quotes the
file that renders the fixed sentence at HEAD. Each pair fixed a real defect: a
claim that was not in the deck, a timeline that could not be true, or a model
stated twice.

The provenance is the sealed boot. The v3.4 prototype
(`prototype/userflow.html`) carried the key paste, the host-key modal, the
"minute thirty" kicker and the passphrase sentence — all retired when
t_13cacab8 re-cut it to the sealed boot (`84a4fa9`, merged pre-flip at
`94ee0477`); the one-root cut of 2026-11-14 (`24ee323`) then left `prototype/`
out of the public tree. ODL-462 landed the access and first-run rows in
`docs/CLAIMS.md` (pre-flip `3355f2c`), so the sealed-boot sentences now render
on `web/`. Where a v3.4 screen left the tree, its pair names the public surface
that carries the fixed sentence and the Why records the move. In this lineage
those four After strings are carried by the one-root cut `24ee323`; pair 1's
sentence arrived later, at `d2938b3`. The old pair 4 — the verify
screen's "99.0% target · 30-day" status line — is dropped: the line left the
tree with the prototype, `/status` publishes a computed uptime from heartbeats
instead, and the target is the §1 reliability row in
`docs/product/CLAIMS-RECUT-STRICT.md`.

| # | Surface | Before (retired, at the named commit) | After (shipped at HEAD) | Why |
|---|---|---|---|---|
| 1 | Landing lede, `<meta name=description>`, `og:description` (`web/index.html`) | "Your always-on agent with full privacy. Runs on confidential models and its own confidential computer." (`69c4444^`) | "Full agent with full privacy. Always-on agents running on confidential computers and powered by confidential models." | "full privacy" and "always-on" were in no `docs/CLAIMS.md` row, so `69c4444` routed the lede through the machine and access rows; **Savio reinstated his own sentence on 2026-11-14** (deck v3.9d), and it is now a §1 row verbatim where he wrote it. It renders at HEAD (commit `d2938b3`). A1's decided row landed in `docs/CLAIMS.md` with ODL-462 (pre-flip `3355f2c`); its custody sentence renders on the claim and ceremony pages, not in the lede. |
| 2 | Claim page, browser rule (`web/claim.html`) | Label: "First connect, and every boot after" — "SSH asks The authenticity of host … can't be established. Compare the fingerprint to the PASS line and type yes only then." (`714d0e0^`) | "You need a browser. Not a terminal." | Two host-key models in one screen; `714d0e0` split the first connect from a later boot. The sealed boot retires the terminal path entirely; the host-key modal was retired by t_13cacab8 (`84a4fa9`, merged `94ee0477`), and the prototype itself left the tree on 2026-11-14 (`24ee323`). The claim page carries the §3 audience row at HEAD. |
| 3 | FAQ, reboot answer (`web/faq.html`) | "It worked while you slept." (`714d0e0^`) | "A repeating job — the morning brief, a watchdog — runs when the machine comes back up if it was due while it was down." | If the machine waited for its owner at 03:14 and the brief ran at 07:03, it did not work while anyone slept. The v3.4 fix, "It ran when you unlocked." (retired), was still false under the seal: there is no unlock step. The shipped FAQ states the scheduler's actual guarantee (A11) at HEAD; the brief screen left the tree with the prototype. |
| 4 | Ceremony page, "The machine" notes (`web/ceremony.html`) | "If the key file leaks, the machine can be opened: give it its own passphrase and keep a second offline copy." (`714d0e0^`) | "There is no passphrase to type." | "Can be opened" is true only while the machine is running; `714d0e0` said so and kept the passphrase, and its second-copy clause was trimmed at `932a008` for having no `docs/CLAIMS.md` row. The sealed boot retires the passphrase (A4: the disk key is sealed to the measured boot), and the ceremony page carries the public half at HEAD; the claim screen left the tree with the prototype. |
| 5 | Claim page, lede (`web/claim.html`) | "The product, minute thirty" (`714d0e0`) | "Claiming is a form and a confirmation. Then the machine builds itself." | The arithmetic was never measured: 3 minutes of claim, ~5 of build and ~20 of setup came to about minute 28, so the v3.4 fix ("first ten minutes" → "minute thirty", both retired) still carried a clock. The sealed boot cut the clock and re-cut the kicker to `The product, first run` (`84a4fa9`, merged `94ee0477`); the claim page now says what the step is (the v3.9 row in `docs/CLAIMS.md`), and the screen left with the prototype (`24ee323`). |

A sixth pair belongs to the video and is recorded there: the end card's
"Repo: the link in the post" became "Repo: `<REPO_URL>`" so the address can be
typed (ODL-325, §Video).

### Formats

- **Sentences:** one idea per sentence; three short sentences before an
  address; no semicolon chains longer than one clause; no exclamation mark; no
  emoji; no "successfully", "revolutionary", "seamless", "effortless",
  "delight", "empower" or "supercharge".
- **The roadmap's status sentence is a composed status line.** It joins
  computed facts — the open gate(s), the next close, the next gate, the
  in-flight and blocked counts — with connectives, and is exempt from the
  one-idea-per-sentence rule for the same reason a table row is: it is a row
  of facts, not prose. The line is computed, never edited, and the page is
  unlisted.
- **Numbers:** only measured numbers. A target is labelled "target"; a range is
  `3–5`; a percentage keeps one decimal when it is measured (`99.0%`); money is
  `$49/month`, `$12`; a count is a numeral (`10 machines`, `3 minutes`).
- **Dates and times:** `6 November 2026` (no ordinal, no comma); owner-facing
  times carry the zone (`07:02 Asia/Hong_Kong`); operator times are UTC
  (`09:41 UTC`). Never "today" in a document that outlives the day.
- **Address:** the reader is *you*; the agent is *I*; the company is *Odile* or
  *we* only when the company acts. Never "the user" in public copy.
- **Punctuation:** prose ends with a full stop; labels and buttons do not. No
  Oxford comma by default; use one where it removes ambiguity.
- **Capitalisation:** sentence case for headings; title case only for the
  wordmark and proper names; caps are a type treatment, never a writing style.

### Lexicon

**Use:** kept, house, owner, key, page, room, ring, sight, quiet, yours, the
machine.

**Never:** platform, instance, ecosystem, seamless, effortless, magic, delight,
journey, empower, supercharge, fortress, vault, impenetrable, unhackable,
bank-grade, military-grade, zero-knowledge, trustless, blessed, sacred.

Reserved words, one meaning each:

| Word | Means | Never |
|---|---|---|
| key | the boot key in the measured command line, a public key with no private half; the disk key, sealed to the measured boot | the sign-in, a token, an API key, a session |
| NEAR AI key | the machine's one inference credential, one per user, written at install and read by the agent; named in full on first use, *the key* after that in the same passage | the boot key, the sign-in, a token, a session |
| token | a machine-to-machine credential Odile issues (the setup token) | anything the owner keeps |
| password | the single Webapp gate, hashed on the machine | a disk passphrase |
| passphrase / unlock | retired: there is no passphrase to type and no unlock step; the disk key is sealed to the measured boot | the sign-in, a password reset |
| recovery code | the code that resets a lost sign-in on the machine | the backup password, a key |
| recovery file | the file that holds the backup password and the certificate fingerprint; a copy is downloaded on your first visit | the recovery code, a key |
| brief | the job the agent delivers | a chat message |

The forbidden list in `docs/CLAIMS.md` §2 governs every public sentence; this
lexicon sits on top of it and never overrides it.

## Email

Status: the three account email templates are built — `templates/emails/invite.txt`
(Savio sends it personally in alpha.0), `templates/emails/confirm.txt` (queued by
the A2 account path, ODL-341) and `templates/emails/ready.txt` (filled by
`odilectl/claim.py` and queued as one message per tenant). The brief is built on
the same bar: `templates/brief.txt` is its message frame, filled and sent on the
tenant by `scripts/send_brief.py` (wiring: ODL-469). `tools/brand_lint.py` scans
every `*.txt` template under `templates/emails/` and `templates/brief.txt`
(`support.md` is the reply template, outside the account set and outside the
lint).

**alpha.0, the current release.** There is no price: Odile pays for the machine
and the inference, and the price and the paid cohort are alpha.1's (A8). There
is no support desk with a person on it: the invitation's fallback is a reply to
Savio's message, the ready email's is the reset page and the guide, and no
template promises a desk, a reader or a response time. (`ROADMAP-ALPHA0` §1.)
The deck's "read by a person" row is a public-page row; no alpha email relies
on it.

### Rules for every email

- **Plain text first.** HTML only when a link needs it. No image by default; no
  tracking pixel; no third-party font, stylesheet or request. The message is
  sent through `mail.odile.fyi` and carries the message, never the disk.
- **One column, 560 px max; body 17 px / 1.55; measure 66ch; left-aligned.**
  Paper `#EADFC4`, ink `#1C1813`, soft `#4A4136`. In HTML use the system serif
  fallback (`Georgia, serif`) — a web font in email is a request and a flash;
  the display face is for the page, not the inbox.
- **One action.** A link is a link: ink, underlined, 1 px; never a filled
  button drawn as an image. Each template has one action: the invitation's
  register link, the confirmation's confirm link, the ready email's tokenless
  first-visit link, where the owner signs in, and the brief's machine link
  (`Open the machine → <url>`). A printed value
  is a fact, not a second action: the ready email prints the machine URL, the
  report URL and the measurement once each, on its own line, as `Label value`.
  The reset page and the first-visit guide are named addresses in sentences,
  not second actions. An email address never appears in the body; a token
  travels only inside its action URL, never as a bare value.
- **Subject line:** sentence case; the subject is a plain name for the
  message — a phrase or one sentence — never a headline, a colon-and-preamble,
  an emoji, an exclamation or "Newsletter #3"; it need not be the body's first
  sentence.
- **Preheader:** the first sentence of the body; no "View in browser", no
  "Having trouble reading this?"; personalisation is the address and the machine
  slug only.
- **Footer:** `Private alpha · Odile pays for the machine and the inference ·
  no price` on all three account emails. No footer carries a price, a cohort
  size or a clock. The brief carries its date, the time of the run in the
  owner's zone, the machine's slug and one line naming `mail.odile.fyi`
  (§Brief email). When an unsubscribe flow exists, its link lives in the footer,
  never on the confirmation page.
- **No saint, no feast, no painting, no ring.** Email is a page, not a room.

### The three templates

| Template | From | Subject | The one action | Footer |
|---|---|---|---|---|
| `invite.txt` | Savio, personally, in alpha.0; `Odile <invite@mail.odile.fyi>` from A7, with an eight-character invitation code | `Your Odile machine` | Register at the link, and bring one real task | the alpha footer |
| `confirm.txt` | `Odile <invite@mail.odile.fyi>`, Reply-To `help@odile.fyi` | `Confirm your email address` | Open the link to confirm | the alpha footer |
| `ready.txt` | `Odile <invite@mail.odile.fyi>`, Reply-To `help@odile.fyi` | `Your machine is waiting.` (the `READY_SUBJECT` constant) | Check the report, then sign in with the password you chose when you registered | the alpha footer |

The invitation is a person's message in alpha.0: the body below is its text, the
sign-off stays `— Odile`, and the `From` becomes the transactional sender when
the invitation is gated by codes (A7). The bench registers openly by design
(`ROADMAP-ALPHA0` G12), so alpha.0's invitation carries no code and no claim
token; the register link is the action. There is one password, and the
invitation and the ready email name it: the machine's sign-in is the password
chosen at registration (D-1, D-15), and the ready email's `Sign in` line is
D-18's step. The register page
(ODL-341) carries D-15 before the password is chosen, and the account page
(ODL-583) carries D-17 in one line under the form. The ready email's check line
is D-7's reduction (`Check the report before you sign in; it says PASS or FAIL.`)
and its reset line is D-4 and D-19's. The which-password pair is gone, so no
template carries a second credential or a second reduction of one. An email may
quote published rows only, so every privacy, security or reliability sentence in
the invitation and the ready email is a deck row or a subset of one.

### Invitation (`invite.txt`)

```text
Hello,

You asked for a computer that keeps working when you close the tab. A machine is
reserved for you in the private alpha.

Register here: https://test.odile.fyi/register

Use the address this email reached. Your Odile password opens your machine and
your account page. You chose it when you registered. No one at Odile holds a
key to your machine.

Bring one real thing you would do by hand this week — a page of notes to turn
into a brief, a page to watch, a meeting to write up. After five short questions,
that is the first thing you will ask it for.

Nothing to install. No key to make, no terminal required. Nothing to pay: Odile
pays for the machine and the inference during the alpha.

When your machine is ready, we email you its address. If anything goes wrong,
reply to this email — there is no support desk in the alpha.

— Odile

Private alpha · Odile pays for the machine and the inference · no price
```

The register link is the bench's account host, `https://test.odile.fyi/register`,
where every alpha.0 tester is. It is written out because the invitation is a
person's message and must be sendable as it stands. The A7 re-cut makes it a
transactional template with an eight-character code; the A9 re-cut names
`https://odile.fyi/register` in production.

### Confirmation (`confirm.txt`)

```text
Hello,

Open this link to confirm your address:

{{CONFIRM}}

After you confirm, the machine is built and starts itself. If you did not
register, ignore this message.

— Odile

Private alpha · Odile pays for the machine and the inference · no price
```

`{{CONFIRM}}` names the configured account host, not the request's `Host`
header: `https://test.odile.fyi/confirm?t=<64 hex>` on the bench and
`https://odile.fyi/confirm?t=<64 hex>` in production
(`docs/product/DECISION-password-reset.md` §3.1). The link is single-use; the
GET renders a button and the POST spends the token, so a mail scanner's prefetch
does not confirm the address. The email states no expiry, because no clock is
measured.

### Ready (`ready.txt`)

```text
URL {{URL}}
Report {{URL}}/verify
Measurement {{MEASUREMENT}}

PASS means the machine booted the image whose fingerprint is above. The page
shows the measurement too, if you want to compare.

Sign in: {{URL}}/first-visit — the password you chose when you registered.

Check the report before you sign in; it says PASS or FAIL. That password opens
your machine too.

Later, if you forget your sign-in:

{{URL}}/first-visit/reset — your recovery code resets it.

The first-visit guide is at {{GUIDE}}

— Odile

Private alpha · Odile pays for the machine and the inference · no price
```

The ready email's body never carries a passphrase, a key or the sign-in. It
carries the machine URL, the report URL, the expected measurement, the tokenless
first-visit page where the owner signs in, the reset page and one navigation link
to the guide. The template is `templates/emails/ready.txt`; once ODL-925 lands
(`odilectl/claim.py` is in its touches) `odilectl/claim.py` will fill its three
values (`{{URL}}`, `{{MEASUREMENT}}`, `{{GUIDE}}`) and queue one message per
tenant. The report and reset URLs are composed from `{{URL}}`, so the filler
needs no new value. The guide carries the four steps — open, check
the report, sign in, start — and no command. The recovery file attached to the
message is the machine's own document and is re-cut by its own card (it still
carries the Model A terminal).

### Every privacy, security or reliability sentence, and its row

The map is the card's evidence (ODL-581): a sentence added without a row is a
defect. Every entry is a deck row (`D-*`: a §1 row or a recorded reduction) or a
subset of one; for the measurement comparison it is the §4.4 evidence sentence
that row's proof uses. None quotes a held clause from `docs/CLAIMS.md` §1a:
D-1's app-layer clause and D-3's sign-in clause stay off the two templates.

| Template | Sentence | Approved in `docs/CLAIMS.md` |
|---|---|---|
| invite | "You asked for a computer that keeps working when you close the tab." | §1 The machine: "It keeps working when you close the tab." |
| invite | "A machine is reserved for you in the private alpha." | §1 First run: "A machine is reserved for you. When you claim it, it is built and starts itself." (the reservation clause; the alpha's trigger is the confirmation) |
| invite | "Your Odile password opens your machine and your account page. You chose it when you registered." | §1 Access, D-1: "Your Odile password opens your machine and your account page. You chose it when you registered." (the first two sentences; the app-layer clause is held, §1a) |
| invite | "No one at Odile holds a key to your machine." | §1 Access, D-3: "No one at Odile holds a key to your machine: no standing key, no root password, no disk passphrase." (the first sentence; the sign-in clause is held until ODL-925, §1a) |
| invite | "Nothing to install." | §1 First run, D-18: "Claiming is a form and a confirmation. Then the machine builds itself: you check the report, sign in with the password you chose when you registered, and start." (a subset: there is no install step) |
| invite | "No key to make, no terminal required." | §1 Access, A2: "The boot key in the command line is a public key with no private half. It is plumbing, not a credential." (a subset: nothing for the user to generate) |
| confirm | "After you confirm, the machine is built and starts itself." | §1 First run: "A machine is reserved for you. When you claim it, it is built and starts itself." (the clause, with the alpha's trigger) |
| ready | "PASS means the machine booted the image whose fingerprint is above." | §1 Confidential computing: "The boot is measured: firmware, kernel, initramfs and command line are hashed into a value the CPU signs."; D-7, the PASS/FAIL row |
| ready | "The page shows the measurement too, if you want to compare." | §4.4: "Compare the measurement to the one in your ready email."; D-7 |
| ready | "Sign in: {{URL}}/first-visit — the password you chose when you registered." | §1 First run, D-18: "…you check the report, sign in with the password you chose when you registered, and start." (the sign-in clause) |
| ready | "Check the report before you sign in; it says PASS or FAIL." | §1 Confidential computing, D-7: "Check the machine before you trust it: the verify page shows PASS or FAIL." (the reduction) |
| ready | "That password opens your machine too." | §1 First run, D-10: "…that password opens your account page and, until you change it, your machine too." (the machine clause) |
| ready | "Later, if you forget your sign-in: … — your recovery code resets it." | §1 First run, D-4: "One recovery code resets both: your account password at the account page and your machine's sign-in on the machine. There is no emailed reset."; §1 Access, D-6: "…the code resets your sign-in…"; D-19: "A lost Odile sign-in is reset on the machine with your recovery code; HQ cannot reset your machine's sign-in." |

The money, support and readiness sentences are not privacy, security or
reliability claims: "Nothing to pay: Odile pays for the machine and the
inference during the alpha" and "there is no support desk in the alpha" are
`ROADMAP-ALPHA0` §1's own two facts, and "When your machine is ready, we email
you its address" is the invitation's flow step — the notice the ready email
fulfils — so it needs no row. No template says "the only sign-in is yours"
(held, §1a), "the disk key is sealed to the measured boot" (held, §1a), or
"Claiming takes 3 minutes" (dropped, never clocked).

### Brief email

`templates/brief.txt` is the frame; `scripts/send_brief.py` fills it and sends
it from the tenant, one message per day.

- **From:** `Odile <brief-<slug>@mail.odile.fyi>` — the machine's own address.
- **Subject:** `<Weekday>, <D Month> — your brief` (e.g. `Friday, 20 November — your brief`).
- **Body:** plain text, past tense, number first: what ran, when, how much.
  Three items at most, each with the evidence line the owner can open. No
  tracking pixel; no HTML wrapper unless a link requires one. The one action is
  `Open the machine → <url>`; the sign is `— Odile`.
- **The first line is the run's own clock, never a fixed greeting.** `Good
  morning.` before 12:00, `Good afternoon.` from 12:00, `Good evening.` from
  18:00, in the owner's zone — so a catch-up that lands at 15:02 does not open
  with "Good morning." and then name 15:02 three lines later. The sender fills
  the greeting; the template never carries a fixed one.
- **Footer:** the date, the time of the run in the owner's zone, the machine's
  slug, then one line naming the sender: `Brief emails go out through our own
  mail server at mail.odile.fyi; it carries the message, never your disk.`
  (§4.10).
- **Lateness is Hermes's verdict, not the machine's state.** A brief whose
  dispatch record says `late` or `catch_up` carries one sentence naming the
  local time and the delay — `This one ran at 07:14 — 14 minutes after its
  07:00 slot.`; past 90 minutes the delay reads in hours and minutes (`3 hours
  2 minutes`). An on-time brief carries no such sentence. No brief says "while
  you slept", and the retired `It ran when the machine came back up.` does not
  return: the sealed machine has no unlock step, and the dispatch record is the
  only source of lateness.

## Video

The video is a paper surface with a 1440×900 canvas; H.264 High, 25 fps, no
audio stream. X shows it at 390×244 (27.1 %). The cards are the branded surface; the screen recording is evidence
and may be white, but no blank load frame ships. The video is posted, not
embedded: no `<video>` element, iframe or player on a public page; the file is
uploaded to the post.

### Card values

| Element | 1440×900 | 390×244 (27.1 %) |
|---|---|---|
| Canvas | 1440×900 | 390×244 |
| Page | `#EADFC4` | `#EADFC4` |
| Wordmark | `Odile`, 102 px, 500, −.03em, .88, `--ink` | 27.6 px; 21 px ink height |
| Sentence / claim | 64 px, 500, 1.15, `--soft`, ≤1100 px | 17.3 px; 14 px ink height |
| Address | 52 px mono, 1.5, `--ink`, one line each | 14.1 px |
| Inset | 96 px; nothing inside it | 26 px |
| Caption plate | `--paper` block, padding 24 px 32 px, max-width 1140 px, 64 px `--ink`, 1–2 lines | 309 px wide; 17.3 px type; 26 px inset |

“Ink height” is the measured height of the rendered glyphs in the exported
PNG, not the CSS font size. The end-card blocks are left-aligned on the inset:
wordmark at the top, claim vertically centred, addresses at the foot with
24 px between the two lines. Until the public URL exists, the master carries
the literal `Repo: <REPO_URL>` and is replaced with one command; never
`the link in the post`.

### Cards

- **Title card (0:00–0:03):** paper; wordmark top-left; one sentence at 64 px
  in `--soft`; one address at 52 px mono, bottom-left at y=746 —
  `Odile: https://odile.fyi/judge`, the same measure and foot as the end
  card's Demo line, so the first frame answers where to go; no plate, no other
  line. The opening sentence is "A real Linux machine, not a chat window." —
  verbatim from `docs/CLAIMS.md`.
- **Caption card:** paper; one sentence at the same 64 px in `--soft`; no
  wordmark, no plate. It may separate two shots with no clear empty area. The
  opening uses "Odile's demo machine. One public password." so no plate covers
  the sign-in card.
- **End card (8 s hold):** paper; wordmark; the claim "Inference runs on NEAR
  AI's confidential cloud, inside a hardware enclave." at 64 px in `--soft`,
  broken after `confidential` so `enclave.` never sits alone; two addresses at
  52 px mono — `Demo: https://odile.fyi/judge` (the page that carries the
  password, the boundary and the route to `/verify`) and `Repo: <REPO_URL>`; no
  logo lockup, no tagline, no ring.
- **End-card address pair:** before the fix the card read `Repo: the link in
  the post`; the final cut prints the real `<REPO_URL>` so it can be typed.
  That is a public-behaviour change, not a new colour or face (ODL-325).
- **Caption plate positions:** bottom-left at y=560 (390: y=152) for the
  workspace, preview, scheduled-jobs and tab-close shots; top-left at y=48
  (390: y=13) for the prompt, answer, terminal and verifier shots. It must not
  cover the proof: the file line at y=73–130, the `Last` line at y=167 and the
  PASS line at y=732 stay visible.
- **Motion:** hard cuts only; no dissolve, fade, zoom, animated text or loop.
  Cards hold at least 3 s; the opening title is 3 s, the mid-cut title and the
  end card 8 s. The cut is silent — no music, no voiceover; the captions are
  the voice.
- **The 390 test:** downscale a finished frame to 390×244; every prose line is
  at least 17 px (14 px ink) and every address at least 14 px; no line crosses
  the 26 px inset. Read the frame on a phone before posting.
- **Palette:** source `#EADFC4` / `#1C1813` (13.33:1). The shipped cut was
  encoded from the old paper `#F6F0E4`; the next cut is encoded from this paper
  and its decode measured again. Judge the source, not an eyedropper on the
  encode. No pure white, no black, no gold, no blue, no second hue.

## Product surfaces

| Surface | Art | Type | Palette |
|---|---|---|---|
| Marketing / waitlist | One full-bleed painting, paper veil, no other image | Display for the wordmark and headings; text serif for the description; caps for buttons | Core |
| Claim, verify, status | Paper field. The hosted demo sign-in and HQ's `demo.odile.fyi/verify` fallback page carry one contained plate band (a detail of the Ruysch) above the first section; every machine's `/verify` — tenant and demo tenant — is paper only | Text serif for prose; display for headings; mono for credentials and machine values | Core + product extension |
| Webapp | First run, workspace, brief, verify, reboot, support and status; paper field; one contained artwork allowed in an empty state or onboarding, never behind text or controls | UI sans for interface and chat prose; display for the wordmark and headings; mono for terminal and metadata | Core + product extension |
| Demo (`demo/local`) | Paper field; no painting; a reproduction of the surface, not the machine | Display + text serif + mono | Core + product extension |
| Hosted demo sign-in (`demo.odile.fyi/login`) | Paper field; one contained plate band — the same Ruysch, `center 50%`, 84 px at 320, 120 px at 390 and 140 px at 1440, inlined as a data URI between the landing line and the boundary note; the 32 px wordmark, the h1 role for `The demo machine`, the landing line, the boundary note with its 2 px oxblood rule, one filled **Sign in** and one quiet `Check the boot report →` to the same host's `/verify`; no card | Display for the wordmark and h1; text serif for the landing line, the boundary note and the button; the one arrow glyph in mono | Core + product extension |
| First-visit preview (`odile.fyi/first-visit-preview`) | Paper field; no painting; the reviewed first-visit form in a read-only state, with one disabled primary and its reason within 8 px | Display for the wordmark and h1; text serif for prose; mono for the sign-in name and the masked values | Core + product extension |
| Email | Plain text first; HTML only when a link needs it, no images by default | System serif fallback; mono for values | Core |
| Video | Paper cards and caption plates; no painting; the recording is evidence | Cormorant embedded; mono for addresses | Core |
| Prototype | May simulate the product and use JavaScript; same tokens and art rules | Display + text serif + mono | Core + product extension |

The screen's field inventory and validation copy belong to the product spec;
every field and error uses §Component states. Density is calm: a panel has one
job; a rule separates, it does not decorate.
The product surfaces never use the marketing veil, the painting as a backdrop,
the wordmark at display size, or the confirmation copy.

## Interaction

- **One primary action per screen.** A second filled button is a decision the
  user did not ask to make; it is secondary, tertiary or absent.
- **Destructive actions are never the filled button.** Revoke, delete and
  cancel are quiet, underlined or outlined, and say what they destroy.
- **Focus order follows reading order.** No positive `tabindex`; overlays trap
  Tab, close on Escape and restore the opener; a modal never opens on load.
- **Nothing is hover-only.** Every hover state has a focus equivalent; every
  drag has a keyboard path.
- **No toast for a page change.** The confirmation replaces the form; a saved
  panel changes in place and announces with `role="status"`.
- **No JavaScript on a public page.** `web/` is HTML and CSS only, with no
  cookie and no third-party request. The prototype may simulate; the product
  may not.
- **The error is in place.** The message appears where the mistake is, 8 px
  under the field, not in a summary at the top; the field keeps its value.
- **The empty state is an invitation.** One sentence, one action, no apology,
  no illustration unless it earns its place.

## Brand review

Every visual change gets a review against `docs/brand/REVIEW.md`. The Designer
seat owns this sheet and the review, and changes it directly — palette,
typeface, artwork, surface rules. A change to `BRAND.md` is the Designer's call,
with a row in the change log below and verifier review; Savio approves only when
the change alters a public sentence in `docs/CLAIMS.md`. A `Brand: pass` comment
from the Designer is required before the verifier merges a visual card.

The review names the surface, the `BRAND.md` blob it measured, the two
viewport sizes, the states exercised and the rule behind each finding. “Looks
nice” is not a finding. A pass with an open **Must** is not a pass. A conflict
between brand and accessibility is resolved in favour of accessibility and
recorded in the change log. `tools/brand_lint.py` also fails when one of the
required sections disappears or an unfinished marker is left in the sheet.

## Change log

Every edit to this sheet adds a row here. `By` is the seat that made the call;
`Review` is the verifier's card or comment. A row that alters a public sentence
names the `docs/CLAIMS.md` row that moved with it.

| Date | Change | By | Review |
|---|---|---|---|
| 2026-11-06 | Ownership: `BRAND.md` is the Designer's sheet — Designer decides, change-log row and verifier review; Savio only when a change alters a public sentence in `docs/CLAIMS.md` (D-003). | Savio (direction), chief-of-staff (edit) | verifier, ODL-302 |
| 2026-11-06 | **The name** added: the story layer, the six rules (house, key, boundary, kept, readable, the agent's voice), and the ring rule (never filled, doubled, gapped or animated). No palette, typeface or artwork change; no public sentence changed. | Designer | verifier, t_bcec799c |
| 2026-11-07 | Opening and **Contents** added: the sheet is declared a build contract, and every section is indexed with the question it answers. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Name and mark** completed: wordmark clear space (0.5 × type size, measured to other elements), minimum size (32 px; 16 px ring), ring geometry (32 viewBox, r 7.5, stroke 1.6) and canvas padding (7.7 / 3.9 px), the no-lockup rule and the video-card exception. Pronunciation added. No visual change. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Palette**: contrast-pair table added (29 measured pairs, including the worst veil and the terminal) and the composited-surface rule stated; no token value changed. `--placeholder` remains `#6B5F50` and `--placeholder-on-paper` `#776B5A`. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Accessibility tokens** added: role table, focus ring, 44 px target, measure bands, zoom/reflow, semantic markup and pseudo-element measurement. No new colour. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Typography** completed: 27 roles across marketing, product, email and video; exact serif and mono fallback stacks; minimum sizes and tracking rules. Corrected the button to 44 px and the wordmark floor to 80 px at 390 — both already shipped; no face or size token changed. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Spacing and layout** added: tokenised 4 px scale, the three frozen optical exceptions (6, 22, 34 px), container widths, the 560/700/820/900/980 breakpoints, measure and above-the-fold rules; shipped off-scale gaps (54/50/40) and inset (26) round to 48 and 24 in new work. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Component states** added: primary and secondary buttons, quiet action, input, textarea, checkbox, link, focus, disabled, error, empty, loading, success and notice, each with values. Error uses text plus underline, never a fill. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Iconography** added: no icon family; 1 px stroke on 16/20/24 px boxes; the permitted text glyphs; no icon-only control; favicon ring exception. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Motion** completed: 120/180/240 ms, `cubic-bezier(.2,0,0,1)`, 300 ms maximum, ≤8 px travel, the 1.2 s progress rule, and the reduced-motion recipe. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Art direction** completed: Ruysch file, licence, measured grade targets, the 1440 whole-plate and 390 source-window crop, the exact radial and linear veil stops, and product-artwork limits. No image or grade change. | Designer | verifier, ODL-328 |
| 2026-11-07 | **The landing page** renamed from "The four surfaces" and completed: element-by-element values, the hidden label, honeypot, error ARIA and confirmation structure. No visual change. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Voice** completed: six real before/after pairs with commit provenance, formats for numbers, dates, money and address, and the reserved-word lexicon. No new public claim. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Email** added: invite and ready email specifications, subject lines, body structure, the plain-text and 560 px HTML rules, the brief format and the no-tracking-pixel rule. Not yet built; `emails/` is the landing place. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Video** added: title, caption and end cards, the 1440 and 390 values, caption-plate positions, holds, hard cuts, the silent cut and the 390 test; the `Repo:` pair recorded. No public claim changed. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Product surfaces** and **Interaction** consolidated: the demo row added, density and the one-primary, destructive, focus, hover, toast and no-JS rules stated. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Brand review** updated: a pass names the measured `BRAND.md` blob and both viewports; the lint now fails on a missing required section or an unfinished marker. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Do / Don't** replaced the bare prohibition list with 15 pairs; the absolute prohibitions that have no pair are kept as a short closing list. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Spacing and layout**: the direction and language decision stated — V0.1 is English only, no RTL layout, the font subset follows the page's characters, and the 4 px scale and containers are direction-neutral. This is the written "not applicable" the gap report lists. No visual change. | Designer | verifier, ODL-328 |
| 2026-11-07 | Final read-through after a cold-reader build test: veil and card contrast pairs added (29 total), the 821–1439 crop rule, `center 42%` clarified as x 50 %/y 42 %, three spacing exceptions (6/22/34), empty/loading/notice type roles, email link and ready-field rules, video file/position/ink-height rules, dark-mode N/A and `::selection`/`::marker`/forced-colors. No colour, face or surface change. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Voice** pair 5 corrected: the quoted after is now the HEAD sentence ("Give it its own passphrase."); the second-copy clause trimmed in `prototype/REVIEW.md` R1-S6f is named in the Why column. No public sentence added; an unapproved clause is no longer quoted as shipped. | Designer | verifier, ODL-328 |
| 2026-11-07 | **Video**: the posted-not-embedded rule stated — no `<video>`, iframe or player on a public page; the file is uploaded to the post. This is the written "not applicable" the gap report lists. No visual change. | Designer | verifier, ODL-328 |
| 2026-11-07 | **The landing page**: the shipped field padding `8px 2px 11px` named against the 9 px component standard; new work uses 9 px and the 44 px min-height holds either way. No visual change. | Designer | verifier, ODL-328 |
| 2026-11-07 | **The judge path** shipped on the existing tokens: a sixth public surface at `/judge` (one filled primary, one quiet secondary, credentials in mono), two quiet links on the landing page and in its success state, the verify page re-ordered so the command comes before the report, and the README's first screen. No new colour, face or artwork. The access row now reads "a tenant machine"; the demo rows are `docs/CLAIMS.md` v3.4. | Designer | verifier, ODL-329 |
| 2026-11-07 | **Video cards**: the title card gains one 52 px mono address line at the end-card foot (`Odile: https://odile.fyi/judge`) and the end card's Demo address moves from `demo.odile.fyi` to the judge page, so both ends carry one address. The cut is rebuilt with the same command and re-verified (`failures: []`); new bytes and sha256 pinned in `DEMO-SCRIPT.md` §3b, `CUT-REPORT.md` and `HACKATHON-SUBMISSION.md` §2. | Designer | verifier, ODL-329 |
| 2026-11-10 | **Palette**: old paper `#EADFC4`, oxblood `#6E2A2A` as the second ink, placeholder `#5D5244` (the old `#776B5A` measured 3.93:1 on the new paper and was retired), recessed `--panel` `#DDD0AE` for credentials and the report, `--card` and `--paper-light` re-based on the new paper. Every pair re-measured on the composited surface; the four legacy values named for the un-migrated prototype and templates. **Art direction**: the same Ruysch, one contained plate band allowed on `/judge` and `/verify`. No public sentence changed. | Designer | verifier, t_a1235fb0 |
| 2026-11-10 | **Propagation after review**: the role table, component-state focus, landing field and line pairs re-stated from the shipped old-paper values; the plate clamps corrected to the shipped CSS (`clamp(140px,20vw,170px)` / flat 150 px at ≤560 on judge); `--panel` applied to the `/judge` credential panel, the use its row already declared; `docs/brand/REVIEW.md` and `docs/brand/st-odile.html` moved to `#EADFC4`, `#F2E9D4`, `#6E2A2A`. No token value changed; no public sentence changed. | Designer | verifier, t_a1235fb0 |
| 2026-11-08 | **Status surface** recorded on the existing tokens: three typography roles (status lede `1.14rem`/1.5 `--soft`, balanced at 66ch; status figure mono `clamp(2rem,4.5vw,3.1rem)` tabular, one per screen; table label mono caps `.72rem`/`.78rem`), and the container row corrected from 700 px/≤700 to the shipped 760 px frame and 18 px inset at ≤560 (verify.html measures 712 px content; judge keeps 700). No colour, face or artwork change; no public sentence changed. | Designer | verifier, t_cc01a297 |
| 2026-11-08 | **Product surfaces**: the hosted demo's sign-in page (`demo.odile.fyi/login`) is recorded as a product surface — paper, no painting, the 32 px wordmark above one card — and as the one exception to the no-JavaScript rule: it carries a password handler for Hermes's `/auth/password-login` because the endpoint accepts JSON only. It sets no cookie of its own and makes no third-party request; `web/` stays script-free. The demo's empty state uses the existing empty/loading role (17 px/1.55 Cormorant, `--ink`, 60ch) with the two commands in mono. No colour, face or artwork change; no public sentence changed. | Designer | verifier, ODL-390 |
| 2026-11-10 | **The demo door** takes the 2026-11-09 palette: old paper `#EADFC4`, card `#F2E9D4`, oxblood `#6E2A2A` — the page's own tokens, with no door-only values. The steer's measured ground: the old `#F6F0E4` sat ΔE 2.66 from Claude's ivory `#F0EEE6` (the same colour to the eye); old paper is ΔE 11.62 from it, and the old error's hue 44.5° shared the terracotta's 44.2° while the oxblood is 27.1°. The coral the steer named was in the rejected painting variant, not on the shipped page. The composer's `Ask Odile` is re-applied when the vendor shell overwrites it. No public sentence changed. | Designer | verifier, ODL-390 |
| 2026-11-10 | **Typography**: the hash-wrap rule split by length — a short machine value (a fingerprint, an IP, a version, a commit hash) never wraps mid-token; a value longer than its panel wraps inside its own field with `overflow-wrap:anywhere`, is never set below 12 px to fit, and never widens the page. The 96-hex SEV-SNP measurement measures 795 px at the 13.76 px mono against the 674 px `/verify` panel (316 px at 390), so it wraps; the attestation table has done so since ODL-322. No token, face or public sentence changed. | Designer | verifier, ODL-458 |
| 2026-11-10 | **Selection**: the terminal pair recorded — `::selection` inside a terminal is `--term-ink` on `--term` (15.04:1). The page pair (`--paper` on `--ink`) paints `--ink` on `--term` (1.05:1) there, so a copied fingerprint showed no highlight. No token, face or public sentence changed. | Designer | verifier, ODL-458 |
| 2026-11-10 | **Email**: the ready email is built — `templates/emails/ready.txt`, filled by `odilectl/claim.py` and queued once per tenant. It carries the machine URL, the expected measurement from the release pin and one link to the first-visit guide; the IPv4/host/release/setup-token block and the four-step sentence are gone, because the strict bar removes the terminal, the key and the passphrase from the user's path. The one-action rule now says the machine URL is a printed value, not a second action; the From row on both email tables is the runbook's transactional sender `invite@mail.odile.fyi`; the "not built, no `emails/` directory" status is corrected. No palette, face or artwork change; no public sentence in `docs/CLAIMS.md` changed. | Designer | verifier, ODL-459 |
| 2026-11-10 | **Email and the row cut**: the ready email's one action is the machine's first-visit URL, which carries the single-use setup token; the token travels only in that URL, and the guide link is the one navigation link (the earlier 2026-11-10 ready-email row is superseded on the link point). The never-carries rule is scoped to the body; the attached recovery file is its own surface (its re-cut is a follow-up card). The claim and ceremony pages are re-cut to the decided v3.9 rows (claim A8; ceremony A2/A3/A4 and "the verify page shows PASS or FAIL"). A10 is held from the public pages until ODL-217 proves the grant, so the trust-moment rule no longer promises the grant and ODL-474 returns it. No palette, face or artwork change; no public sentence in `docs/CLAIMS.md` changed. | Designer | verifier, ODL-459 |
| 2026-11-11 | **Product surfaces**: the `/verify` plate band is the demo's — `/judge` and the demo's `/verify` carry it; a tenant's `/verify` is paper only. The tenant page is served by the machine's first-visit process, which has no static route for the painting, and the machine's own report page is a document before it is a room; the 120 px band would also push the access report further below the 390 fold. Measured sizes unchanged where the band renders. `docs/brand/REVIEW.md`'s art test is scoped to match. No palette, face or public sentence changed. | Designer | verifier, ODL-473 |
| 2026-11-11 | **Claim and ceremony**: the 4 px scale applied — section gap 48, paragraph 16, form gap 16, notice 24 with a 16 px inset, foot 32/16, section 32/24 at ≤560; the error message is 8 px under its field (form gap 16 offset by −8); the closed-link and claimed pages' confirmation line is the sheet's italic 1.22 rem role at weight 400. The header carries the wordmark alone: the duplicate `← odile.fyi` link (same destination) is removed from both pages. **Component states** gains the primary button's width row: 340 px at ≥561 on the claim form, full width at ≤560 — the landing's block at the product page's inset breakpoint, because 100 % above 560 would be a 672 px bar and the 820 px breakpoint belongs to the marketing wash. (Round 2 corrects the row from ≥821/≤820, which the page never implemented.) `/verify` and the first-visit page keep their copy of the shell — 24 px wordmark, `1.34rem`/600 h2 — because both are embedded byte for byte in the signed installer (`VERIFY_PAGE_SHA256`, `FIRST_VISIT_PAGE_SHA256`), and a change there needs `embed_verify.py`/`embed_first_visit.py`, a re-sign and a release re-cut. That shell scale stays the S1b debt ODL-243 recorded, now named with its blocking step. No palette, face or public sentence changed. | Designer | verifier, t_d1216f0c |
| 2026-11-11 | **Voice**: the roadmap page's status sentence is recorded as a composed status line — computed facts joined by connectives, exempt from the one-idea-per-sentence rule like a table row. The line is computed, never edited, and the page is unlisted. No palette, face or public sentence changed. | Designer | verifier, ODL-494 |
| 2026-11-11 | **The embedded shell** (S1b): `/verify` and the first-visit page take the sheet's shell — the 32 px wordmark (was 24 px), the h2 role `clamp(1.9rem,3vw,2.6rem)`/500/−.02em (was `1.34rem`/600), and the claim page's 4 px pass (section 48/24, section 32/24 at ≤560, paragraph 16, foot 32/16, report card 24, table cells 12/16, `pre` 16, gaps 24). `.caption` and `.state` take the 9 px/.20em micro-label, not the `.72rem`/.12em mono table label. On the first-visit done and reset-done pages the one filled control is the recovery download (ink fill, `--paper` label, .28em); `Open your workspace` is quiet, and the one-time warning sits above the download. Both pages are embedded byte for byte, so the act is a release: installer re-signed (`4aee80c5…`, 554,867 bytes), `release/` re-cut as `2026.11.11-8`. S1b stays open on the /faq shell pages (claim, ceremony, /faq, /privacy, /terms) — 24 px wordmark, `1.34rem`/600 h2; the /judge wordmark item (24 px; its h2 was the judge's own `1.45rem`/500 role) is retired with the page at `cd35286` (ODL-901) and the `/abuse` item is retired with its page at `242fca8` (ODL-903); /roadmap took the sheet in ODL-494. No palette, face or public sentence changed. | Designer | verifier, t_12e0c690 |
| 2026-11-12 | **The sealed boot reaches the sheet.** **The name**'s first rule moves its custody line from "the owner holds the key" to "the owner holds the only sign-in", and the "One key" bullet becomes **"The seal is the key"**: *key* is the boot key in the measured command line — a public key with no private half (A2) — and the seal it belongs to (A5); the owner's credential is the sign-in (A1); the way back is the recovery code and the recovery file (A9). The lexicon carries the same model — the `key` row re-cut, `passphrase / unlock` retired, `recovery code` and `recovery file` added; `password` stays the single Webapp gate. The invite email's three body rows quote the decided §1 and §3 rows, and its "what happens next" strip drops the 20-minute ceremony; the brief's headline rule is `It ran when the machine came back up.`; the Email status line and the invite's provenance no longer say the copy is the prototype's mock. The trust-moment rule (§Spacing and layout) and its Do/Don't pair read "no one at Odile holds the disk key, the seal is the key, there is no reboot wait". **Also re-cut in the same change:** `docs/brand/ST-ODILE.md` §1, §3, §4, §5, §11, §12 and §13 (§5's "the first boot is the owner's" becomes "the first run is the owner's"; the owner checks the report and sets the sign-in, not the installer; §12's landing row now names ODL-462 as the lede's re-cut), and the six voice pairs are labelled as pre-sealed-boot transcriptions — pair 1 re-quoted at HEAD, pair 5 quoted at its trim commit `932a008` (= HEAD), and pairs 2, 3, 5 and 6's Whys corrected to the sealed-boot facts; the re-quote at the post-landing HEAD is `t_96bdaf09`, gated on ODL-462 and t_13cacab8. One line in `docs/craft/designer.md` (principle 7) named the same stale trust facts and moved with them. Every replacement is a decided row in `docs/product/CLAIMS-RECUT-STRICT.md`: §2 A1/A2/A4/A5/A9/A11, §3, §1's machine row ("A machine is reserved for you…") and §4.1's "no user key, no operator key"; the deck is v3.8a, and **ODL-462** lands those rows in `docs/CLAIMS.md` and the public pages. The prototype and the specimen still rendered the v3.4 ceremony — the key paste, the host-key modal, the "minute thirty" kicker and the claim screen's "No key yet." — and t_13cacab8 re-cut them (merged `94ee0477`; the one-root cut of 2026-11-14, `24ee323`, then left `prototype/` out of the public tree — see the 2026-11-15 row). No palette, face, artwork or surface rule changed; no public sentence in `docs/CLAIMS.md` changed. | Designer | verifier, t_a6caad4c |
| 2026-11-13 | **Product surfaces**: the read-only first-visit preview (`odile.fyi/first-visit-preview`) is recorded — paper, no painting, one disabled primary with its reason within 8 px; the hosted demo sign-in gains one quiet navigation link to it. The demo workspace's titlebar carries one quiet `Verify this machine` link to `https://demo.odile.fyi/verify` — the demo's own page, same host, one hop (re-cut 2026-11-16, ODL-564): 44 px, ink on the vendor shell, left of the app controls; at ≤560 px the visible label is `Verify` and the anchor keeps the full aria-label, and the vendor tab's max-width yields the room so the title never passes under the link. The demo Webapp takes the page palette from the existing tokens — `--paper`, `--card`, `--panel`, `--ink`, `--soft`, `--line` and oxblood as the destructive ink; its primary is ink, its sans role is the embedded Cormorant subset, and its machine face stays the vendor mono stack. No new colour, face or public sentence. | Designer | verifier, ODL-500 |
| 2026-11-13 | **The status page leaves the legacy list.** ODL-467 publishes `templates/status.html` at `odile.fyi/status` with the page palette — `--paper` `#EADFC4` and `--card` `#F2E9D4`, measured `rgb(234,223,196)` at 390 and 1440 — and the report's row labels take the sheet's table-label role (`.78rem`, 600, `.12em`, uppercase). §Legacy surfaces now names two surfaces (the prototype and the demo template), and the two v0 paper tokens say "prototype and demo template only". The page's lede, meta description and Terms' Availability row carry the deck's v3.9a status and reliability sentences verbatim; those two `docs/CLAIMS.md` §1 rows moved with this card (change log v3.9a). No new colour, face or artwork. | Designer | verifier, ODL-467 |
| 2026-11-13 | **The demo sign-in is the front door.** `demo.odile.fyi/login` is re-cut from one card to a page: the 32 px wordmark, the h1 role for `The demo machine`, the landing line (`Your own computer, with an agent inside it.`), Savio's boundary sentence in the notice position with its 2 px oxblood rule, one filled **Sign in**, and one quiet `Check the boot report →` link to the same host's `/verify`. The card, the first-visit-preview link and the Hermes version credit are removed. The error line's slot under the button is reserved for one line — the longest shipped message at 390 and 1440 — so the action does not move when it appears. No painting: the judge page one click earlier carries the plate, and the door repeats none of it. No new colour, face or public sentence: the landing line is §1 *The machine*, Savio's sentence is § *The demo machine*. | Designer | verifier, t_330cea5c |
| 2026-11-13 | **The demo sign-in carries the plate.** Savio's order of 2026-11-13 (*"Add an artwork on the page."*) supersedes the earlier 2026-11-13 front-door row's "No painting: the judge page one click earlier carries the plate". `demo.odile.fyi/login` gains one contained plate band — the same Ruysch, `center 50%`, 84 px at 320, 120 px at 390 and 140 px at 1440 — between the landing line and the boundary note, in the judge page's reading order (wordmark, h1, line, plate, boundary note, one action). The band is **inlined** as a `data:image/webp` URI, the same 96,726-byte file, not a re-cut, because the demo host serves the page from one file and has no static route for the painting; the page measures 167,426 bytes. The page's colour balance moves — a dark warm block takes 120 px of the column's 611 px height at 390 — while the paper field, the tokens and every text pair are unchanged (ink 13.33:1, soft 7.55:1, oxblood 7.82:1 on paper, measured at 390 and 1440). No new colour, face or public sentence: the landing line is §1 *The machine*, Savio's sentence is § *The demo machine*. | Designer | verifier, t_9896a577 |
| 2026-11-13 | **The website icon.** Savio: "use the attached image as the Odile website icon." The ring is retired as the favicon; every page under `web/` carries `favicon.ico`, `icon-32.png` and an opaque-paper `apple-touch-icon.png`, served first-party with an immutable cache, with `icon-192.png` and `icon-512.png` in the set. The mark is Saint Ottilia's head from Lucas Cranach the Elder, *Saints Christina and Ottilia* (Katharinenaltar, Dresden), 1506 — public domain, Wikimedia Commons; `web/icon-source.txt` records the reproduction's URL, its sha256 and the crop box, and `web/icon-source-supplied.jpg` keeps Savio's 480×480 attachment byte-identical. **One rendition, every size.** Savio, 2026-11-13: "Why create the 16 px sign? Simply shrinking seems to keep branding more consistent and easier to maintain, rather than spinning up a weirdly different looking sign. I personally think simply shrinking looks fine." The 16 px sign and the four-ink cut are both retired: the crop is warm-graded once at 512 px and downsampled with LANCZOS to 16/32/48/180/192/512, so the ICO's three entries are the same master and the 16 px entry is the 512 px render at 16 (a test asserts it). The grade is measured on the master — saturation 0.34, hue 41°, R−B 12/29/56 across the luminance bands — and so is the 16 px read: face 172 luminance against 59 in the corners, skin R−B 64, no eye-band cell below 140, the eyes a shadow about 20 levels under the cheek. The four-ink cut had given up the skin hue the face needs at 16 px and read as a skull; the warm grade keeps it. §Name and mark, §Iconography and §Art direction name the one exception: a painted saint in browser chrome, never on a page. `/verify` and the first-visit page are re-embedded, the installer is re-signed (`ef559548…`) and the release re-cut (`2026.11.13-6`, manifest `881bc481…`; the icon-only rebuild moves no embedded page blob). The cut is `-6`, not `-5`: `main` re-signed the `-4` pair after the three live addresses left the tree, and ODL-514 took and deployed `2026.11.13-5`. **Also re-cut in the same change:** `docs/brand/ST-ODILE.md` (intro, §9, §10, §12, §13), `docs/brand/st-odile.html` (the story paragraph and the colophon icon row) and `docs/brand/REVIEW.md` (the art test's icon bullet) — the retired no-saint rule, the one-painting line, the ring-as-mark bullet and the two-rendition sentence now mirror this sheet. `docs/brand/GAP-2026-11-07.md` and `docs/brand/REVIEW-demo-entry.md` are dated records and keep the ring where they quote it. No public sentence in `docs/CLAIMS.md` changed. | Designer | verifier, ODL-504 |
| 2026-11-14 | **Voice, the lexicon**: *NEAR AI key* is recorded — the machine's one inference credential, one per user, written at install and read by the agent, named in full on first use. It is the term `docs/CLAIMS.md` v3.9c and `web/privacy.html` already carry as the meter's replacement (ODL-205, card t_7f339817), so the sheet names the shipped word instead of leaving it outside the reserved table. The `token` row drops the retired *metering token*. No palette, face, artwork or public sentence changed. | Designer | verifier, t_7f339817 |
| 2026-11-14 | **Typography re-cut into roles.** Cormorant Garamond is the **display face** — wordmark, h1/h2/h3 and the demo sign-in's h1 — and never sets a sentence at reading size. Public prose moves to the platform's **text serif** (Iowan Old Style / Palatino Linotype / Georgia / DejaVu Serif) at 18 px / 400 / 1.6; notes 16 px; the landing description stays 36ch, product prose 60–72ch. The Webapp's `--dt-font-sans` resolves to the bundle's own UI sans stack (OpenDyslexic first, then the platform sans) at the vendor's sizes, with chat prose 16 px / 1.6 and a 60–72ch measure; `p.wordmark` and the h1/h2/h3 heading selector are the only Cormorant left in the chrome. No new colour, no new image, no public sentence: this supersedes the type clause of ODL-503 (t_0127daed) and the type decision of t_dc4559ee. | Designer | verifier, t_99262fa2 |
| 2026-11-14 | **The website icon reaches the machine's own surfaces.** Savio, 2026-11-13: "Could the favicon also apply on the Hermes agent web interface page after logging in (e.g. https://demo.odile.fyi/ logged in)? Currently it shows the Hermes favicon." The five reviewed files now travel inside the signed installer; the machine writes them to its own icon root and its page server answers the five site paths — `/favicon.ico`, `/icon-32.png`, `/icon-192.png`, `/icon-512.png`, `/apple-touch-icon.png` — with the reviewed types and the immutable cache, HEAD with no body and a missing file a 404, so a tenant's `/verify` and first-visit page carry the tab first-party. The Webapp's tab is Odile's too: the installer copies `odile-favicon.ico` and `odile-apple-touch-icon.png` into the bundle root and points the shell's three tab links (`rel=icon`, `rel=apple-touch-icon`, `rel=shortcut icon`) at them, strip-then-write the way the brand script is written, so a rebuild is repaired on the next run and a second run changes nothing; the re-assert helper a rebuild triggers (`deploy/image/odile-webapp-brand.py`) carries the same three links, so an update puts the tab back with the name. The vendor's `apple-touch-icon.png` stays in the bundle as a vendor file; the About hero's "Powered by Hermes" is the remaining vendor credit, and the gateway's server-rendered `GET /login` is the one surface that still shows the vendor's, because v0.23.0 exposes no seam for it. The demo host's own sign-in page drops the retired ring's `data:` favicon for the same three links, and its Caddy block carries the five exact icon routes, so `/login`, `/verify` and the Webapp are Odile's tab on the machine a judge sees. §Name and mark's Files and Use bullets record both, `docs/runbooks/install.md`'s What is not themed is re-cut to that one screen, and `docs/brand/ST-ODILE.md` §12's browser-chrome row names the tenant path and the Webapp tab. No new colour, face or artwork; no public sentence in `docs/CLAIMS.md` changed. | Designer | verifier, ODL-505 |
| 2026-11-15 | **The `/judge` surface retires with its page; `/abuse` retires with its page.** ODL-901 (`cd35286`, merged `83eefd2`) deleted `web/judge.html` (`d00cb727…`) and `app.py` answers `/judge` with a 301 to `https://demo.odile.fyi/login`; ODL-903 (`242fca8`, merged `91aa597`) deleted `web/abuse.html` and answers `/abuse` with a 301 to `/terms`. The sheet no longer gives either one a live surface: §Product surfaces drops `judge` from the paper-field list and from the surface row (the row keeps the demo's `/verify` band and drops the judge's one filled primary and quiet secondary, which the hosted sign-in row already carries), §Art direction keeps only the demo's `/verify` (`clamp(120px,18vw,180px)`, 180 × 712 px at 1440 / 120 × 354 px at 390, `center 70%`) and the hosted sign-in (`clamp(120px,14vw,140px)`, 140 × 520 px at 1440 / 120 × 354 px at 390, `center 50%`, inlined) in the plate-band rule and its measured sizes, §Containers drops the judge page's 700 px column, and §Do/Don't's one-painting row names the two surviving bands. The 2026-11-11 S1b note keeps claim, ceremony, `/faq`, `/privacy` and `/terms` open and marks the `/judge` wordmark (ODL-901) and `/abuse` (ODL-903) items retired. **Also re-cut in the same change on the HQ operating record** (local-only, `.git/info/exclude`): `docs/brand/REVIEW.md`'s art test and `docs/brand/ST-ODILE.md` §9 now name the two bands. The canonical judge entry is `https://demo.odile.fyi/login`; no new brand copy prints `/judge`, and the 301 is kept for the posted cut's two printed cards (§Video's `Odile:` and `Demo:` address lines) and the pinned quote. No palette, typeface, artwork or public sentence changed (`docs/CLAIMS.md` v3.9f already names the 301). | Designer | verifier, t_2f1f9d46 |
| 2026-11-15 | **The demo sign-in page's tab is first-party from its own root.** `demo.odile.fyi/login` carries the three `rel=icon` links and the credit comment beside them (the credit line is byte-identical to the one on the eleven `web/` pages), and the three committed `web/` renditions — `favicon.ico`, `icon-32.png`, `apple-touch-icon.png` — sit beside the page in `/srv/odile-demo`; the demo block's one `@tabicons` static handle answers those three paths from that root with `image/x-icon` / `image/png` and the one-year immutable cache, so the demo's tab does not depend on the first-visit service. `/icon-192.png` and `/icon-512.png` stay the page server's for the Webapp shell. §Name and mark's Files bullet is scoped to match. The sign-in page's body, palette, type and copy are unchanged; no public sentence in `docs/CLAIMS.md` changed. | Designer | verifier, t_6b25f6e5 |
| 2026-11-15 | **Voice, the pairs re-quoted at HEAD.** The six pairs become five, each After quoted from the file that renders it at HEAD: pair 1 the landing lede (`web/index.html`, `d2938b3`, deck v3.9d); pair 2 the claim page's browser rule (`web/claim.html`, §3), replacing the retired host-key modal; pair 3 the FAQ's reboot answer (`web/faq.html`, A11), replacing the retired brief headline; pair 4 the ceremony page's passphrase rule (`web/ceremony.html`, A4), replacing the retired claim screen's key-leak sentence; pair 5 the claim lede (`web/claim.html`, v3.9), replacing the retired "minute thirty" kicker. The old pair 4 (the demo's "99.0% target · 30-day") is dropped: the line left the tree with the prototype, `/status` publishes a computed uptime from heartbeats instead, and the target is the §1 reliability row. The vintage note is replaced by the landed provenance — ODL-462 pre-flip `3355f2c` and t_13cacab8's re-cut (`84a4fa9`, merged pre-flip `94ee0477`), which retired the four v3.4 items — and by the one-root cut `24ee323` (2026-11-14), which carries each of the four After strings in pairs 2–5; pair 1's sentence arrived later, at `d2938b3`. **Also re-cut:** the overflow state names the 249-character email that scrolls inside its field and the 248-character confirmation address that wraps in its line, not "a long key"; the invite's provenance says the prototype's mock left the tree; `docs/brand/ST-ODILE.md` §12's landing and prototype rows name the landed sentences and the one-root cut (HQ-only, outside this commit). No palette, face, artwork or surface rule changed; no public sentence in `docs/CLAIMS.md` changed. | Designer | verifier, t_96bdaf09 |
| 2026-11-16 | **Product surfaces**: the demo workspace's `Verify this machine` link is the demo's own page on the demo's own host — `https://demo.odile.fyi/verify` — so a judge reaches the report in one hop and HQ's host never appears in the address bar. The 2026-11-13 product-surfaces row is re-cut to that target; the link's 44 px box, its ≤560 visible label `Verify` with the full aria-label, ink on the vendor shell and the tenant's relative `/verify` are unchanged. Both chrome copies (`deploy/demo-host/webapp-chrome.js`, `deploy/image/webapp-chrome.js`, byte-identical, sha256 `f73b90d2…`, 26,836 B) carry the constant once, and the comment above it names the demo's own page. On the demo host the installed script and the bundle's `odile-demo-chrome.js` are the same bytes, and `GET /odile-demo-chrome.js` with a judge session serves that constant. Measured in a browser at 1440×900 and 390×844: `href` `https://demo.odile.fyi/verify`, box 188 × 44 px at x 1184 and 55 × 44 px at x 267, ink `rgb(28,24,19)` on paper `rgb(234,223,196)` 13.33:1, the 2 px ink focus ring at 3 px offset, `scrollWidth == clientWidth` at both widths, and one hop to the page (200, 43,159 B, sha256 `3cdd7427…`, one `<!--demo-check-->` pair). `deploy/demo-host/deploy_demo_icons.py` uploads the chrome — the re-assert helper's source — and reads the served script, so the link the browser runs is measured, not assumed. No palette, face, artwork or surface rule changed; no public sentence in `docs/CLAIMS.md` changed (v3.10e already records the 302). | Designer | verifier, ODL-564 |
| 2026-11-18 | **Email, the alpha.0 account set.** §Email is re-cut to the three templates A2 sends: `invite.txt` (Savio's personal invitation on the bench; the transactional sender from A7), `confirm.txt` (the address confirmation, naming the configured account host), and `ready.txt` (now the machine URL, the report URL, the measurement, the first-visit link, the reset page and the guide). The v5 footer `Founding alpha · 10 machines · $49/month` and "Claiming takes 3 minutes" leave; the footer is `Private alpha · Odile pays for the machine and the inference · no price`, and the no-desk sentence says alpha.0 has no support desk with a person on it. The templates name the machine's sign-in only; the which-password pair is carried by the register page's PR/FAQ §3 sentence and the account page's one line, and the ready email's `Set your sign-in` is their reduction (`docs/product/DECISION-password-reset.md` §3.5). Each template's privacy, security and reliability sentences are mapped to their `docs/CLAIMS.md` rows in the section. `tools/brand_lint.py` scans `templates/emails/*.txt`, so the rule the section states is the gate it runs. No palette, face or artwork change; no public sentence in `docs/CLAIMS.md` changed. | Designer | verifier, ODL-581 |
| 2026-11-18 | **The `/verify` plate band names its surface.** The band the sheet called "the demo's `/verify`" is HQ's demo-role page — `web/verify-public.html`, which answers `demo.odile.fyi/verify` only when the name falls back to HQ — and it keeps that band unchanged (`clamp(120px,18vw,180px)`, 180 × 712 px at 1440 / 120 × 354 px at 390, `center 70%`). A machine's `/verify` is paper only on every machine, a tenant's and the demo tenant's alike: the page is `verify/verify.py` + `templates/verify.html` under `odile-verify.service` on loopback (t_4fcd465f), whose server carries no static route for the painting, and its rows are a document before they are a room. Measured on the live demo tenant 2026-11-18: `demo.odile.fyi/verify` 200, 43,159 B, 0 `<img>`; the demo block's `/verify` proxies 8098, the machine's page. The hosted demo sign-in keeps its inline band (140 × 520 px at 1440 / 120 × 354 px at 390) unchanged. §Product surfaces, the surface table and §Do/Don't's one-painting row are re-cut to match, and `docs/brand/REVIEW.md`'s art test names the same surfaces (HQ-only). No palette, face or public sentence changed. | Designer | verifier, t_4fcd465f |
| 2026-11-19 | **Two live scope sentences name alpha.0.** The dark-mode sentence in §Accessibility tokens and the direction-and-language sentence in §Spacing and layout now name alpha.0, the current release; the decisions are unchanged, and the dated 2026-11-07 row keeps the number it recorded. No palette, face or artwork change; no public sentence in `docs/CLAIMS.md` changed. | Designer | verifier, ODL-918 |
| 2026-11-19 | **Legacy surfaces: the prototype retires from the list.** The one-root cut of 2026-11-14 (`24ee323`) left `prototype/` out of the tree, so `git ls-tree HEAD prototype` is empty and no commit reachable from `main` carries `prototype/userflow.html`: the prototype is a record in this sheet, not a file a builder can open. §Legacy surfaces now names one surface — the demo template (`demo/local/template.html`) — and the prototype is named as gone rather than un-migrated; the three Use cells that said "prototype and demo template only" now say "demo template only", as `--error-v0` already did. The table keeps its four rows, so `tools/brand_lint.py` still reports 4 legacy-only and stays green; the lint's `prototype/*.html` glob and the comments that name it a carrier are unchanged, and the glob matches nothing at HEAD. A first cut of this row said "read it at that commit"; `24ee323` is a root commit and does not carry the file, so the pointer is dropped. This corrects the 2026-11-13 status row's "now names two surfaces" and agrees with `docs/brand/ST-ODILE.md` §12's prototype row, which already calls the surface a record. No palette, typeface or artwork re-value; no public sentence in `docs/CLAIMS.md` changed. | Designer | verifier, t_41439551 |
| 2026-11-19 | **Email, the §Email nits from ODL-581's brand pass.** The subject rule is reworded to the rule the three-row table actually keeps — a plain name for the message, a phrase or one sentence, never a headline, a colon-and-preamble, an emoji, an exclamation or "Newsletter #3", and it need not be the body's first sentence. The ready email's guide line moves from `Open the first-visit guide: {{GUIDE}}` to `The first-visit guide is at {{GUIDE}}`: a named address in a sentence, not a second imperative, leaving `Set your sign-in` as the ready email's only imperative URL. The invitation's "When your machine is ready, we email you its address" is named in the non-claim paragraph as a flow step — the notice the ready email fulfils — so every sentence in the three templates has a row or a named place. No palette, face or artwork change; no public sentence in `docs/CLAIMS.md` changed. | Designer | verifier, ODL-912 |
| 2026-11-20 | **The grant sentence returns.** ODL-217 proved the grant and ODL-474 landed the A10 row, so the trust-moment rule no longer holds the support grant back: §Spacing and layout and the Do/Don't pair now say the grant is the page's own row — a tenant's `/verify` (`verify/verify.py` + `templates/verify.html`) names a live grant by its id, expiry, reason and key fingerprint in its `support access` row, read from `odile-support status`, instead of showing a bare FAIL, and a machine with no grant renders `no grant`. The page's tenant block also carries the published scoped row `No one at Odile can log in unless you grant it.` (`docs/CLAIMS.md` §1 *Access*), so the sentence above the rows and the row that proves it agree. No palette, typeface or artwork change; no public sentence in `docs/CLAIMS.md` changed (the A10 row shipped with ODL-474). | Designer | verifier, t_e23f3288 |
| 2026-11-20 | **Component states: the skip link is a component, not a rule copied out of a commit.** §Accessibility tokens' Keyboard row asked for a skip link, but the sheet gave no values, so the carriers drifted: at this cut `web/roadmap.html` shipped `.20em` mono and revealed on `:focus`, its ring supplied only by the page's generic `a:focus-visible` rule, while t_eaa22faa's four page files at `1856358` — `web/first-visit.html`, `web/verify.html`/`web/verify-public.html` and `templates/verify.html` — ship `--text`, `:focus-visible` and the ring in the skip-link rule itself. The new row group pins the shipped rule (off screen at `left:-9999px; top:0` and first focusable; 24 px inset, 18 px at ≤560; `top:12px`; `z-index:10`; 44 px high at 12/16 padding; `--ink` fill; `--paper` label at 10 px caps / 600 / `.20em` / 1.2 in the text face; 2 px `--ink` ring at 3 px) and the three decisions the carriers were guessing: `:focus-visible` never `:focus`, no positive `tabindex`, never a second primary action. Measured on those four carriers at 1440, 390 and 320: 170.7 × 44 at (24, 12) and (18, 12), no horizontal overflow; the 390 bar covers the wordmark and `elementFromPoint` returns the link itself, recorded as accepted. The roadmap copy was still mono and `:focus`-revealed at that cut; t_2281fb61 has since re-cut it (merge `a08ec1f`) to `:focus-visible` with the ring in its own rule, keeping its page-local `ui-monospace` label, which the new row names as the one exception. No palette, face, artwork or public sentence changed. | Designer | verifier, t_36a2ae5a |
| 2026-11-22 | **Email, the brief is built and its late sentence is the dispatch verdict.** `templates/brief.txt` is the brief's message frame, filled and sent from the tenant by `scripts/send_brief.py` (the tenant wiring is ODL-469), so §Email's status names it as built and `tools/brand_lint.py` scans it with the account templates. §Brief email's footer names the fourth footer line — `Brief emails go out through our own mail server at mail.odile.fyi; it carries the message, never your disk.` (§4.10) — and the retired `It ran when the machine came back up.` is replaced by the one sentence a `late` or `catch_up` dispatch carries, naming the local time and the delay (`This one ran at 07:14 — 14 minutes after its 07:00 slot.`), with none on an on-time brief: the sealed machine has no unlock step, and the dispatch record is the only source of lateness. The first line is the run's own greeting — morning before 12:00, afternoon from 12:00, evening from 18:00 in the owner's zone — so a 15:02 catch-up cannot open with "Good morning."; §Email's one-action rule names the brief's machine link. The greeting rule is the sheet's; the surface follows it on this card's next head. No palette, face or artwork change; no public sentence in `docs/CLAIMS.md` changed (§4.10 is unchanged). | Designer | verifier, ODL-235 |
| 2026-11-22 | **Email, the copy map after B-once.** B-once (Savio, 20 November 2026) and the one recovery code (`docs/product/DECISION-one-recovery-code.md` §4) retire the pair the map quoted, so the sheet is re-pointed — and the invitation's template re-cut with it, while the ready template's cut is ODL-925's: the sign-in chosen at first visit, the operator having never seen it, the after-PASS gate, the claim page's set-the-sign-in step, the machine-stored hash with no account at HQ and the machine-only reset that said HQ never resets it. The invitation's sign-in paragraph becomes D-1 and D-3 (one password; no key held), its "Nothing to install." cell cites D-18, and `templates/emails/invite.txt` carries the same sentences (D-1's first two, D-3's first). The ready email loses the `{{FIRST_VISIT}}` action line and its single-use setup token: the action names the tokenless `{{URL}}/first-visit` and the password chosen at registration (D-15, D-18), the after-PASS line becomes D-7's reduction (`Check the report before you sign in; it says PASS or FAIL.`), "That password opens your machine too." is D-10's, and the reset line is D-4, D-6 and D-19's. §Email's one-action rule, the template table, the ready prose and the guide's four steps name the tokenless page and "sign in", not "set the sign-in"; the map's held-clause rule is unchanged. The §Ready block equals ODL-925's shipped `templates/emails/ready.txt` at `459daf4` byte for byte — the duplicated PASS/FAIL lead-in is dropped from the quote and its map cell with it, D-7 staying cited by the check line below `Sign in`. No palette, face or artwork change; no public sentence in `docs/CLAIMS.md` changed (the rows are v3.11's). | Designer | verifier, ODL-928 |
| 2026-11-23 | **Typography: the section title has two roles, one for each shell.** The sheet's single h2 row described only the report pages, and another rule shipped on the paper pages. Measured on the tree render at 1440×900 and 390×844: claim, ceremony, faq, privacy, terms, register, forgot and account compute h2 21.44 px / 600 / 1.2 / `letter-spacing: normal` (3, 3, 7, 8, 7, 0, 0 and 1 sections — register and forgot carry the rule and render none), while /verify, /verify-public and /roadmap compute 41.6 px / 500 / −0.832 px at 1440 and 30.4 px / 500 / −0.608 px at 390; `/status` and the verify page template carry the same display rule. At this cut eight files under `web/` carry the 1.34 rem/600 rule — ODL-578 added `forgot.html`'s bare `h2` and `account.html`'s scoped `.account h2`, and merged at `8eb1ceb` — and the `/signin` shell in `app.py` declares the same h2 for the form page it renders. No page overflows at either width (`scrollWidth == clientWidth`), and the document pages are identical at both. The three pages built after the 2026-11-11 S1b note — register and account (11-22) and forgot (11-23) — chose the small role again, so S1b's plan to migrate the /faq shell pages up is resolved the other way: the `1.34rem`/600 role is the sheet's document section title, the report pages keep the display role, and that is the only shell the byte-for-byte embedded verify page can carry without a release. The two rows and the *Two section-title roles* rule are the sheet; `tools/brand_lint.py` now fails on a page that pairs a 24 px wordmark with the display h2 (or a 32 px wordmark with the document h2), on an h2 between 1.2 rem and the document size in rem or in px, on a `.brand` rule whatever its parent selector, and on a page that mixes the two roles. No palette, typeface, artwork or public sentence changed. | Designer | verifier, t_7f1fb414 |
| 2026-11-23 | **Name and mark: the product header wordmark is 24 px, and the page minimum follows it.** `.brand` computes 24 px in Cormorant 500 / −.03em on claim, ceremony, faq, privacy, terms, register, forgot and account, and 32 px on /verify, /verify-public, /roadmap, /status, first-visit and first-visit-preview (2 rem; the verify page template likewise). `/status` was the one page that mixed the two — a 24 px wordmark over the display h2 — and it takes 2 rem in this change, so the pairing holds on every page the lint reads. Clear space at 24 px is 12 px, and the shipped pages hold it, measured from the wordmark's box to the h1's box: 12.0 px at 390 and 20.0 px at 1440 on claim, ceremony, register and forgot, 21.7 / 31.7 px on privacy, faq and terms, and 24.0 px at both widths on account. At 32 px the floor is 16 px, and two report pages were under it: `/roadmap` and `/status` cleared 12.0 / 20.0 px, so their ≤560 header margin rises from 24 to 32 px — the sheet's 4 px scale, not the 28 px a probe proposed first, which `tests/test_status.py`'s spacing check rejects — and both now clear 20.0 px at 390, 320 and 1440 (`scrollWidth == clientWidth`); `/verify` and `/verify-public` clear 26.3 / 34.3 px, and the first-visit pages 16.6 and 37.6 px. The tenant `/verify` page (`templates/verify.html`) still clears 12.0 px at ≤560 — the one recorded exception, 4 px under the floor, because closing it changes bytes embedded in the signed installer and ships with the release that re-embeds it (t_fc412bd9). The 32 px figure stays where the wordmark is the demo door's and the report shell's size; it stops being the page minimum, and the video card's 27.6 px rendition stops being an exception because 24 px is now the floor. No palette, typeface, artwork or public sentence changed. | Designer | verifier, t_7f1fb414 |
| 2026-11-23 | **Typography: the password fields stop being tracked.** `input[type=password]{letter-spacing:.02em}` shipped on web/register.html, and on web/forgot.html and web/account.html at ODL-578's head (2c266a6). At 16.8 px it computes 0.336 px at 390 and 1440 — under one device pixel, invisible as a mask aid, and a positive tracking on lowercase that this sheet forbids. The declaration leaves web/register.html at this cut and the fields compute `letter-spacing: normal`, matching `/signin`, which never tracked; web/forgot.html and web/account.html lost it in ODL-578 (t_296182c8), which merged at `8eb1ceb`, so all four password fields agree. The tracking rule gains its second sentence — the document h2 and every form field are untracked — and the lint now fails on any `letter-spacing` inside an `input`, `textarea` or `select` rule. No palette, face, artwork or public sentence changed. | Designer | verifier, t_7f1fb414 |

## Do / Don't

| Do | Don't |
|---|---|
| One filled button per screen; everything else quiet. | Two filled buttons, or a primary below the fold. |
| Paper `#EADFC4`, ink `#1C1813`, soft `#4A4136`, oxblood rules `#6E2A2A`. | Pure white `#FFF` or neutral black `#000`; both fight the paper. |
| One painting on a page: full-bleed on marketing, one contained detail band on the demo's verify and the demo sign-in; one painted head in the tab. | A second image on a page, a collage, a carousel, a frame, a vignette, a gold border. |
| The wordmark alone, live text in Cormorant 500. | A lockup, a monogram, a tagline beside it, or the wordmark set in a sans. |
| Put the trust moment above the fold on the tenant surfaces: no one at Odile holds the disk key, the seal is the key, there is no reboot wait, and a live grant is named on `/verify` by its id, expiry and key fingerprint. | Bury it under decoration or explain the flow with a disclaimer. |
| 44×44 px targets and a 2 px ink focus ring at 3 px offset. | 32 px links, `outline: none`, or focus signalled by colour alone. |
| Error text and the invalid underline in `--error`, with the message 8 px under the field. | A red fill, a red primary button, or an error carried only by colour. |
| Empty states with one sentence and one action (`No session yet.` in the terminal). | "No data", a shrug, or an empty panel with three competing actions. |
| Loading on paper with the wordmark and one status line; no blank white frame. | A spinner over a white screen, a skeleton shimmer, or a 4.4 s blank load. |
| The agent says *I*; the company says *Odile*; the owner is *you*. | "We" for the agent, emoji, exclamation marks or performed warmth. |
| Numbers that were measured; a target labelled target. | "Always-on", "99.9%", "full privacy", "unhackable". |
| Plain-text email first; one link; no image; no tracking pixel. | An HTML card with images, a web font, a second CTA or a pixel. |
| Captions and cards on paper; the voice is the caption. | Text over the UI, a dissolve, a zoom, a loop or a voiceover. |
| A link that is a link: ink, underlined, one action. | A filled button drawn as an image, or a link styled as a second primary. |
| The confirmation removes the form and offers no control. | A remove link, a still-active form, or a "sign up again" button. |

The absolute prohibitions that have no pair: no nav on a marketing page, no
eyebrow, no artwork caption, no accent colour; no email address in a link or a URL (the token does
that job); no saint on a page — the one painted head is the browser icon — no
crozier, Latin or feast-day promotion, and no blindness used as a metaphor; no
JavaScript, cookie or third-party request on a public page.
