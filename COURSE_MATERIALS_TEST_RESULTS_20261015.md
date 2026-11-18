# Course Materials tests — October 15, 2026


**Update: the requested retry of `physics7a-17.5` passed at 7:31 PM PDT.**
It reused the saved BDA extraction (`--skip-bda`), reran real semantic compilation,
and passed validation with zero errors/warnings. A subsequent validation of all
154 current compiled documents also passed. All 116 Physics documents now have
passing results. No publication occurred. The original 153/1 batch result below
is preserved as attempt history, not overwritten. Current consolidated results:
`abscribe_backend/instance/course-materials-test-runs/reingestion-20261015/current-document-status.csv`.
Retry logs/result and full validation report:
`abscribe_backend/instance/course-materials-test-runs/retry-physics7a-17.5-20261015-1/`.
The retry changed no manifest metadata, lesson bindings, source PDF or unrelated
compiled document. This was a new model output, not a compiler-code fix; the original
missing-example-ID failure remains recorded for possible robustness improvement.

Both requested tests are complete. No batch publication or runtime S3 configuration
write occurred. PromptHive's copied pipeline was used; no OATutor checkout code was
executed. The implementation was not redesigned or changed for these tests.

## Test 1: existing raw PDFs

Actual configuration was loaded through the application's shared configuration
loader before testing. `COURSE_MATERIALS_DATA_DIR` and
`COURSE_MATERIALS_PIPELINE_DATA_DIR` were unset, so their resolved defaults were:

- Job storage: `/Users/gilbertharijanto/Developer/oatutor-testing/PromptHive/abscribe_backend/instance/course-materials`
- Pipeline storage: `/Users/gilbertharijanto/Developer/oatutor-testing/PromptHive/abscribe_backend/instance/course-materials/pipeline`
- Sources: the pipeline storage's `documents/raw/`
- Manifest: the pipeline storage's `documents/manifest.json`
- Assignments: the pipeline storage's `coursePlans.json`
- Code/command working directory: `/Users/gilbertharijanto/Developer/oatutor-testing/PromptHive/abscribe_backend/course_materials_pipeline`

Inventory found **154 PDFs / 1,776 readable pages**, and 154 manifest IDs. There
were no duplicate JSON keys/document IDs or shared source paths. Initially 141
source paths matched. Thirteen Data 100 PDFs were at
`raw/data100/data100-discNN.pdf`, while their existing manifest entries pointed to
`raw/data100-discNN.pdf`. These were path mismatches, not documents needing guessed
metadata. After explicit batch approval, only those 13 `source` fields were corrected
atomically. All IDs and other metadata were preserved. No genuinely unregistered
PDF remained, and no source file was missing after those corrections.

None of the intended 154 IDs had local BDA cache or compiled output before this run.
Existing other local compiled/assets/cache files were backed up before starting;
the existing remote versions were not replaced. The user approved **fresh BDA
extraction plus semantic compilation for all 154 PDFs**, using the existing
`oatutor-docs` profile/account `127859764376`, staging bucket
`oatutor-course-docs-bucket`, BDA project `c41dab0e65bf` in `us-west-2`, and the
existing OpenAI compiler credentials. No resources were created.

The sequential run held the existing shared writer lock and used argument arrays,
Node 22, the shared environment and explicit package working directory:

```sh
npm run compile-docs -- --doc <existing-document-id>
npm run validate-docs -- --doc <existing-document-id>
```

It did **not** use `--only-missing`, `--skip-bda`, dry-run mode or publish-docs.
`--skip-bda` is the supported alternative for cached BDA recompilation, but was not
used. Cached recompilation still makes semantic-provider calls unless dry-run is
explicitly selected; it should not be described as entirely free processing.

The run completed from **4:31–6:13 PM PDT** (102 minutes), without automatically
retrying any document:

- Data 100: 13 passed.
- Physics: 115 passed, 1 failed validation.
- Chemistry: 11 passed.
- Math 1B: 13 passed.
- Anthropology: 1 passed.
- Total: **153 passed / 1 failed**; **154 fresh BDA extractions completed**, with
  154 distinct invocation ARNs; **zero BDA-cache reuses**.

Every successful result passed both compilation's validation and the separate
validator CLI. Its compiled identity/hash was verified after the run. The only
failed document is **physics7a-17.5**, “17.5 Sources of Musical Sound”:

```
/sections/0/examples/0 must have required property 'example_id'
```

BDA completed, but the semantic output put an object with `note_id`, `text`, and
`visibility` into `examples`, whose schema requires `example_id`. Both CLI commands
returned exit code 1. The invalid compiled output and generated assets were isolated
in this document's report directory; its successful BDA cache remains available.
There was no previous local compiled version for this ID, so the original absence
was restored in the publishable compiled tree. Its existing S3 version remains
untouched. No paid retry or manual artifact repair was performed.

