# Approved live pilot — passed 2026-10-15

The instructor explicitly approved this isolated paid processing/publication test
and three synthetic Oski turns. They selected the local frontend/API and deferred
hosted deployment/login testing. No AWS resources were created, no Lambda code was
deployed, and no OATutor source files or existing Anthropology material were changed.

## Actual targets

- Frontend: `http://127.0.0.1:5174/#/course-materials`; API: loopback port `8082`.
- PDF: `/private/tmp/oski-live-test/output/pdf/oski-reference-test.pdf`, one page,
  2,157 bytes; SHA-256
  `8dcf6ca9c9b84675c5044b2a5287e65575516bf98cda2cf023d51f6d34c9eed4`.
- Marker: `AMBER-LANTERN-427`. Synthetic example: three panels, two paper stars
  per panel, six stars total. This is test content, not course instruction.
- New server ID: `ph-20724984daa043cb90a89b93e3617214`.
- Title: `Oski Course Materials — Isolated Verification`; author:
  `Synthetic pilot reference`; type `textbook`; visibility `private_tutor`.
- New course: `Oski: Course Materials Verification`; lesson:
  `ph-verification-20261015-amber-lantern`; Office Hours:
  `office-hours--oski-course-materials-verification`.
- Negative check: existing `Data 8 Discussion Worksheets`, lesson `Lesson Disc1`,
  ID `4GULy8Bk-yEIe-u8BrmlWIqG` (no bindings added).
- AWS profile `oatutor-docs`; STS-confirmed account `127859764376`, identity
  `arn:aws:iam::127859764376:user/gharijanto`.
- Staging `oatutor-course-docs-bucket`, `us-west-2`; BDA project
  `arn:aws:bedrock:us-west-2:127859764376:data-automation-project/c41dab0e65bf`;
  BDA profile
  `arn:aws:bedrock:us-west-2:127859764376:data-automation-profile/us.data-automation-v1`.
- Existing OpenAI semantic compiler; runtime `oatutor-runtime-course-docs`,
  `us-west-1`, prefix `documents`; existing Lambda `oatutor-ai-agent`.

## Executed workflow and evidence

1. Synced the current S3 baseline into PromptHive-managed storage. Under the
   existing shared lock, added only the approved isolated test course. Saved
   before snapshots. No existing course/manifest entry was modified.
2. Used the browser form to choose the PDF, metadata, course and lesson, and
   click Upload. Verification automatically queued the durable job. Refreshed
   during real extraction; the same job remained visible.
3. The existing worker ran PromptHive's bundled compiler and validator. Actual
   BDA invocation:
   `arn:aws:bedrock:us-west-2:127859764376:data-automation-invocation/a49428fc-b242-43e8-9e4c-3d016b72213c`.
   Semantic compilation succeeded; validation returned zero errors/warnings.
4. Restarted the worker **after validated review output was saved**. The same
   SQLite job, awaiting-review state and compiled hash survived. This does not
   claim an active BDA process was killed and resumed without another charge.
5. Reviewed all three bounded content pages and the real extracted image in the
   local UI. Clicked the confirmation and **Approve & Publish**. The worker ran
   `npm run publish-docs`; its internal 47 pipeline tests and preflight passed.
6. Publication uploaded the new compiled JSON and source image, then shared
   manifest and coursePlans, and verified the artifacts/configuration before
   setting Published. The publication journal has phase `verified`.
7. Refreshed/navigated away and back: Published persisted. Desktop 1440 px and
   mobile 390 px layouts were checked; neither had horizontal overflow.
   Existing authoring navigation still opens the Google Sheets authoring screen.
8. Independently read runtime S3 and compared parsed configuration: all **154
   prior manifest entries and 22 prior courses** are identical; exactly one new
   manifest entry and one isolated course were added. Other local QA uploads
   remain uploaded and were not compiled/published.
9. Waited beyond the deployed cache TTL of `300000` ms, then exercised the
   existing Oski Lambda. Three completed synthetic turns selected the new
   reference in the lesson and Office Hours, and excluded it in the unrelated
   course. Scoped CloudWatch `document_context_loaded` logs confirm these exact
   allowlists, selected document IDs, no missing documents and no errors.
   Initial requests lacking the required `session_` prefix were rejected before
   retrieval/model execution; corrected requests completed the three turns.

Both positive answers cited the marker and the PDF's two-stars-per-panel fact.
Oski retained its tutoring behavior and asked the student to calculate the total;
it did not directly reveal six. The unrelated answer reported the reference
unavailable; its retrieval log has empty allowed/selected arrays. Lexical retrieval
worked with no generated embeddings. No runtime format/retrieval change was made.

Publication verified at `2026-10-15T22:26:42.586Z`:

- Compiled SHA-256:
  `183798543e6b2319e327f84f295bb72828ce7c6ab791394bcf24cd0a5ded2cfe`.
- Source image SHA-256:
  `85a9c99066c589760641c7b06a8e83dd2c2bb6d4497b93923e0ed0ae2405b846`.
- Manifest SHA-256:
  `e60caf9b9ea85163c57e589157d1f1465716cabe669913b88e81e5922203d144`.
- Course plans/version SHA-256:
  `817ea7927f1fe3d75dfac2b9ac0c5c0e671a4257a1eef959e257609254394e19`.

Persistent job, review, publication receipt and private logs remain in
`abscribe_backend/instance/course-materials/ph-20724984daa043cb90a89b93e3617214/`.
Local test snapshots, requests/responses, scoped logs and screenshots are in
`/private/tmp/oski-live-test/live-evidence/` (temporary evidence; copy elsewhere
before OS cleanup if needed). No credentials were included in these evidence files.

## Verification and remaining boundaries

Fresh checks: **63 backend materials tests**, **43 frontend tests**, Node 22
production build to `/private/tmp/cm-live-test-build`; **47 pipeline tests** also
passed inside the real CLI publication. Test fixtures now explicitly disable live
switches instead of inheriting enabled operator configuration.

The API/worker remain running locally with processing/publication enabled and the
existing server-side credentials. The test course/material remain published.
Hosted login, hosted authenticated images and hosted publishing are **unverified**
and were explicitly deferred. This pilot uses loopback development authentication.
Durability requires preserving the single-host SQLite/data directory; no claim is
made about ephemeral/multiple hosts. Active-process crash/retry was not exercised.

Use PromptHive as the sole publisher for this pilot. The original OATutor publisher
remains untouched and must not publish stale shared configuration. The local lock
cannot constrain an independent external writer or arbitrary manual file edits.
No additional paid calls or publications are authorized by this test's completion.

## Cleanup completed 2026-10-15 (Pacific)

The instructor approved removal of the isolated verification test. Its runtime
manifest entry and entire synthetic verification course were removed using the
existing writer lock, conditional S3 writes, and read-back verification. Only the
test document's compiled JSON and referenced image were deleted from runtime S3;
its BDA staging prefix was confirmed empty. Unrelated manifest entries, courses,
and lesson bindings were preserved. The original OATutor checkout contains none
of this test's IDs or course name and was not modified.

All four verification job records were removed from the active SQLite store.
Private recovery archives, including original records, uploaded files, compiled
artifacts, the verified publication journal, and before/after cleanup receipts,
remain under `abscribe_backend/instance/course-materials-cleanup/` (ignored by Git).
There is no unresolved publication journal from this test. All 154 real source
PDFs and unrelated compilation outputs remain in the pipeline data directory.
The successful ingestion/retrieval findings above are historical; the verification
material is now unavailable after Oski's configuration cache refreshes.
