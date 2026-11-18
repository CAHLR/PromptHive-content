import test from 'node:test';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { compileMaterial, publishReviewed, manifestEntry } from './course-materials-cli.mjs';
import { publishAll } from './publish-docs.mjs';
import { PACKAGE_ROOT } from './pipeline-paths.mjs';
import { atomicWrite, jsonBytes, pathsFor, hash, materialArtifacts, readJson, stateDirectory, withPublicationLock, assertNoPendingPublication } from './publication-state.mjs';

function fixture(t) {
  const temp = mkdtempSync(join(tmpdir(), 'oski-cli-test-')); t.after(() => rmSync(temp, { recursive: true, force: true }));
  const repo = join(temp, 'repo'), workspace = join(temp, 'workspace'); mkdirSync(repo);
  const p = pathsFor(repo);
  atomicWrite(p.manifest, jsonBytes({ existing: { compiled: 'compiled/existing.json', title: 'Unrelated' } }));
  atomicWrite(p.plans, jsonBytes([{ courseName: 'Test', lessons: [{ id: 'a', chat_documents: ['existing'] }, { id: 'a', chat_documents: [] }, { id: 'b' }] }, { courseName: 'Other', lessons: [{ id: 'c' }] }]));
  atomicWrite(join(p.docs, 'compiled/existing.json'), jsonBytes({ document_id: 'existing' }));
  const record = { documentId: 'ph-' + 'a'.repeat(32), title: 'Reference', author: 'Instructor', courseName: 'Test', lessonIds: ['a'], documentType: 'textbook', visibility: 'private_tutor' };
  const pdf = Buffer.from('%PDF-1.4\nTEST ONLY\n%%EOF'); record.sha256 = hash(pdf);
  atomicWrite(join(workspace, 'documents/raw', `${record.documentId}.pdf`), pdf);
  const compiled = { document_id: record.documentId, sections: [{ section_id: 's', notes: [{ note_id: 'n', text: 'Explicit test fixture' }] }] };
  const calls = [], objects = new Map([['documents/manifest.json', { body: readFileSync(p.manifest), etag: 'm' }], ['documents/coursePlans.json', { body: readFileSync(p.plans), etag: 'p' }]]);
  const puts = [];
  const cloud = { async get(key) { return objects.get(key) || null; }, async put(key, body, etag) { assert.equal(objects.get(key)?.etag || null, etag); puts.push(key); objects.set(key, { body: Buffer.from(body), etag: hash(body) }); } };
  const validate = () => ({ ok: true, warnings: ['Test fixture'] });
  const run = async (args, options) => {
    calls.push(args); assert.equal(options.cwd, PACKAGE_ROOT); assert.ok(options.env.COURSE_MATERIALS_LOCK_TOKEN); if (args[1] !== 'publish-docs') assert.equal(options.env.COMPILER_DRY_RUN, '0');
    if (args[1] === 'compile-docs') {
      assert.deepEqual(args, ['run', 'compile-docs', '--', '--doc', record.documentId]);
      assert.deepEqual(readJson(p.manifest)[record.documentId], manifestEntry(record));
      atomicWrite(join(p.docs, `compiled/${record.documentId}.json`), jsonBytes(compiled));
    } else if (args[1] === 'publish-docs') {
      assert.deepEqual(args, ['run', 'publish-docs']);
      // Exercise the real existing CLI publisher, injecting only its cloud client and schema validator.
      const receipt = await publishAll({ repo, cloud, bucket: 'TEST', validate, args: { skipTests: true } });
      atomicWrite(options.env.COURSE_MATERIALS_CLI_RECEIPT, jsonBytes(receipt));
    }
  };
  async function prepare() {
    await compileMaterial({ repo, workspace, record, run, validate });
    record.approval = { fingerprint: materialArtifacts(workspace, record).fingerprint,
      baseManifestHash: hash(readFileSync(p.manifest)), baseCoursePlansHash: hash(readFileSync(p.plans)), manifestEntry: manifestEntry(record) };
  }
  return { repo, workspace, record, p, cloud, objects, puts, calls, run, validate, prepare,
    publish: () => publishReviewed({ repo, workspace, record, cloud, run, validate }) };
}
test('PDF raw path, temporary manifest, existing CLI flags, bounded review metadata and staging', async t => {
  const f = fixture(t), before = readFileSync(f.p.manifest); await f.prepare();
  assert.ok(existsSync(join(f.p.docs, `raw/${f.record.documentId}.pdf`)));
  assert.equal(existsSync(join(f.p.docs, `compiled/${f.record.documentId}.json`)), false);
  assert.deepEqual(readFileSync(f.p.manifest), before);
  assert.deepEqual(f.calls[1], ['run', 'validate-docs', '--', '--doc', f.record.documentId]);
  assert.equal(readJson(join(f.workspace, 'review.json')).validation.ok, true);
  assert.equal(readJson(join(f.workspace, 'review.json')).compiled, undefined);
});
for (const failing of ['compile-docs', 'validate-docs']) test(`${failing} failure isolates registration/output and retries the same PDF`, async t => {
  const f = fixture(t), original = readFileSync(f.p.manifest);
  const run = async (...args) => { await f.run(...args); if (args[0][1] === failing) throw new Error('TEST command failure'); };
  await assert.rejects(() => compileMaterial({ ...f, run }), /TEST command failure/);
  assert.deepEqual(readFileSync(f.p.manifest), original);
  assert.equal(existsSync(join(f.p.docs, `compiled/${f.record.documentId}.json`)), false);
  await f.prepare(); assert.ok(readJson(join(f.workspace, 'review.json')).validation.ok);
});
test('CLI publication preserves unrelated data, duplicate variants, and repeated publication is additive', async t => {
  const f = fixture(t); await f.prepare(); const result = await f.publish();
  assert.equal(result.activation, 'verified'); assert.equal(result.publicationMethod, 'npm run publish-docs');
  const plans = readJson(f.p.plans);
  assert.deepEqual(plans[0].lessons[0].chat_documents, ['existing', f.record.documentId]);
  assert.deepEqual(plans[0].lessons[1].chat_documents, ['existing', f.record.documentId]);
  assert.equal(plans[0].lessons[2].chat_documents, undefined); assert.equal(plans[1].lessons[0].chat_documents, undefined);
  assert.equal(readJson(f.p.manifest).existing.title, 'Unrelated');
  await f.publish(); assert.equal(readJson(f.p.plans)[0].lessons[0].chat_documents.filter(id => id === f.record.documentId).length, 1);
  // A later ordinary CLI run retains these additions without a new registry or Git commit.
  await publishAll({ repo: f.repo, cloud: f.cloud, bucket: 'TEST', validate: f.validate, args: { skipTests: true } });
  assert.ok(JSON.parse(f.objects.get('documents/manifest.json').body)[f.record.documentId]);
});
test('changed reviewed content or assignments never publish', async t => {
  const f = fixture(t); await f.prepare(); f.record.lessonIds = ['b']; await assert.rejects(f.publish, /review again/); assert.equal(f.puts.length, 0);
});
test('conflicting checkout and ambiguous cross-course lesson IDs fail closed', async t => {
  const f = fixture(t); await f.prepare(); const plans = readJson(f.p.plans); plans[1].lessons.push({ id: 'a' }); atomicWrite(f.p.plans, jsonBytes(plans));
  await assert.rejects(f.publish, /shared across courses/); assert.equal(f.puts.length, 0);
});
test('failure before activation retains journal, blocks other writers, and retries', async t => {
  const f = fixture(t); await f.prepare(); const put = f.cloud.put;
  f.cloud.put = async () => { throw new Error('TEST offline'); };
  await assert.rejects(f.publish, /TEST offline/);
  assert.equal(readJson(join(stateDirectory(f.repo), 'cli-publication.json')).phase, 'prepared');
  assert.throws(() => assertNoPendingPublication(f.repo, 'other'), /recovery required/);
  f.cloud.put = put; assert.equal((await f.publish()).activation, 'verified');
});
test('lost activation response remains recoverable and verifies every artifact before Published', async t => {
  const f = fixture(t); await f.prepare(); const put = f.cloud.put;
  f.cloud.put = async (...args) => { await put(...args); if (args[0].endsWith('coursePlans.json')) throw new Error('TEST response lost'); };
  await assert.rejects(f.publish, /TEST response lost/);
  assert.equal(readJson(join(stateDirectory(f.repo), 'cli-publication.json')).phase, 'activating');
  f.cloud.put = put; assert.equal((await f.publish()).activation, 'verified');
});
test('shared lock rejects concurrent writers and invalid inherited tokens', async t => {
  const f = fixture(t); await withPublicationLock(f.repo, async () => { await assert.rejects(f.prepare, /lock/); });
  process.env.COURSE_MATERIALS_LOCK_TOKEN = 'TEST-invalid';
  try { await assert.rejects(() => withPublicationLock(f.repo, () => {}), /Invalid inherited/); } finally { delete process.env.COURSE_MATERIALS_LOCK_TOKEN; }
});
test('missing CLI verification receipt never marks success', async t => {
  const f = fixture(t); await f.prepare();
  await assert.rejects(() => publishReviewed({ ...f, run: async () => {} }), /did not return verified/);
});

