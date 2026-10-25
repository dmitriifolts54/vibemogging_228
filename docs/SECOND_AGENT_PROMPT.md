# Task for the UI contributor

Implement the browser UI for Vibemogging V1, a local video editor with its own
Git-like video/audio versioning. The core developer is concurrently implementing
the Python backend and tests. Work only on the frontend and follow `docs/API.md`.

Repository: git@github.com:dmitriifolts54/vibemogging_228.git

1. Fetch the remote. Create `feature/v1-ui` from `origin/feature/v1-core`.
   If that local branch exists, inspect and continue it without resetting work.
2. Read `docs/API.md` in full. It is the agreed backend contract. The backend
   server may not be in the initial commit yet; do not invent a replacement.
3. Own only `vibemogging/static/` and optionally `docs/UI.md`. Do not edit Python,
   tests, packaging, README, the API contract, or application data. Do not merge
   into main, rewrite history, or force-push. Coordinate API changes in your
   final report rather than silently changing the contract.
4. Use plain HTML/CSS/JavaScript with no build step or CDN dependencies. UI files
   must be `index.html`, `style.css`, `app.js` in `vibemogging/static/`.
5. Build the actual functional editor, not a static mockup: import; player;
   seekable video/audio timeline; fixed-duration range edits; branches and
   per-branch drafts; commits/history/restore; diff and frame comparison;
   merge and manual conflict resolution; MP4 export; portable repository
   export/import. Use the exact actions and endpoints from the contract.
6. Interface language is Russian. Choose a polished, legible dark desktop
   editor with clear hierarchy, accessible controls, responsive narrow layout,
   and clear empty, loading, success, failure, and disabled states. Show async
   job/upload progress. Display backend errors. Never display mocked success.
7. V1 keeps total duration, resolution and FPS fixed. Video and audio are
   independent. Pixel fusion and automatic audio conflict fusion are future
   hooks: offer manual base/ours/theirs choices for conflicts. Insertion and
   ripple deletion are V2 and must not be exposed as working controls.
8. Test against the real backend when it becomes available. If unavailable,
   you may use an explicitly isolated test fixture for UI checks, but ship no
   mock transport and do not claim real integration has passed.
9. Commit your changes and push `feature/v1-ui`. Final report: commit hash,
   implemented controls, verification performed, any integration limitations,
   and deviations needed from `docs/API.md`.

Acceptance example: user A changes video 00:30–00:45 and audio 00:20–00:30;
user B changes video 00:20–00:25. Merging includes all changes without conflict.
Conflicting edits to the same frame/sample must instead prompt a manual choice.
