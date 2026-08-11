# Design gap analysis — `ref/render-farm (7).html` vs. BlendFleet

Audit date: 2026-08-11. Reference: `ref/render-farm (7).html` (2,670 lines — theme
tokens, five pages, ~1,580 lines of behaviour). Subject: the PySide6 app under
`blendfleet/ui/`.

The reference is a **browser mockup with simulated data** — fake instances, a fake
API-key format, procedurally generated frame images, a simulated network. Not
everything in it maps onto BlendFleet, which drives real Kaggle notebook sessions.
Every gap below is therefore marked as a genuine gap or as **N/A for Kaggle**, with
the reason. Adopting the mockup wholesale would import features that cannot be
made true.

---

## 1. The headline gap: shell shape

| | Reference | BlendFleet today |
|---|---|---|
| Structure | Five pages behind a collapsible icon sidebar (Dashboard, Files, Instances, Logs, Settings), each nav item carrying a live count pill | **One** scrolling column plus a fixed 320px rail (`ui/dashboard.py:335`); no navigation at all |
| Header | Page title, connection-health button with live latency, theme toggle, notification bell with unread count | none |
| Secondary UI | In-page panels and card-styled modals | `QMessageBox` / `QFileDialog` / two `QDialog`s |
| Sidebar content | Navigation, brand, clock/version footer; collapses to a 68px pill of circular icons | Brand, one `InstanceCard` per account, "+ add account" |

Our rail holds *content* (the account cards). The reference's sidebar holds
*navigation*, and puts instance cards in a responsive grid on the dashboard
(`repeat(auto-fill, minmax(320px, 1fr))`).

That one decision is upstream of most of what follows: it is what gives the
reference somewhere to put a Files page, a Logs page and a Settings page, and it
is why ours reads as a control panel rather than an application.

---

## 2. Looks — token by token

| Token | Reference | Ours (`ui/theme.py`) | Gap |
|---|---|---|---|
| Themes | light **and** dark; light is the default | dark only (`BG_SHELL #16181D`) | no light mode |
| Neutral family | warm/greenish — `#F3F3F0`, `#101210`, `#191B19` | cool blue-charcoal — `#16181D`, `#1E212A` | different hue family |
| Accents | five muted: `#E8935A` blender, sky, mint, violet, rose | five saturated: `#F5792A`, green, purple, blue, red | ours louder |
| Accent usage | `--accent-soft` washes, `--accent-ink` text, tinted nav items, badges, chips | mostly borders plus one primary button | accent barely present in ours |
| Radii | 22 / 14 / 10px, plus 50% pills | 4 / 6 / 8px | the single largest visual difference |
| Elevation | `box-shadow`, plus an optional frosted-glass mode | deliberately none — lightness steps instead (`theme.py:305-318`) | flat vs. lifted |
| Ambient | two radial accent glows behind the page (`body::before`) | none | — |
| Type | Roboto **plus Roboto Condensed**, uppercase with `.14em`–`.22em` tracking on buttons, nav and headers; mono for inputs | Roboto only; mono reserved for numeric/machine values | see note below |
| Section headers | `h2.sec` — 11.5px caps, hairline rule filling the row, mono `meta` subtitle | `QLabel("<b>Filmstrip</b> — one cell per frame, tinted by…")`, a full paragraph inline (`dashboard.py:527`) | the reference labels and moves on; we explain |
| Status | badge system: soft-tinted pill + dot + word, nine variants | icon + word + colour (`status_for`, `instance_card.py:139`) | same principle, no pill form |
| Scrollbars | 6px, transparent until hover | 8px, always visible | — |
| Motion | theme spin, bell ring, icon scale on hover/press, cell pulse, wifi wave | none | — |

**On type:** we are not vendoring Roboto Condensed. The condensed, tracked,
uppercase character is reproduced from the already-bundled Roboto using weight,
size, uppercase text and `QFont.setLetterSpacing` — Qt Style Sheets have no
`letter-spacing` property, so tracking has to be set on the `QFont`, per widget,
not in the stylesheet.

**On the brand:** the reference's SVG lens mark is not adopted. The brand mark
stays `theme.brand_icon()`, tinted from `assets/logo/mark-white.png`.

