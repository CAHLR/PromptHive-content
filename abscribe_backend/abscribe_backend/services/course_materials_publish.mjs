// Publish through npm run publish-docs, under the same lock as manual CLI writers.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
const [pipeline, repo, workspace] = process.argv.slice(2);
const load = name => import(pathToFileURL(join(pipeline, 'scripts', name)).href);
const { publishReviewed } = await load('course-materials-cli.mjs');
const { atomicWrite, jsonBytes, readJson, stateDirectory } = await load('publication-state.mjs');
const { createPublicationCloud } = await load('publication-cloud.mjs');
const record = JSON.parse(readFileSync(join(workspace, 'publish-input.json')));
const resultPath = join(workspace, 'publication-result.json');
try {
  const options = { repo, workspace, record,
    cloud: createPublicationCloud({ bucket: process.env.COURSE_DOCS_RUNTIME_BUCKET, region: process.env.COURSE_DOCS_RUNTIME_REGION || 'us-west-1' }),
    prefix: (process.env.COURSE_DOCS_RUNTIME_PREFIX || 'documents').replace(/^\/+|\/+$/g, '') };
  const result = await publishReviewed(options);
  atomicWrite(resultPath, jsonBytes({ ok: true, publication: result }));
} catch (error) {
  let journal = null, known = false;
  try { journal = readJson(join(stateDirectory(repo), 'cli-publication.json')); known = true; } catch { /* Unknown state requires reconciliation. */ }
  const owned = journal?.sourceDocumentId === record.documentId;
  const oldJournal = known && readJson(join(stateDirectory(repo), `${record.documentId}.json`));
  const uncertain = error.activation === 'uncertain' || !known || owned && ['activating', 'verified'].includes(journal.phase);
  atomicWrite(resultPath, jsonBytes({ ok: false, error: String(error.message), activation: uncertain ? 'uncertain' : 'not_activated',
    phase: error.phase || (owned ? journal.phase : 'preflight'), journalExists: owned || Boolean(oldJournal) || !known }));
  process.exitCode = 1;
}