test('real CLI subprocess inherits only the live coordinated lock', async t => {
  const f = fixture(t);
  await withPublicationLock(f.repo, async token => {
    const moduleUrl = new URL('./publication-state.mjs', import.meta.url).href;
    const source = `import {withPublicationLock} from ${JSON.stringify(moduleUrl)}; await withPublicationLock(${JSON.stringify(f.repo)}, () => console.log('inherited'));`;
    assert.equal(execFileSync(process.execPath, ['--input-type=module', '-e', source], { encoding: 'utf8', env: { ...process.env, COURSE_MATERIALS_LOCK_TOKEN: token } }).trim(), 'inherited');
  });
});
test('compilation cleanup preserves unrelated concurrent manual edits', async t => {
  const f = fixture(t);
  const run = async (...args) => { await f.run(...args); const manifest = readJson(f.p.manifest); manifest.existing.title = 'Manual edit preserved'; atomicWrite(f.p.manifest, jsonBytes(manifest)); };
  await compileMaterial({ ...f, run });
  assert.equal(readJson(f.p.manifest).existing.title, 'Manual edit preserved');
  assert.equal(readJson(f.p.manifest)[f.record.documentId], undefined);
});
test('interrupted registration blocks CLI publishing and is recovered by its own retry', async t => {
  const f = fixture(t), entry = manifestEntry(f.record), original = readFileSync(f.p.manifest);
  atomicWrite(join(f.workspace, 'cli-registration.json'), jsonBytes({ entry, original: original.toString('base64') }));
  atomicWrite(join(stateDirectory(f.repo), 'compilation.json'), jsonBytes({ documentId: f.record.documentId, workspace: f.workspace }));
  atomicWrite(f.p.manifest, jsonBytes({ ...readJson(f.p.manifest), [f.record.documentId]: entry }));
  assert.throws(() => assertNoPendingPublication(f.repo, 'cli'), /Interrupted compilation/);
  await f.prepare(); assert.deepEqual(readFileSync(f.p.manifest), original);
  assert.doesNotThrow(() => assertNoPendingPublication(f.repo, 'cli'));
});
test('changed unrelated artifacts after a partial publication require reconciliation', async t => {
  const f = fixture(t); await f.prepare(); const put = f.cloud.put; f.cloud.put = async () => { throw new Error('TEST offline'); };
  await assert.rejects(f.publish, /TEST offline/); f.cloud.put = put;
  atomicWrite(join(f.p.docs, 'compiled/existing.json'), jsonBytes({ document_id: 'existing', changed: true }));
  await assert.rejects(f.publish, /artifacts changed/);
});

