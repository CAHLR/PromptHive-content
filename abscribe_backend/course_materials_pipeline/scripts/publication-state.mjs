/** Single-host publication coordinator shared by CLI and PromptHive.
 * Keep the materials working directory and its publication state on durable storage.
 */
import { createHash, randomUUID } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, isAbsolute, join, relative, resolve } from 'node:path';

export const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
export const jsonBytes = (value) => Buffer.from(JSON.stringify(value, null, 2) + '\n');
export function stateDirectory(repo) {
  const directory = resolve(repo, 'publication-state');
  mkdirSync(directory, { recursive: true });
  return directory;
}
export function atomicWrite(path, bytes) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(`${path}.tmp`, bytes, { mode: 0o600 });
  renameSync(`${path}.tmp`, path);
}
export async function withPublicationLock(repo, operation) {
  const lock = join(stateDirectory(repo), 'writer.lock');
  // Only a subprocess holding the exact live parent's nonce may inherit this lock.
  const inherited = process.env.COURSE_MATERIALS_LOCK_TOKEN;
  if (inherited) {
    const owner = readJson(join(lock, 'owner.json'));
    if (!owner?.token || owner.token !== inherited) throw new Error('Invalid inherited publication lock');
    process.kill(owner.pid, 0);
    return operation(inherited);
  }
  const acquire = () => mkdirSync(lock);
  try { acquire(); } catch (error) {
    if (error.code !== 'EEXIST') throw error;
    // Single-host recovery: serialize reclaimers and only reclaim a confirmed
    // dead PID. Unknown ownership and live/reused PIDs fail closed.
    const recovery = join(stateDirectory(repo), 'lock-recovery');
    try { mkdirSync(recovery); } catch { throw new Error('Another publication owns the repository lock; retry later'); }
    try {
      const owner = readJson(join(lock, 'owner.json'));
      if (!owner?.pid || !Number.isSafeInteger(owner.pid) || owner.pid < 1) throw new Error('Publication lock has unknown ownership; operator recovery required');
      let alive = true;
      try { process.kill(owner.pid, 0); } catch (probe) { if (probe.code === 'ESRCH') alive = false; else throw probe; }
      if (alive) throw new Error('Another publication owns the repository lock; retry later');
      rmSync(lock, { recursive: true });
      acquire();
    } finally { rmSync(recovery, { recursive: true }); }
  }
  const token = randomUUID();
  writeFileSync(join(lock, 'owner.json'), jsonBytes({ pid: process.pid, token, startedAt: new Date().toISOString() }));
  try { return await operation(token); } finally { rmSync(lock, { recursive: true }); }
}
export function readJson(path, fallback = null) { return existsSync(path) ? JSON.parse(readFileSync(path, 'utf8')) : fallback; }
export function assertNoPendingPublication(repo, currentId = null, { allowConfigSync = false } = {}) {
  if (!allowConfigSync && existsSync(join(stateDirectory(repo), 'config-sync.json'))) throw new Error('Interrupted configuration sync; finish sync-config before another pipeline operation');
  const compiling = readJson(join(stateDirectory(repo), 'compilation.json'));
  if (compiling && compiling.documentId !== currentId) throw new Error(`Interrupted compilation registration for ${compiling.documentId}; retry that material before another pipeline operation`);
  for (const name of readdirSync(stateDirectory(repo)).filter((name) => /^(ph-|cli-publication).*\.json$/.test(name))) {
    const entry = readJson(join(stateDirectory(repo), name));
    if (entry.documentId !== currentId && entry.phase !== 'verified') throw new Error(`Publication recovery required for ${entry.documentId || 'CLI writer'} before another writer may publish`);
  }
}
export function pathsFor(repo) {
  return { docs: join(repo, 'documents'),
    manifest: join(repo, 'documents/manifest.json'),
    plans: join(repo, 'coursePlans.json'),
    registry: join(repo, 'documents/ui-materials.json') };
}
export function collectAssetPaths(node, out = new Set()) {
  if (!node || typeof node !== 'object') return out;
  for (const [key, value] of Object.entries(node)) {
    if (['path', 'csv_path', 'crop_path'].includes(key) && typeof value === 'string' && value) out.add(value);
    else if (value && typeof value === 'object') collectAssetPaths(value, out);
  }
  return out;
}
export function safeFile(root, rel) {
  if (!rel || isAbsolute(rel) || rel.includes('\\') || rel.split('/').includes('..')) throw new Error('Unsafe artifact path');
  const file = realpathSync(join(root, rel));
  const inside = relative(realpathSync(root), file);
  if (inside.startsWith('..') || isAbsolute(inside)) throw new Error('Artifact escapes its workspace');
  return file;
}
export function approvalFingerprint(record, artifacts) {
  return hash(jsonBytes({ documentId: record.documentId, title: record.title, author: record.author,
    documentType: record.documentType, visibility: record.visibility, courseName: record.courseName,
    lessonIds: [...record.lessonIds].sort(), artifacts: Object.fromEntries(Object.entries(artifacts).sort()),
    ...(record.newCourse ? { newCourse: record.newCourse } : {}) }));
}
export function materialArtifacts(workspace, record) {
  const root = join(workspace, 'documents');
  const compiledRel = `compiled/${record.documentId}.json`;
  const compiledFile = safeFile(root, compiledRel);
  const compiled = JSON.parse(readFileSync(compiledFile));
  if (compiled.document_id !== record.documentId) throw new Error('Compiled document identity changed');
  const files = new Map([[compiledRel, compiledFile]]);
  for (const rel of collectAssetPaths(compiled)) {
    if (!rel.startsWith('assets/') || !rel.split('/').includes(record.documentId)) throw new Error('Material references an asset outside its namespace');
    files.set(rel, safeFile(root, rel));
  }
  const sidecar = compiledRel.replace(/\.json$/, '.embeddings.json');
  if (existsSync(join(root, sidecar))) files.set(sidecar, safeFile(root, sidecar));
  const hashes = Object.fromEntries([...files].map(([rel, path]) => [rel, hash(readFileSync(path))]));
  return { compiled, files, hashes, fingerprint: approvalFingerprint(record, hashes) };
}
export function mergeMaterial(manifest, plans, record, entry) {
  const mergedManifest = structuredClone(manifest);
  const mergedPlans = structuredClone(plans);
  if (mergedManifest[record.documentId] && JSON.stringify(mergedManifest[record.documentId]) !== JSON.stringify(entry)) throw new Error('Document ID already has conflicting metadata');
  if (record.newCourse) {
    const draft = record.newCourse;
    if (draft.courseName !== record.courseName || !draft.lessons?.length || !draft.office_hours) throw new Error('Invalid pending course');
    const sameName = mergedPlans.filter(c => c.courseName?.trim().toLowerCase() === draft.courseName.trim().toLowerCase());
    if (sameName.length) {
      const withoutBindings = course => ({ ...course, lessons: course.lessons.map(({ chat_documents, ...lesson }) => lesson) });
      if (sameName.length !== 1 || JSON.stringify(withoutBindings(sameName[0])) !== JSON.stringify(draft)) throw new Error('Pending course conflicts with existing configuration');
    } else mergedPlans.push(structuredClone(draft));
  }
  const courses = mergedPlans.filter((course) => course.courseName === record.courseName && !course.editor);
  if (courses.length !== 1 || !record.lessonIds.length) throw new Error('Course or lesson assignment is invalid');
  const selected = new Set(record.lessonIds);
  const found = new Set();
  for (const lesson of courses[0].lessons || []) {
    if (!selected.has(lesson.id) || lesson.type) continue;
    found.add(lesson.id);
    lesson.chat_documents = [...new Set([...(lesson.chat_documents || []), record.documentId])];
  }
  if (found.size !== selected.size) throw new Error('Assigned lesson no longer belongs to the course');
  // Identical IDs in another course would bypass course-scoped permission.
  for (const course of mergedPlans.filter((course) => course !== courses[0])) {
    if ((course.lessons || []).some((lesson) => selected.has(lesson.id))) throw new Error('Lesson ID is shared across courses; resolve the ambiguous assignment first');
  }
  mergedManifest[record.documentId] = entry;
  return { manifest: mergedManifest, plans: mergedPlans };
}
export function assertManagedRetained(repo) {
  const p = pathsFor(repo);
  const registry = readJson(p.registry, {});
  const ledger = readJson(join(stateDirectory(repo), 'runtime.json'), {});
  const manifest = readJson(p.manifest, {});
  const plans = readJson(p.plans, []);
  for (const id of new Set([...Object.keys(registry), ...Object.keys(ledger.managed || {})])) {
    const managed = registry[id];
    if (!managed || !manifest[id] || JSON.stringify(manifest[id]) !== JSON.stringify(managed.manifestEntry)) throw new Error(`Checkout lost UI-managed material ${id}; restore its publication commit before publishing`);
    for (const [rel, expected] of Object.entries(managed.artifacts)) {
      if (!existsSync(join(p.docs, rel))) {
        const archived = safeFile(join(stateDirectory(repo), 'artifacts', id), rel);
        if (hash(readFileSync(archived)) !== expected) throw new Error('Private artifact archive is corrupt');
        atomicWrite(join(p.docs, rel), readFileSync(archived));
      }
      if (hash(readFileSync(safeFile(p.docs, rel))) !== expected) throw new Error(`Managed artifact changed: ${rel}; re-review through PromptHive`);
    }
    const course = plans.find((c) => c.courseName === managed.courseName);
    for (const lessonId of managed.lessonIds) {
      const variants = (course?.lessons || []).filter((l) => l.id === lessonId);
      if (!variants.length || variants.some((l) => !l.chat_documents?.includes(id))) throw new Error(`Checkout lost managed lesson bindings for ${id}`);
    }
  }
  return registry;
}
export async function runtimeBaseline(repo, cloud, prefix) {
  const saved = readJson(join(stateDirectory(repo), 'baseline.json'));
  if (saved?.target && (!cloud.target || JSON.stringify(saved.target) !== JSON.stringify({ ...cloud.target, prefix }))) throw new Error('Runtime target differs from synchronized configuration; reconcile before publication');
  const manifest = await cloud.get(`${prefix}/manifest.json`);
  const plans = await cloud.get(`${prefix}/coursePlans.json`);
  const ledger = readJson(join(stateDirectory(repo), 'runtime.json'));
  const local = pathsFor(repo);
  const actual = { manifestHash: manifest ? hash(manifest.body) : null, coursePlansHash: plans ? hash(plans.body) : null };
  if (ledger) {
    if (actual.manifestHash !== ledger.manifestHash || actual.coursePlansHash !== ledger.coursePlansHash) throw new Error('Runtime configuration changed outside the coordinated publisher; reconcile the runtime journal before publishing');
  } else if ((manifest && actual.manifestHash !== hash(readFileSync(local.manifest))) || (plans && actual.coursePlansHash !== hash(readFileSync(local.plans)))) {
    throw new Error('Runtime differs from local working configuration without a baseline. Synchronize runtime configuration before publishing.');
  }
  return { manifest, plans };
}
export async function verifyObject(cloud, key, expected) {
  const value = await cloud.get(key);
  if (!value || hash(value.body) !== expected) throw new Error(`Runtime verification failed: ${key}`);
  return value;
}
export function writeRuntimeLedger(repo, manifestBytes, plansBytes, managed) {
  atomicWrite(join(stateDirectory(repo), 'runtime.json'), jsonBytes({ manifestHash: hash(manifestBytes), coursePlansHash: hash(plansBytes), managed, verifiedAt: new Date().toISOString() }));
}

