# Scene library

A list of the scenes already on Kaggle, so a file uploaded days ago can be
rendered again without re-uploading it, and one that is no longer wanted can
be deleted.

Asked for as: *"can we have a file or folder system view of the shared files
ie have a list of uploaded files then we can render a file that was uploaded
5 days ago without necessarily reuploading it. i can also delete a file if
necessary"* (2026-08-12).

## Why it does not exist already

The app is built around one local `.blend` at a time. `Fleet.launch` takes a
`Path`, derives the Kaggle slug from its filename, and verifies every
account against `blend.stat().st_size`. Rendering something that exists only
on Kaggle has no path through that today — which is the substance of this
work, not the list itself.

## Decisions taken

| Question | Decision | Why |
|---|---|---|
| Where the list comes from | Live from Kaggle | Always true, and survives reinstalling the app. A local record would go stale the moment a dataset is deleted on kaggle.com. |
| What delete does | Deletes from Kaggle for real | Frees the owner's storage. One account already carries 8 datasets, most of them diagnostic leftovers. |
| Sharing on re-render | Re-verified every time | Accounts may have been added since the upload, and a grant can lapse. Never launch into a scene half the fleet cannot see. |

## What a scene is

Every dataset this app uploads is named `<stem>-blend` (`fleet.slug_stem` +
`-blend`), so the listing is filtered on that suffix. Two things follow, and
both are stated in the UI rather than hidden:

- The Blender runtime dataset (`blender-<version>-linux`) is **excluded**. It
  is not a scene, it is the renderer, and deleting it would silently cost
  every account a 380 MB re-upload.
- The suffix is a convention, not proof. Before a re-render, the chosen
  dataset's files are listed and the `.blend` inside it is confirmed to
  exist. A dataset that merely *looks* like a scene fails there, with a
  message saying so, rather than at render time on somebody's quota.

## Components

### `KaggleClient.list_datasets(username) -> list[DatasetInfo]`

Wraps `api.dataset_list(user=...)`. `DatasetInfo` carries `ref`, `title`,
`total_bytes`, `last_updated`, `is_private`, `owner`. Note the installed SDK
takes `page`/`max_size` and **not** `page_size` — passing `page_size` raises
`TypeError`, which is how the first probe of this failed.

### `KaggleClient.delete_dataset(slug) -> None`

Wraps `api.dataset_delete(owner_slug, dataset_slug, no_confirm=True)`. The
confirmation is the app's own, in the UI, where the consequence can be
spelled out; `no_confirm=True` only stops the CLI prompting at a terminal
nobody is watching.

Only ever called with the **owner's** token. A friend's token cannot delete
another account's dataset, and attempting it would produce a permission
error that reads like a bug.

### `Fleet.list_scenes() -> list[Scene]`

Every account's datasets, filtered to the `-blend` suffix, minus the Blender
runtime, sorted newest first. Each `Scene` carries its slug, owner, size,
last-updated, and whether the local `.blend` currently selected matches it
(so the panel can say "this is the one you have open").

One account being unreachable must not empty the list: that account's error
is attached to the result and the rest still show — the same discipline
`CollectReport.worker_errors` already follows.

### `Fleet.launch_from_dataset(...)`

The heart of it. Today `launch()` needs a local file for three things:

| Needs `blend` for | Replacement when the file is only on Kaggle |
|---|---|
| `slug_stem(blend)` → kernel slug | the dataset slug's own stem |
| `blend.name` → which file to verify | the `.blend` found by listing the dataset's files |
| `blend.stat().st_size` → expected size | the **owner's** reported size for that file |

The size check keeps its meaning: it is no longer "does Kaggle match my
local file" but "does every account see the same copy the owner sees". That
is the property that actually matters for a fleet render, and it is the one
the current check is really enforcing.

The share step then runs exactly as it does for a fresh upload — the same
per-account confirmation, the same checklist — with the upload skipped.

### UI — the Files page

The page already exists in the shell (`nav-files`) and currently shows a
count of 1. It becomes a list, one row per scene: name, size, age, owner,
and whether every account can see it.

Two actions per row:

- **Render this** — re-verifies sharing, then opens the existing render
  controls with the frame range.
- **Delete** — a confirm naming the scene, its size, and that the accounts
  sharing it will lose access. Irreversible, and says so.

## Errors

Every failure names the account and what to do, per `ui/messages.py`:

- An account whose token is revoked contributes no datasets and one clear
  row saying so, rather than vanishing from the list.
- A delete that Kaggle refuses (permission, already gone) reports which and
  leaves the list untouched until a refresh confirms it.
- A re-render whose dataset has no `.blend` in it stops before launching.

## Testing

Fakes for `dataset_list`/`dataset_delete` in the existing
`tests/test_kaggle_client.py` style, plus:

- the runtime dataset is never offered as a scene, and never deletable
- one unreachable account does not empty the list
- `launch_from_dataset` verifies every account against the owner's size and
  refuses to launch when one disagrees
- deleting uses the owner's token, never a friend's
- a dataset with no `.blend` fails before any kernel is pushed

Page tests (`test_web_page.py`) for the list rendering, the empty state, and
that the delete confirm names the scene.

## The seam for multi-scene rendering

Added after the fact (2026-08-12), when the follow-up question arrived:
*"i decide machine a b and c render this scene and d e f render another
scene, can we also have that provision"*.

That is a bigger change than this spec — the app is single-job by
construction. `fleet.json` is a single slot and `launch()` refuses to start
while a job is live, because overwriting it would leave running kernels
uncancellable and uncollectable, spending other people's quota. Making jobs
concurrent means a list of jobs, each owning a subset of accounts, and
per-job poll / collect / cancel / dashboard. It is its own project.

**But one cheap change here makes it much cheaper later**: `launch` and
`launch_from_dataset` take an explicit `accounts` argument instead of
assuming `self.accounts`. Nothing else changes yet — the caller passes every
account, and behaviour is identical — but "which machines render this
scene" stops being implicit, which is the whole difficulty of the later
work.

Deliberately NOT done now, so this stays one reviewable change:

- multiple concurrent jobs in the state file
- per-job dashboard grouping
- assigning specific accounts to a scene in the UI

## Output folders

Also from that question: where do two scenes' results go?

Today `collect` writes `<dest>/<stem>_0001.png`, so two scenes rendering at
once would write into the same folder — and two scenes whose `.blend` files
share a stem would overwrite each other outright.

`collect` gains a per-scene subfolder: `<dest>/<stem>/<stem>_0001.png`. It
costs nothing while there is one job, and it is a precondition for two.
Worth doing now rather than as part of the larger change, because it is a
one-line difference in where files land and a large difference in whether
the later work can collide.

## Not in scope

- Renaming or re-uploading over an existing scene.
- Nested folders. The scene list is flat and sorted by date; "folder system
  view" was the user's phrase for browsability, and a flat list of a handful
  of scenes is browsable without inventing a hierarchy to maintain. Output
  folders (above) are a different thing: one directory per scene, not a tree.
- Frame previews for old renders — outputs expire with their kernels and are
  a separate concern from scenes. (Previewing a frame of the CURRENT job
  shipped separately, 2026-08-12.)
- Multiple concurrent jobs — see the seam above.
