# Native Agent Bridge: implementation status

Status: **initial Linux profile implemented and tested**, developed on `mine`. This is a compiled service with built-in
Python scene adapters, not a Blender add-on or an MCP server. Broader profiles in the
[design target](native_llm_bridge.md) remain explicit future work below.

## Build and use

Use the existing system-dependency build profile:

```sh
cmake -S . -B build-system
cmake --build build-system --parallel 8 --target install
./build-system/bin/blender
```

On Linux with embedded Python, `WITH_AGENT_BRIDGE` defaults ON. Blender starts the local service
automatically after window-manager initialization, in GUI and background mode, preserving normal
preferences and add-ons. `--disable-agent-bridge` suppresses automatic startup for that process;
`--agent-bridge` enables it explicitly. These startup settings are parsed before command-line
scripts/files, and the last setting wins. Arguments following `--` belong to the caller's script.
Unsupported platforms default to an excluded bridge; explicit `-DWITH_AGENT_BRIDGE=OFF` still works.

**Stop Native Agent Bridge** or the **Agent** sidebar can stop it during a session. A file load
does not restart a stopped service. Start remains available in operator search/the sidebar.
Private workers explicitly pass `--disable-agent-bridge` and expose no extra listener. Background
process lifetime is unchanged: a batch command still exits normally when its work finishes.

In-process configuration and recipe registration use `bpy.agent_bridge`. For a trusted project:

```python
import bpy
bpy.agent_bridge.stop()  # Stop the automatically started service before changing its workspace.
bpy.agent_bridge.start(workspace="/absolute/project/path", allow_scripts=False)
print(bpy.agent_bridge.status()["descriptor"])
```

The descriptor is private and contains credentials; do not copy it into reports or project files.
The public status contains its path, not the token. Standard-library client example:

```sh
PYTHONPATH=scripts/modules python3 -m bl_agent.client \
  --session /tmp/blender-agent-EXAMPLE/session.json bridge.status
```

`--params parameters.json --wait job.submit` submits a job and waits for its result. Inspection and
operations use explicit scene/view-layer context. Mutations require the session, scene epoch,
revision and geometry fingerprint returned by `scene.inspect` with `fingerprint: true`.

## Implemented paths

| Area | Current behavior | Evidence / limit |
| --- | --- | --- |
| Native service | Private Unix socket, OS randomness, peer credentials, token-authenticated hello, bounded framed JSON-RPC, native thread/queue/job ownership and shutdown join. An OS eventfd wakes the I/O thread for completed replies. | Linux only; initial native build and local install pass. |
| Main-thread dispatch | Compiled BLI timer, persistent across file loads, calls built-in Python adapter. No Python networking thread. | Background tests explicitly pump the native dispatcher; GUI test uses the actual event-loop timer. |
| Context | Explicit scene/view layer; scoped window-scene switching and restoration for native operators. | Cross-scene regression plus real Boolean roof construction match. |
| Jobs | Native operation keys, deduplication, queued/running/ready/applying/terminal states, compact job lists/events, cooperative cancel, registered wall-time limits and progress. Native `job.wait` returns completion or a bounded deadline response without Python polling. | Same-process reconnect works while Python is busy. Waiters are bounded and do not occupy Blender's main thread. Automatic crash replay is absent. |
| Inspection | Bounded object/material/count and evaluated-instance summaries, original-ID handles, transform/depsgraph deltas, epoch reset on load/undo and opt-in geometry fingerprints. Recipe schemas support conditional digest revalidation. | Fingerprint covers coordinates, topology, UVs, transforms and modifier names/types; not full material/node/animation state. |
| Recipes | File-backed callables, parameter schemas, dependency hashes, optional trusted workspace scripts, live undo operator and isolated worker recipes. | Live scope is a trusted recipe contract, not enforcement or a Python sandbox. Worker functions must be file-backed top-level callables without closures. |
| Staging | Frozen scene, declared source files and supported images; up to two private worker processes. Ready candidates support apply/discard, scoped checkpoints and restore. | First apply profile supports local flat collections of mesh/empty IDs. See the ownership limits below. |
| Captures | Bounded evaluated mesh occurrences, including nested/mirrored collection instances; named orthographic views, clay/material appearance, Cycles CPU or Workbench GPU. Cycles provides raw EXR ID/world-normal/Z-depth, PNG previews and capture-specific pixel picking. | Uses the selected **VIEWPORT** depsgraph; render-only modifier differences need another profile. Material proxies reject linked identity-dependent shader inputs. Lighting is for construction review. Temporary IDs may dirty the file. |
| Artifacts | Persistent private indices, owned handles, chunk reads, hashes/signatures, inline-result externalization above 64 KiB, cache admission/reservations and explicit release for current/stopped sessions. | Budgets cover controlled outputs and a worker file-size limit/watchdog; this is not an OS filesystem quota or a sandbox for arbitrary trusted Python. Unknown/modified files are preserved. |
| Recovery | Bounded native event ring; private accepted-parameter/outcome journal; declared execution manifests; prior-session interruption summaries, artifact reload and current-session scoped checkpoint restore. | Appends are asynchronous with periodic sync, not a mutation WAL. Exhaustion is explicit in status. Crash tails can be lost; live/applying effects may be unknown. Old checkpoints require deliberate inspection/reopen, not pointer resurrection. |
| UI/client | Start/Stop, job phases/progress, cancel/apply/discard, evidence links and checkpoint buttons. In-process submission uses the same native registry without a socket. | GUI undo/redo and the restore operator are tested. An MCP adapter and provider/model integration are outside this implementation. |

