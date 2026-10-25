# Compact media storage: shared implementation contract

This is the coordination contract for the storage contributor (primary agent)
and the exchange/workspace/UI contributor (second computer). It describes the
API being implemented, not an assertion that it is already available.

Base: `main` at `56d74eb`. Integration branch: `feature/compact-media-storage`.
Second contributor branch: `feature/compact-projects-cloud`.

## Ownership

Primary agent owns:

- `vibemogging/storage.py`, `media.py`, and a new media-source/index module;
- `tests/test_compact_media.py` and storage/decoder-specific tests;
- this contract (coordinate changes rather than independently redefining it).

Second contributor owns:

- `repository.py` (portable format and snapshot helpers; preserve merge logic);
- `server.py`, `__main__.py`, new workspace/cloud/maintenance modules;
- `vibemogging/static/`, project chooser and cloud/combined-import controls;
- new exchange/workspace/cloud/migration tests, README and user documentation.

Do not change the other's files. Merge the primary integration branch regularly.

## Core invariants

Existing manifests, commit graphs and raw content IDs remain valid. Video IDs
are still `digest('video', canonical_rgb24_bytes)`, not hashes of compressed
packets or of an operation name. Audio stays exact PCM16/48 kHz/stereo in this
iteration. This keeps frame/sample three-way merging meaningful.

New imports retain the compressed source asset once and register frame recipes
instead of persisting RGB for every frame. Brighten/darken store LUT recipes.
The engine recreates RGB on demand and checks the original content hash. It
must fail explicitly if it cannot reproduce a frame, not silently return a
different image. In-memory frame cache and live decoders are bounded.

Source assets and metadata are immutable. SQLite is a local index, never a
database shared live through a cloud folder. Temporary previews and downloads
are disposable caches, not the canonical history.

## Store API (primary implementation)

The current `Store(root)`, `put`, `get`, `path`, `put_json`, `get_json`,
`digest`, `canonical`, `valid_id`, `atomic_write`, `UserError` stay compatible.
`get` can retrieve either a legacy zlib object or a compact video recipe.

Added methods:

```python
store.add_asset(path: Path) -> str
store.asset_path(asset_id: str) -> Path
store.import_asset(stream, expected_id: str, size: int) -> str
store.register_source_frame(asset_id: str, profile: dict,
                            frame_index: int, rgb: bytes) -> str
store.put_video_lut(base_id: str, table: bytes,
                    rendered: bytes | None = None) -> str
store.recipe(video_id: str) -> dict | None
store.import_recipe(video_id: str, recipe: dict) -> None
store.export_plan(references) -> dict
store.has(kind: str, object_id: str) -> bool
store.flush() -> None
store.close() -> None
store.stats() -> dict
```

Assets use `digest('asset', file_bytes)`. `import_asset` streams, checks length
and digest, and installs atomically. `asset_path` validates the ID and never
accepts user-controlled relative paths. It returns the immutable media file.

Source profiles have `width`, `height`, `fps: [numerator, denominator]`,
`offset` in seconds (default 0), `normalization` (default 2).
`register_source_frame` computes the raw video ID and registers its recipe.
It never stores the RGB payload. Repeated IDs reuse their first representation.

`put_video_lut` accepts a 256-byte channel lookup table. `rendered`, when
provided, is the already computed RESULT frame. If omitted the engine computes
it from the base frame and table. The result ID remains a raw RGB content ID.
Tables use ordinary typed objects of kind `lut`; repeated tables are deduped.

Portable recipes returned by `recipe` and accepted by `import_recipe`:

```json
{"type":"source", "asset":"64 hex", "width":1920, "height":1080,
 "fps":[30,1], "offset":0.0, "normalization":2, "frame":42}
```

```json
{"type":"lut", "base":"raw video ID", "table":"lut object ID"}
```

`recipe` returns null when the chosen representation is an ordinary object.
`import_recipe` validates the schema/IDs and does not overwrite existing valid
representations. `get` checks reconstructed hashes and detects dependency cycles.

`export_plan` takes an iterable of `(kind, id)` references and recursively
expands recipe dependencies. Its JSON-serializable result is:

