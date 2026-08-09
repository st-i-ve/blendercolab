# Known issues and deferred work

Everything here was found by review, judged non-blocking, and merged deliberately.
Nothing in this list is a surprise or an unknown.

These were tracked in `.superpowers/sdd/*/progress.md`, which is **gitignored** —
extracted here so they survive.

Last updated: 2026-08-09, merging `saas-dashboard`.

---

## Worth fixing before this is used in anger

### No timeout on any Kaggle call
`grep -n "timeout=" blendfleet/*.py` returns nothing. Every Kaggle request can in
principle block indefinitely. Mitigated in practice — uploads have their own
retry/resume, and network I/O runs on worker threads so the UI survives — but a
genuinely hung call outlives the 3s `closeEvent` join and the window closes
anyway. **Whole-codebase, pre-existing.**

### SSE stop window before the response exists
`log_stream.stream_progress` can only be stopped once the response object exists.
A thread stopped during connect, TLS, or the 30s log-URL hold has no closer and
unwinds on the 120s read timeout, while `closeEvent` abandons it after
`STREAM_JOIN_TIMEOUT_S = 3.0`. Narrow, and only on the exit path.

### `_install_request_timeout` reaches into kagglesdk internals
It degrades to `False` and streams on if a future SDK release reshuffles them —
safe, but silent. The test that covers it uses a fake client, so a rename would
leave the test green while production quietly degraded. **Pin the real attribute
chain and log on degrade.**

### Friends keep READER access after a render
`sharing.revoke_reader` is implemented and tested but has no production caller.
Collaborators retain read access to the owner's private `.blend` indefinitely.
This is a **product decision** — when should access end? — not a defect.

---

## Correctness, low impact

- **`charts.frame_done` approximation.** Assumes `frames[:frames_done]` are the
  completed ones, but the notebook's `done=` counter counts *successes only*, so
  one failed frame shifts every later filmstrip cell. Documented in the UI header,
  not just a docstring. Real fix: carry explicit frame numbers.
- **`instance_state.save()` is non-atomic** — no temp+rename. Inherited from
  `AccountStore.save()`; a crash mid-write truncates the file. Not a regression.
- **Staging dirs** `.raw_<label>` under the output folder are cleaned after
  collect, but a crash between fetch and cleanup leaves them.
- **`collect_data_files("kagglesdk")`** collects zero files — kagglesdk ships no
  data. Harmless defensive inclusion.

## Test-quality gaps

- No test drives `Dashboard._open_settings()` or clicks `settings_btn`; only
  `SettingsView` in isolation.
- No test pins the per-push `_save` in `fleet.launch` — deleting that line leaves
  the suite green, because the `try/finally` save masks it.
- No tests for the skip-if-busy worker guards, button re-enable on failure paths,
  or the `closeEvent` worker wait. All three were read and judged correct.
- `no_leaked_threads` matches on `t.ident`, which the OS can reuse after a thread
  dies — a rare false negative.

## Performance / polish

- `_poll` triggers a quota refresh every 30s → 2N Kaggle calls per 30s, forever.
  Fine at friend-group scale.
- `closeEvent` joins stream threads sequentially, so a pathological close could
  block up to 3s × N accounts.
- `_default_upload_blob` builds a fresh SDK client per uploaded file. Staging only
  ever holds one `.blend`, so this is one client per sync.
- Linear label lookup in `dashboard.py` where a `by_label` dict already exists
  nearby.
- Rail, upload and GPU panels grow unbounded with many accounts.

## Environment-specific, not real bugs

- **1280px `QSpinBox` glyph smear** — reproduced in a bare Qt app with zero
  BlendFleet code, at exactly 1280px width, only under `QT_QPA_PLATFORM=offscreen`.
  Absent at 1279/1300/1920/2560. The packaged build never uses that platform.
- **Account-dot tofu box** under offscreen font fallback. Re-rendered under the
  native Windows platform: paints correctly.

---

## Deliberate design decisions, recorded so they are not "fixed" by mistake

**Uploads are one request, not chunked.** Kaggle's endpoint is a GCS resumable
session. Out-of-order and concurrent ranges are both rejected — the server aborts
the connection — and chunking measured *slower* (0.59 vs 0.95 MB/s). Chunking
would trade ~40% throughput for a smaller retry unit. See
`docs/upload-concurrency-findings.md`.

**The dataset check compares size, not content.** Kaggle's API exposes no hash or
checksum on `ApiDatasetFile` — pinned by a test that fails if a future SDK adds
one. The message says "size matches" and never claims more.

**Idle instance cards show no live gauges.** Kaggle has no idle instances; a
session exists only while a kernel runs. Idle and live are *mutually exclusive
widgets* so a cached number can never appear in a live-looking dial.

**`cancel_all` does not persist `worker.state`.** Left to the next `poll()`, per
the original design.

**Warning amber is never derived from the accent.** Otherwise a red accent makes
error states indistinguishable from ordinary chrome.