Current limits: 8 MiB frames, eight clients, eight outstanding read requests per client, 64 queued
requests, 128 retained jobs, 512 KiB job parameters, 512 artifact records and 1 MiB artifact-read
chunks, a 1,024-event ring and a journal cap up to 16 MiB. At most 64 native waiters and eight
outstanding requests per client are retained; a wait deadline is at most 25 seconds. Exhaustion is explicit; job eviction
is not implemented. `events.poll` rejects expired cursors with `RESET_REQUIRED`. A registered worker
timeout defaults to 300 seconds, bounded to 1–3,600 seconds. The active polling interval is
20 ms, with a 4 ms dispatch budget **between** calls, not preemption of a blocking call.

Authenticated hello uses a main-thread metadata cache so a reconnect can query/cancel jobs while
Python is busy. It explicitly marks `scene_state_cached`; use `bridge.status` or `scene.inspect`
for current scene state. Main-thread scene queries wait behind a live blocking operation.

The supported schema subset is object/array/string/boolean/number/integer types, properties,
required, additionalProperties=false, enum, numeric min/max and array item/count constraints.
Other JSON Schema vocabulary has no promised semantics.

Scene operations require Object mode, no active render and no modal operator in any window.
Recursive dispatch/start/stop from an executing recipe is rejected. The GUI timer defers its own
cleanup if a blocking operator processes nested events. Stopping drains outgoing replies for at
most 250 ms; a non-reading client cannot keep the service alive indefinitely.

## Capture correspondence and storage

`scene.inspect(include_instances=True)` and `capture.render` use the same bounded visibility census.
An occurrence records its source ID, outer instancer, persistent ID and exact evaluated matrix.
Nested hidden branches sharing a prototype are excluded. The current bounds are 4,096 mesh
occurrences and 16,384 total evaluated entries. Worker tables use candidate semantic keys; they do
not fabricate live handles. Captures also record frame/subframe, units, projection, actual camera,
color settings, construction lighting and a settings digest.

ID/depth are Cycles unfiltered first-sample geometric hits with pass alpha threshold zero; zero
means miss. Normals are Cycles world shading normals averaged over samples; boundary values can
be non-unit. Pixel coordinates are integer top-left coordinates. `capture.pick` resolves through
the selected artifact's table and returns captured pose/provenance plus
`requires_live_revalidation: true`. It does not pretend an old image describes a moved object.
Raw data stays linear in EXR; display previews have separate documented encoding.

