import { WORK_ROOT } from './pipeline-paths.mjs';
/** Read-only S3 baseline check; --initialize records matching hashes locally.
 * Run on the durable publishing host BEFORE adding unpublished course config.
 * Never adopts differing runtime/repository configuration or writes to S3.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createPublicationCloud } from './publication-cloud.mjs';
import { assertManagedRetained, assertNoPendingPublication, hash, pathsFor, runtimeBaseline, withPublicationLock, writeRuntimeLedger } from './publication-state.mjs';
export async function checkPublicationBaseline({ repo, cloud, prefix = 'documents', initialize = false }) {
  return withPublicationLock(repo, async () => {
    assertNoPendingPublication(repo);
    const managed = assertManagedRetained(repo);
    const baseline = await runtimeBaseline(repo, cloud, prefix);
    const p = pathsFor(repo), manifest = readFileSync(p.manifest), plans = readFileSync(p.plans);
    if (!baseline.manifest || !baseline.plans || hash(baseline.manifest.body) !== hash(manifest) || hash(baseline.plans.body) !== hash(plans)) throw new Error('Baseline initialization requires byte-identical existing runtime and repository configuration');
    if (initialize) writeRuntimeLedger(repo, manifest, plans, managed);
    return { initialized: initialize, manifestHash: hash(manifest), coursePlansHash: hash(plans) };
  });
}
async function main() {
  if (!process.env.COURSE_DOCS_RUNTIME_BUCKET) throw new Error('Set COURSE_DOCS_RUNTIME_BUCKET and the existing AWS profile/region');
  const repo = WORK_ROOT;
  const result = await checkPublicationBaseline({ repo, initialize: process.argv.includes('--initialize'),
    cloud: createPublicationCloud({ bucket: process.env.COURSE_DOCS_RUNTIME_BUCKET, region: process.env.COURSE_DOCS_RUNTIME_REGION || 'us-west-1' }),
    prefix: (process.env.COURSE_DOCS_RUNTIME_PREFIX || 'documents').replace(/^\/+|\/+$/g, '') });
  console.log(JSON.stringify(result, null, 2));
}
if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) main().catch(error => { console.error(error.message); process.exitCode = 1; });