test('publishing preserves remote baseline material without copying archives or changing its S3 key', async t => {
  const f = fixture(t); await f.prepare();
  const existing = join(f.p.docs, 'compiled/existing.json');
  f.objects.set('documents/compiled/existing.json', { body: readFileSync(existing), etag: 'original' });
  atomicWrite(join(stateDirectory(f.repo), 'baseline.json'), jsonBytes({ manifest: readJson(f.p.manifest) }));
  rmSync(existing);
  assert.equal((await f.publish()).activation, 'verified');
  assert.equal(f.puts.includes('documents/compiled/existing.json'), false);
  assert.equal(f.objects.get('documents/compiled/existing.json').etag, 'original');
  assert.ok(f.puts.includes(`documents/compiled/${f.record.documentId}.json`));
});

test('newer runtime configuration is never overwritten by an old approval', async t => {
  const f = fixture(t); await f.prepare();
  f.objects.set('documents/coursePlans.json', { body: jsonBytes([{ courseName: 'Newer remote', lessons: [] }]), etag: 'new' });
  await assert.rejects(f.publish, /Runtime differs/); assert.equal(f.puts.length, 0);
});

test('S3 bootstrap to compile, review approval and verified publish uses only managed storage', async t => {
  const f = fixture(t), { syncRuntimeConfig } = await import('./sync-runtime-config.mjs');
  f.cloud.target = { bucket: 'TEST', region: 'us-west-1' };
  const existing = join(f.p.docs, 'compiled/existing.json');
  f.objects.set('documents/compiled/existing.json', { body: readFileSync(existing), etag: 'existing' }); rmSync(existing);
  await syncRuntimeConfig({ repo: f.repo, cloud: f.cloud, target: { ...f.cloud.target, prefix: 'documents' } });
  await f.prepare();
  assert.equal(readJson(join(f.workspace, 'review.json')).validation.ok, true);
  const result = await f.publish();
  assert.equal(result.activation, 'verified'); assert.equal(result.courseName, 'Test');
  assert.ok(JSON.parse(f.objects.get('documents/coursePlans.json').body)[0].lessons[0].chat_documents.includes(f.record.documentId));
  assert.ok(JSON.parse(f.objects.get('documents/manifest.json').body).existing);
  assert.equal(f.objects.get('documents/compiled/existing.json').etag, 'existing');
});