Material capture uses temporary evaluated mesh proxies and preserves UVs, attributes, object
color/properties and material-slot overrides. It rejects linked Object Info Random, indexed
Object Info Object Index, particle information and instancer-dependent attributes/UVs. These
proxies and construction lights are not a universal beauty-render equivalence guarantee.

Default storage limits, configurable through `bpy.agent_bridge.start(storage_limits=...)`, are
256 MiB per file, 512 MiB per job, 2 GiB per session and 8 GiB per workspace. Admission counts
actual files plus live reservations under a shared filesystem lock; PID/start-time leases coordinate
processes. The accounting census aggregates directories once, avoiding a repeated scan for each
retained job. Controlled capture sizes are admitted before rendering; workers have a file-size
limit and an aggregate-output watchdog. Trusted code can still write outside those paths or
transiently exceed an admission estimate; this is not a security/resource sandbox.

`storage.stats` reports use/reservations. Submit `storage.prune` as a normal preconditioned job with
`target_job_id`, optional `target_session_id`, `dry_run` (default true) and
`release_checkpoints` (default false). It refuses running/ready jobs, live workers, candidate input
dependencies, files used by Blender, unknown/changed files and unapproved checkpoint release.
Archived release rechecks that the prior session is inactive under the workspace lock. It never
claims that session's lease. Current-session release retains the idempotency tombstone, so retrying
the old key cannot run the mutation again. Incomplete/unsealed crash output may require manual
inspection; arbitrary files are never adopted merely to make cleanup succeed.

`artifacts.list(session_id=...)` reloads a bounded prior-session index; file reads check its original
signature and still expose its SHA256. Artifacts must belong to their declared job directory.
Sealing cannot re-sign an already registered file after modification, and release checks every
inventory path before deleting any file. Old scene handles remain invalid after restart.

## Staged worker contract

Recipes advertise `execution_modes: ["worker_candidate"]`, an exact
`scope: {"collection_name": "..."}` and optional `object_key` custom-property name. A worker-mode
`job.submit` uses the same recipe/version/hash and scene preconditions as a live operation, plus
`apply_mode: "preview"`. Optional `preview` accepts named views, resolution, engine and appearance.
Poll until `ready`, inspect/download the candidate artifacts, then issue `job.apply` or
`job.discard` with that job ID. Explicitly authorized `auto_if_valid` uses the same apply path.

The adapter freezes the selected scene and hashes its serialized bytes. Declared recipe sources
and dependencies are copied; recipes access them through `context.read_source` or `source_path`.
Packed images and clean single-file image resources are supported; dirty image buffers, linked
libraries and UDIM/movie resources currently require another adapter. Worker processes use the
same executable with a private factory profile and autoexec disabled. The normal parent session's
preferences/add-ons are preserved. Required worker add-ons must be declared explicitly. Python
recipes remain trusted code; this is process isolation, not a filesystem/security sandbox.

Apply verifies session/epoch/revision, geometry fingerprint, the entire serialized scene, target
collection identity, source/dependency bytes and candidate hash. Full-scene comparison is
conservative: even an unrelated scene setting can require rebuilding the candidate. No broad
per-ID read-set completeness is claimed. Matched mesh/empty object IDs retain identity, external
parents continue to reference them, and shared mesh users outside the scope keep the old mesh.
Removing externally referenced objects is rejected. New/changed modifier stacks, animation and
shape-key edits require specialized adapters; compatible existing modifier fields are copied.

Field copying is rehearsed on temporary objects before a source edit. Once commit begins, a
failure reports possible partial effects and includes a checkpoint when available; there is no
universal rollback. Scoped restore checks the target and current preconditions. GUI apply/restore
have real undo steps; background sessions report `undo_available: false`.

