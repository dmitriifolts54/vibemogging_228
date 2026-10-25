# V1 editor API contract

This contract is shared by the backend and UI contributors. The application runs
locally at `http://127.0.0.1:8765`. The UI is served by the same Python process;
there is no frontend build step, remote server, or Git dependency at runtime.

## Files and ownership

- UI contributor: `vibemogging/static/index.html`, `app.js`, `style.css` (and static assets).
- Core contributor: Python modules, media pipeline, tests, packaging, README.
- Coordinate any changes to this contract. UI must not require unlisted routes.

## HTTP basics

All JSON uses UTF-8. JSON errors are `{ "error": "Readable message" }` with a
non-2xx status. Action failures are also exposed in the job response.
Media timestamps and input range boundaries are seconds, with half-open ranges
`[start, end)`. Video boundaries snap to the nearest frame. Audio boundaries snap
to 48 kHz sample frames. Resolution, FPS and total duration stay fixed in V1.

### GET /api/status

Empty repository: `{ "project": null, "ffmpeg": true, "busy": false }`.

Loaded repository (illustrative):

```json
{
  "project": {"id": "32 hex characters", "name": "demo.mp4"},
  "branch": "main",
  "branches": {"main": "64-char commit hash", "color": "another commit hash"},
  "drafts": ["color"],
  "head": "64-char commit hash",
  "manifest": "64-char manifest hash",
  "dirty": true,
  "width": 1280, "height": 720, "fps": 30, "frames": 1500, "duration": 50,
  "ffmpeg": true, "busy": false,
  "preview_url": "/media/preview/MANIFEST.mp4",
  "history": [
    {"id": "commit hash", "manifest": "manifest hash", "parents": [],
     "message": "First import", "author": "Local editor",
     "created": "2026-10-25T12:00:00+00:00", "branches": ["main"]}
  ],
  "diff": {
    "video": [{"start": 20, "end": 25, "first": 600, "last": 750}],
    "audio": [{"start": 20, "end": 30}],
    "video_frames": 150, "audio_samples": 480000
  },
  "merge": null
}
```

`preview_url` may be null when there is no cached preview. Request `preview`
to render it. `drafts` identifies branches with saved working drafts; these
drafts survive switching branches and restarting the application.

An additional `incoming` string array lists imported tracking branches (such as
`bob/main`). Bundles export local branches and their reachable committed history;
tracking branches are not re-exported as nested `bob/alice/bob/...` names.

When a merge is pending, `merge` has the form:

```json
{
  "base": "commit hash",
  "parents": ["ours commit hash", "theirs commit hash"],
  "source": "color",
  "conflicts": [
    {"id": "video:600:620", "kind": "video", "start": 600, "end": 620,
     "start_time": 20, "end_time": 20.6666667, "choice": null}
  ]
}
```

Use `start_time` / `end_time` in the UI. Raw audio `start` / `end` are scalar
PCM sample indices, not seconds. Choices are null, `base`, `ours`, or `theirs`.
All conflicts must have a choice before committing the merge. Before resolution,
preview uses ours on conflicting positions and includes independent changes.

### POST /api/upload?name=filename.mp4

Send the raw File body (not multipart); `Content-Type: application/octet-stream`.
Returns `{ "upload": "opaque-upload-token" }`. Maximum upload size: 4 GiB.
Tokens are single-use once an action starts; re-upload a file when retrying.
Use the same route for a replacement audio/video file or a `.vmrepo` bundle.

### POST /api/action

Send JSON `{ "action": "ACTION", ...arguments }`.
Returns HTTP 202 `{ "job": "opaque-job-id" }`. Only one action can run at a
time; another action or upload while busy returns HTTP 409.

Poll `GET /api/jobs/JOB_ID` approximately every 500–1000 ms:

```json
{ "id": "job id", "status": "running", "progress": 0.6, "message": "Обработка…" }
```

Terminal success:

