import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


CHART = Path(__file__).resolve().parents[1]
ROOT = CHART.parents[3]


def json_documents(text):
    decoder = json.JSONDecoder()
    documents = []
    while text.strip():
        value, end = decoder.raw_decode(text.lstrip())
        documents.append(value)
        text = text.lstrip()[end:]
    return documents


@unittest.skipUnless(shutil.which("yq") and shutil.which("helm"), "helm and yq are required")
class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rendered = subprocess.check_output([
            "helm", "template", "ssc-renovate", str(CHART),
            "--set", "gitlab.endpoint=https://gitlab.example.test/api/v4/",
        ], text=True)
        cls.resources = json_documents(subprocess.check_output(
            ["yq", "-o=json", ".", "-"], input=rendered, text=True,
        ))
        cls.triggers = json_documents(subprocess.check_output([
            "yq", "-o=json", ".", str(ROOT / "pipelines/triggers/agentic-issue-triggers.yaml")
        ], text=True))

    def test_commands_do_not_overlap(self):
        listener = next(d for d in self.triggers if d["kind"] == "EventListener")
        commands = {}
        for trigger in listener["spec"]["triggers"]:
            if trigger["name"] not in ("gitlab-issue-remediate", "gitlab-issue-remediate-renovate"):
                continue
            expression = next(p["value"] for i in trigger["interceptors"] for p in i.get("params", []) if p["name"] == "filter")
            pattern = expression.split(".matches('", 1)[1].split("')", 1)[0].replace("\\\\", "\\")
            commands[trigger["name"]] = re.compile(pattern)
        ai = commands["gitlab-issue-remediate"]
        renovate = commands["gitlab-issue-remediate-renovate"]
        self.assertTrue(ai.match("/remediate"))
        self.assertFalse(ai.match("/remediate-renovate"))
        self.assertTrue(renovate.match("/remediate-renovate\nFIXED_VERSION: 2.2"))
        self.assertFalse(renovate.match("/remediate-renovate-other"))
        self.assertFalse(renovate.match("/remediate"))

    def test_trigger_uses_comment_identity_and_renovate_pipeline(self):
        resources = json_documents(subprocess.check_output([
            "yq", "-o=json", ".", str(ROOT / "pipelines/triggers/renovate-remediation-trigger.yaml")
        ], text=True))
        template = next(d for d in resources if d["kind"] == "TriggerTemplate")
        run = template["spec"]["resourcetemplates"][0]
        self.assertEqual(run["metadata"]["name"], "renovate-issue-$(tt.params.PROJECT_ID)-$(tt.params.NOTE_ID)")
        self.assertEqual(run["spec"]["pipelineRef"]["name"], "cve-remediation-renovate")
        self.assertEqual(run["spec"]["taskRunTemplate"]["serviceAccountName"], "renovate")
        names = {p["name"] for p in run["spec"]["params"]}
        self.assertTrue({"REQUESTER_ID", "NOTE_ID", "ISSUE_IID"}.issubset(names))
        self.assertNotIn("DRY_RUN", names)

    def test_maven_has_no_gitlab_token_or_renovate_runtime_mount(self):
        task = next(d for d in self.resources if d["kind"] == "Task" and d["metadata"]["name"] == "renovate-remediate-dependency")
        common = task["spec"]["stepTemplate"]
        maven = next(s for s in task["spec"]["steps"] if s["name"] == "maven-verify")
        self.assertNotIn("RENOVATE_TOKEN", {e["name"] for e in common["env"] + maven["env"]})
        mounts = {v["name"] for v in common["volumeMounts"] + maven["volumeMounts"]}
        self.assertNotIn("runtime", mounts)
        self.assertNotIn("runtime-tmp", mounts)
        self.assertIn("maven-home", mounts)
        self.assertFalse(any(d["kind"] == "CronJob" for d in self.resources))


if __name__ == "__main__":
    unittest.main()
