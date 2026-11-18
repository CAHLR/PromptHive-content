import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { join, resolve, dirname, relative } from 'node:path';
import { tmpdir } from 'node:os';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { PACKAGE_ROOT } from './pipeline-paths.mjs';
import { syncRuntimeConfig } from './sync-runtime-config.mjs';
import { pathsFor, stateDirectory, readJson, atomicWrite, jsonBytes, hash, assertNoPendingPublication } from './publication-state.mjs';

function fixture(t) {
  const repo = mkdtempSync(join(tmpdir(), 'prompthive-local-pipeline-'));
  t.after(() => rmSync(repo, { recursive: true, force: true }));
  const manifest = { existing: { source: 'raw/existing.pdf', compiled: 'compiled/existing.json', custom: 'preserve' } };
  const plans = [{ courseName: 'Runtime Course', custom: { keep: true }, lessons: [{ id: 'a', chat_documents: ['existing'] }, { id: 'a', chat_documents: ['existing'] }] }, { courseName: 'Unrelated', lessons: [{ id: 'b' }] }];
  const objects = new Map([['documents/manifest.json', { body: jsonBytes(manifest), etag: 'm' }], ['documents/coursePlans.json', { body: jsonBytes(plans), etag: 'p' }]]);
  let gets = 0;
  const cloud = { async get(key) { gets++; return objects.get(key); }, async put() { throw new Error('Sync must never write S3'); } };
  const target = { bucket: 'TEST', region: 'us-west-1', prefix: 'documents' };
  return { repo, manifest, plans, objects, cloud, target, gets: () => gets, sync: () => syncRuntimeConfig({ repo, cloud, target }) };
}
test('complete relative import graph stays within PromptHive and has no checkout dependencies', () => {
  function visit(dir) { return readdirSync(dir, { withFileTypes: true }).flatMap(e => e.isDirectory() && e.name !== 'node_modules' ? visit(join(dir, e.name)) : e.isFile() && e.name.endsWith('.mjs') ? [join(dir, e.name)] : []); }
  for (const file of visit(PACKAGE_ROOT)) {
    const text = readFileSync(file, 'utf8');
    if (file.includes('test-')) continue;
    assert.doesNotMatch(text, /OATUTOR_REPO_PATH|aws\/aiAgentGeneration|src\/content-sources|git-common-dir/);
    for (const [,ref] of text.matchAll(/(?:from\s*|import\s*\(\s*)['"](\.[^'"]+)['"]/g)) {
      const path = resolve(dirname(file), ref);
      assert.ok(!relative(PACKAGE_ROOT, path).startsWith('..'), `${file}: import escaped package`);
      readFileSync(path);
    }
  }
});
test('extraction, semantic providers and runtime embedding helpers are byte-identical copies', () => {
  const provenance = JSON.parse(readFileSync(join(PACKAGE_ROOT, 'UPSTREAM.json')));
  for (const name of ['scripts/bda-client.mjs', 'scripts/semantic-compiler.mjs', 'scripts/providers/llm-provider.mjs', 'scripts/providers/openai-provider.mjs', 'scripts/providers/bedrock-provider.mjs', 'document-context.mjs', 'document-id-utils.mjs']) {
    assert.equal(createHash('sha256').update(readFileSync(join(PACKAGE_ROOT, name))).digest('hex'), provenance.files[name], name);
  }
});
test('working copies bootstrap byte-for-byte from runtime S3 without changing bindings or AWS', async t => {
  const f = fixture(t); await f.sync(); const p = pathsFor(f.repo);
  assert.deepEqual(readFileSync(p.manifest), f.objects.get('documents/manifest.json').body);
  assert.deepEqual(readJson(p.plans), f.plans); assert.equal(readJson(p.manifest).existing.custom, 'preserve');
  await f.sync(); assert.equal(f.gets(), 4);
});
test('newer remote configuration refreshes only clean working copies', async t => {
  const f = fixture(t); await f.sync(); f.plans[1].lessons[0].extra = 'new remote field';
  f.objects.set('documents/coursePlans.json', { body: jsonBytes(f.plans), etag: 'p-new' }); await f.sync();
  const p = pathsFor(f.repo); assert.equal(readJson(p.plans)[1].lessons[0].extra, 'new remote field');
  const before = readFileSync(p.plans); const dirty = readJson(p.manifest); dirty.pending = {}; atomicWrite(p.manifest, jsonBytes(dirty));
  await assert.rejects(f.sync, /unpublished edits/); assert.deepEqual(readFileSync(p.plans), before); assert.ok(readJson(p.manifest).pending);
});
test('pending publication blocks baseline refresh without discarding state', async t => {
  const f = fixture(t); await f.sync(); const path = join(stateDirectory(f.repo), 'cli-publication.json');
  atomicWrite(path, jsonBytes({ documentId: 'cli', phase: 'activating', sourceDocumentId: 'ph-pending' }));
  const gets = f.gets(); await assert.rejects(f.sync, /recovery required/); assert.equal(f.gets(), gets); assert.equal(readJson(path).phase, 'activating');
});
test('runtime target changes cannot silently reuse another target baseline', async t => {
  const f = fixture(t); await f.sync(); await assert.rejects(() => syncRuntimeConfig({ ...f, target: { ...f.target, bucket: 'OTHER' } }), /Runtime target changed/);
});
test('actual bundled CLI help runs without Git or an OATutor checkout', t => {
  const f = fixture(t);
  const env = { ...process.env, OATUTOR_REPO_PATH: '/does-not-exist', COURSE_MATERIALS_PIPELINE_DATA_DIR: f.repo };
  const output = execFileSync(process.execPath, [join(PACKAGE_ROOT, 'scripts/compile-documents.mjs'), '--help'], { cwd: PACKAGE_ROOT, env, encoding: 'utf8' });
  assert.match(output, /compile-documents/); assert.match(output, /--doc/);
});

test('copied schema validator accepts canonical output and rejects an incompatible runtime format', async t => {
  const f = fixture(t), { validateCompiledDocument } = await import('./validate-compiled.mjs');
  const doc = { document_id: 'ph-test', metadata: { course: 'Test', document_type: 'textbook', visibility: 'private_tutor', parser_version: 'TEST', content_hash: 'TEST', source_pdf: 'raw/ph-test.pdf' }, sections: [] };
  assert.equal(validateCompiledDocument(doc, pathsFor(f.repo).docs).ok, true);
  doc.metadata.visibility = 'unknown'; assert.equal(validateCompiledDocument(doc, pathsFor(f.repo).docs).ok, false);
});
test('optional embeddings command resolves bundled runtime helpers without paid calls', t => {
  const f = fixture(t);
  const out = execFileSync(process.execPath, [join(PACKAGE_ROOT, 'scripts/embed-documents.mjs'), '--help'], { cwd: PACKAGE_ROOT, env: { ...process.env, COURSE_MATERIALS_PIPELINE_DATA_DIR: f.repo }, encoding: 'utf8' });
  assert.match(out, /embed-documents/); assert.match(out, /EMBEDDING_MODEL/);
});

test('publisher test subprocess never inherits the production writer lock or receipt', async () => {
  const { runTests } = await import('./publish-docs.mjs');
  process.env.COURSE_MATERIALS_LOCK_TOKEN = 'TEST-parent-nonce';
  process.env.COURSE_MATERIALS_CLI_RECEIPT = 'TEST-parent-receipt';
  try {
    runTests((command, args, options) => {
      assert.equal(command, 'npm'); assert.deepEqual(args, ['test']); assert.equal(options.cwd, PACKAGE_ROOT);
      assert.equal(options.env.COURSE_MATERIALS_LOCK_TOKEN, undefined);
      assert.equal(options.env.COURSE_MATERIALS_CLI_RECEIPT, undefined);
      return { status: 0 };
    });
  } finally { delete process.env.COURSE_MATERIALS_LOCK_TOKEN; delete process.env.COURSE_MATERIALS_CLI_RECEIPT; }
});

for (const refresh of [false, true]) for (const interruptedAfter of [1, 2, 3, 4]) {
  test(`configuration sync resumes ${refresh ? 'refresh' : 'bootstrap'} after write ${interruptedAfter}`, async t => {
    const f = fixture(t);
    if (refresh) {
      await f.sync();
      f.manifest.existing.custom = 'new remote'; f.plans[1].updated = true;
      f.objects.set('documents/manifest.json', { body: jsonBytes(f.manifest), etag: 'm-new' });
      f.objects.set('documents/coursePlans.json', { body: jsonBytes(f.plans), etag: 'p-new' });
    }
    let writes = 0;
    await assert.rejects(() => syncRuntimeConfig({ ...f, write(path, bytes) {
      atomicWrite(path, bytes);
      if (!path.endsWith('config-sync.json') && ++writes === interruptedAfter) throw new Error('TEST sync interrupted');
    } }), /TEST sync interrupted/);
    assert.throws(() => assertNoPendingPublication(f.repo), /Interrupted configuration sync/);
    const gets = f.gets();
    const result = await syncRuntimeConfig({ ...f, cloud: { async get() { throw new Error('Recovery must not depend on S3'); } } });
    assert.equal(f.gets(), gets);
    const p = pathsFor(f.repo), state = stateDirectory(f.repo);
    assert.equal(hash(readFileSync(p.manifest)), result.manifestHash);
    assert.equal(hash(readFileSync(p.plans)), result.coursePlansHash);
    assert.deepEqual(readJson(join(state, 'baseline.json')).manifest, f.manifest);
    assert.equal(readJson(join(state, 'runtime.json')).manifestHash, result.manifestHash);
    assert.doesNotThrow(() => assertNoPendingPublication(f.repo));
    await f.sync();
  });
}

for (const edited of ['manifest', 'plans', 'baseline', 'ledger']) test(`interrupted sync preserves real ${edited} edits and does not advance other files`, async t => {
  const f = fixture(t); await f.sync();
  f.manifest.existing.custom = 'remote change';
  f.objects.set('documents/manifest.json', { body: jsonBytes(f.manifest), etag: 'new' });
  await assert.rejects(() => syncRuntimeConfig({ ...f, write(path, bytes) { atomicWrite(path, bytes); if (path.endsWith('/manifest.json')) throw new Error('TEST stop'); } }), /TEST stop/);
  const p = pathsFor(f.repo), state = stateDirectory(f.repo);
  const changed = { manifest: p.manifest, plans: p.plans, baseline: join(state, 'baseline.json'), ledger: join(state, 'runtime.json') }[edited];
  atomicWrite(changed, jsonBytes({ note: 'Genuine local edit' }));
  const paths = [p.manifest, p.plans, join(state, 'baseline.json'), join(state, 'runtime.json')];
  const hashes = paths.map(path => hash(readFileSync(path)));
  await assert.rejects(f.sync, /unpublished edits/);
  assert.deepEqual(paths.map(path => hash(readFileSync(path))), hashes);
  assert.ok(readJson(join(state, 'config-sync.json')));
});

test('older unmarked partial sync completes only known ledger or current S3 bytes', async t => {
  const f = fixture(t); await f.sync();
  f.manifest.existing.custom = 'remote update'; f.plans[1].newField = true;
  f.objects.set('documents/manifest.json', { body: jsonBytes(f.manifest), etag: 'new' });
  f.objects.set('documents/coursePlans.json', { body: jsonBytes(f.plans), etag: 'new' });
  atomicWrite(pathsFor(f.repo).manifest, jsonBytes(f.manifest));
  await f.sync(); assert.deepEqual(readJson(pathsFor(f.repo).plans), f.plans);
});

test('interruption immediately after recording sync intent is recoverable', async t => {
  const f = fixture(t);
  await assert.rejects(() => syncRuntimeConfig({ ...f, write(path, bytes) { atomicWrite(path, bytes); throw new Error('TEST stop after intent'); } }), /TEST stop after intent/);
  await f.sync(); assert.doesNotThrow(() => assertNoPendingPublication(f.repo));
});
test('interrupted sync rejects a changed runtime target without writing files', async t => {
  const f = fixture(t);
  await assert.rejects(() => syncRuntimeConfig({ ...f, write(path, bytes) { atomicWrite(path, bytes); throw new Error('TEST stop'); } }), /TEST stop/);
  const pending = readFileSync(join(stateDirectory(f.repo), 'config-sync.json'));
  await assert.rejects(() => syncRuntimeConfig({ ...f, target: { ...f.target, bucket: 'OTHER' } }), /Runtime target changed/);
  assert.deepEqual(readFileSync(join(stateDirectory(f.repo), 'config-sync.json')), pending);
});