The focused compiler issue is section example-shape enforcement: the copied
semantic compiler passes model `examples` through at
`scripts/semantic-compiler.mjs` around lines 1146–1175, while the schema at
`schemas/learning-object.schema.json` requires `example_id`. A follow-up should
ensure examples receive valid stable IDs without relaxing validation; it should
have a regression for a note-shaped example. This test reports the actual failure
rather than changing the output and retroactively calling the original run a pass.

## Reports, backups and preservation

Durable test directory:
`abscribe_backend/instance/course-materials-test-runs/reingestion-20261015/`

- `per-document-results.csv`: all 154 IDs, titles, courses, types, visibility,
  pages, status, cache mode, BDA ARN, command exit codes, hashes and log paths.
- `results.json`: complete saved results, timestamps, errors/warnings and failure
  log excerpts.
- `inventory-before.json`, `pdf-inventory.json`: inventory and page counts.
- `before/`: original manifest/course plans and existing compiled/assets/BDA data.
- `documents/<id>/`: compiler/validator logs and reports; `before/` and `failed/`
  preserve replaced/failed artifacts where present.
- `preservation-verification.json`: verified source/metadata/configuration safety.
- `runtime-verification.json`: runtime configuration ETags and last-modified times.

Post-run checks verified every original raw-directory file unchanged, all original
manifest IDs/metadata unchanged except the 13 approved source-path corrections,
byte-identical course plans, unchanged existing job rows and recovery files,
unchanged pre-existing assets, valid successful-output hashes, isolated failed
output and released writer lock. The earlier verified publication journal was
preserved; no unresolved recovery state was removed.

Read-only S3 checks confirmed runtime manifest/coursePlans ETags and last-modified
times are unchanged. The current runtime configuration still dates to the earlier
isolated pilot publication. **This batch is not published.**

A pre-existing configuration discrepancy remains: the local manifest no longer
contains earlier published pilot ID `ph-20724984daa043cb90a89b93e3617214`, while
its test lesson still references it. The runtime S3 manifest still contains it.
This was present before these tests and was preserved. Reconcile that local entry
with the authoritative runtime baseline before any future global publication;
do not delete the binding or blindly replace the whole local manifest.

## Test 2: actual UI upload and controlled registration

The real browser used `http://127.0.0.1:5174/#/course-materials` and the actual
local API on port 8082. For this test only, the API process was restarted with
processing and publishing disabled, preventing upload completion from making a
paid call. The shared environment file was not changed. The API has since been
restored to its original configuration; the existing worker is online.

A clearly labelled 1,734-byte temporary PDF was uploaded through the form:

- Title: `TEMP Upload Registration Check 20261015`
- Author: `Temporary QA Author`
- Document type: `discussion_solutions`
- Visibility: `student`
- Course: `Oski: Course Materials Verification`
- Selected lesson: `ph-verification-20261015-amber-lantern`
- Server-generated unique ID: `ph-1fe3feacc18d43f38cf0146b5aa9bed2`
- SHA-256: `e0aaffa6218713e8b5c6c5f2eaf4fa0461a8080fc309f735a45069a7267a7a70`

The browser upload saved exact matching bytes at
`<job-storage>/<id>/source.pdf`, with form metadata in `materials.sqlite3`.
The UI correctly showed **Uploaded**, with verification successful and queueing
unavailable. At this point **no shared raw PDF or shared manifest entry existed**;
shared configuration was byte-identical. This is the implemented lifecycle, not a
registration failure.

A separate controlled test exercised the real `compileMaterial` orchestration,
reproducing the worker's private staging at `<job>/documents/raw/<id>.pdf` and
mocking only its external command boundary to inspect registration and stop before
any cloud processing. It verified:

- Shared source: `<pipeline-storage>/documents/raw/<id>.pdf`, with identical bytes.
- Temporary manifest entry: `source: raw/<id>.pdf`,
  `compiled: compiled/<id>.json`, and exactly the form's title, author, course,
  `document_type` and visibility.
- Every unrelated manifest entry and lesson binding unchanged.
- After the mocked command failure: temporary manifest entry/registration markers
  removed, exact original manifest restored, raw PDF retained for retry.

This is **real browser upload evidence plus separately mocked worker-registration
evidence**, not a successful paid UI compilation. On successful real compilation,
this UI workflow stages compiled/assets privately for review and likewise removes
its temporary manifest entry; publication later registers/activates it. The batch
CLI in Test 1 operates on already registered IDs and keeps their manifest entries.

Cleanup removed only the temporary ID's SQLite row, private job directory, shared
raw PDF and temporary source PDF. No staging S3 objects were created. Exact pre-test
manifest/course plans, all four pre-existing job rows, all 156 existing raw-directory
files and all recovery files were verified unchanged. The UI returned to its
original four materials. No temporary test binding was ever activated.

UI evidence and detailed checks:
`abscribe_backend/instance/course-materials-test-runs/ui-registration-20261015/`
(`REPORT.md`, record/registration/cleanup JSON, before snapshots and screenshots).
No production source changes, deployment or publication were needed for these tests.
