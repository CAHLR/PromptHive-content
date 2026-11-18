import { WORK_ROOT } from './pipeline-paths.mjs';
/** Operator diagnostics only. Reads S3; never uploads, deletes or discards journals. */
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createPublicationCloud } from './publication-cloud.mjs';
import { hash, readJson, stateDirectory, withPublicationLock } from './publication-state.mjs';
export async function inspectPublication({repo, documentId, cloud, prefix = 'documents'}) {
  if (documentId !== 'cli' && !/^ph-[a-f0-9]{32}$/.test(documentId)) throw new Error('Expected a managed ph-<UUID> document ID');
  return withPublicationLock(repo, async () => {
    const directory = stateDirectory(repo), path = join(directory, documentId === 'cli' ? 'cli-publication.json' : `${documentId}.json`);
    const journal = readJson(path); if (!journal) throw new Error('Publication journal not found');
    const manifest = await cloud.get(`${prefix}/manifest.json`), plans = await cloud.get(`${prefix}/coursePlans.json`);
    const classify = (value, target, base, baseHash) => {
      const actual = value ? hash(value.body) : null;
      return actual === target ? 'target' : actual === (baseHash || (base ? hash(Buffer.from(base, 'base64')) : null)) ? 'base' : 'conflict';
    };
    const remote = {manifest: classify(manifest, journal.manifestHash, journal.baseManifest, journal.baseManifestHash), plans: classify(plans, journal.coursePlansHash, journal.basePlans, journal.basePlansHash)};
    let verifiedArtifacts = 0; const problems = [];
    for (const [rel, expected] of Object.entries(journal.artifacts || {})) {
      const actual = await cloud.get(`${prefix}/${rel}`);
      if (actual && hash(actual.body) === expected) verifiedArtifacts++; else problems.push(rel);
    }
    return {documentId, phase:journal.phase, activation: remote.plans === 'target' ? 'target_bindings_present' : remote.plans === 'base' ? 'base_bindings_present' : 'conflicting_configuration', remote,
      artifactCount:Object.keys(journal.artifacts || {}).length, verifiedArtifacts, artifactProblems:problems,
      parentCommit:journal.commit || null, contentCommit:journal.contentCommit || null,
      journalPath:path, archivePath:join(directory,'artifacts',documentId),
      nextAction: remote.manifest === 'conflict' || remote.plans === 'conflict' ? 'Operator must resolve the conflicting configuration against saved base/target bytes; retain journal and last working state.' : 'Use Reconcile publication with the original approval; do not change assignments or delete the journal.'};
  });
}
async function main() {
  const i=process.argv.indexOf('--document-id'); if(i < 0) throw new Error('Usage: node scripts/publication-recovery.mjs --document-id ph-<UUID>');
  if (!process.env.COURSE_DOCS_RUNTIME_BUCKET) throw new Error('Configure the existing runtime bucket/profile');
  console.log(JSON.stringify(await inspectPublication({repo:WORK_ROOT,documentId:process.argv[i+1],cloud:createPublicationCloud({bucket:process.env.COURSE_DOCS_RUNTIME_BUCKET,region:process.env.COURSE_DOCS_RUNTIME_REGION || 'us-west-1'}),prefix:(process.env.COURSE_DOCS_RUNTIME_PREFIX || 'documents').replace(/^\/+|\/+$/g,'')}),null,2));
}
if(process.argv[1] && pathToFileURL(resolve(process.argv[1])).href===import.meta.url) main().catch(e=>{console.error(e.message);process.exitCode=1;});
