# Kaggle render desktop app — design

**Date:** 2026-07-30
**Repo:** `blendercolab`
**Depends on:** `kaggle_blender_render.ipynb`

## Problem

Rendering on Kaggle currently means: open a browser, open the notebook, edit the CONFIG
cell by hand, click Save Version, remember to check back. The API can drive all of that
headlessly. A small Windows app should remove the browser from the loop entirely.

## Scope (v1)

Submit and monitor. Pick a `.blend`, set frames/samples/resolution, press Render, watch
progress. Nothing else.

**Explicitly out of v1:** multi-account pooling, in-app frame viewer, scene inspection,
render presets, queue management.

## Architecture

```
  Desktop app (Windows, Python + PySide6)
        |
        |-- kaggle datasets version ----> private dataset  <project>      (the .blend)
        |-- kaggle datasets version ----> private dataset  render-creds   (rclone.conf)
        |-- kaggle kernels push -------> notebook + kernel-metadata.json  -> RUNS
        |-- kaggle kernels status -----> queued / running / complete / error
        |
        `-- rclone lsf gdrive:... -----> live frame count (see "Progress" below)
```

Everything the app does is a Kaggle API call or an rclone call. No scraping, no browser
automation.

## Components

| Component | Responsibility |
|---|---|
| `kaggle_client.py` | Thin wrapper over the Kaggle API: dataset version, kernel push, status. No UI knowledge. |
| `notebook_builder.py` | Renders the CONFIG cell from user settings into a copy of the template notebook. Returns a path. |
| `drive_client.py` | `rclone lsf` against the output folder to count finished frames. |
| `job.py` | One render job: build → upload → push → poll. Emits state changes. |
| `ui/` | PySide6 windows. Talks only to `job.py`. |

Each is independently testable with the others stubbed; the UI never imports the Kaggle
API directly.

## How the notebook gets parameterised

The app owns `kaggle_blender_render.ipynb` as a **template**. On submit it replaces the
whole CONFIG cell with generated assignments, writes the result to a temp `.ipynb`, and
pushes that.

Chosen over the alternative (notebook reads a `params.json` from a dataset) because the
pushed notebook is then self-describing — you can open the run on Kaggle and see exactly
what settings produced it, which matters when a render looks wrong three days later.

`kernel-metadata.json` is generated alongside:

```json
{
  "id": "<username>/blendercolab-render",
  "title": "blendercolab render",
  "code_file": "render.ipynb",
  "language": "python",
  "kernel_type": "notebook",
  "is_private": true,
  "enable_gpu": true,
  "enable_internet": true,
  "dataset_sources": ["<username>/render-creds", "<username>/<project>"]
}
```

## Credentials

**Kaggle:** reuse the standard `~/.kaggle/kaggle.json` that the official client already
reads. The app does not invent its own store. If the file is missing, first-run setup
walks the user through *Account → Create New API Token*.

**Google Drive:** `rclone.conf` is pushed to a dedicated private dataset `render-creds`
and attached via `dataset_sources`. The notebook reads it from
`/kaggle/input/render-creds/rclone.conf`.

> **Security tradeoff, stated plainly.** A private dataset is visible only to the owner
> but is weaker than a Kaggle Secret — it is a file, and sharing that dataset shares the
> token. Mitigations: it is a dedicated dataset holding nothing else, the app never
> offers to share it, and the underlying Google token stays revocable. This is accepted
> deliberately because Secrets cannot be set through the API at all (Kaggle
> `kaggle-cli` issue #582), and requiring a browser visit per run defeats the purpose.

**Notebook change required:** read creds from the dataset path first, fall back to
`kaggle_secrets` if absent, so the notebook still works standalone in a browser.

## Progress reporting

`kaggle kernels status` returns only job-level state — queued, running, complete, error.
It does **not** expose per-frame progress.

Live frame progress therefore comes from counting files in the Drive output folder with
`rclone lsf`, run locally by the app on a timer. This works because the notebook already
uploads each frame as it completes, and it is the same source of truth the notebook's own
resume logic uses.

Poll intervals: kernel status every 30 s, frame count every 20 s, both backing off to
60 s after ten minutes without change.

## Error handling

| Failure | Behaviour |
|---|---|
| No `kaggle.json` | First-run setup, with the link to create a token. |
| Dataset upload fails | Surface the API error; do not push the kernel. |
| Kernel push rejected | Show the message verbatim — usually a slug or metadata problem. |
| Kernel status `error` | Fetch `kernels output` log and show the tail, which is where the Blender traceback lands. |
| rclone unreachable | Job continues; progress shows "frame count unavailable" rather than failing. |
| GPU quota exhausted | Kaggle queues rather than refusing; surface as "queued" with a note. |

## Packaging

PyInstaller one-file `.exe`. PySide6 pushes this to roughly 80–150 MB — acceptable for a
desktop tool, and worth noting up front so it isn't a surprise.

## Resolved unknowns (verified 2026-07-30)

Documentary evidence only — none of this was executed against a live account, so treat
each as "best available evidence", not a test result.

**1. Weekly GPU quota is NOT exposed by the API.** No endpoint exists. The public API
covers competitions, datasets, kernels and models; there is no user-quota or usage
endpoint, and Kaggle has said there is no user-fetch API at all.

→ **Design change:** drop the "6.2 / 30 h this week" readout from the UI. v1 shows
elapsed time for jobs it launched itself and links to Kaggle's settings page for the real
figure. Do not fake a number the API cannot supply.

**2. `kernels push` always triggers a run.** It is never a no-op — every push creates a
new version and executes it, even when the code and data are byte-identical.

→ **Design change:** re-rendering needs no cache-busting trick. But an accidental double
submit costs real quota, so the submit button must guard against it.

**3. Concurrent limits are 2 GPU / 5 CPU sessions — and pushing does NOT stop the
previous run.** Prior versions of the same kernel keep running concurrently after a new
push. There is **no API method to stop a kernel**; issue #388 is an open feature request.
Stopping requires the website.

→ **Design change, the significant one:** the app can *start* work it cannot *stop*. So:
- Track every submission the app makes, persisted across restarts.
- Refuse a second submit while one is believed active; require explicit override.
- Warn at the 2-concurrent-GPU ceiling before pushing, not after it fails.
- Surface a prominent "Stop on Kaggle" link to *Active Events*, since that is the only
  way to kill a run.
- The UI must never imply it can cancel. A greyed-out Cancel that does nothing is worse
  than no Cancel.

## Out of scope

- Anything requiring a browser
- Multi-account pooling (v2 — this is what `blenderCOllab` is ultimately for)
- Editing the .blend, previewing the scene, or choosing cameras
- Non-Windows builds