Native `events.poll` returns small ordered summaries without outcome payloads. `job.status` accepts
`include_result: false`; the client uses summaries while waiting and fetches the full final result
once. The current client uses native `job.wait` when advertised, with a polling fallback for older
servers. `history.sessions` reads only the configured cache workspace's journal directory and returns
compact state summaries. The **private journal** also retains accepted parameters and outcomes;
`history.job` retrieves one job's details deliberately. Service credentials/hello tokens are not
journaled. Live execution artifacts record declared recipe/source/dependency hashes; workers retain
their frozen-input manifest. These are declared inputs, not a complete trace of arbitrary Python I/O.
Closed/interrupted sessions are diagnostic history; old handles/jobs cannot resume automatically.
Crash tails can be lost, and live/applying effects may require manual inspection/checkpoint reopen.

Worker cancellation remains responsive during a blocking worker bake/render. Cooperative checks
are followed by termination/kill if needed; service shutdown joins owned workers and Linux
parent-death handling terminates a worker when its parent is killed. A live blocking Python call
still occupies Blender's main thread; cancellation is cooperative there.

## Verification completed

```sh
python3 tests/python/agent_bridge/test_protocol.py \
  --blender build-system/bin/blender
```

Twenty-five protocol tests pass in an actual background Blender process: private permissions,
bounded stable inspection, failed authentication with fragmented framing, reconnect/deduplication,
stale-revision rejection, source pinning, cooperative cancellation, handle-preserving rename,
script boundary checks, rendered PNG artifact reads/source preservation, parameter types,
cross-scene native context/restoration, large-result externalization, reconnect/cancel while Python
is running, malformed/combined/deep frames, invalid lengths, slow partial frames, event cursors,
journal contents and explicit partial-edit errors. Tests verify process exit/descriptor cleanup.

`test_workers.py` includes those tests and eighteen staged cases: **43 pass**. They cover
source-preserving preview/apply/discard, stable IDs, shared data/external users, checkpoints,
responsive inspection/cancel, stale candidates, unsupported stacks, existing modifier fields,
automatic apply, the serialized scene guard, in-process submission, worker timeouts and explicit
failure of an over-nested result rather than leaving the job running, instance-pick provenance,
UTF8 bounds, finite raw/evaluated candidates, artifact release/dependencies, storage-file limits,
large ready-result externalization, schema revalidation, native wait deadlines/client limits and
recursive dispatch/stop rejection.
`test_worker_lifetime.py` passes both graceful-stop and actual parent-kill cases and checks recovered
interruption states. `api_lifecycle.py` verifies start/stop/restart handle isolation and truncated
journal recovery, archived checkpoint release and live-file dependency refusal. Fifteen standalone
storage tests cover reservations, aggregate limits, private indices, cross-session reads, ownership,
modified files and release races. Three transport-pressure tests cover the client limit, a slow
reader exceeding the output budget, and bounded stop with unread output. `journal_capacity.py`
fills an 8 KiB journal and verifies explicit exhaustion and complete retained records.
`capture_diagnostics.py` actually renders nested/rotated/mirrored instances, checks normal/depth
against an independent BVH and rejects identity-dependent material capture. These are supported-path
tests, not proof of arbitrary Blender graph transactions.

Startup-default follow-up: all six `test_startup.py` cases pass, covering automatic background/normal-GUI startup,
manual start after opt-out, flag order and the `--` separator, startup policy before scripts,
and file loading after Stop. The normal-GUI case also exercises the actual native timer through
an external client and preserves enabled add-ons. Explicit-service test harnesses use
`--disable-agent-bridge` before configuring their own workspace; worker fixtures still assert
that child listeners stay disabled.
The 43 protocol/worker tests, both worker-lifetime tests and lifecycle script pass with explicit
opt-out in their isolated harnesses. All 312 standard API tests also pass with automatic startup active.

The separate asset workspace contains `scripts/native_bridge/` adapters and selected evidence under
`assets/eternity-forge/experiments/native-bridge/01-native-foundation/`:

- Three roof edits reuse selected definitions from the existing Level 2 blacksmithery continuous
  roof builder, excluding its historical top-level publication/file writes.
- Both reopened roof modules match the fresh-process reference at 1 micrometre coordinate
  precision, are manifold with positive volume, and pass eight sampled section measurements.