for (const failure of ['hash', 'missing', 'working-source', 'setup']) test(`early compilation ${failure} failure leaves no registration or global blocker`, async t => {
  const f = fixture(t), before = readFileSync(f.p.manifest), source = join(f.workspace, 'documents/raw', `${f.record.documentId}.pdf`);
  if (failure === 'hash') f.record.sha256 = 'incorrect';
  if (failure === 'missing') rmSync(source);
  if (failure === 'working-source') atomicWrite(join(f.p.docs, `raw/${f.record.documentId}.pdf`), Buffer.from('different source'));
  if (failure === 'setup') atomicWrite(join(f.p.docs, 'raw'), Buffer.from('blocks raw directory creation'));
  await assert.rejects(f.prepare);
  assert.deepEqual(readFileSync(f.p.manifest), before);
  assert.equal(f.calls.length, 0);
  assert.equal(existsSync(join(f.workspace, 'cli-registration.json')), false);
  assert.equal(existsSync(join(stateDirectory(f.repo), 'compilation.json')), false);
  assert.doesNotThrow(() => assertNoPendingPublication(f.repo));
});

test('failed source recheck retains an older interrupted registration that still needs recovery', async t => {
  const f = fixture(t), entry = manifestEntry(f.record), before = readFileSync(f.p.manifest);
  atomicWrite(join(f.workspace, 'cli-registration.json'), jsonBytes({ entry, original: before.toString('base64') }));
  atomicWrite(join(stateDirectory(f.repo), 'compilation.json'), jsonBytes({ documentId: f.record.documentId, workspace: f.workspace }));
  atomicWrite(f.p.manifest, jsonBytes({ ...readJson(f.p.manifest), [f.record.documentId]: entry }));
  f.record.sha256 = 'incorrect';
  await assert.rejects(f.prepare, /Verified PDF changed/);
  assert.ok(readJson(f.p.manifest)[f.record.documentId]);
  assert.throws(() => assertNoPendingPublication(f.repo), /Interrupted compilation/);
});
test('instructor cancellation after compilation cleans registration and isolates output', async t => {
  const f=fixture(t),before=readFileSync(f.p.manifest);
  await assert.rejects(()=>compileMaterial({...f,run:async(args,options)=>{
    await f.run(args,options);atomicWrite(join(f.workspace,'cancel-requested'),Buffer.from(''));
  }}),/cancelled by instructor/);
  assert.deepEqual(readFileSync(f.p.manifest),before);
  assert.equal(existsSync(join(f.p.docs,`compiled/${f.record.documentId}.json`)),false);
  assert.equal(existsSync(join(stateDirectory(f.repo),'compilation.json')),false);
  assert.equal(f.calls.length,1);
});
test('real CLI honours a cancellation checkpoint before any BDA call', async t => {
  const f=fixture(t),cancelPath=join(f.workspace,'cancel-requested');atomicWrite(cancelPath,Buffer.from(''));
  const before=readFileSync(f.p.manifest);
  await assert.rejects(()=>compileMaterial({...f,run:async(args,options)=>{
    try {execFileSync(process.execPath,[join(PACKAGE_ROOT,'scripts/compile-documents.mjs'),'--doc',f.record.documentId],{
      cwd:PACKAGE_ROOT,env:{...options.env,COURSE_MATERIALS_CANCEL_FILE:cancelPath},stdio:'pipe'});}
    catch(error){assert.match(String(error.stdout),/cancelled by instructor/);assert.doesNotMatch(String(error.stdout),/Running Bedrock Data Automation/);throw error;}
  }}));
  assert.deepEqual(readFileSync(f.p.manifest),before);
  assert.equal(existsSync(join(stateDirectory(f.repo),'compilation.json')),false);
});

