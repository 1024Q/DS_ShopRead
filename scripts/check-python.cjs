const fs = require('node:fs');
const { spawnSync } = require('node:child_process');
const python =
  process.env.ECMP_TEST_PYTHON || (fs.existsSync('.venv/Scripts/python.exe') ? '.venv/Scripts/python.exe' : 'python');
const formatting = process.argv.includes('--format') || process.argv.includes('--format-check');
const checks = formatting
  ? ['-m', 'ruff', 'format', ...(process.argv.includes('--format-check') ? ['--check'] : []), 'backend', 'scripts']
  : process.argv.includes('--lint')
    ? ['-m', 'ruff', 'check', 'backend', 'scripts']
    : [
        '-W',
        'ignore',
        '-m',
        'unittest',
        'backend.tests.test_optimization_foundation',
        'backend.tests.test_cost_tables',
        'backend.tests.test_cost_supplements',
        'backend.tests.test_invoice_import',
        'backend.tests.test_center_tunnel',
        'backend.tests.test_store',
        'backend.tests.test_overview_snapshot',
      ];
const result = spawnSync(python, checks, { stdio: 'inherit' });
process.exit(result.status ?? 1);
