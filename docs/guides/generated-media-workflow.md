# Generated media workflows

The compound server's `generated_media(action, params)` tool connects completed
generations to Resolve. It supports asset ingest, immutable generation versions,
storyboard assembly, reversible replacement takes, local credit accounting,
visual review evidence, and exact-build capability checks.

All actions except `capabilities` take `workspace`, an **absolute directory**.
`init` binds that directory to the current Resolve **project ID**, not its name.
Records, managed media and review frames live in `.generated-media/` beneath it;
this directory is ignored by Git. Back it up with the project. Moving the workspace
requires updating stored absolute media paths; it does not automatically relink
Resolve clips. This tool is available in the default compound server, not `--full`.

## Start a project and record a scene

```json
{"action":"init","params":{"workspace":"/absolute/path/to/production","budget_credits":10000}}
```

The default budget is zero. Initialization writes local records, not Resolve edits.
Repeated initialization preserves the existing budget. Use `budget_set` to change it.

```json
{"action":"scene_put","params":{"workspace":"/absolute/path/to/production","scene_id":"scene-3","prompt":"The pilot enters the hangar","duration_seconds":4,"reference_images":["/absolute/path/to/pilot.png"]}}
```

Scene edits increment the scene revision. Generations retain their own prompt,
model, reference paths, settings, provider ID, local path, SHA-256, estimated
credits, actual credits and version number. At reservation time, local reference
images are verified and copied into content-addressed storage, so an overwritten
or deleted original reference does not erase an earlier take’s reference. Original
paths and snapshot checksums remain recorded. Remote references must be downloaded
first; the tool does not upload references or call a generation endpoint.
`records` returns the complete local history.

## Estimate, reserve, generate once

```json
{"action":"estimate","params":{"workspace":"/absolute/path/to/production","unit_credits":"125","quantity":4,"variants":1,"price_source":"Current provider quote for model/settings on date"}}
```

This multiplies a supplied **current** credits-per-unit quote by quantity and
variants. No universal model prices are hardcoded; pricing depends on model and
settings. USD and credits must not be mixed. This ledger is local production
accounting, not the provider account balance or a billing reconciliation service.

```json
{"action":"reserve","params":{"workspace":"/absolute/path/to/production","scene_id":"scene-3","request_key":"scene-3-take-1","model":"provider-model-id","estimated_credits":500,"price_source":"Current provider quote","settings":{"duration_seconds":4}}}
```

The first result returns `may_submit_once: true`. **Reservation does not authorize
spending** and never calls a generation endpoint. After the user authorizes the
generation, use the provider's tool separately and immediately attach its returned
ID with `generation_update`. Repeating a request returns `may_submit_once: false`.
Identical generation parameters also deduplicate across different request keys.
For a user-requested new take, supply a new key and `regenerate_from` pointing at
the prior local record ID. An uncertain provider submission must be reconciled with
provider history; it must not be submitted again just because the result was lost.

```json
{"action":"generation_update","params":{"workspace":"/absolute/path/to/production","record_id":"LOCAL_RECORD_ID","generation_id":"PROVIDER_ID","status":"completed","actual_credits":480,"cost_evidence":"Provider receipt or usage result identifier"}}
```

Valid progress: reserved → submitted → completed/failed. A provider ID is
immutable and unique within this workspace/provider. Actual charges can exceed
an estimate; they are recorded and the overrun is reported. Unknown charges,
including failed jobs, retain their reservation until actual credits are supplied
with evidence. Download/import failures do not change generation status or incur
generation charges. The tool does not submit any paid provider requests.

## Retrieve and import a completed generation

`attach(record_id, local_path)` copies an existing completed download to managed
storage. `download(record_id, url)` retrieves a completed HTTPS result URL.
For an ElevenLabs **API-created image/video**, `download(record_id, kind="video")`
can resolve the completed generation ID using `ELEVENLABS_API_KEY` from the MCP
server environment. It performs GET requests only. Credentials are not stored
in records and are not forwarded on redirects. API errors say to retry retrieval,
not generation. Audio outputs use completed local files or URLs.

