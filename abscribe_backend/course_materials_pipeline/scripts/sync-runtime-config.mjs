/** Read-only S3 synchronization of the existing authoritative runtime configuration. */
import { existsSync, readFileSync, rmSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { WORK_ROOT } from './pipeline-paths.mjs';
import { createPublicationCloud } from './publication-cloud.mjs';
import { atomicWrite, assertNoPendingPublication, hash, jsonBytes, pathsFor, readJson, stateDirectory, withPublicationLock } from './publication-state.mjs';
export function runtimeTarget() {
  return { bucket: process.env.COURSE_DOCS_RUNTIME_BUCKET, region: process.env.COURSE_DOCS_RUNTIME_REGION || 'us-west-1', prefix: (process.env.COURSE_DOCS_RUNTIME_PREFIX || 'documents').replace(/^\/+|\/+$/g, '') };
}
function finishSync(repo, pending, write) {
  const p = pathsFor(repo), state = stateDirectory(repo);
  const paths = { manifest: p.manifest, plans: p.plans, baseline: join(state, 'baseline.json'), ledger: join(state, 'runtime.json') };
  if (pending.version !== 1 || Object.keys(pending.files || {}).sort().join(',') !== Object.keys(paths).sort().join(',')) throw new Error('Invalid interrupted configuration sync; retain state for operator recovery');
  const writes = Object.entries(paths).map(([name, path]) => {
    const file = pending.files[name];
    const bytes = Buffer.from(file.body, 'base64');
    const actual = existsSync(path) ? hash(readFileSync(path)) : null;
    if (actual !== file.before && actual !== hash(bytes)) throw new Error(`Local ${name} changed during interrupted configuration sync; retain pending sync and reconcile unpublished edits`);
    return { path, bytes, actual };
  });
  // Check the entire set first; a genuine local edit must not cause more partial writes.
  for (const { path, bytes, actual } of writes) if (actual !== hash(bytes)) write(path, bytes);
  rmSync(join(state, 'config-sync.json'));
  return pending.result;
}
export async function syncRuntimeConfig({ repo = WORK_ROOT, cloud, prefix = runtimeTarget().prefix, target = runtimeTarget(), write = atomicWrite }) {
  return withPublicationLock(repo, async () => {
    assertNoPendingPublication(repo, null, { allowConfigSync: true });
    const state = stateDirectory(repo), p = pathsFor(repo);
    const pendingPath = join(state, 'config-sync.json'), pending = readJson(pendingPath);
    if (pending) {
      if (JSON.stringify(pending.target) !== JSON.stringify(target)) throw new Error('Runtime target changed during interrupted configuration sync; reconcile the saved target first');
      // Finish the saved local update even if S3 is offline or changed since the interruption.
      return finishSync(repo, pending, write);
    }
    const previous = readJson(join(state, 'baseline.json'));
    if (previous?.target && JSON.stringify(previous.target) !== JSON.stringify(target)) throw new Error('Runtime target changed; use a separate materials data directory or explicitly reconcile its saved state');
    const ledger = readJson(join(state, 'runtime.json'));
    const manifest = await cloud.get(`${prefix}/manifest.json`), plans = await cloud.get(`${prefix}/coursePlans.json`);
    if (!manifest || !plans) throw new Error('Runtime S3 manifest.json or coursePlans.json is missing; no empty catalog was created');
    const m = JSON.parse(manifest.body), c = JSON.parse(plans.body);
    if (!m || Array.isArray(m) || typeof m !== 'object' || !Array.isArray(c)) throw new Error('Runtime manifest/coursePlans has an invalid format');
    for (const [file, remote, saved] of [[p.manifest, manifest, ledger?.manifestHash], [p.plans, plans, ledger?.coursePlansHash]]) {
      if (existsSync(file) && ![saved, hash(remote.body)].includes(hash(readFileSync(file)))) throw new Error('Local working configuration has unpublished edits; reconcile them before syncing runtime configuration');
    }
    const result = { courses: c.length, documents: Object.keys(m).length, manifestHash: hash(manifest.body), coursePlansHash: hash(plans.body) };
    const files = {
      manifest: [p.manifest, manifest.body], plans: [p.plans, plans.body],
      baseline: [join(state, 'baseline.json'), jsonBytes({ target, manifest: m, plans: c, syncedAt: new Date().toISOString() })],
      ledger: [join(state, 'runtime.json'), jsonBytes({ manifestHash: result.manifestHash, coursePlansHash: result.coursePlansHash, managed: ledger?.managed || {}, verifiedAt: new Date().toISOString() })],
    };
    // A small local intent record distinguishes this sync's partial writes from real edits.
    write(pendingPath, jsonBytes({ version: 1, target, result, files: Object.fromEntries(Object.entries(files).map(([name, [path, bytes]]) =>
      [name, { before: existsSync(path) ? hash(readFileSync(path)) : null, body: bytes.toString('base64') }])) }));
    return finishSync(repo, readJson(pendingPath), write);
  });
}
async function main() {
  const target = runtimeTarget();
  console.log(JSON.stringify(await syncRuntimeConfig({ cloud: createPublicationCloud(target), target }), null, 2));
}
if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) main().catch(error => { console.error(error.message); process.exitCode = 1; });
