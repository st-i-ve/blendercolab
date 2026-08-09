# SaaS Dashboard, Theming and Brand — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the render dashboard into a SaaS-style instance console — per-account cards showing quota, allocation and load — add a Settings area with a selectable accent colour, adopt the new brand mark, and make the app full-screen and genuinely appealing.

**Architecture:** A `Settings` store (accent colour, window state) alongside the existing `AccountStore`. `theme.py` becomes accent-parameterised and re-appliable at runtime. A new instance-card view replaces the plain account rail. Telemetry caching persists last-known hardware per account so cards stay informative when nothing is running.

**Tech Stack:** Python 3.11+, PySide6, pytest, Pillow (asset generation only).

## Global Constraints

- Python 3.11+; all paths via `pathlib`.
- **No OS branching outside `blendfleet/platform_paths.py`** — a grep guard enforces this.
- **No network in unit tests.** `tests/conftest.py` has autouse guards that block sockets and fail on leaked threads — **do not weaken or bypass them.** They exist because a test making live calls aborted ~15% of runs.
- **All network I/O stays off the UI thread.** Five methods already use `_CallWorker`; anything new follows that pattern.
- **Never write `os.environ["KAGGLE_API_TOKEN"]`** outside the lock-guarded `_with_env_token`.
- **Status is never conveyed by colour alone** — symbol + word. Failure uses amber, not red (~8% of men cannot distinguish red/green). **This survives every accent-colour choice**, including the red accent.
- Qt objects in tests must be deterministically torn down, not left to GC — that caused fatal aborts before.

- **One typeface family, one icon set.** UI text is **Roboto**; all numeric and
  machine data is **Roboto Mono** (same family, so the app keeps a single voice
  while figures stay fixed-width and do not reflow as they tick). Status icons
  come from the vendored **Lucide** set in `assets/icons/`, never Unicode
  glyphs — those render differently depending on the machine's fonts.
  Both are already vendored and verified loading in Qt (`assets/fonts/`,
  `assets/icons/`, licences alongside).
- **Icons supplement labels, never replace them.** Every status is symbol AND
  word. An icon-only status fails the same accessibility rule as colour-only.
- Fonts must be registered via `QFontDatabase.addApplicationFont` at startup
  and **bundled in the PyInstaller spec** — a frozen app has no access to the
  source tree, and an unregistered family silently falls back to a system font.
- No `Co-Authored-By` trailers on commits.

## The governing fact

**Kaggle has no idle instances.** A session exists only while a kernel runs; between renders there is no machine to poll. Every GPU/CPU/RAM number comes from `nvidia-smi` inside the render notebook, streamed over SSE.

So the instance console shows:
- **quota** — genuinely live, free to poll, already implemented
- **last-known hardware** — cached from that account's most recent run, labelled with its age
- **live util/VRAM/RAM** — only while that account is rendering

**Never render a live-looking gauge for data that is not live.** An idle card must read as idle. This is the single most important honesty constraint in the plan: a dashboard that implies it is watching an idle machine is lying.

---

### Task 1: Settings store and accent-parameterised theme

**Files:** create `blendfleet/settings.py`, `tests/test_settings.py`; modify `blendfleet/ui/theme.py`, `tests/test_theme.py` (create if absent)

**Produces:** `Settings(accent: str, fullscreen: bool)` with `load()`/`save()`; `ACCENTS: dict[str, AccentPalette]` for orange, green, purple, blue, red; `theme.apply(app, accent)` re-appliable at runtime.