```json
{
  "objects": [{"kind":"commit", "id":"..."}, {"kind":"lut", "id":"..."}],
  "recipes": {"raw video ID": {"type":"source", "asset":"...", "...":"..."}},
  "assets": ["asset ID"]
}
```

Ordinary metadata/audio/video objects are listed in `objects`; recipe video
payloads must NOT be materialized into RGB during export. Assets are streamed
once, using ZIP_STORED in portable archives. LUT base dependencies are included.
`has` checks availability without decoding, not full content verification.
`stats` returns at least `object_bytes`, `asset_bytes`, `index_bytes`,
`total_bytes`, `frame_cache_bytes`, and counts `objects`, `assets`, `recipes`.
`close` closes decoder processes and SQLite; call it when closing a project.

## Media API (primary implementation)

Existing public signatures remain supported. Added keyword arguments:

```python
media.import_media(store, path, progress=..., *, compact=True)
media.decode_frames(store, path, width, height, fps, offset=0,
                    count=None, progress=..., *, compact=True, normalization=2)
```

`compact=False` creates legacy raw objects for migration/benchmark fixtures.
Normalization 1 reproduces the previous scale/fps pipeline; normalization 2
uses an explicit deterministic conversion profile. For source-assisted legacy
migration, compare every raw frame ID; fall back to lossless packing on mismatch.

`edit_media` adds operation `replace_av`: replace video AND audio from one
source in one operation. Selection is frame-aligned, sound uses matching sample
boundaries. A too-short video/audio replacement fails before changing the working
manifest. There is no insertion or timeline ripple in this iteration.

## Portable snapshots and migration (second contributor)

Introduce `.vmrepo` format 2 with `objects`, `recipes`, `assets` from the plan.
Keep importing V1 archives. All participating applications need the updated
backend to read V2 archives. Verify asset digests and reconstructed frame IDs;
activate branch refs only after the complete snapshot validates.

Implement reusable `Repository.export_snapshot() -> dict` and
`Repository.install_snapshot(index: dict, peer='peer') -> None` so cloud exchange
can use the same graph/validation rules without creating a giant temporary ZIP.
The snapshot contains format/project/branches/head plus the storage plan above.
The installer assumes referenced files/recipes were staged in Store, validates
the graph/content, then updates state atomically. Importing is not auto-merging.

Migration is COPY-TO-NEW-DIRECTORY, never in-place deletion. Preserve all commits,
branch refs, drafts, pending conflicts and raw IDs. Optional original source can
replace legacy root frames if all IDs match. Otherwise pack legacy RGB frames
into bounded-length LOSSLESS `libx264rgb -crf 0` clips, register source recipes
and verify every reconstructed raw ID. Stream frames instead of loading a clip
or whole video into RAM. Refuse existing output directories; leave original
repository untouched even on failure. Report before/after sizes and limitations.

## Workspace, UI and shared cloud folder (second contributor)

Implement a project start screen: list existing projects, create/name a project,
open one, return to the list. Keep `--data-dir` compatibility for existing
single-project workflows; a workspace mode can manage multiple project folders.
Update both server and frontend together and document actual routes.

Add a `replace_av` control and retain the existing initial import of both tracks.
Cloud means a LOCAL folder synced by the user's existing cloud client, not a
new hosted service, paid API, public server or live collaborative timeline.

Each device keeps its working state/index locally. Shared folder layout uses
immutable content-addressed assets/objects and separately published per-device
snapshots. Copy only missing blobs; publishing versions must not duplicate the
entire source file or serialize the whole repository into a new ZIP each time.
Use atomic writes, verify prerequisites before accepting a snapshot and handle
partially synced folders as incomplete, without changing the active project.
Never concurrently rewrite one shared SQLite database or common state.json.
Do not treat cloud sync as a cross-computer locking service. Users explicitly
publish/receive/merge versions; show the current state clearly in the UI.

## Verification

Keep existing tests. Add compact-media exchange, backwards compatibility,
source/LUT dependencies, tampered/missing assets, legacy migration with history
and conflicts, chooser persistence, atomic combined import and two copies
publishing to one simulated cloud folder. Verify raw frame/sample IDs, not just
perceptual similarity. Record measured sizes without promising a fixed ratio.
