# Machine shape findings

**Date:** 2026-08-10
**Question:** The user believes a Kaggle instance has 2×16 GB GPUs but their Blender render only uses one. Is that because (a) Kaggle is only ever giving us one GPU (we set the deprecated `enable_gpu` and never `machine_shape`), or (b) Kaggle gives two and Cycles only uses one?

**Answer: (a).** Kaggle was giving us exactly one GPU because we never set `machine_shape`. Setting `machine_shape: "NvidiaTeslaT4"` gets a real second GPU from Kaggle, and the app's existing, unmodified device-enabling code in `blendfleet/notebook_builder.py`'s `SETUP_SCRIPT` already turns both of them on and Cycles genuinely splits a single frame across both — confirmed by live `nvidia-smi` telemetry showing both cards at 88–98% utilization at the same timestamps during the same render. There is no Cycles-side bug (b).

Everything below was measured against the live Kaggle account `stivestivewithani`, not inferred from documentation.

## 1. Where `machine_shape`'s valid values come from

`kagglesdk` ships no enum — `kaggle_api_extended.py` says so explicitly at the call site (`kernels_push`, line ~6449): *"The allowed names are in an enum that is not currently included in kagglesdk."* The only local source is the docstring on `ApiSaveKernelRequest`/`ApiCreateKernelSessionRequest` in `kagglesdk/kernels/types/kernels_api_service.py`:

```
machine_shape (str)
  The machine shape to use for this session. Currently supported options:
     * NvidiaTeslaT4
     * NvidiaTeslaP100
     * Tpu1VmV38
```

