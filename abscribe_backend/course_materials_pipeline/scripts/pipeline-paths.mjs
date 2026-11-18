import { dirname, resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
export const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const WORK_ROOT = resolve(process.env.COURSE_MATERIALS_PIPELINE_DATA_DIR || join(process.env.COURSE_MATERIALS_DATA_DIR || resolve(PACKAGE_ROOT, '../instance/course-materials'), 'pipeline'));
export const DOCS_ROOT = join(WORK_ROOT, 'documents');
export const COURSE_PLANS_PATH = join(WORK_ROOT, 'coursePlans.json');