**Where we are stricter than the reference:** our colour-blindness discipline.
`WARNING` amber is fixed independently of the accent, and status is never carried
by colour alone (symbol + word, always — see `theme.py:34` and
`instance_card.status_for`). The reference leans on paired red/green
`--active` / `--offline` tokens. Keep ours.

---

## 3. Functionality gaps, by reference page

### Dashboard

- **Missing:** four KPI tiles with sparkline and trend chip — instances online,
  GPUs active, average GPU usage, fleet disk free. We have no fleet-level summary
  of any kind.
- **Missing:** a fleet event log panel with timestamps, severity colouring and a
  live clock. We have no event history anywhere; the nearest thing is the single
  `poll_status_label` (`dashboard.py:563`) plus modal popups that vanish.
- **Missing:** instance cards in a reflowing grid. Ours are locked into a fixed
  320px rail.
- **Missing, and worth fixing:** per-instance disk free with a low-disk warning.
  The generated notebook queries `nvidia-smi` only, so we collect no disk
  telemetry at all — while `/kaggle/working` has a hard 20GB cap that a long
  render can genuinely hit.

### Files — *the entire page is missing*

- Drag-and-drop dropzone with an upload list. Ours is `Browse for .blend…` into a
  `QFileDialog` (`dashboard.py:767`).
- A file **library**: several `.blend` files, each with its own state, stats,
  assigned-instance chips and actions. We hold exactly one `self.blend` and one
  `FleetState`.
- **Frame preview thumbnails and a lightbox** (previous/next, arrow keys,
  download, per-frame details). BlendFleet cannot currently show the user a single
  rendered pixel.
- A per-file frame grid segmented by instance, with a legend — this *is* our
  Filmstrip, and ours is more honest: it documents that completed cells are
  approximate (`dashboard.py:527`).
- A "collect & zip" progress strip and a downloads panel with progress. Ours is a
  `QFileDialog` followed by a `QMessageBox`.

### Instances

Genuine gaps:

- **Assignment.** The reference has a modal for choosing *which* instances render
  a file. We always fan out across every account (`assignment.py`) with no opt-out.
- **Inline field validation** with per-field error text. `SetupDialog` validates
  through modal popups instead.
- **A fleet setup table with hardware columns** — GPU type, VRAM, RAM, disk free,
  quota left, current file. Our dashboard table has six columns and no hardware.

N/A for Kaggle:

- Host/IP and API-key-test form — accounts are added by token, and there is no
  host to address.
- Pause/resume — Kaggle's unit of control is the whole session, not a GPU or a
  pause state. `instance_card.py` already documents this, and `_cancel_instance`
  is worded honestly around it.
- Remote filesystem browser with per-file delete — there is no persistent instance
  to browse; a session's disk exists only while the kernel runs.

### Logs — *the entire page is missing*

The reference keeps an error list with severity dots and tags, plus failure logs
grouped per machine in collapsible groups. We fetch a failure log tail
(`_fetch_failure_log`, `dashboard.py:1095`), show one line on the card, and discard
it. Nothing accumulates and nothing is browsable after the fact.

### Settings

Ours is a modal (`ui/settings_view.py`) with accent swatches and the minimum-GPU
gate. Missing relative to the reference: theme switch, translucency toggle, sound
toggle, and the three notification-event toggles.

### Cross-cutting — all missing in ours

- **Toast stack.** Every outcome in BlendFleet is currently a blocking
  `QMessageBox`.
- **Notification centre** — bell, unread count, clear all.
- **Connection health** — live latency, and a popup covering internet, latency,
  fleet link, packet loss and last sync, with ping history and a re-run button.
- **Offline banner** with retry, queued-event count, reconnect animation and
  `online`/`offline` listeners. Worth having: the whole app is network-bound, and
  a Kaggle blip currently only turns the poll label amber.
- Sound effects, empty-state copy throughout, skeleton loaders, focus-visible
  outlines, and keyboard handling beyond F11/Esc.

---

## 4. What we have that the reference does not

Do not regress these while restyling:

- The Filmstrip, with explicit "approximate" semantics.
- Per-GPU rows, never combined across physical GPUs.
- Live SSE telemetry, and preflight hardware within seconds of launch.
- Quota shown with an explicit warning that the API disagrees with the settings
  page.
