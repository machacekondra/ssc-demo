const fs = require('node:fs');
const path = require('node:path');
const buildConfig = require('./base-config.cjs');

module.exports = function remediationConfig(request) {
  if (!request.fixedVersion || !request.baseBranch || !request.branch || !request.pomPath) {
    throw new Error('An exact fixed version and validated request context are required');
  }
  const config = {
    ...buildConfig({ repository: request.repository, packageName: request.package, fixedVersion: request.fixedVersion }),
    baseBranchPatterns: [request.baseBranch],
    includePaths: [request.pomPath],
    branchPrefix: request.branch.slice(0, request.branch.lastIndexOf('/') + 1),
    branchName: request.branch,
    prTitle: `fix(${request.cveId}): update ${request.package} to ${request.fixedVersion}`,
    prBodyNotes: [
      request.justification,
      request.issueIid ? `Related to #${request.issueIid}` : '',
      `Tekton remediation run: ${request.runName}. This MR remains draft until Maven verification passes.`,
    ].filter(Boolean),
    prCreation: 'immediate', prHourlyLimit: 0, prConcurrentLimit: 0, branchConcurrentLimit: 0,
    separateMajorMinor: false,
  };
  if (request.dryRun) config.dryRun = 'full';
  return config;
};

// Renovate's CLI reads an object from its config file, not this factory.
if (process.env.RENOVATE_REMEDIATION_CONFIG === 'true') {
  const request = JSON.parse(fs.readFileSync(path.join(
    process.env.RENOVATE_RUNTIME_DIR || '/work/runtime', 'request.json'
  ), 'utf8'));
  module.exports = module.exports(request);
}
