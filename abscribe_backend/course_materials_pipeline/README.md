# PromptHive course-document pipeline

This package contains the OATutor document pipeline copied into PromptHive:
BDA extraction, semantic compilation and providers, schema validation, optional
embeddings and their runtime flattening helpers, preflight/publication and shared
single-host coordination. `UPSTREAM.json` records the original file hashes.
Extraction/compiler/providers/runtime helpers are unchanged. Path adapters separate
this package's code/dependencies from writable material data. No OATutor checkout,
Git repository, content submodule, credentials file or document archive is bundled.

Use Node 22 and `npm ci` here. Commands remain `compile-docs -- --doc <id>`,
`validate-docs -- --doc <id>`, optional `embed-docs -- --doc <id>`, and `publish-docs`.
The existing Python worker invokes them with the shared server environment.
`sync-config` only reads the configured runtime S3 manifest/coursePlans and writes
local working copies; it never writes AWS. `npm test` uses isolated fixtures/mock
cloud clients, never live extraction/publication. Both CLI entrypoints and adapters
use the same local lock. Arbitrary manual edits/other publishing hosts are outside it.

Data defaults to `../instance/course-materials/pipeline`, or
`COURSE_MATERIALS_PIPELINE_DATA_DIR`; `COURSE_MATERIALS_DATA_DIR` changes the parent.
`documents/raw`, `compiled`, `assets`, `bda-raw`, `documents/manifest.json`,
`coursePlans.json` and `publication-state/` belong there, never in the code package.
Back up the complete durable materials data directory with the existing SQLite/jobs.

Existing S3 materials not present locally are retained by exact baseline manifest
entries and checked for remote compiled identity/existence during publication.
They are not downloaded as private archives or bulk-uploaded again. Local approved
output uses the original keys/format. The existing publisher uploads local assets/
compiled files, then the merged manifest, then coursePlans last, verifies local
artifact and shared configuration hashes, and returns the existing receipt.
Conditional writes and baseline hashes reject newer remote configuration. Pending
journals prevent baseline refresh and remain available for normal reconciliation.

See the repository's COURSE_MATERIALS.md for startup, environment and recovery.