- [ ] **Step 1: failing tests** — `Settings.load()` with no file returns defaults (accent `"orange"`); save/load round-trips; an **unknown accent name falls back to the default rather than raising**, so a hand-edited or future-version config cannot brick the app; every accent in `ACCENTS` defines the full token set (no `KeyError` at paint time).
- [ ] **Step 2: run, confirm failure**
- [ ] **Step 3: implement.** Derive each accent's hover/pressed/disabled shades from the base rather than hand-listing them, so adding a colour is one line.
- [ ] **Step 4: contrast test** — assert every accent meets at least 4.5:1 against the shell background for text use. Compute it; do not eyeball. An accent that fails is a bug, not a taste question.
- [ ] **Step 5: amber-survives-accent test** — assert the warning colour stays amber and is not derived from the accent, including when the accent IS red. Otherwise a red accent makes error states indistinguishable from normal ones.
- [ ] **Step 6: register the bundled fonts and expose the icon set.** Load all
  five TTFs from `assets/fonts/` via `QFontDatabase.addApplicationFont` at
  startup and assert the families register (`Roboto`, `Roboto Mono`) — a
  silent fallback to a system font is the failure mode here. Add an
  `icon(name, colour)` helper that loads from `assets/icons/` and tints via
  the SVG's `stroke="currentColor"`, so icons follow the active accent.
  Test: every icon the app references exists on disk and loads non-null —
  a missing icon must fail a test, not render as a blank square at runtime.
- [ ] **Step 7: full suite + commit**

---

### Task 2: Adopt the new brand mark

**Files:** modify `blendfleet/__main__.py`, `packaging/blendfleet.spec`; remove the old `assets/blendfleet_icon*.png` / `blendfleet.ico` and `assets/make_icon.py`

`assets/logo/` already holds the derived set from `assets/newLogo.png`: `mark-white.png` (tintable master), `mark-black.png`, five accent tints, `app-icon-{16..512}.png`, `blendfleet.ico`, `glyph-{24,32,48}.png`.

- [ ] **Step 1** — point the window icon and the PyInstaller spec at `assets/logo/`. The frozen-path helper `_icon_path()` in `__main__.py` resolves via `sys._MEIPASS` first; keep that working and update the bundled data entry in the spec to match.
- [ ] **Step 2** — put the glyph in the app's own chrome (header/rail), tinted to the active accent, so the brand is present in-app and not only on the taskbar.
- [ ] **Step 3** — delete the superseded generator and its outputs. Do not leave two icon sets; a stale one will be picked up by mistake later.
- [ ] **Step 4** — verify offscreen that the icon loads and is non-null, and that `assets/make_logo.py` still regenerates the set from source.
- [ ] **Step 5** — full suite + commit

---

### Task 3: Cache last-known instance hardware

**Files:** create `blendfleet/instance_state.py`, `tests/test_instance_state.py`; modify `blendfleet/ui/dashboard.py`

**Produces:** `InstanceSnapshot(username, gpus, cpu_count, ram_total, observed_at)`; `InstanceStore` with `record(label, snapshot)`, `get(label)`, `load()`, `save()`.

The notebook's first cell already prints CPU count, RAM and the `nvidia-smi` GPU listing; telemetry lines carry per-GPU util and memory. Persist the hardware facts per account so an idle card can show what that account last ran on.

