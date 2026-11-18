# Historical single-material publisher recovery

Historical reference only, retained to preserve the previous runbook. The current PromptHive worker uses bundled code and ordinary managed storage; it does not follow these Git paths or automatically convert historical journals. No old publication journals existed in this installation at relocation. If another installation has one, preserve it and reconcile/migrate that specific operation before switching workers. The original OATutor files remain available unchanged; they are not a runtime dependency of PromptHive. See COURSE_MATERIALS.md for the current procedure.

### Publication recovery and persistence runbook

Ingestion failures can be reassigned and retried. A publication failure, queued
publication, uncertain activation, or existing journal **freezes original metadata
and assignments**. Reconcile publication requeues the original approval; ordinary
assignment edits/reapproval return 409. Neither the UI nor an operator utility
silently cancels, rebases, or deletes a pending journal. Durable activation-attempt
state survives later retry-stage changes; preflight failures cannot label a
previously uncertain activation as definitely inactive.

The material details show parent/content-submodule commits and the private archive
path relative to the Git common directory. New receipts include archive/journal
relative paths. The **publishing host operator/repository maintainer** owns normal
integration of both commits: integrate/push the content-submodule commit before
integrating the parent gitlink/manifest/registry commit through the team's normal
review process. This feature never pushes automatically.

Back up, as one consistent snapshot while the worker is stopped or drained:

1. API SQLite database (use SQLite backup or stop writers), instructor accounts,
   source PDFs, immutable review metadata, compiled/assets workspaces, original
   `publish-input.json`, and private worker logs.
2. The authoritative checkout, parent Git common directory including all unpushed
   commits, content-submodule Git directory/commits, and Git configuration/identity.
3. `<git-common-dir>/course-publications/`: runtime ledger, **all pending/verified
   journals**, and private artifact archives. A Git clone/push does not carry this
   directory. Back up server configuration/credentials through approved secret
   management, never in the repository.

To restore another single publishing host: stop every old CLI/API/worker writer,
restore these snapshots, restore submodule Git metadata with the same relative
layout, update absolute OATUTOR_REPO_PATH/DATA_DIR/secret configuration, verify
Node/dependencies and Git identity, and rebuild review indexes locally. Pending
journals rebind absolute content-repo paths only if their relative layout matches.
Do not run writers simultaneously on old/new hosts. Read-only inspection first:

```sh
# OATutor/aws/aiAgentGeneration; use the existing runtime AWS environment
node scripts/publication-recovery.mjs --document-id ph-<UUID>
# For the existing CLI publisher's pending journal:
node scripts/publication-recovery.mjs --document-id cli
```

Inspection reports saved phase, remote base/target/conflict classification,
verified/missing artifact hashes, commits, journal and archive paths. It reads S3
but does not write it. If base/target states match the journal, use the UI's
Reconcile publication; remote verification determines whether activation already
succeeded. For CLI recovery, restore the exact saved CLI configuration and retry
that coordinated CLI command. Do not bulk-publish unrelated content to resolve a
single-material job.

If a historical version already cleared approval/changed assignments after a
failed publication, the explicit operator repair restores the original approved
record from private `publish-input.json`, verifies its full metadata fingerprint
against the journal, checks current instructor permissions, saves a private backup
of the damaged SQLite record, and queues ordinary worker reconciliation:

```sh
# This queues publication; run only with authorization for the pending live job.
poetry run python -m abscribe_backend.services.course_materials_recovery \
  --restore-original-approval ph-<UUID>
```

If the original approver is revoked, an active course instructor can review that
**exact original version**. The trusted host operator may add `--approved-by <id>
--confirm-original-reviewed`; this changes only the approval actor/time, never the
journal fingerprint, content or assignments. The named instructor must be active
and scoped to the course. Hosted endpoints do not offer this administrative repair.
If saved input/artifacts are missing, restore them from the consistent backup first.
For a genuine remote conflict, preserve current working state and the journal's
base/target snapshots, resolve the conflicting writer with the repository maintainer,
then rerun inspection and original-version reconciliation. No blind overwrite or
journal deletion is a supported recovery procedure.

Subsequent CLI publications retain UI-managed entries via the committed registry,
private archive restoration, assignment checks and conditional runtime baseline.
Tests include an actual CLI publication path with mocked S3, recovery followed by
a second publication, lost activation responses, and restoration on another host.

Final hardening verification: the browser used an isolated, explicitly marked
TEST/DEMO extraction with a 5.25 MB single learning object. Desktop/mobile paging,
section changes, stale-version rejection, authenticated image loading, advanced
JSON open/close, sign-in persistence, logout and revocation passed. Normal content
pages rendered at most 12 fragments (~18,000 characters); closing advanced view
removed its raw JSON DOM. A backend regression test serves a multi-megabyte single
object with less than 2 MB of traced request allocations after worker indexing.
These checks demonstrate bounded API/DOM work, not a full browser heap benchmark.
No synthetic result was published or presented as a real cloud extraction.
