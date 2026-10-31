const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
function findTests(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) return findTests(file);
    return /\.test\.(?:cjs|mjs|js)$/.test(entry.name) ? [file] : [];
  });
}
const files = ['app', 'tests'].flatMap(findTests).sort();
const result = spawnSync(process.execPath, ['--test', ...files], { stdio: 'inherit' });
process.exit(result.status ?? 1);
