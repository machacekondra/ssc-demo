import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


FILES = Path(__file__).resolve().parents[1] / "files"
sys.path.insert(0, str(FILES))
SPEC = importlib.util.spec_from_file_location("remediation_test_module", FILES / "remediation.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

POM = '''<project><properties><lib.version>1.0</lib.version></properties><dependencies>
<dependency><groupId>org.example</groupId><artifactId>lib</artifactId><version>{version}</version></dependency>
<dependency><groupId>org.other</groupId><artifactId>other</artifactId><version>{other}</version></dependency>
</dependencies></project>'''


class RemediationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("runtime", "base", "candidate", "evidence", "results"):
            (self.root / name).mkdir()
        self.patch = patch.multiple(
            MODULE, RUNTIME=self.root / "runtime", STATE=self.root / "runtime/request.json",
            BASE=self.root / "base", CANDIDATE=self.root / "candidate", EVIDENCE=self.root / "evidence",
            VERIFY_MARKER=self.root / "candidate/RUN_VERIFY",
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.env = {
            "TARGET_REPOSITORY": "development/application", "TARGET_PACKAGE": "org.example:lib",
            "CURRENT_VERSION": "1.0", "TARGET_VERSION": "1.0.rhlw-00001", "CVE_ID": "CVE-2025-12345",
            "BASE_BRANCH": "main", "SUBDIRECTORY": ".", "SELECTED": "1",
            **{"RESULT_" + k: str(self.root / "results" / k) for k in ("STATUS", "MR_URL", "MR_IID", "BRANCH", "COMMIT_SHA")},
        }
        self.environment = patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.state = {
            "repository": "development/application", "status": "active", "projectId": 1,
            "package": "org.example:lib", "currentVersion": "1.0", "fixedVersion": "1.0.rhlw-00001",
            "pomPath": "pom.xml", "baseBranch": "main", "baseSha": "base-sha", "candidateSha": "candidate-sha",
            "branch": "renovate-remediate/example/fix",
            "mrIid": 7, "mrUrl": "https://gitlab.example.test/mr/7", "runName": "run-1",
        }
        MODULE.save(self.state)

    def base_pom(self, version="1.0", other="2.0"):
        (MODULE.BASE / "pom.xml").write_text(POM.format(version=version, other=other))

    def candidate(self, other="2.0"):
        state = MODULE.load()
        state["status"] = "changed"
        MODULE.save(state)
        (MODULE.RUNTIME / "changed-files.json").write_text('["pom.xml"]')
        (MODULE.CANDIDATE / "pom.xml").write_text(POM.format(version="1.0.rhlw-00001", other=other))

    def test_inline_update_scope(self):
        self.base_pom()
        MODULE.inspect_pom()
        self.candidate()
        MODULE.check_change()
        self.assertTrue(MODULE.VERIFY_MARKER.exists())

    def test_unrelated_pin_change_is_rejected(self):
        self.base_pom()
        MODULE.inspect_pom()
        self.candidate(other="3.0")
        with self.assertRaisesRegex(ValueError, "beyond the requested"):
            MODULE.check_change()
        self.assertFalse(MODULE.VERIFY_MARKER.exists())

    def test_local_property_update(self):
        self.base_pom(version="${lib.version}")
        MODULE.inspect_pom()
        self.candidate()
        (MODULE.CANDIDATE / "pom.xml").write_text(
            POM.format(version="${lib.version}", other="2.0").replace(
                "<lib.version>1.0</lib.version>", "<lib.version>1.0.rhlw-00001</lib.version>"
            )
        )
        MODULE.check_change()
        self.assertTrue(MODULE.VERIFY_MARKER.exists())

    def test_shared_property_is_rejected(self):
        self.base_pom(version="${lib.version}", other="${lib.version}")
        with self.assertRaisesRegex(ValueError, "shared"):
            MODULE.inspect_pom()

    def test_stale_decision_and_transitive_dependency_are_rejected(self):
        self.base_pom(version="1.1")
        with self.assertRaisesRegex(ValueError, "stale"):
            MODULE.inspect_pom()
        state = MODULE.load()
        state["package"] = "org.transitive:missing"
        MODULE.save(state)
        with self.assertRaisesRegex(ValueError, "transitive"):
            MODULE.inspect_pom()

    def test_already_fixed_needs_no_mr(self):
        self.base_pom(version="1.0.rhlw-00001")
        MODULE.inspect_pom()
        self.assertEqual(MODULE.load()["status"], "already-fixed")
        with patch.object(MODULE, "api") as api:
            MODULE.collect()
            MODULE.finalize()
        api.assert_not_called()

    def test_selected_zero_performs_no_api_request(self):
        with patch.dict(os.environ, {"SELECTED": "0"}), patch.object(
            MODULE.prepare, "settings_config", return_value={"repositories": ["development/application"]}
        ), patch.object(MODULE, "api") as api:
            MODULE.preflight()
        api.assert_not_called()
        self.assertEqual(MODULE.load()["status"], "skipped")

    def test_dry_run_cannot_write_to_gitlab(self):
        state = MODULE.load()
        state["dryRun"] = True
        MODULE.save(state)
        with patch.object(MODULE, "api") as api:
            MODULE.redraft()
            MODULE.collect()
            MODULE.finalize()
        api.assert_not_called()
        self.assertEqual(MODULE.load()["status"], "dry-run")

    def test_wrong_resolved_version_cannot_pass(self):
        state = MODULE.load()
        state["status"] = "changed"
        MODULE.save(state)
        (MODULE.EVIDENCE / "maven-verified").touch()
        (MODULE.EVIDENCE / "dependency-tree.json").write_text(json.dumps({
            "groupId": "app", "artifactId": "app", "children": [
                {"groupId": "org.example", "artifactId": "lib", "version": "1.0"}
            ],
        }))
        with self.assertRaisesRegex(ValueError, "resolved dependency tree"):
            MODULE.check_dependency()

    def test_moved_mr_remains_draft(self):
        state = MODULE.load()
        state.update(status="changed", verified=True)
        MODULE.save(state)
        with patch.object(MODULE, "api", side_effect=[
            {"state": "opened", "sha": "another-sha", "title": "Draft: Fix"},
            {"commit": {"id": "base-sha"}},
        ]) as api:
            with self.assertRaisesRegex(ValueError, "moved"):
                MODULE.finalize()
        self.assertTrue(all(len(call.args) == 1 for call in api.call_args_list))

    def test_verified_exact_sha_becomes_ready(self):
        state = MODULE.load()
        state.update(status="changed", verified=True)
        MODULE.save(state)
        with patch.object(MODULE, "api", side_effect=[
            {"state": "opened", "sha": "candidate-sha", "title": "Draft: Fix", "description": "Related to #1"},
            {"commit": {"id": "base-sha"}}, {},
        ]) as api:
            MODULE.finalize()
        self.assertEqual(api.call_args.args[1], "PUT")
        self.assertEqual(api.call_args.args[2]["title"], "Fix")
        self.assertEqual((self.root / "results/STATUS").read_text(), "ready")

    def test_commenter_role_is_required(self):
        with patch.dict(os.environ, {"ISSUE_IID": "1", "REQUESTER_ID": "9", "NOTE_ID": "10"}), patch.object(
            MODULE.prepare, "settings_config", return_value={"repositories": ["development/application"]}
        ), patch.object(MODULE, "api", side_effect=[{"id": 1}, {"state": "opened"}, {"access_level": 20}]):
            with self.assertRaisesRegex(ValueError, "Developer"):
                MODULE.preflight()


if __name__ == "__main__":
    unittest.main()
