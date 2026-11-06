const buildConfig = require('./base-config.cjs');

module.exports = {
  ...buildConfig({
    repository: process.env.TARGET_REPOSITORY,
    packageName: process.env.TARGET_PACKAGE,
    fixedVersion: process.env.TARGET_VERSION,
  }),
  // Validation can never create a branch, dashboard, or merge request.
  dryRun: 'lookup',
};
