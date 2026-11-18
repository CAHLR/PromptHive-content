/**
 * Publish validated compiled course documents to the us-west-1 runtime S3 bucket.
 *
 *   npm run publish-docs
 *   npm run publish-docs -- --dry-run
 *
 * Env:
 *   COURSE_DOCS_RUNTIME_BUCKET
 *   COURSE_DOCS_RUNTIME_REGION (default us-west-1)
 *   COURSE_DOCS_RUNTIME_PREFIX (default documents)
 */
import { spawnSync } from 'child_process';
import {
    existsSync,
    readFileSync,
    readdirSync,
    statSync,
} from 'fs';
import { dirname, join, relative, resolve } from 'path';
import { fileURLToPath, pathToFileURL } from 'url';
import {
    PutObjectCommand,
    S3Client,
} from '@aws-sdk/client-s3';
import { validateCompiledDocument } from './validate-compiled.mjs';
import { preflightPublish } from './publish-preflight.mjs';
import { createPublicationCloud } from './publication-cloud.mjs';
import { assertManagedRetained, assertNoPendingPublication, atomicWrite, hash, jsonBytes,
    readJson, runtimeBaseline, stateDirectory, verifyObject, withPublicationLock, writeRuntimeLedger } from './publication-state.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
import { PACKAGE_ROOT as PKG_ROOT, WORK_ROOT } from './pipeline-paths.mjs';

function parseArgs(argv) {
    const args = { dryRun: false, skipTests: false };
    for (let i = 0; i < argv.length; i++) {
        const a = argv[i];
        if (a === '--dry-run') args.dryRun = true;
        else if (a === '--skip-tests') args.skipTests = true;
    }
    return args;
}

export function runTests(run = spawnSync) {
    const env = { ...process.env };
    // Tests own temporary locks; they must never inherit the production writer nonce.
    delete env.COURSE_MATERIALS_LOCK_TOKEN;
    delete env.COURSE_MATERIALS_CLI_RECEIPT;
    const result = run('npm', ['test'], {
        cwd: PKG_ROOT,
        env,
        stdio: 'inherit',
        shell: process.platform === 'win32',
    });
    if (result.status !== 0) {
        throw new Error('npm test failed; refusing to publish');
    }
}

function contentTypeFor(path) {
    if (path.endsWith('.json')) return 'application/json';
    if (path.endsWith('.png')) return 'image/png';
    if (path.endsWith('.jpg') || path.endsWith('.jpeg')) return 'image/jpeg';
    if (path.endsWith('.gif')) return 'image/gif';
    if (path.endsWith('.webp')) return 'image/webp';
    if (path.endsWith('.csv')) return 'text/csv';
    return 'application/octet-stream';
}

function walkFiles(dir, excludeNames = new Set()) {
    const out = [];
    if (!existsSync(dir)) return out;
    for (const name of readdirSync(dir)) {
        if (excludeNames.has(name)) continue;
        const p = join(dir, name);
        const st = statSync(p);
        if (st.isDirectory()) out.push(...walkFiles(p, excludeNames));
        else out.push(p);
    }
    return out;
}

async function putFile(s3, bucket, key, localPath) {
    const body = readFileSync(localPath);
    await s3.send(
        new PutObjectCommand({
            Bucket: bucket,
            Key: key,
            Body: body,
            ContentType: contentTypeFor(localPath),
        })
    );
}

const PUBLISH_EXCLUDE_NAMES = new Set([
    '.DS_Store',
    '.gitkeep',
    '_compile-report.json',
    '_validation-report.json',
]);

async function syncDir({
    cloud,
    s3,
    bucket,
    prefix,
    localDir,
    remoteSubdir,
    excludeNames = PUBLISH_EXCLUDE_NAMES,
    dryRun,
}) {
    const files = walkFiles(localDir, excludeNames);
    let uploaded = 0;
    for (const abs of files) {
        const rel = relative(localDir, abs).replace(/\\/g, '/');
        const key = `${prefix}/${remoteSubdir}/${rel}`.replace(/\/+/g, '/');
        if (dryRun) {
            console.log(`[dry-run] put s3://${bucket}/${key}`);
        } else {
            if (cloud) {
                const existing = await cloud.get(key);
                const bytes = readFileSync(abs), expected = hash(bytes);
                if (!existing || hash(existing.body) !== expected) {
                    await cloud.put(key, bytes, existing?.etag || null);
                    await verifyObject(cloud, key, expected);
                }
            } else await putFile(s3, bucket, key, abs);
            console.log(`Uploaded s3://${bucket}/${key}`);
        }
        uploaded += 1;
    }
    return uploaded;
}