`enable_gpu`/`enable_tpu` are annotated `DEPRECATED: use machine_shape instead` right next to it. No `MachineShape` enum exists anywhere in the installed `kagglesdk` or `kaggle` packages (checked both trees). Public sources (Kaggle-CLI GitHub issues #490/#821, the CLI's own `kernels_metadata.md`) confirm the same three strings and give no separate name for "T4 x2" — the UI's "GPU T4 x2" label is not a distinct API value.

**`kernel-metadata.json` does read `machine_shape`, and it is not silently dropped by the client.** `kaggle_api_extended.py::kernels_push` explicitly does `request.machine_shape = acc if acc else self.get_or_default(meta_data, "machine_shape", None)` — the key is read from the file and always passed to the server. Whether the *value* takes effect is a separate, server-side question (section 2).

## 2. What was actually pushed, and what came back

All pushes went through `blendfleet.kaggle_client.KaggleClient.push_kernel` (the app's real code path) against a throwaway kernel `stivestivewithani/bf-shape-probe`, `enable_gpu: true`, `enable_internet: true`, no dataset.

| `machine_shape` in kernel-metadata.json | Push result | Hardware obtained |
|---|---|---|
| *(key omitted — today's app behavior)* | accepted | **1×** Tesla P100-PCIE-16GB, 4 vCPU, 31 GB RAM |
| `"NvidiaTeslaP100"` | accepted | **1×** Tesla P100-PCIE-16GB, 4 vCPU, 31 GB RAM |
| `"NvidiaTeslaT4"` | accepted | **2×** Tesla T4 (15360 MiB each), 4 vCPU, 31 GB RAM |
| `"Tpu1VmV38"` | accepted | 0 NVIDIA GPUs (`nvidia-smi: not found`) — TPU v3-8 pod, 4 vCPU, 31 GB RAM. No CUDA/OptiX, useless for Cycles. |
| `"NvidiaTeslaT4x2"` (guess) | accepted, **no error** | **1×** Tesla P100 — i.e. it silently fell back to the account's plain default, exactly like the key had been omitted |
| `"nonsense-value-123"` (garbage) | accepted, **no error** | **1×** Tesla P100 — same silent fallback |

This confirms the brief's warning: an unrecognized `machine_shape` string is **not rejected** — `kernels_push` returns success either way — it is **silently ignored** and the session falls back to the account-tier default (a single P100 here). A wrong value in `kernel-metadata.json` would look identical to a correct one in the push response; the only way to tell is to check the hardware the session actually got.

Also checked: `enable_gpu: false` combined with `machine_shape: "NvidiaTeslaT4"` **still yields 2× Tesla T4**. The deprecation note is accurate in practice — `machine_shape` alone is sufficient; `enable_gpu` does not need to be `true` once a valid GPU shape is set.

**So: yes, this account tier gets two real GPUs — but only if `machine_shape` is set to exactly `"NvidiaTeslaT4"`.** That single missing field is the entire explanation for cause (a).

## 3. Real Blender render on `NvidiaTeslaT4` (2 GPUs)

Ran a genuine Cycles render (no dataset needed — the scene was built procedurally inside the kernel with `bpy`: 81 subdivided icospheres (subdivision 5) with randomized glossy/diffuse materials, one area light, 1920×1080, 512 samples) through the **exact, unmodified** `SETUP_SCRIPT` from `blendfleet/notebook_builder.py` (imported and written to the kernel folder verbatim, not copied by hand), then `blender -b scene.blend -P render_setup.py -f 1`. A background thread polled `nvidia-smi` every 1 s for the render's duration. Reproduced twice with the same result.

Blender's own setup line:
```
[setup] OPTIX -> ['Tesla T4', 'Tesla T4']
```
— i.e. the existing `d.use = (d.type == chosen)` loop enabled **both** physical cards under OptiX, exactly as intended.

`nvidia-smi` telemetry during the actual sample-rendering window (run 2 shown; run 1 matched):

```
t=+5.3s  gpu=0 util=58  gpu=1 util=63
t=+6.3s  gpu=0 util=98  gpu=1 util=98
t=+7.4s  gpu=0 util=79  gpu=1 util=93
t=+8.4s  gpu=0 util=90  gpu=1 util=97
t=+9.4s  gpu=0 util=91  gpu=1 util=96
t=+10.5s gpu=0 util=88  gpu=1 util=95
```

Both indices spike to 88–98% **at the same timestamps**, and end the render holding almost identical memory (869 MiB vs 833 MiB) — a near-even split of the same frame's work, not one card idle while the other carries everything. Total wall time 30–31 s (most of it BVH/OptiX-kernel build, ~5–6 s of that being actual sample rendering at 512 samples — the workload was sized to be conclusive, not to be a benchmark).

**This directly answers step 4 of the brief: there was no need to separately "investigate Cycles" — both GPUs were demonstrably busy on the same frame.** Cause (b) does not exist for this scene/backend; the existing device-enabling code in `notebook_builder.py` requires no change.

## 4. What was measured vs. what was inferred

**Measured, live, on this account:**
- The three accepted `machine_shape` strings and the hardware each produced (`nvidia-smi -L`, full `nvidia-smi` table, `os.cpu_count()`, `free -h`).
- That unrecognized strings are accepted at push time but silently produce the plain default (1 GPU), reproduced with two different garbage values.
- That `enable_gpu` can be `false` once `machine_shape` is a valid GPU shape and 2 GPUs still show up.
- That Cycles/OptiX genuinely uses both GPUs concurrently on one frame, via two independent render runs with 1-second-cadence utilization telemetry plus Blender's own device-selection log line.

**Not measured — inferred or out of scope:**
- Whether a different Kaggle account tier (e.g. Kaggle Pro/paid) exposes additional `machine_shape` values beyond these three, or a genuinely distinct multi-P100 shape.
- CUDA-backend (non-OptiX) multi-GPU split behavior — OptiX was available and tried first by the existing setup script, so CUDA's path was never exercised here.
- Multi-frame/animation renders, or renders much longer than ~30 s, might behave differently under sustained load; this test used a single frame sized to be conclusive within a couple of minutes of quota, not a long-duration benchmark.

## 5. Recommendation

Set `machine_shape: "NvidiaTeslaT4"` in `kernel-metadata.json` as the app's default (Task 1's job, not done here — no app code was changed for this experiment). Do not rely on `enable_gpu` alone — it is deprecated and, per section 2, produces only a single P100. Do not invent other `machine_shape` strings speculatively — an invalid one fails silently (section 2), so any future candidate must be pushed and its hardware checked, exactly as done here, before being trusted. The UI should describe the actual entitlement (2× Tesla T4 once `machine_shape` is set correctly) rather than continuing to imply two GPUs are already in use today.

## Account hygiene

- All pushes went to the single throwaway kernel `stivestivewithani/bf-shape-probe`. `remember-blend` and `remember-render` were never touched, and no dataset was created (the render probe built its test scene with `bpy` inside the kernel rather than uploading one).
- No sessions were left running — each push was polled to `complete` before the next one, and `bf-shape-probe` was deleted via `KaggleClient.api.kernels_delete(...)` at the end. A listing of the account's remaining kernels immediately afterward confirms it is gone and only pre-existing kernels from before this experiment remain.
- Total GPU quota consumed by this entire experiment (8 pushes: 1 baseline, 3 valid shapes, 2 invalid-value checks, 1 enable_gpu=false recheck, 2 render runs): **~255 seconds (about 4.3 minutes)** of the account's 108,000 s/week (30 h) quota, confirmed via `KaggleClient.quota()` before (873 s used) and after (1128 s used).
- **Nothing was left behind.** No datasets, no running/queued sessions, no kernels other than the app's own pre-existing ones.
