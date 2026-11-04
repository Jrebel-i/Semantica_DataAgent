import { execFileSync } from 'node:child_process';
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const dir = mkdtempSync(join(tmpdir(), 'dataagent-types-'));
try {
  const snapshot = execFileSync('../backend/.venv/bin/python', ['-m', 'semantica_dataagent.openapi'], { encoding: 'utf8' });
  if (snapshot !== readFileSync('contract/openapi.json', 'utf8')) throw new Error('OpenAPI snapshot is stale');
  const output = join(dir, 'api.ts');
  execFileSync('node_modules/.bin/openapi-typescript', ['contract/openapi.json', '-o', output]);
  if (readFileSync(output, 'utf8') !== readFileSync('src/api.ts', 'utf8')) throw new Error('Generated types are stale');
} finally { rmSync(dir, { recursive: true, force: true }); }
