import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch


FILES = Path(__file__).resolve().parents[1] / "files"
SPEC = importlib.util.spec_from_file_location("renovate_prepare", FILES / "prepare.py")
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = self.directory / "settings.json"
        self.config.write_text(json.dumps({
            "gitlabEndpoint": "https://gitlab.example.test/api/v4/",
            "repositories": ["development/application"],
            "registryUrl": "https://artifactory.example.test/artifactory/maven",
            "mirrorId": "artifactory",
        }))
        self.settings = self.directory / "settings.xml"
        self.settings.write_text('''<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">
          <mirrors><mirror><id>artifactory</id>
            <url>https://artifactory.example.test/artifactory/maven/</url>
          </mirror></mirrors>
          <servers>
            <server><id>unrelated</id><username>wrong</username><password>wrong</password></server>
            <server><id>artifactory</id><username>reader</username><password>secret&amp;value</password></server>
          </servers></settings>''')
        self.runtime = self.directory / "runtime"
        self.env = {
            "RENOVATE_SETTINGS_FILE": str(self.config),
            "MAVEN_SETTINGS_FILE": str(self.settings),
            "RENOVATE_RUNTIME_DIR": str(self.runtime),
            "TARGET_REPOSITORY": "development/application",
            "TARGET_PACKAGE": "org.apache.commons:commons-lang3",
            "TARGET_VERSION": "3.14.0.rhlw-00001",
        }

    def fetch(self, request, **kwargs):
        self.assertEqual(request.get_header("Authorization"), "Basic cmVhZGVyOnNlY3JldCZ2YWx1ZQ==")
        if request.full_url.endswith("maven-metadata.xml"):
            return io.BytesIO(b'''<metadata><versioning><versions>
              <version>3.14.0</version><version>3.14.0.rhlw-00001</version>
              </versions></versioning></metadata>''')
        self.assertTrue(request.full_url.endswith("/3.14.0.rhlw-00001/commons-lang3-3.14.0.rhlw-00001.pom"))
        return io.BytesIO(b"<project/>")

    def test_matching_server_xml_entities_and_target_pom(self):
        output = io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            PREPARE.urllib.request, "urlopen", side_effect=self.fetch
        ) as fetch, contextlib.redirect_stdout(output):
            PREPARE.prepare()
        self.assertEqual(fetch.call_count, 2)
        credentials = self.runtime / "artifactory.json"
        self.assertEqual(json.loads(credentials.read_text()), {"username": "reader", "password": "secret&value"})
        self.assertEqual(stat.S_IMODE(credentials.stat().st_mode), 0o600)
        self.assertNotIn("secret", output.getvalue())
        self.assertNotIn("Basic", output.getvalue())
        self.assertIn("3.14.0.rhlw-00001", output.getvalue())

    def test_unavailable_version_fails(self):
        with patch.dict(os.environ, {**self.env, "TARGET_VERSION": "9.9.9"}, clear=True), patch.object(
            PREPARE.urllib.request, "urlopen", side_effect=self.fetch
        ):
            with self.assertRaisesRegex(ValueError, "absent from Artifactory"):
                PREPARE.prepare()

    def test_repository_outside_allowlist_fails_before_network(self):
        with patch.dict(os.environ, {**self.env, "TARGET_REPOSITORY": "other/project"}, clear=True), patch.object(
            PREPARE.urllib.request, "urlopen"
        ) as fetch:
            with self.assertRaisesRegex(ValueError, "allowlist"):
                PREPARE.prepare()
        fetch.assert_not_called()

    def test_registry_mismatch_does_not_send_credentials(self):
        config = json.loads(self.config.read_text())
        config["registryUrl"] = "https://other.example.test/maven"
        self.config.write_text(json.dumps(config))
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            PREPARE.urllib.request, "urlopen"
        ) as fetch:
            with self.assertRaisesRegex(ValueError, "must match"):
                PREPARE.prepare()
        fetch.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to check the Renovate config")
    def test_exact_version_and_task_scope(self):
        self.runtime.mkdir()
        (self.runtime / "artifactory.json").write_text(json.dumps({"username": "reader", "password": "fixture"}))
        script = r'''
          const build = require(process.argv[1]);
          const c = build({repository: process.env.TARGET_REPOSITORY,
            packageName: process.env.TARGET_PACKAGE, fixedVersion: process.env.TARGET_VERSION});
          const expression = c.packageRules[1].allowedVersions;
          const pattern = new RegExp(expression.slice(1, -1));
          console.log(JSON.stringify({
            exact: pattern.test('3.14.0.rhlw-00001'),
            different: pattern.test('3x14x0xrhlw-00001'),
            next: pattern.test('3.14.0.rhlw-00002'),
            managers: c.enabledManagers, requireConfig: c.requireConfig,
            disabledByDefault: c.packageRules[0].enabled === false,
            autodiscover: c.autodiscover, automerge: c.automerge, draftPR: c.draftPR
          }));
        '''
        result = subprocess.run(
            ["node", "-e", script, str(FILES / "base-config.cjs")],
            env={**os.environ, **self.env}, text=True, capture_output=True, check=True,
        )
        config = json.loads(result.stdout)
        self.assertTrue(config["exact"])
        self.assertFalse(config["different"])
        self.assertFalse(config["next"])
        self.assertEqual(config["managers"], ["maven"])
        self.assertEqual(config["requireConfig"], "ignored")
        self.assertTrue(config["disabledByDefault"])
        self.assertTrue(config["draftPR"])
        self.assertFalse(config["autodiscover"])
        self.assertFalse(config["automerge"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to check the Renovate config")
    def test_remediation_config_scopes_branch_pom_and_dry_run(self):
        self.runtime.mkdir()
        (self.runtime / "artifactory.json").write_text(json.dumps({"username": "reader", "password": "fixture"}))
        script = r'''
          const build = require(process.argv[1]);
          const request = {repository: process.env.TARGET_REPOSITORY,
            package: process.env.TARGET_PACKAGE, fixedVersion: process.env.TARGET_VERSION,
            baseBranch: 'main', branch: 'renovate-remediate/request/fix', pomPath: 'pom.xml',
            cveId: 'CVE-2025-12345', runName: 'run-1', issueIid: '7'};
          const real = build(request);
          const dry = build({...request, dryRun: true});
          console.log(JSON.stringify({branch: real.branchName, prefix: real.branchPrefix,
            paths: real.includePaths, base: real.baseBranchPatterns,
            dry: dry.dryRun, realDry: real.dryRun || null,
            draft: real.draftPR, automerge: real.automerge,
            version: real.packageRules[1].allowedVersions}));
        '''
        config = json.loads(subprocess.check_output(
            ["node", "-e", script, str(FILES / "remediate-config.cjs")],
            env={**os.environ, **self.env}, text=True,
        ))
        self.assertEqual(config["branch"], "renovate-remediate/request/fix")
        self.assertEqual(config["prefix"], "renovate-remediate/request/")
        self.assertEqual(config["paths"], ["pom.xml"])
        self.assertEqual(config["base"], ["main"])
        self.assertEqual(config["dry"], "full")
        self.assertIsNone(config["realDry"])
        self.assertTrue(config["draft"])
        self.assertFalse(config["automerge"])
        self.assertEqual(config["version"], r"/^3\.14\.0\.rhlw-00001$/")


if __name__ == "__main__":
    unittest.main()