- The first failed iteration exposed a real omitted Boolean cut: overriding `scene` alone left
  `window.scene` wrong for the native operator. The correction and rejected renders are preserved.
- Repeated edits took about 104 ms versus 866 ms for a fresh CLI process including startup in the
  recorded Cycles run. The native recipe job itself took 92–98 ms; CLI recipe execution took
  62 ms. These data show process reuse, not a faster geometry kernel.
- Three 512-pixel clay views took 10.59 s with Cycles CPU versus 3.30 s with Workbench GPU in
  separate runs. Workbench showed course edges more clearly in this construction exercise.
- Recorded edit requests, including status polls, used roughly 1.5–1.9 KiB; unchanged-scene delta
  round trips initially took about 20 ms. Adding an OS eventfd wake for I/O reduced the median
  to 2.42 ms (maximum 3.06 ms in 20 queries) in the headless harness with its 2 ms main-thread pump.
  The GUI main-thread timer remains 20 ms; this is not a sub-3-ms GUI latency promise. Bytes are
  measured; exact model token counts and MCP comparisons have not been measured.
- The GUI test used normal preferences, preserved all 11 enabled add-ons, exercised the native
  timer and built-in Start operator, and exited with code 0. The same Vulkan allocation and
  42-block shutdown diagnostics occurred in the service-disabled GUI baseline; these remain
  an independent diagnostic, not a clean-memory claim.
- Four selected blacksmithery master/module files retained their hashes. No catalog asset was
  published or changed. The Blender checkout's three pre-existing modifications remain intact.

These are construction-module experiments, not material/style acceptance or an engine benchmark.
The four original API suites (`bl_pyapi_prop`, `bl_pyapi_prop_array`, `bl_pyapi_idprop`,
`bl_pyapi_mathutils`) pass all 312 tests with the feature compiled in and left disabled. The
command-line `--agent-bridge` start/exit probe also passes. Both feature-OFF and ON configure,
install and startup probes pass. OFF exposes neither API nor panel. Those earlier probes tested
the original opt-in policy; the startup-default follow-up above supersedes that policy. The
selected historical milestone reports/hashes retain their original tested versions.
Slow-output/client limits and a real GUI modal boundary are now exercised. Sanitizer runs and broader
operator/resource profiles remain future work.

The selected second milestone is in the asset repository at
`assets/eternity-forge/experiments/native-bridge/02-staged-workers/`:

- Three isolated roof candidates match the independent CLI construction. Candidate/apply images
  match pixel-for-pixel in three Workbench views. Prepare takes 755–1,608 ms (last includes three
  previews); apply takes 11–36 ms. A fresh CLI build takes 716 ms. Worker isolation is useful for
  responsiveness/inspection; it is not a speed win for these tiny edits.
- Normal-preferences GUI tests preserve 11 add-ons and pass actual undo, redo and checkpoint
  restoration. The earlier default-Python-operator undo failure is retained. Vulkan shutdown
  diagnostics still match the service-disabled baseline; do not claim a clean-memory GUI exit.
- The finished damaged infirmary canopy is exercised at two flap angles. All 18 shared attachment
  samples remain seated, both shells are closed/positive, thickness stays 4 mm, all material UVs
  remain unchanged and the main canvas is stationary. Material renders show the hanging tear.
  The existing-modifier enum-copy failure and its regression are retained.
- A frozen Level 1 blacksmithery door goes through UV packing, isolated basecolor/physical-normal/
  ORM bakes, staged apply and GLB export. The result has 1,304 triangles versus 1,556 evaluated
  source triangles; useful unique UV union is 52.2% at the sample grid. This generic pack is a
  bridge exercise, not an optimal atlas or a replacement for the building's existing runtime.
  A rejected unisolated bake is retained because a fitting projected into the wood.
- Khronos reports no GLB errors/warnings. A fresh Blender process reimports all 1,304 triangles
  with zero position/normal error and maximum UV error 2.98e-8. Actual reimport renders differ
  from the candidate by at most one 8-bit value. These checks do not establish custom-engine use.
