import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = fileURLToPath(new URL('../', import.meta.url));
function files(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const name = path.join(directory, entry.name);
    return entry.isDirectory() ? files(name) : [name];
  });
}

test('frontend relative imports resolve after feature-directory moves', () => {
  const missing = [];
  for (const file of files(path.join(root, 'app')).filter((name) => /\.(?:js|mjs|css)$/.test(name))) {
    const imports = readFileSync(file, 'utf8').matchAll(
      /(?:\bfrom\s*|\bimport\s*(?:\(\s*)?|@import\s*)['"](\.{1,2}\/[^'"]+)['"]/g,
    );
    for (const [, specifier] of imports) {
      const target = path.resolve(path.dirname(file), specifier);
      if (![target, `${target}.js`, `${target}.mjs`, path.join(target, 'index.js')].some(existsSync)) {
        missing.push(`${path.relative(root, file)} -> ${specifier}`);
      }
    }
  }
  assert.deepEqual(missing, []);
});
