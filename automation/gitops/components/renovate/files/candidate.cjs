const fs = require('node:fs');
const path = require('node:path');
const { execFileSync, spawn } = require('node:child_process');

const runtime = process.env.RENOVATE_RUNTIME_DIR || '/work/runtime';
const statePath = path.join(runtime, 'request.json');
const request = JSON.parse(fs.readFileSync(statePath, 'utf8'));
const command = process.argv[2];
fs.mkdirSync(process.env.HOME, { recursive: true, mode: 0o700 });

function git(args) {
  return execFileSync('git', args, {
    encoding: 'utf8',
    env: { ...process.env, GIT_ASKPASS: '/etc/renovate/git-askpass.sh', GIT_TERMINAL_PROMPT: '0' },
    stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
}

function repositoryUrl() {
  const config = JSON.parse(fs.readFileSync('/etc/renovate/settings.json', 'utf8'));
  const endpoint = new URL(config.gitlabEndpoint);
  endpoint.pathname = endpoint.pathname.replace(/\/api\/v4\/?$/, '') + '/' + request.repository + '.git';
  endpoint.search = '';
  endpoint.hash = '';
  return endpoint.toString();
}

function main() {
  if (command === 'base') {
    if (request.status !== 'active') return;
    git(['check-ref-format', '--branch', request.baseBranch]);
    git(['clone', '--depth=1', '--branch', request.baseBranch, '--', repositoryUrl(), '/work/base']);
    request.baseSha = git(['-C', '/work/base', 'rev-parse', 'HEAD']);
    fs.writeFileSync(statePath, JSON.stringify(request));
    console.log('Checked out the target branch at ' + request.baseSha);
  } else if (command === 'run') {
    if (request.status !== 'active') return;
    if (request.dependencyMode === 'managed-override') {
      console.log('Renovate update skipped; the verified transitive dependency override was committed directly.');
      return;
    }
    require('./remediate-config.cjs')(request);
    fs.mkdirSync(process.env.RENOVATE_BASE_DIR, { recursive: true, mode: 0o700 });
    const env = { ...process.env,
      RENOVATE_CONFIG_FILE: '/etc/renovate/remediate-config.cjs',
      RENOVATE_REMEDIATION_CONFIG: 'true',
    };
    delete env.RENOVATE_DRY_RUN;
    const child = spawn('renovate', [], { stdio: 'inherit', env });
    child.on('error', () => { console.error('Could not start Renovate'); process.exitCode = 1; });
    child.on('exit', (code, signal) => { process.exitCode = signal ? 1 : code || 0; });
  } else if (command === 'export') {
    if (request.status !== 'changed') return;
    git(['clone', '--depth=1', '--branch', request.branch, '--', repositoryUrl(), '/work/candidate-git']);
    const sha = git(['-C', '/work/candidate-git', 'rev-parse', 'HEAD']);
    if (sha !== request.candidateSha) throw new Error('Candidate branch moved before checkout');
    git(['-C', '/work/candidate-git', 'fetch', '--depth=1', 'origin', request.baseSha]);
    const changed = git(['-C', '/work/candidate-git', 'diff', '--name-only', '-z', request.baseSha, sha])
      .split('\0').filter(Boolean).sort();
    fs.writeFileSync(path.join(runtime, 'changed-files.json'), JSON.stringify(changed));
    fs.cpSync('/work/candidate-git', '/candidate/project', {
      recursive: true,
      filter: (source) => path.basename(source) !== '.git',
    });
    console.log('Exported candidate ' + sha + ' for isolated Maven verification');
  } else {
    throw new Error('Unknown candidate operation');
  }
}

try { main(); }
catch (error) {
  console.error('Renovate candidate preparation failed: ' + error.message);
  process.exitCode = 1;
}