- Selected master/module hashes are unchanged. No town master, runtime or catalog was published.
  The checkout's three pre-existing modifications retain their original hashes.

## Diagnostic/storage/efficiency milestone

The third selected review is in the asset repository at
`assets/eternity-forge/experiments/native-bridge/03-diagnostics-efficiency/`.

- A real damaged infirmary canopy is posed through a worker, applied, and captured as 42 mesh
  occurrences of 21 prototypes in two differently transformed instances. Both material and clay
  views retain the hanging tear. All source master/module hashes remain unchanged.
- In oblique/underside views, 34/23 stable pixel hits match an independent world BVH. Maximum
  camera-Z error is 0.000025 m; maximum instance-matrix error is 0.00000048. Pixel-boundary and
  depth-discontinuity samples are deliberately excluded; this is sampled evidence.
- The independent nested-box fixture also verifies a hidden branch sharing the same prototype,
  source/instancer handles, mirrored normals, captured pose after movement and source preservation.
  A real canopy rerun caught an evaluated-object lifetime crash missed by unmodified box meshes.
  The census now retains original IDs/copied values only and capture copies evaluated meshes while
  each iterator entry is alive. A bevel-backed fixture and the complete canopy rerun pass; the
  crash trace is retained. Inspection and capture still use the same visibility decisions.
- Eighteen interleaved roof variants use explicit component dependencies. Reuse requires matching
  parameters, builder hashes and current geometry; a manual edit invalidates the corresponding
  part. Object identities stay stable. Every variant matches a full rebuild at 1 micrometre;
  final reopened geometry matches the independent CLI construction and three views match exactly.
- The latest isolated run has full/reused total-edit medians of 223/203 ms, including inspection,
  provenance and storage. Both require three round trips, about 1 KiB of requests and 3 KiB of
  responses. A fresh CLI build is 1,116 ms including startup. These are small construction edits,
  not universal modelling or rendering speedups. Reuse can be slower when both components change.
  Total-edit maxima were 560/1,413 ms; storage/scheduling variability remains visible. The prior
  isolated run recorded 194/171 ms medians; the latest result is retained rather than selecting
  only the fastest run.
- A pre-optimization run used hundreds of status polls per edit. Profiling exposed repeated path
  containment scans in storage accounting. A single aggregation pass preserves the same quota
  calculation; native completion waiting removes repeated status traffic. Historical timings are
  retained as diagnostic evidence, not a controlled speed ratio across different runs.
- Conditional schema revalidation returns 201/208 bytes versus 999/1,013 bytes for full schemas.
  Wire bytes and round trips are measured; exact model tokens/context cost and a controlled MCP
  comparison are not established. Bridge development/setup cost is recorded separately in the
  selected review rather than omitted from the workflow cost.
- A normal-preferences GUI heartbeat requested every 10 ms had a 10.6 ms median gap. It observed
  a 696 ms startup/drawing gap before any job and up to 112 ms during the staged exercise. These
  include drawing/timer work, not a pure bridge timing attribution or a latency guarantee. All
  11 add-ons, the modal boundary, actual undo/redo and checkpoint restore passed.

## Deliberate limits and follow-up experiments

This is a tested Linux prototype for trusted recipes and the stated local mesh/empty apply and
VIEWPORT capture profiles. General graph transactions, automatic crash resume, arbitrary external
resource closure, render-depsgraph capture, all identity-dependent materials and a security sandbox
are outside this implementation. Broader profiles need their own adapters and regression evidence.
Long live calls/captures can still stall the GUI; workers are the responsive path for heavy work.
Ready candidates use a conservative full-scene guard rather than claiming complete per-ID read sets.

Further experiments can compare model token/context costs against an equivalent MCP workflow,
amortize setup/development over real asset work, test worker reuse, add calibrated beauty capture,
extend checkpoint recovery and run sanitizers. Optional MCP/provider integrations and other operating
systems follow the design; no provider account or API key is embedded in this prototype.