- [ ] **Step 1: failing tests** — a snapshot round-trips to disk; `get()` for an unseen account returns `None`; **a snapshot older than a threshold is reported as stale** (Kaggle's allocation genuinely varies between runs — a P100 last time does not mean a P100 next time, and the UI must not imply otherwise); loading a file written by an older version without a field does not crash.
- [ ] **Step 2: run, confirm failure**
- [ ] **Step 3: implement**
- [ ] **Step 4** — record snapshots from the live stream as a render starts, wired through the existing telemetry callback. No new network calls.
- [ ] **Step 5** — full suite + commit

---

### Task 4: Instance console

**Files:** create `blendfleet/ui/instance_card.py`, `tests/test_instance_card.py`; modify `blendfleet/ui/dashboard.py`

One card per account, replacing the plain rail. Target shape:

```
INSTANCE 1   stive                            ● idle
  Quota      2.4 / 30.0 h                     live
  Last run   2h ago -- Tesla P100 16GB, 4 vCPU, 31.3 GB
  ---------------------------------------------------
INSTANCE 2   stepheneechikoi                  ● rendering
  Quota      6.1 / 30.0 h                     live
  GPU 0      util ▁▃▅▇█▇▅▃ 87%   6.0/15.0 GB  live
  RAM        8.3 / 31.3 GB                    live
  Frames     28 / 84
```

- [ ] **Step 1: failing tests** — an idle card shows quota and last-known hardware and **does NOT show a live gauge**; a rendering card shows live util/VRAM; an account with no history reads "never run — launch to see specs"; a stale snapshot is visibly marked.
- [ ] **Step 2: run, confirm failure**
- [ ] **Step 3: implement.** Live values carry an explicit `live` marker; cached values carry their age. **A number must never be ambiguous about whether it is current** — that is the whole reason this task is shaped this way.
- [ ] **Step 4** — quota keeps its existing "API figure" labelling and settings link; the API and settings page were once observed disagreeing for the same account.
- [ ] **Step 5** — offscreen verification through: no accounts, one idle, one rendering, one stale, one errored. Report what you ran.
- [ ] **Step 6** — full suite + commit

---

### Task 5: Verify each account has the correct .blend

**Files:** modify `blendfleet/fleet.py`, `blendfleet/kaggle_client.py`; extend their tests

Today's pre-launch check confirms each account can **reach** the dataset. A stale copy from an earlier upload passes it and renders the wrong scene — discovered only from the output.

- [ ] **Step 1: failing test** — an account whose dataset file size differs from the local `.blend` causes launch to be **refused**, naming that account, with **zero kernels pushed** (assert the push count, not merely that an exception was raised).
- [ ] **Step 2: implement** using `dataset_list_files`, which already returns names and sizes, and is the call `dataset_reachable` uses. Prefer size plus whatever hash Kaggle exposes; if no hash is available, say so in the message rather than implying a content check.
- [ ] **Step 3** — the failure message states which account, what differs, and what to do (re-share or re-upload).
- [ ] **Step 4** — full suite + commit

---

### Task 6: Settings screen, full-screen shell, visual pass

**Files:** create `blendfleet/ui/settings_view.py`, `tests/test_settings_view.py`; modify `dashboard.py`, `__main__.py`

- [ ] **Step 1** — Settings view: accent picker (five swatches, **each labelled with its name, never colour alone**), applied live without restart, persisted.
- [ ] **Step 2** — window opens maximised by default, persisted; a real full-screen toggle (F11) with a visible way back out.
- [ ] **Step 3** — visual pass: consistent spacing scale, card elevation, generous breathing room at large sizes. **Layouts must not merely stretch** — at 2560px a stretched two-column layout looks broken. Decide what grows and what stays fixed.
- [ ] **Step 4** — re-audit user-facing strings touched by this branch: what happened, why, what to do next. No bare status codes, no raw exceptions.
- [ ] **Step 5** — offscreen verification at 1280×720, 1920×1080 and 2560×1440, in every accent. Report what you ran.
- [ ] **Step 6** — full suite + commit

---

### Task 7: Rebuild and verify the exe

- [ ] **Step 1** — `powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1`. It refuses the wrong interpreter and fails on a suspiciously small binary; do not weaken those guards.
- [ ] **Step 2** — confirm the bundle contains PySide6/kaggle/kagglesdk **and the new `assets/logo/` data** by scanning `build/blendfleet/blendfleet.pkg`, not by launching the app — Kaggle imports are lazy, so a window opening proves nothing.
- [ ] **Step 3** — report size and counts.

---

## Out of scope

- Keeping an idle session warm for always-on telemetry. Considered and rejected: it burns the weekly quota continuously doing nothing, which is the opposite of the app's purpose.
- An on-demand probe kernel. Deferred, not rejected — revisit if last-known specs prove insufficient.
- Light theme. The app is dark by design; a second full theme is a larger job than an accent swap.

## Self-Review

**Coverage:** key verification → already shipped, confirmed in code. Instance console → Tasks 3–4. Correct-file verification → Task 5. Accent colour → Tasks 1 and 6. Appealing and full-screen → Task 6. New logo → Task 2.

**Sequencing is load-bearing:** Task 1 defines the accent tokens every later view consumes; Task 3 supplies the data Task 4 renders. Do not build a card for data that is not being recorded yet.

**The honesty constraint is the thing most likely to be quietly violated under design pressure:** a SaaS dashboard *wants* to show live gauges everywhere, and Kaggle simply does not provide them when idle. Tasks 3 and 4 each carry an explicit test that idle cards do not render live-looking values. If a reviewer finds a live-styled widget bound to cached data, that is an Important finding, not a nitpick.