```json
{ "id": "job id", "status": "done", "progress": 1,
  "result": { "message": "Готово", "download_url": null } }
```

Terminal failure: `{ "id": "...", "status": "error", "error": "Readable message" }`.
Refresh `/api/status` after every terminal job, including failures. The `result`
object can contain `commit`, `mode`, and `download_url`; display its `message`.
Backend automatically renders a cached preview after actions that change the
working media. While running, disable mutations and show progress, including
upload progress where practical. Do not imply failed jobs succeeded.

| Action | Arguments | Behavior |
| --- | --- | --- |
| `import` | `upload`, `name` | Create initial project; only valid for empty repository |
| `edit` | `operation`, `start`, `end`, optional `upload`, `offset`, `value` | Modify a fixed range; see operations below |
| `commit` | `message`, optional `author` | Save working changes; complete a resolved merge |
| `branch` | `name` | Create and switch to branch at current head; copy working draft |
| `checkout` | `name` | Switch branch; preserve per-branch drafts |
| `restore` | `ref` | Load a historical commit as working draft; history stays intact |
| `discard` | none | Discard current draft; confirm in UI |
| `merge` | `source` | Merge named branch into current branch |
| `resolve` | `conflict`, `choice` | Choose `base`, `ours`, or `theirs` for a conflict |
| `cancel_merge` | none | Return to current head; confirm in UI |
| `preview` | optional `ref` | Render playback preview for `working` or a commit; result has `preview_url` |
| `export` | optional `ref` | Render MP4; result has `download_url` |
| `bundle_export` | none | Download portable committed repository (`.vmrepo`); requires clean current branch |
| `bundle_import` | `upload`, optional `peer` (default `peer`) | Clone into empty repo, otherwise add/update `peer/BRANCH` refs |

Importing cannot overwrite an owned local branch with the same incoming name;
choose another `peer` prefix in that case. Updates of a currently checked-out
incoming branch are rejected until the user switches away from it.

Edit operations:

- `replace_video`: `upload` required; `offset` is source start time, default 0.
- `replace_audio`: `upload` required; audio-only files and video files are accepted.
- `brighten`, `darken`: no upload; simple pixel brightness adjustment.
- `fill`: `value` is a color such as `#22aa88`.
- `mute`: no upload.
- `gain`: `value` is linear gain from 0 to 4 (`1` means unchanged).

Imported replacements are scaled/letterboxed to the project resolution and
resampled to project FPS / 48 kHz stereo. Too-short replacements are rejected.
Edits are blocked while a merge is pending; users must resolve it or cancel it.

### GET /api/diff?left=REF&right=REF

Refs are branch names, full commit hashes, or `working`. Returns the same diff
shape as status. Use this for explicit history comparisons. The default
timeline diff in status always compares current head against working changes.

### GET /api/frame?ref=REF&frame=INTEGER

Returns lossless PNG. Optional `diff_with=REF` returns absolute RGB difference.
Clamp frame index to `[0, frames - 1]`; URL-encode branch names and refs.
Frame scrubbing can display these images without re-encoding the preview.

### GET /api/audio?ref=REF&start=SECONDS&end=SECONDS

Returns PCM WAV for the selected range (maximum 60 seconds per request).
Useful for comparing audio in a conflict / history comparison.

### GET /media/preview/HASH.mp4

Supports HTTP byte ranges for native `<video controls>`. Export job returns a
download URL pointing to this file with an attachment query. Bundles are served
from `/media/bundles/TOKEN.vmrepo` with attachment disposition.

## Suggested UI scope

Russian interface, responsive desktop layout, native HTML/CSS/JavaScript,
no network dependencies. Empty state with import video / open repository.
Loaded state: preview, seekable two-track timeline, range controls, edit tools,
branch switcher, commit form, history, compare, merge and conflict controls,
export MP4 and repository exchange. Make destructive actions explicit.
No insertion/deletion of time, no automatic pixel fusion, no fake server data.