test('pending course publishes additively, binds lessons, retries idempotently and fingerprints names', async t => {
  const f = fixture(t);
  const original = readJson(f.p.plans);
  original[0].lessons[1].chat_documents = ['existing']; // Valid unrelated duplicate variants.
  atomicWrite(f.p.plans, jsonBytes(original));
  f.objects.set('documents/coursePlans.json', { body: readFileSync(f.p.plans), etag: 'p-valid' });
  const before = readJson(f.p.plans);
  f.record.courseName = 'New Biology';
  f.record.lessonIds = ['ph-lesson-' + 'b'.repeat(32)];
  f.record.newCourse = { courseName: 'New Biology', office_hours: true, language: 'en', courseOER: 0, courseLicense: 0,
    lessons: [{ id: f.record.lessonIds[0], name: 'Cells', topics: '', allowRecycle: false, chat_display_mode: 'Window', learningObjectives: {} }] };
  await f.prepare();
  assert.deepEqual(readJson(f.p.plans), before, 'compiling must not create the course');
  await f.publish();
  const plans = readJson(f.p.plans);
  assert.deepEqual(plans.slice(0, before.length), before);
  assert.deepEqual(plans.at(-1).lessons[0].chat_documents, [f.record.documentId]);
  await f.publish();
  assert.deepEqual(readJson(f.p.plans), plans);
  f.record.newCourse.lessons[0].name = 'Changed after approval';
  await assert.rejects(f.publish(), /review again/);
});

test('pending course rejects an existing case-insensitive course name', async t => {
  const f = fixture(t);
  f.record.courseName = 'test';
  f.record.newCourse = { courseName: 'test', office_hours: true, lessons: [{ id: 'fresh', name: 'Fresh' }] };
  f.record.lessonIds = ['fresh'];
  await f.prepare();
  await assert.rejects(f.publish(), /conflicts/);
  assert.equal(f.puts.length, 0);
});