- Cached last-known hardware, labelled with its age.
- Per-instance cancel, worded honestly as cancelling the account's whole session.
- Failure-cause fetching for a worker that errored with no message.
- ETA estimation from a measured seconds-per-frame figure.

---

## 5. Qt feasibility landmines

1. **A light mode walks straight into the trap `theme.py:113-123` documents.**
   `instance_card.py:52` and `dashboard.py:29` do
   `from ...theme import TEXT_SECONDARY, WARNING, TELEMETRY` — captured by value at
   import time. These need `current_*()` accessors, exactly like `current_accent()`,
   before a runtime theme switch can work. Otherwise the light theme reproduces the
   "zero red pixels" bug in monochrome form.
2. **`box-shadow` does not exist in QSS.** It needs a `QGraphicsDropShadowEffect`
   per card, which is slow at scale. The lightness-step elevation already in use is
   the cheaper answer and should stay.
3. **`backdrop-filter` has no Qt equivalent.** The translucency setting should be
   dropped rather than faked.
4. **`letter-spacing` is not a QSS property.** Tracked uppercase labels must set it
   on the `QFont` (`QFont.setLetterSpacing`), so this belongs in a helper in
   `theme.py`, not in the stylesheet string.
5. Radii, accent-soft washes and the badge system are pure QSS and cheap. That trio
   alone closes most of the perceived visual gap.

---

## 6. Sequencing

**Cheap, high impact** — token pass (radii, tracked uppercase type, badge pills,
accent washes), KPI tile row, toast stack replacing non-critical `QMessageBox`es,
event log panel.

**Structural** — sidebar navigation and the page split, which then gives Logs and
Settings somewhere to live.

**Genuinely new product surface** — file library, frame previews and lightbox,
instance assignment, disk telemetry.

## 7. Status

**Done — the sidebar and page shell.** `ui/sidebar.py` (collapsible navigation
with count pills), `ui/flow_layout.py` (the reflowing card grid), and a
five-page `QStackedWidget` in `ui/dashboard.py`. The account cards moved out of
the old 320px rail onto the Dashboard page; Settings became a page sharing one
`SettingsPanel` with the dialog; Logs exists for the first time, so a failure
survives the modal that announced it.

Three long-standing defects surfaced and were fixed on the way: word-wrapped
labels never reported their wrapped height (invisible in a forgiving
`QVBoxLayout`, a clipped card in the new grid), and both the `●` account dot and
the `⚠` failure marker rendered as tofu boxes because Roboto has no glyph for
either — the alert one meaning colour alone was carrying the failure state, the
exact thing `theme.WARNING`'s docstring forbids.

**Done — the design system.** `ui/theme.py` is now `ThemePalette` × light/dark,
every value taken from the reference verbatim, with per-theme accent ink. Every
colour resolves at paint time through `current_theme()`; landmine 1 is closed.
`Settings.theme` persists and a segmented control applies it live.

**Done — window chrome.** `ui/title_bar.py` (frameless, `startSystemMove` /
`startSystemResize`) and `ui/mica.py` (Windows 11 backdrop, immersive dark
chrome, rounded corners), exposed as the reference's own "Translucent surfaces"
toggle and off by default.

**Done — components.** `ui/components.py`: `Badge`, `StatTile` (KPI tile with
sparkline and trend chip), `EventLog`, `Toast`/`ToastStack`, `OfflineBanner`.
The Dashboard now has the reference's four-tile summary row and its
content/log two-column grid; acknowledgement-only outcomes report through a
toast and the fleet log instead of a blocking modal.

### Contrast: where the reference lands, and the one place we depart

Measured with the WCAG formula (`tests/test_theme.py` carries an independent
implementation). The reference clears AA comfortably for body text, and sits
around 3.5:1 for status and accent text — AA's large-text level. Those levels
are now encoded as floors with no headroom, so nothing can silently get worse.

One departure: the reference's primary button is accent-filled with **white**
text, which measures 2.40:1 on its orange accent — the worst pairing in the
design, on this app's most important button. We keep its fill exactly and use a
fixed near-black label instead, which measures 5.72–7.84 across all five
accents in both themes.

**Still to build for a full 1:1:** notification centre, connection-health
panel and their header buttons; the Files page's dropzone, file library, frame
previews and lightbox; card-styled modals including the instance-assignment
modal. Reference features that stay unbuilt because Kaggle cannot support them
are listed in section 3.
