// Shared by the read-only validation task and the future remediation task.
// Credentials are loaded at runtime, not stored in this ConfigMap or logged.
const fs = require('node:fs');
const path = require('node:path');

module.exports = function buildConfig({ repository, packageName, fixedVersion } = {}) {
  const settings = JSON.parse(fs.readFileSync(
    process.env.RENOVATE_SETTINGS_FILE || path.join(__dirname, 'settings.json'), 'utf8'
  ));
  if (!settings.repositories.includes(repository)) {
    throw new Error('Repository is outside the configured Renovate allowlist');
  }
  if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*$/.test(packageName || '')) {
    throw new Error('PACKAGE must be a Maven groupId:artifactId');
  }
  if (fixedVersion && (fixedVersion.length > 256 || /[\s/\\]/.test(fixedVersion))) {
    throw new Error('FIXED_VERSION must be an exact Maven version');
  }
  const credentials = JSON.parse(fs.readFileSync(path.join(
    process.env.RENOVATE_RUNTIME_DIR || '/work/runtime', 'artifactory.json'
  ), 'utf8'));
  const targetRule = {
    matchManagers: ['maven'], matchPackageNames: [packageName], enabled: true,
    registryUrls: [settings.registryUrl], versioning: 'maven',
  };
  if (fixedVersion) {
    const escaped = fixedVersion.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    targetRule.allowedVersions = `/^${escaped}$/`;
  }
  return {
    platform: 'gitlab', endpoint: settings.gitlabEndpoint,
    token: process.env.RENOVATE_TOKEN,
    autodiscover: false, repositories: [repository],
    onboarding: false, requireConfig: 'ignored',
    enabledManagers: ['maven'],
    maven: { registryUrls: [settings.registryUrl] },
    hostRules: [{
      hostType: 'maven', matchHost: settings.registryUrl,
      username: credentials.username, password: credentials.password,
      allowInternal: true,
    }],
    packageRules: [
      { matchManagers: ['maven'], matchPackageNames: ['*'], enabled: false },
      targetRule,
    ],
    automerge: false, draftPR: true, dependencyDashboard: false,
    branchPrefix: 'renovate-remediate/', pruneStaleBranches: false,
    ignoreUnstable: false, respectLatest: false,
    allowScripts: false, allowPlugins: false, allowedCommands: [],
    ignoreScripts: true, exitCodeForErrors: true,
  };
};
