/** Single-host UI orchestration of the existing CLI. No Git commits or second publisher. */
import { spawn } from 'node:child_process';
import { PACKAGE_ROOT } from './pipeline-paths.mjs';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
require('ajv/dist/2020.js'); require('ajv-formats');
import { cpSync, existsSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { atomicWrite, assertNoPendingPublication, hash, jsonBytes, materialArtifacts,
  mergeMaterial, pathsFor, readJson, runtimeBaseline, stateDirectory, withPublicationLock } from './publication-state.mjs';
import { validateCompiledDocument } from './validate-compiled.mjs';

export function manifestEntry(record) {
  if (!/^ph-[a-f0-9]{32}$/.test(record.documentId)) throw new Error('Invalid managed document ID');
  return { source: `raw/${record.documentId}.pdf`, compiled: `compiled/${record.documentId}.json`,
    title: record.title, author: record.author, course: record.courseName,
    document_type: record.documentType, visibility: record.visibility };
}
export function runCommand(args, { cwd, env, onOutput = () => {} }) {
  return new Promise((resolve, reject) => {
    const child = spawn('npm', args, { cwd, env, shell: false, stdio: ['ignore', 'pipe', 'pipe'] });
    for (const stream of [child.stdout, child.stderr]) stream.on('data', bytes => {
      process.stdout.write(bytes); onOutput(bytes.toString());
    });
    child.on('error', reject);
    child.on('close', code => code === 0 ? resolve() : reject(new Error(`npm ${args.join(' ')} exited ${code}; inspect the private worker log`)));
  });
}
export async function compileMaterial({ repo, workspace, record, run = runCommand, validate = validateCompiledDocument, stage = () => {} }) {
  return withPublicationLock(repo, async token => {
    assertNoPendingPublication(repo, record.documentId);
    const p = pathsFor(repo), entry = manifestEntry(record), id = record.documentId;
    const originalBytes = readFileSync(p.manifest);
    const original = JSON.parse(originalBytes);
    if (original[id] && JSON.stringify(original[id]) !== JSON.stringify(entry)) throw new Error('Document ID has conflicting manifest metadata');
    // A small registration marker survives process interruption. A retry owns only this ID.
    const markerPath = join(workspace, 'cli-registration.json');
    const marker = readJson(markerPath);
    if (original[id] && !marker) throw new Error('Document ID already registered; refuse to replace it');
    if (marker && JSON.stringify(marker.entry) !== JSON.stringify(entry)) throw new Error('Recover the original compilation registration before changing metadata');
    // Validate the source before creating any new recovery ownership.
    const pdf = readFileSync(join(workspace, 'documents', 'raw', `${id}.pdf`));
    if (hash(pdf) !== record.sha256) throw new Error('Verified PDF changed');
    const source = join(p.docs, entry.source);
    if (existsSync(source) && hash(readFileSync(source)) !== record.sha256) throw new Error('Working PDF changed; reconcile before retrying');
    const registration = marker || { entry, original: originalBytes.toString('base64') };
    const sharedMarker = join(stateDirectory(repo), 'compilation.json');
    let compilationStarted = Boolean(marker);
    const env = { ...process.env, COURSE_MATERIALS_PIPELINE_DATA_DIR: repo, COURSE_MATERIALS_LOCK_TOKEN: token };
    // Block dotenv in the existing CLI from reintroducing a package dry-run flag.
    env.COMPILER_DRY_RUN = '0';
    const options = { cwd: PACKAGE_ROOT, env,
      onOutput: text => { if (text.includes('Running semantic compiler')) stage('compiling'); if (text.includes('Validation FAIL') || text.includes('Validation PASS')) stage('validating'); } };
    try {
      // Setup is inside cleanup too: failed file preparation must release unused markers.
      atomicWrite(markerPath, jsonBytes(registration));
      atomicWrite(sharedMarker, jsonBytes({ documentId: id, workspace }));
      atomicWrite(source, pdf);
      atomicWrite(p.manifest, jsonBytes({ ...original, [id]: entry }));
      stage('extracting');
      compilationStarted = true;
      await run(['run', 'compile-docs', '--', '--doc', id], options);
      if (existsSync(join(workspace, 'cancel-requested'))) throw new Error('Processing cancelled by instructor');
      stage('validating');
      await run(['run', 'validate-docs', '--', '--doc', id], options);
      if (existsSync(join(workspace, 'cancel-requested'))) throw new Error('Processing cancelled by instructor');
      const artifacts = materialArtifacts(repo, record);
      const validation = validate(artifacts.compiled, p.docs);
      if (!validation.ok || validation.schemaSkipped) throw new Error('Strict validation failed');
      // Stage this job's output outside the global publishable tree until approval.
      for (const [rel, file] of artifacts.files) {
        atomicWrite(join(workspace, 'documents', rel), readFileSync(file));
        rmSync(file);
      }
      const review = { documentId: id, compiledHash: artifacts.hashes[entry.compiled], artifacts: artifacts.hashes,
        validation, uncertainties: artifacts.compiled.metadata?.uncertainties || [], generatedAt: new Date().toISOString(), demo: false };
      atomicWrite(join(workspace, 'review.json'), jsonBytes(review));
      return review;
    } finally {
      // Remove only our temporary registration, preserving edits to every other entry.
      const latest = readJson(p.manifest);
      const registered = Object.hasOwn(latest, id);
      if (registered && JSON.stringify(latest[id]) !== JSON.stringify(entry)) throw new Error('Manifest entry changed during compilation; operator reconciliation required');
      delete latest[id];
      // Preserve exact original formatting when nobody else edited shared configuration.
      const before = JSON.parse(Buffer.from(registration.original, 'base64'));
      delete before[id];
      if (registered)
        atomicWrite(p.manifest, JSON.stringify(latest) === JSON.stringify(before) ? Buffer.from(registration.original, 'base64') : jsonBytes(latest));
      // Isolate incomplete output too, so it cannot leak into a later global publication.
      if (compilationStarted) for (const rel of [entry.compiled, entry.compiled.replace(/\.json$/, '.embeddings.json')]) {
        const file = join(p.docs, rel);
        if (existsSync(file)) { atomicWrite(join(workspace, 'incomplete', rel), readFileSync(file)); rmSync(file); }
      }
      if (compilationStarted) for (const category of ['figures', 'tables']) {
        const generated = join(p.docs, 'assets', category, id);
        if (existsSync(generated)) {
          cpSync(generated, join(workspace, 'incomplete/assets', category, id), { recursive: true });
          rmSync(generated, { recursive: true });
        }
      }
      rmSync(sharedMarker, { force: true });
      rmSync(markerPath, { force: true });
    }
  });
}

export function mergeAssignments(manifest, plans, record, entry) {
  const merged = mergeMaterial(manifest, plans, record, entry);
  const course = merged.plans.find(course => course.courseName === record.courseName && !course.editor);
  for (const id of record.lessonIds) {
    const variants = course.lessons.filter(lesson => lesson.id === id);
    const union = [...new Set(variants.flatMap(lesson => lesson.chat_documents || []))];
    // Runtime lookup is by stable lesson ID; every duplicate must have the same allowlist.
    for (const lesson of variants) lesson.chat_documents = [...union];
  }
  return merged;
}
export async function publishReviewed({ repo, workspace, record, cloud, prefix = 'documents', run = runCommand, validate = validateCompiledDocument }) {
  return withPublicationLock(repo, async token => {
    const p = pathsFor(repo), journalPath = join(stateDirectory(repo), 'cli-publication.json');
    const artifacts = materialArtifacts(workspace, record);
    if (artifacts.fingerprint !== record.approval?.fingerprint) throw new Error('Reviewed content or assignments changed; review again');
    const validation = validate(artifacts.compiled, join(workspace, 'documents'));
    if (!validation.ok || validation.schemaSkipped) throw new Error('Strict validation failed');
    const entry = manifestEntry(record);
    mergeAssignments(readJson(p.manifest), readJson(p.plans), record, entry);
    if (JSON.stringify(entry) !== JSON.stringify(record.approval.manifestEntry)) throw new Error('Approved manifest metadata changed');
    assertNoPendingPublication(repo, 'cli');
    let journal = readJson(journalPath);
    const resuming = journal?.sourceDocumentId === record.documentId;
    if (journal && journal.phase !== 'verified' && !resuming) throw new Error('Reconcile the pending CLI publication first');
    if (resuming && journal.approvalFingerprint !== artifacts.fingerprint) throw new Error('Pending CLI publication owns the original approval');
    if (!resuming) {
      if (hash(readFileSync(p.manifest)) !== record.approval.baseManifestHash || hash(readFileSync(p.plans)) !== record.approval.baseCoursePlansHash) throw new Error('Working configuration changed after review; review again');
      const baseline = await runtimeBaseline(repo, cloud, prefix);
      const merged = mergeAssignments(readJson(p.manifest), readJson(p.plans), record, entry);
      const manifestBytes = jsonBytes(merged.manifest), plansBytes = jsonBytes(merged.plans);
      journal = { documentId: 'cli', sourceDocumentId: record.documentId, approvalFingerprint: artifacts.fingerprint,
        phase: 'prepared', manifestHash: hash(manifestBytes), coursePlansHash: hash(plansBytes),
        baseManifestHash: baseline.manifest ? hash(baseline.manifest.body) : null,
        basePlansHash: baseline.plans ? hash(baseline.plans.body) : null,
        // Exact targets allow recovery if interrupted between the two atomic local writes.
        manifest: merged.manifest, plans: merged.plans };
      atomicWrite(journalPath, jsonBytes(journal));
    }
    for (const [path, target, base] of [[p.manifest, journal.manifest, record.approval.baseManifestHash], [p.plans, journal.plans, record.approval.baseCoursePlansHash]]) {
      const actual = hash(readFileSync(path));
      if (![base, hash(jsonBytes(target))].includes(actual)) throw new Error('Working conflict during publication preparation');
      atomicWrite(path, jsonBytes(target));
    }
    // Never replace an existing artifact with differing bytes on retries.
    for (const [rel, file] of artifacts.files) {
      const target = join(p.docs, rel);
      if (existsSync(target) && hash(readFileSync(target)) !== artifacts.hashes[rel]) throw new Error('Working artifact conflicts with reviewed output');
      atomicWrite(target, readFileSync(file));
    }
    const receiptPath = join(workspace, 'cli-receipt.json');
    rmSync(receiptPath, { force: true });
    await run(['run', 'publish-docs'], { cwd: PACKAGE_ROOT,
      env: { ...process.env, COURSE_MATERIALS_PIPELINE_DATA_DIR: repo, COURSE_MATERIALS_LOCK_TOKEN: token, COURSE_MATERIALS_CLI_RECEIPT: receiptPath } });
    const receipt = readJson(receiptPath);
    if (receipt?.activation !== 'verified' || receipt.manifestHash !== journal.manifestHash || receipt.coursePlansHash !== journal.coursePlansHash) throw new Error('CLI did not return verified publication');
    return { ...receipt, documentId: record.documentId, artifacts: artifacts.hashes, courseName: record.courseName, lessonIds: record.lessonIds,
      publicationMethod: 'npm run publish-docs', workingDirectory: repo };
  });
}
