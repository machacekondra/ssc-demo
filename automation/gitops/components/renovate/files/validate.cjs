const fs = require('node:fs');
const { spawn } = require('node:child_process');

for (const dir of [process.env.HOME, process.env.RENOVATE_BASE_DIR]) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
}
// Check inputs locally before starting Renovate; never dump the resulting config.
require('./validate-config.cjs');
const child = spawn('renovate', ['--dry-run=lookup'], {
  stdio: 'inherit',
  env: { ...process.env, RENOVATE_DRY_RUN: 'lookup' },
});
child.on('error', () => {
  console.error('Could not start the pinned Renovate runtime');
  process.exitCode = 1;
});
child.on('exit', (code, signal) => {
  if (code !== 0 || signal) {
    console.error('Renovate lookup failed; no merge request was created');
    process.exitCode = code || 1;
    return;
  }
  fs.writeFileSync(process.env.VALIDATION_RESULT_PATH, 'validated');
  console.log('Renovate GitLab/Artifactory validation completed in lookup mode');
});