ElevenLabs' API history is scoped to API-created generations: assets made in its
web app do not appear there. Download those in the app and use `attach`, or supply
an available completed result URL. This is a provider constraint, not a missing
API key. See the [official quickstart](https://elevenlabs.io/docs/eleven-api/guides/cookbooks/image-and-video)
and [generation lookup](https://elevenlabs.io/docs/api-reference/flows/video/get).

Ingest uses bounded public HTTPS downloads, atomic promotion after verification,
SHA-256 addressing, `ffprobe` inspection and a full `ffmpeg` decode. Optional
`expected_sha256` verifies a known digest. The default file limit is 1 GiB (maximum
4 GiB). Network access is disabled for the media decoder. Invalid, truncated,
HTML and unsupported-container results are not accepted. Original input files are
never overwritten. Content-addressed managed copies avoid duplicate downloads.

```json
{"action":"import_asset","params":{"workspace":"/absolute/path/to/production","record_id":"LOCAL_RECORD_ID"}}
```

Imports go to `Generated/<scene slug>-<stable hash>`. Import retries scan the
Media Pool for matching file contents (size and SHA-256), including earlier manual
imports at other paths and lost responses. Same-size candidate files are read for
verification; very large Media Pools may make this slower. The
previous current bin is restored. Identical content shared between scenes reuses
one Media Pool item in its first bin; scene associations remain in the ledger.
No duplicate clip is created merely to put it in another scene bin.

## Storyboard to timeline

```json
{"action":"plan_storyboard","params":{"workspace":"/absolute/path/to/production","name":"First assembly","fps":24,"scenes":[{"scene_id":"scene-1","record_id":"VISUAL_1","voiceover_record_id":"VOICE_1"},{"scene_id":"scene-3"}],"music_record_id":"MUSIC_RECORD"}}
```

The ordered plan freezes durations and asset hashes. It reports audio trimming.
Missing visual records become synthetic video cards with scene/prompt markers.
Generated still images can be imported and reviewed; storyboard visual records
currently require video. Free 20.3.2 ignores still-image append extents and uses its
default still duration, so this workflow refuses those shots before assembly rather
than silently creating incorrect timing. It does not convert source images.
Frames use cumulative rounding and half-open source intervals. Existing videos
must match the timeline FPS and be long enough: no implicit retiming or looping.
V1 holds shots; A1 holds per-scene voiceover; A2 holds music. Source video audio is
excluded. Voiceover/music are trimmed to their slot when longer and leave silence
when shorter. Preview the plan, then execute:

```json
{"action":"build_storyboard","params":{"workspace":"/absolute/path/to/production","plan_id":"PLAN_ID"}}
```

Assembly uses a new uniquely named timeline. The prior current timeline is restored.
The same plan resumes partial appends and verifies existing positions, durations
and media before adding missing slots. Edits outside the tool cause a refusal;
create a new plan in that case. If Resolve honors a write incorrectly, a partial
new storyboard may remain for inspection; existing user timelines are untouched.

## Replace one generated shot

Use exact IDs returned by the build or Resolve inspection, not clip names/indexes:

```json
{"action":"plan_replace","params":{"workspace":"/absolute/path/to/production","timeline_id":"TIMELINE_ID","item_id":"TIMELINE_ITEM_ID","record_id":"NEW_TAKE_RECORD","source_start_frame":0}}
```

The plan reports required/available frames and `duration_mismatch_frames`.
Short replacements cannot execute. Extra frames are unused. Replacement currently
accepts matching-FPS **video** assets. A structure snapshot detects stale plans.

`replace_shot(plan_id)` imports the new file and selects it through Resolve's
**take selector**. It preserves item timing, surrounding edits and linked audio.
The old take remains available; nothing is finalized or ripple-deleted. It checks
selected media and the timeline structure after the operation, and attempts to
restore the previous take on failure. Complex retimes/transitions are not asserted
visually correct by structural readback; inspect rendered frames in real edits.

## Visual review loop

`review(record_ids=[...])` treats records as ordered shots. It extracts start,
middle and end images, reports near-black intervals and large within-shot changes,
and returns `pending_host_vision`. Read the provided frame paths and reference
images in the host. Inspect character appearance, unwanted text and between-shot
transitions. Findings are **possible problems**, not identity claims or a guarantee.

`commit_review(review_id, reviewed_frame_ids, findings)` requires every frame to
be acknowledged. Each finding has `category`, `frame_id`, `confidence` (0..1) and
`reason`. Categories: character_change, unwanted_text, blank_frame,
abrupt_transition, other. An empty list means no issues observed in these samples,
not proof of a flawless clip. The result becomes `ready_for_user_review` and remains
local; no edits or markers are automatically applied to user media. Sparse samples
can miss brief defects, and intended darkness/cuts can produce false positives.

## Exact Resolve setup and testing

`capabilities` reads the running product/version, current project, ffmpeg/ffprobe
availability and relevant method exposure. It never tries Studio AI features or
21.x-only methods to discover failure. On Free 20.3.2 the existing in-app bridge
provides the connection. Method exposure is labeled separately from live behavior.

```sh
venv/bin/python -m unittest tests.test_generated_media
venv/bin/python tests/live_generated_media.py --run
```

The opt-in live harness creates synthetic H.264 video/audio and a disposable
project, tests duplicate-free imports, placeholder/video/voiceover/music assembly,
retry behavior and take replacement, then restores the original project and deletes
the test project. Evidence stays under ignored `tmp/generated-media-live/`.
It performs no paid generation and does not fabricate a host visual-review result.

The configured MCP process loads tool definitions on startup. Restart that MCP
connection after updating this checkout so `generated_media` becomes discoverable.
No provider key is needed for completed local-file or URL ingest.

### Validation recorded on 2026-09-16

- 48 focused unit/static/drift tests passed, plus the repository import test,
  API parity, API-limitations documentation and read/write symmetry checks.
- A fresh stdio MCP session discovered `generated_media` among 38 compound tools
  and returned the connected Resolve version and current project successfully.
- Live synthetic tests passed on **DaVinci Resolve Free 20.3.2.9** through the
  existing bridge: verified ingest, duplicate import reuse, two-shot storyboard,
  voiceover/music, an exact-duration placeholder, repeated assembly, duration
  mismatch reporting, take replacement, repeated replacement, and review frames.
- Resolve-rendered first and last frames of the replacement were both blue
  (`RGB [1,0,255]`), proving the new synthetic take was visible across its slot.
- The original project was restored and disposable test projects were deleted.
- ElevenLabs authenticated retrieval was not tested against a real account.
  HTTPS ingest has fixture tests; API lookup uses the documented GET endpoint.
  No paid generation was submitted. Host vision remains a required real review
  step; tests do not invent character-consistency judgments.

The live harness also caught two build-specific behaviors: integral FPS must be
formatted as `24` rather than `24.0`, and native still appends ignore requested
extents. The implementation handles both as described above.
