// Run the bundled pipeline CLI from PromptHive with separate writable job storage.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
const [pipeline, repo, workspace] = process.argv.slice(2);
const scripts = join(pipeline, 'scripts');
const require = createRequire(pathToFileURL(join(scripts, 'validate-compiled.mjs')));
require('ajv/dist/2020.js'); require('ajv-formats');
const { compileMaterial } = await import(pathToFileURL(join(scripts, 'course-materials-cli.mjs')));
const { atomicWrite, jsonBytes } = await import(pathToFileURL(join(scripts, 'publication-state.mjs')));
try {
  await compileMaterial({ repo, workspace, record: JSON.parse(readFileSync(join(workspace, 'input.json'))),
    stage: status => atomicWrite(join(workspace, 'stage.json'), jsonBytes({ status })) });
} catch (error) {
  console.error(error.message);
  atomicWrite(join(workspace, 'processing-error.json'), jsonBytes({ error: error.message }));
  process.exitCode = 1;
}