export async function publishAll(options = {}) {
    const args = options.args || parseArgs(process.argv.slice(2));
    const repo = options.repo || WORK_ROOT;
    const DOCS_ROOT = join(repo, 'documents');
    const COURSE_PLANS_PATH = join(repo, 'coursePlans.json');
    const validate = options.validate || validateCompiledDocument;
    const bucket = options.bucket || process.env.COURSE_DOCS_RUNTIME_BUCKET;
    const region =
        process.env.COURSE_DOCS_RUNTIME_REGION || 'us-west-1';
    const prefix = (
        process.env.COURSE_DOCS_RUNTIME_PREFIX || 'documents'
    ).replace(/^\/+|\/+$/g, '');
    assertNoPendingPublication(repo, 'cli');
    const managed = assertManagedRetained(repo);

    if (!args.skipTests) {
        console.log('Running tests…');
        runTests();
    } else {
        console.warn('Skipping tests (--skip-tests)');
    }

    if (!existsSync(COURSE_PLANS_PATH)) {
        throw new Error(`coursePlans.json not found: ${COURSE_PLANS_PATH}`);
    }
    const manifest = JSON.parse(
        readFileSync(join(DOCS_ROOT, 'manifest.json'), 'utf8')
    );
    const coursePlans = JSON.parse(readFileSync(COURSE_PLANS_PATH, 'utf8'));

    const s3 = new S3Client({ region });
    const cloud = options.cloud || (bucket ? createPublicationCloud({ bucket, region, client: s3 }) : null);
    if (!bucket && !args.dryRun) throw new Error('COURSE_DOCS_RUNTIME_BUCKET is required');
    const baseline = readJson(join(stateDirectory(repo), 'baseline.json'), { manifest: {} });
    const externalDocIds = new Set();
    console.log('Validating local compiled documents and retaining unchanged runtime materials…');
    for (const [id, entry] of Object.entries(manifest)) {
        const compiledRel = entry.compiled || `compiled/${id}.json`;
        const abs = join(DOCS_ROOT, compiledRel);
        if (!existsSync(abs) && JSON.stringify(baseline.manifest[id]) === JSON.stringify(entry)) {
            if (!compiledRel.startsWith('compiled/') || compiledRel.includes('..') || compiledRel.includes('\\')) throw new Error('Unsafe runtime compiled path');
            if (!args.dryRun) {
                const remote = await cloud.get(`${prefix}/${compiledRel}`);
                if (!remote || JSON.parse(remote.body).document_id !== id) throw new Error(`Baseline runtime document missing or has wrong identity: ${id}`);
            }
            externalDocIds.add(id);
            continue;
        }
        if (!existsSync(abs)) {
            throw new Error(`Missing compiled file for ${id}: ${compiledRel}`);
        }
        const doc = JSON.parse(readFileSync(abs, 'utf8'));
        const result = validate(doc, DOCS_ROOT);
        if (!result.ok) {
            throw new Error(
                `Validation failed for ${id}: ${(result.errors || []).join('; ')}`
            );
        }
    }

    console.log('Preflight chat_documents bindings…');
    const preflight = preflightPublish({
        manifest,
        coursePlans,
        documentsRoot: DOCS_ROOT,
        validateCompiled: validate,
        externalDocIds,
    });
    if (!preflight.ok) {
        for (const e of preflight.errors) console.error(`  error: ${e}`);
        throw new Error(`Publish preflight failed (${preflight.errors.length} errors)`);
    }
    console.log(
        `Preflight OK: ${preflight.referencedDocIds.length} document(s) referenced by chat_documents`
    );

    if (!bucket) {
        if (args.dryRun) {
            console.log(
                '[dry-run] COURSE_DOCS_RUNTIME_BUCKET unset; preflight only'
            );
            return;
        }
        throw new Error(
            'COURSE_DOCS_RUNTIME_BUCKET is required to publish (or use --dry-run for preflight only)'
        );
    }

    const artifacts = Object.fromEntries(['assets', 'compiled'].flatMap(subdir =>
        walkFiles(join(DOCS_ROOT, subdir), PUBLISH_EXCLUDE_NAMES).map(file => [relative(DOCS_ROOT, file).replace(/\\/g, '/'), hash(readFileSync(file))])));
    const manifestBytes = readFileSync(join(DOCS_ROOT, 'manifest.json'));
    const plansBytes = readFileSync(COURSE_PLANS_PATH);
    const journalPath = join(stateDirectory(repo), 'cli-publication.json');
    let journal = readJson(journalPath);
    if (!args.dryRun) {
        if (journal && journal.phase !== 'verified') {
            if (journal.artifacts && JSON.stringify(journal.artifacts) !== JSON.stringify(artifacts)) throw new Error('CLI artifacts changed during interrupted publication; restore the saved artifact set');
            if (journal.manifestHash !== hash(manifestBytes) || journal.coursePlansHash !== hash(plansBytes)) throw new Error('CLI publication is incomplete and working configuration changed; recover the saved configuration first');
        } else {
            const baseline = await runtimeBaseline(repo, cloud, prefix);
            journal = { documentId: 'cli', phase: 'prepared', manifestHash: hash(manifestBytes), coursePlansHash: hash(plansBytes),
                baseManifestHash: baseline.manifest ? hash(baseline.manifest.body) : null,
                basePlansHash: baseline.plans ? hash(baseline.plans.body) : null };
            atomicWrite(journalPath, jsonBytes(journal));
        }
    }

    if (!args.dryRun) {
        journal.artifacts = artifacts;
        atomicWrite(journalPath, jsonBytes(journal));
    }

    console.log(`Syncing assets → s3://${bucket}/${prefix}/assets/`);
    await syncDir({
        cloud,
        s3,
        bucket,
        prefix,
        localDir: join(DOCS_ROOT, 'assets'),
        remoteSubdir: 'assets',
        dryRun: args.dryRun,
    });

    console.log(`Syncing compiled → s3://${bucket}/${prefix}/compiled/`);
    await syncDir({
        cloud,
        s3,
        bucket,
        prefix,
        localDir: join(DOCS_ROOT, 'compiled'),
        remoteSubdir: 'compiled',
        dryRun: args.dryRun,
    });

    const manifestKey = `${prefix}/manifest.json`;
    let remoteManifest, remotePlans;
    if (!args.dryRun) {
        remoteManifest = await cloud.get(manifestKey);
        remotePlans = await cloud.get(`${prefix}/coursePlans.json`);
        const allowed = (remote, base, target) => [base, target].includes(remote ? hash(remote.body) : null);
        if (!allowed(remoteManifest, journal.baseManifestHash, journal.manifestHash) || !allowed(remotePlans, journal.basePlansHash, journal.coursePlansHash)) throw new Error('Runtime configuration conflict; CLI refuses to overwrite another writer');
        if (hash(readFileSync(join(DOCS_ROOT, 'manifest.json'))) !== journal.manifestHash || hash(readFileSync(COURSE_PLANS_PATH)) !== journal.coursePlansHash) throw new Error('Working configuration changed during CLI publication');
    }
    if (args.dryRun) {
        console.log(`[dry-run] put s3://${bucket}/${manifestKey}`);
    } else {
        if (!remoteManifest || hash(remoteManifest.body) !== journal.manifestHash) await cloud.put(manifestKey, manifestBytes, remoteManifest?.etag || null);
        await verifyObject(cloud, manifestKey, journal.manifestHash);
        console.log(`Uploaded s3://${bucket}/${manifestKey}`);
    }

    // Upload coursePlans last so lessons never reference unfinished content.
    const plansKey = `${prefix}/coursePlans.json`;
    if (args.dryRun) {
        console.log(`[dry-run] put s3://${bucket}/${plansKey}`);
    } else {
        journal.phase = 'activating';
        atomicWrite(journalPath, jsonBytes(journal));
        if (!remotePlans || hash(remotePlans.body) !== journal.coursePlansHash) await cloud.put(plansKey, plansBytes, remotePlans?.etag || null);
        await verifyObject(cloud, plansKey, journal.coursePlansHash);
        await verifyObject(cloud, manifestKey, journal.manifestHash);
        for (const [rel, expected] of Object.entries(artifacts)) await verifyObject(cloud, `${prefix}/${rel}`, expected);
        for (const entry of Object.values(managed)) for (const [rel, expected] of Object.entries(entry.artifacts)) await verifyObject(cloud, `${prefix}/${rel}`, expected);
        writeRuntimeLedger(repo, manifestBytes, plansBytes, managed);
        journal.phase = 'verified';
        journal.publishedAt = new Date().toISOString();
        atomicWrite(journalPath, jsonBytes(journal));
        console.log(`Uploaded s3://${bucket}/${plansKey}`);
    }

    console.log(args.dryRun ? 'Dry-run publish complete.' : 'Publish complete.');
    if (!args.dryRun) return { activation: 'verified', version: journal.coursePlansHash,
        publishedAt: journal.publishedAt, manifestHash: journal.manifestHash,
        coursePlansHash: journal.coursePlansHash, artifacts, scope: 'local-artifacts-and-shared-runtime-config' };
}

async function main() {
    const result = await withPublicationLock(WORK_ROOT, publishAll);
    if (process.env.COURSE_MATERIALS_CLI_RECEIPT && result) atomicWrite(process.env.COURSE_MATERIALS_CLI_RECEIPT, jsonBytes(result));
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) main().catch((err) => {
    console.error(err.message || err);
    process.exit(1);
});
